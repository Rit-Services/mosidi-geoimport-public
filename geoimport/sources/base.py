"""Gemeinsame Datenstrukturen und Schnittstelle aller Quell-Plugins.

Ein Plugin arbeitet in zwei Schritten:

1. ``fetch()`` lädt die Rohdaten (Seiten, Dateien) nach ``data/raw/<quelle>/<lauf>/``.
2. ``parse()`` liest die Rohdaten, bildet sie auf das Zielmodell ab, transformiert nach
   EPSG:4326, prüft und liefert gültige Datensätze sowie Fehler je Datensatz.

Die Trennung erlaubt es, gespeicherte Rohdaten erneut einzulesen (Demo-Modus, Wiederherstellung).

Texte von außen (Serverantworten, Dateiinhalte) gelangen über ``SourceFailure``, ``PipelineWarning``
und ``RecordError`` in Bericht, Logs und ``pipeline_meta``. Diese Klassen entfernen deshalb
Steuerzeichen (NUL, Terminal-Escapes, Bidi-Steuerzeichen) und kürzen überlange Texte.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
from shapely.geometry.base import BaseGeometry

from geoimport.config.models import PipelineConfig, SourceBase
from geoimport.http import HttpClient
from geoimport.logging import mask_secrets

MAX_TEXT = 2000
MAX_KEY = 500
# C0 außer Tab/Zeilenumbruch, DEL, C1 sowie Bidi-Steuerzeichen (Umkehr der Leserichtung)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(value: Any, max_len: int = MAX_TEXT) -> str:
    """Fremdtext entschärfen: Steuerzeichen entfernen, Zugangsdaten maskieren, auf ``max_len`` Zeichen kürzen."""
    text = mask_secrets(_CONTROL_RE.sub("", str(value).replace("\r\n", "\n").replace("\r", "\n")))
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def clean_data(value: Any) -> Any:
    """``clean_text`` rekursiv auf alle Texte (auch Schlüssel) in Listen und Dicts anwenden."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {clean_data(k): clean_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_data(v) for v in value]
    return value


class SourceFailure(Exception):
    """Die Quelle als Ganzes ist fehlgeschlagen; der Bestand der Quelle bleibt unverändert."""

    def __init__(self, kind: str, message: str, http_status: int | None = None, url: str | None = None) -> None:
        message = clean_text(message)
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.url = clean_text(url) if url is not None else None


@dataclass(slots=True)
class PipelineWarning:
    """Warnung im Bericht, z. B. ``schema_drift`` oder ``possibly_truncated``."""

    code: str
    message: str
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.message = clean_text(self.message)
        if self.data:
            self.data = clean_data(self.data)

    def as_dict(self) -> dict[str, Any]:
        """Für Bericht und ``import_source_results.warnings``."""
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out


@dataclass(slots=True)
class FeatureRecord:
    """Ein Objekt mit Geometrie (EPSG:4326) und Attributen, bereit für das Zielmodell."""

    key: str
    name: str | None
    geometry: BaseGeometry | None
    attributes: dict[str, Any]
    hash: str = ""
    repaired: bool = False


@dataclass(slots=True)
class RecordError:
    """Ein Einzeldatensatz, der nicht importiert wird (landet in ``import_errors``)."""

    record_key: str | None
    reason_code: str
    reason: str
    geometry: BaseGeometry | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.reason = clean_text(self.reason)
        if self.record_key is not None:
            self.record_key = clean_text(self.record_key, MAX_KEY)


@dataclass(slots=True)
class RawArtifact:
    """Eine gespeicherte Rohdatei (WFS-Seite, CSV/ZIP)."""

    path: Path
    sha256: str
    url: str | None = None
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    """Ergebnis von ``fetch()``. ``unchanged``: Quelle seit dem letzten erfolgreichen Lauf unverändert."""

    artifacts: list[RawArtifact]
    unchanged: bool = False
    info: dict[str, Any] = field(default_factory=dict)
    warnings: list[PipelineWarning] = field(default_factory=list)
    state_updates: dict[str, Any] = field(default_factory=dict)

    @property
    def combined_sha256(self) -> str | None:
        """Ein Hash über alle Rohdateien (Reihenfolge wie geladen)."""
        if not self.artifacts:
            return None
        if len(self.artifacts) == 1:
            return self.artifacts[0].sha256
        import hashlib

        return hashlib.sha256("".join(a.sha256 for a in self.artifacts).encode()).hexdigest()


