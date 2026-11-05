"""Laden und Prüfen der YAML-Konfiguration.

Aufbau des Konfigurationsverzeichnisses (Standard ``/app/config``, per ``CONFIG_DIR`` änderbar)::

    pipeline.yaml       globale Einstellungen
    target.yaml         Abbildung auf die Zieltabellen (per TARGET_CONFIG änderbar)
    sources/*.yaml      eine Datei je Quelle

Werte dürfen Umgebungsvariablen enthalten: ``${NAME}`` oder ``${NAME:-Standard}``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, TypeAdapter, ValidationError

from geoimport import __version__
from geoimport.config.models import (
    CsvSource,
    PipelineConfig,
    SourceBase,
    TargetConfig,
    WfsSource,
)
from geoimport.logging import SECRET_NAME_RE, register_secret

ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def source_adapter() -> TypeAdapter[Any]:
    """Validator für Quelldateien über alle registrierten Quelltypen (Diskriminator ``type``)."""
    from typing import Annotated, Union

    from geoimport.sources import config_models

    models = tuple(config_models())
    union = models[0] if len(models) == 1 else Union[models]  # noqa: UP007
    return TypeAdapter(Annotated[union, Field(discriminator="type")])  # type: ignore[arg-type]


class ConfigError(Exception):
    """Konfiguration fehlt oder ist ungültig. Die Meldung ist für Betreiber formuliert."""


@dataclass
class LoadedConfig:
    """Gesamte, geprüfte Konfiguration eines Laufs."""

    config_dir: Path
    pipeline: PipelineConfig
    target: TargetConfig
    target_file: Path
    sources: list[Any]
    source_files: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    config_hash: str = ""

    def source(self, source_id: str) -> Any:
        """Quelle nach ID; ``ConfigError``, wenn unbekannt."""
        for s in self.sources:
            if s.id == source_id:
                return s
        raise ConfigError(f"Quelle '{source_id}' ist nicht konfiguriert")

    def enabled_sources(self) -> list[Any]:
        """Aktive Quellen in Ladereihenfolge (Dateiname)."""
        return [s for s in self.sources if s.enabled]


def default_config_dir() -> Path:
    """Konfigurationsverzeichnis aus ``CONFIG_DIR`` (Standard ``/app/config`` bzw. ``./config``)."""
    env = os.environ.get("CONFIG_DIR")
    if env:
        return Path(env)
    return Path("/app/config") if Path("/app/config").is_dir() else Path("config")


def _register_param_secrets(src: SourceBase) -> None:
    """Werte geheimer Abfrageparameter (``wfs.params``, ``csv.location.params``) überall maskieren."""
    params: dict[str, str] = {}
    if isinstance(src, WfsSource):
        params = src.wfs.params
    elif isinstance(src, CsvSource):
        params = src.csv.location.params
    for key, value in params.items():
        if SECRET_NAME_RE.match(key):
            register_secret(value)


def expand_env(value: Any, missing: list[str]) -> Any:
    """Ersetzt ``${VAR}``/``${VAR:-default}`` rekursiv; fehlende Variablen werden gesammelt."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                if SECRET_NAME_RE.match(name) or name.upper().endswith("_KEY"):  # LGB_KEY, API_KEY
                    register_secret(os.environ[name])  # z. B. ${INKAR_TOKEN}: Wert nie in Logs/Berichten
                return os.environ[name]
            if name == "APP_VERSION":
                return __version__
            if default is not None:
                return default
            missing.append(name)
            return m.group(0)

        return ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, missing) for v in value]
    return value


def _read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Konfigurationsdatei fehlt: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML-Syntaxfehler: {exc}") from exc


def format_validation_error(path: Path, exc: ValidationError) -> str:
    """Pydantic-Fehler als deutschsprachige Liste mit Datei und Feldpfad."""
    lines = [f"{path}: Konfiguration ungültig"]
    for err in exc.errors():
        parts = [str(p) for p in err["loc"]]
        if len(parts) > 1 and parts[0] in ("wfs", "csv"):  # Diskriminator-Kennung der Quellart
            parts = parts[1:]
        loc = ".".join(parts) or "(Datei)"
        msg = err["msg"]
        msg = (
            msg.replace("Field required", "Pflichtfeld fehlt")
            .replace("Extra inputs are not permitted", "unbekanntes Feld (Tippfehler?)")
            .replace("Value error, ", "")
            .replace("Input should be", "Wert muss sein")
        )
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def _csv_fields(src: CsvSource) -> set[str]:
    """Zielfelder, die eine CSV-Quelle erzeugt (Spaltenmapping und wide→long)."""
    fields = {c.to for c in src.columns.values()}
    w = src.reshape.wide_to_long
    if w:
        fields |= {w.var_name, w.value_name}
        if w.indicator:
            fields.add(src.record.name)
    return fields


