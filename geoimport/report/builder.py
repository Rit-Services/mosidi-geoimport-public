"""Aufbau und Ablage des Import-Berichts nach jedem Lauf (LB 4.5).

Ablage:

- ``<report_dir>/<JJJJ>/<JJJJ-MM-TT>_<run_id>.json`` (maschinenlesbar, Schema ``docs/report.schema.json``)
- ``<report_dir>/<JJJJ>/<JJJJ-MM-TT>_<run_id>.md`` (menschenlesbar, deutsch)
- ``<report_dir>/latest.json`` und ``latest.md`` (Kopie)
- Kopie des JSON in ``pipeline_meta.import_runs.report``

Zeiten in Europe/Berlin mit Offset (ISO 8601).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPORT_SCHEMA_VERSION = "1.0"
SCHEMA_PATH = Path(__file__).with_name("schema.json")
# Nur Quellen mit diesen Status haben ihre Änderungen angewendet (bzw. im Probelauf berechnet)
COUNTED_STATUSES = ("ok", "dry_run", "unchanged")


def local_tz() -> ZoneInfo:
    """Zeitzone der Berichte (``TZ``, Standard Europe/Berlin)."""
    name = os.environ.get("TZ") or "Europe/Berlin"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # unbekannte TZ-Angabe → Standard
        return ZoneInfo("Europe/Berlin")


def iso(ts: datetime | None) -> str | None:
    """Zeitstempel in lokaler Zeit mit Offset."""
    return ts.astimezone(local_tz()).isoformat(timespec="seconds") if ts else None


@dataclass
class SourceReport:
    """Ergebnis einer Quelle im Bericht."""

    id: str
    type: str
    title: str
    dataset: str
    license: str | None = None
    attribution: str | None = None
    status: str = "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    fetched: int = 0
    valid: int = 0
    invalid: int = 0
    repaired: int = 0
    added: int | None = None
    changed: int | None = None
    deleted: int | None = None
    unchanged: int | None = None
    invalid_existing: int = 0
    compared: bool = True
    rows_deleted: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    quality: dict[str, int] = field(
        default_factory=lambda: {"geometry_repaired": 0, "geometry_skipped": 0, "missing_required_field": 0}
    )
    data_unchanged: bool = True
    warnings: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    error_examples: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    info: dict[str, Any] = field(default_factory=dict)
    preview_sql: list[str] | None = None

    @property
    def duration_s(self) -> float | None:
        """Laufzeit der Quelle in Sekunden."""
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 2)
        return None

    def as_dict(self) -> dict[str, Any]:
        """JSON-Darstellung."""
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "dataset": self.dataset,
            "status": self.status,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "duration_s": self.duration_s,
            "fetched": self.fetched,
            "valid": self.valid,
            "invalid": self.invalid,
            "repaired": self.repaired,
            "added": self.added,
            "changed": self.changed,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "invalid_existing": self.invalid_existing,
            "compared": self.compared,
            "rows_deleted": self.rows_deleted,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "quality": self.quality,
            "data_unchanged": self.data_unchanged,
            "warnings": self.warnings,
            "license": self.license,
            "attribution": self.attribution,
        }
        if self.error:
            out["error"] = self.error
        if self.error_examples:
            out["error_examples"] = self.error_examples
        if self.raw:
            out["raw"] = self.raw
        if self.info:
            out["info"] = self.info
        if self.preview_sql:
            out["preview_sql"] = self.preview_sql
        return out


@dataclass
class RunReport:
    """Gesamtbericht eines Laufs."""

    run_id: str
    trigger: str
    started_at: datetime
    app_version: str
    config_hash: str
    target_adapter: str
    simulated: bool = False
    dry_run: bool = False
    finished_at: datetime | None = None
    status: str = "running"
    sources: list[SourceReport] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    refreshed_views: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Kennzahlen über alle Quellen."""

        def total(attr: str, counted_only: bool = True) -> int:
            return sum(getattr(s, attr) or 0 for s in self.sources if not counted_only or s.status in COUNTED_STATUSES)

        by_status: dict[str, int] = {}
        for s in self.sources:
            by_status[s.status] = by_status.get(s.status, 0) + 1
        return {
            "sources_total": len(self.sources),
            "ok": by_status.get("ok", 0),
            "failed": by_status.get("failed", 0) + by_status.get("aborted_incomplete", 0),
            "aborted_incomplete": by_status.get("aborted_incomplete", 0),
            "skipped": by_status.get("skipped_guard", 0),
            "unchanged": by_status.get("unchanged", 0),
            "dry_run": by_status.get("dry_run", 0),
            "added": total("added"),
            "changed": total("changed"),
            "deleted": total("deleted"),
            "errors": total("invalid", counted_only=False),
            "warnings": sum(len(s.warnings) for s in self.sources) + len(self.warnings),
        }

    def as_dict(self) -> dict[str, Any]:
        """JSON-Darstellung (Version ``schema_version``)."""
        duration = round((self.finished_at - self.started_at).total_seconds(), 2) if self.finished_at else None
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run": {
                "run_id": self.run_id,
                "trigger": self.trigger,
                "simulated": self.simulated,
                "dry_run": self.dry_run,
                "started_at": iso(self.started_at),
                "finished_at": iso(self.finished_at),
                "duration_s": duration,
                "status": self.status,
                "app_version": self.app_version,
                "config_hash": self.config_hash,
                "target_adapter": self.target_adapter,
                "timezone": str(local_tz()),
                **({"refreshed_views": self.refreshed_views} if self.refreshed_views else {}),
            },
            "summary": self.summary(),
            "warnings": self.warnings,
            "sources": [s.as_dict() for s in self.sources],
        }


def write_report(report: dict[str, Any], report_dir: Path) -> tuple[Path, Path]:
    """Schreibt JSON und Markdown; aktualisiert ``latest.*``. Liefert (json_pfad, md_pfad)."""
    from geoimport.report.render_md import render_markdown

    started = datetime.fromisoformat(report["run"]["started_at"])
    folder = report_dir / f"{started:%Y}"
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"{started:%Y-%m-%d}_{report['run']['run_id']}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    shutil.copyfile(json_path, report_dir / "latest.json")
    shutil.copyfile(md_path, report_dir / "latest.md")
    return json_path, md_path


def load_schema() -> dict[str, Any]:
    """JSON-Schema des Berichts."""
    data: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return data