VALUE_COLUMNS = ["key", "object_key", "name", "value", "period", "extra", "hash"]


@dataclass
class ParseResult:
    """Ergebnis von ``parse()``.

    ``features``: Objekte mit Geometrie (WFS, CSV mit Koordinaten).
    ``values``: Kennzahlen ohne eigene Geometrie (CSV), als DataFrame mit ``VALUE_COLUMNS`` –
    bewusst kein Objekt je Zeile, weil INKAR allein für Brandenburg > 1 Mio. Zeilen liefert.
    """

    features: list[FeatureRecord] = field(default_factory=list)
    values: pd.DataFrame | None = None
    errors: list[RecordError] = field(default_factory=list)
    fetched: int = 0
    warnings: list[PipelineWarning] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    state_updates: dict[str, Any] = field(default_factory=dict)
    invalid_overflow: int = 0  # Fehler über MAX_STORED_ERRORS hinaus (gezählt, nicht gespeichert)
    overflow_reasons: dict[str, int] = field(default_factory=dict)  # dieselben Fehler nach Grund

    @property
    def invalid(self) -> int:
        """Anzahl ungültiger Datensätze."""
        return len(self.errors) + self.invalid_overflow

    @property
    def valid(self) -> int:
        """Anzahl gültiger Datensätze."""
        return len(self.features) + (0 if self.values is None else len(self.values))

    def reason_counts(self) -> dict[str, int]:
        """Anzahl ungültiger Datensätze je Grund, einschließlich der nicht einzeln gespeicherten."""
        counts: dict[str, int] = {}
        for e in self.errors:
            counts[e.reason_code] = counts.get(e.reason_code, 0) + 1
        for reason, n in self.overflow_reasons.items():
            counts[reason] = counts.get(reason, 0) + n
        return counts

    @property
    def repaired(self) -> int:
        """Anzahl reparierter Geometrien."""
        return sum(1 for f in self.features if f.repaired)

    def keys_and_hashes(self) -> pd.DataFrame:
        """Schlüssel und Hash aller gültigen Datensätze (für den Diff)."""
        if self.values is not None:
            return self.values[["key", "hash"]].copy()
        return pd.DataFrame({"key": [f.key for f in self.features], "hash": [f.hash for f in self.features]})


class StateStore:
    """Kleiner Zustandsspeicher je Quelle (``data/state/<id>.json``): ETag, Hash, letztes Schema."""

    def __init__(self, state_dir: Path, source_id: str) -> None:
        self.path = Path(state_dir) / f"{source_id}.json"

    def load(self) -> dict[str, Any]:
        """Zustand lesen (leer, wenn noch kein Lauf erfolgreich war)."""
        try:
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def forget(self, keys: tuple[str, ...]) -> None:
        """Einträge entfernen (z. B. Hash/ETag, damit die Quelle neu eingelesen wird)."""
        data = self.load()
        if not any(k in data for k in keys):
            return
        self._write({k: v for k, v in data.items() if k not in keys})

    def update(self, values: dict[str, Any]) -> None:
        """Zustand ergänzen; nur nach erfolgreichem Lauf aufrufen."""
        if not values:
            return
        data = self.load()
        data.update(values)
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = datetime.now(UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class SourceContext:
    """Alles, was ein Plugin von außen braucht."""

    pipeline: PipelineConfig
    http: HttpClient
    run_id: str
    raw_dir: Path
    state: StateStore
    use_conditional_requests: bool = True


class Source(ABC):
    """Basisklasse aller Quell-Plugins."""

    kind: ClassVar[str]

    def __init__(self, cfg: SourceBase, ctx: SourceContext) -> None:
        self.cfg = cfg
        self.ctx = ctx

    @abstractmethod
    def probe(self) -> dict[str, Any]:
        """Erreichbarkeit und Eckdaten prüfen, ohne zu importieren."""

    @abstractmethod
    def fetch(self) -> FetchResult:
        """Rohdaten laden und unter ``ctx.raw_dir`` speichern."""

    @abstractmethod
    def parse(self, fetched: FetchResult) -> ParseResult:
        """Rohdaten lesen, abbilden, transformieren, prüfen."""

    def location_url(self) -> str | None:
        """URL bzw. Pfad der Quelle für Metadaten und Bericht."""
        return None