def load_config(config_dir: Path | None = None, target_file: str | None = None) -> LoadedConfig:
    """Lädt und prüft alle Konfigurationsdateien. Wirft ``ConfigError`` mit allen Fehlern."""
    config_dir = Path(config_dir or default_config_dir())
    errors: list[str] = []
    warnings: list[str] = []
    hasher = hashlib.sha256()

    def load(path: Path) -> Any:
        raw = _read_yaml(path)
        hasher.update(path.name.encode())
        hasher.update(json.dumps(raw, sort_keys=True, default=str).encode())
        return raw

    pipeline_path = config_dir / "pipeline.yaml"
    pipeline_cfg = PipelineConfig()
    if pipeline_path.exists():
        missing: list[str] = []
        raw = expand_env(load(pipeline_path), missing)
        if missing:
            errors.append(f"{pipeline_path}: Umgebungsvariablen fehlen: {', '.join(sorted(set(missing)))}")
        try:
            pipeline_cfg = PipelineConfig.model_validate(raw)
        except ValidationError as exc:
            errors.append(format_validation_error(pipeline_path, exc))

    tfile = config_dir / (target_file or os.environ.get("TARGET_CONFIG", "target.yaml"))
    target_cfg: TargetConfig | None = None
    try:
        missing = []
        raw = expand_env(load(tfile), missing)
        if missing:
            errors.append(f"{tfile}: Umgebungsvariablen fehlen: {', '.join(sorted(set(missing)))}")
        target_cfg = TargetConfig.model_validate(raw)
        if target_cfg.has_placeholders():
            warnings.append(f"{tfile}: enthält noch Platzhalter (TODO_TH…) – 'check-target' und Läufe schlagen fehl")
    except ConfigError as exc:
        errors.append(str(exc))
    except ValidationError as exc:
        errors.append(format_validation_error(tfile, exc))

    sources: list[SourceBase] = []
    source_files: dict[str, Path] = {}
    adapter = source_adapter()
    src_dir = config_dir / "sources"
    for path in sorted(src_dir.glob("*.yaml")) if src_dir.is_dir() else []:
        try:
            raw = load(path)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        missing = []
        expanded = expand_env(raw, missing)
        try:
            src = adapter.validate_python(expanded)
        except ValidationError as exc:
            errors.append(format_validation_error(path, exc))
            continue
        if missing and src.enabled:
            errors.append(f"{path}: Umgebungsvariablen fehlen: {', '.join(sorted(set(missing)))}")
        if src.id in source_files:
            errors.append(f"{path}: Quell-ID '{src.id}' ist bereits in {source_files[src.id].name} vergeben")
            continue
        source_files[src.id] = path
        sources.append(src)
        _register_param_secrets(src)
        warnings.extend(src.placeholder_warnings() if src.enabled else [])
        if not src.enabled:
            warnings.append(f"{path.name}: Quelle '{src.id}' ist deaktiviert")

    datasets: dict[str, str] = {}
    for src in sources:
        if not src.enabled:
            continue
        other = datasets.get(src.target.dataset)
        if other:
            errors.append(f"Quellen '{other}' und '{src.id}' schreiben in denselben Datensatz '{src.target.dataset}'")
        datasets[src.target.dataset] = src.id
        if isinstance(src, CsvSource):
            produced = _csv_fields(src)
            absent = [f for f in src.business_key if f not in produced]
            if absent:
                errors.append(
                    f"{src.id}: business_key-Feld(er) {', '.join(absent)} kommen im Datensatz nicht vor "
                    f"(Zielfelder: {', '.join(sorted(produced))})"
                )
        if isinstance(src, CsvSource) and src.geometry_join:
            ref = next((s for s in sources if s.id == src.geometry_join.source), None)
            if ref is None:
                errors.append(f"{src.id}: geometry_join.source '{src.geometry_join.source}' ist nicht konfiguriert")
            elif src.geometry_join.column not in {c.to for c in src.columns.values()} | {
                src.reshape.wide_to_long.var_name if src.reshape.wide_to_long else ""
            }:
                errors.append(
                    f"{src.id}: geometry_join.column '{src.geometry_join.column}' ist kein Zielfeld in columns"
                )
            elif src.geometry_join.column != src.record.object_key:
                # Verglichen wird der Raumbezug der Zeile (record.object_key); eine andere Spalte würde nie geprüft
                errors.append(
                    f"{src.id}: geometry_join.column '{src.geometry_join.column}' muss record.object_key "
                    f"'{src.record.object_key}' sein"
                )

    if errors:
        raise ConfigError("\n".join(errors))
    assert target_cfg is not None
    return LoadedConfig(
        config_dir=config_dir,
        pipeline=pipeline_cfg,
        target=target_cfg,
        target_file=tfile,
        sources=sources,
        source_files=source_files,
        warnings=warnings,
        config_hash="sha256:" + hasher.hexdigest(),
    )


def json_schema() -> dict[str, Any]:
    """JSON-Schema aller Konfigurationsdateien (für Editoren und ``docs/config.schema.json``)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "geoimport – Konfiguration",
        "pipeline.yaml": PipelineConfig.model_json_schema(by_alias=True),
        "target.yaml": TargetConfig.model_json_schema(by_alias=True),
        "sources/*.yaml": source_adapter().json_schema(by_alias=True),
    }
