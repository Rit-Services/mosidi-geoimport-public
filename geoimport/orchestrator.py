"""Ablauf eines Laufs: alle (oder ausgewählte) Quellen nacheinander, jede isoliert.

Je Quelle: Rohdaten laden → lesen/prüfen → Schutzregeln → Diff → in **einer** Transaktion
löschen und ersetzen → Ergebnis, Fehler und Diff-Schlüssel in ``pipeline_meta`` (liegt die
Lauf-Historie in der Zieldatenbank, in derselben Transaktion). Ein Fehler einer Quelle stoppt nie
die nächste; deren Bestand bleibt unverändert (ROLLBACK). Nach dem COMMIT wird eine Quelle nie mehr
als fehlgeschlagen gemeldet; spätere Probleme sind Warnungen.

Auch bei unerwarteten Fehlern, Abbruch (Strg-C, SIGTERM) oder Verlust der Datenbank wird der Lauf
abgeschlossen: Status in ``pipeline_meta`` (notfalls über eine neue Verbindung), Berichtsdateien und
Benachrichtigung. Exit-Codes: ``geoimport.exitcodes.ExitCode``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

import psycopg

from geoimport import __version__
from geoimport.config.loader import ConfigError, LoadedConfig
from geoimport.config.models import CsvSource, SourceBase
from geoimport.db import DatabaseUnavailable, connect
from geoimport.diff import (
    CHANGE_ADDED,
    CHANGE_CHANGED,
    CHANGE_DELETED,
    CHANGE_REPLACED,
    CHANGE_UNCHANGED,
    DiffResult,
    compute_diff,
)
from geoimport.exitcodes import ExitCode
from geoimport.http import HttpClient
from geoimport.logging import event, run_id_var, source_id_var
from geoimport.meta.repository import CHANGE_INVALID, MetaRepository, MetaSchemaError, OwnedDataset, ewkb_hex
from geoimport.quality import QUALITY_CODES, quality_counts, quality_examples, quality_warnings
from geoimport.report.builder import RunReport, SourceReport, write_report
from geoimport.sources import source_class
from geoimport.sources.base import (
    FetchResult,
    ParseResult,
    PipelineWarning,
    RawArtifact,
    RecordError,
    SourceContext,
    SourceFailure,
    StateStore,
    clean_text,
)
from geoimport.target.adapter import (
    DatasetContext,
    OwnershipError,
    ProjectionUnavailable,
    ReplaceStats,
    TargetAdapter,
    make_adapter,
)

log = logging.getLogger(__name__)

# Lauf-Sperre: zweiteiliger Schlüssel, getrennt von den Sperren je Datensatz
RUN_LOCK = "geoimport:run-lock"
# Lauf-Sperre bis 0.10 (ein Schlüssel): zusätzlich halten, damit sich alte und neue Images ausschließen
LEGACY_RUN_LOCK = "geoimport:run"
LOCK_POLL_S = 15.0
FETCH_INFO = "fetch.json"
# Zustand je Quelle: Fingerabdruck der Konfiguration; ändert er sich, gelten Hash/ETag nicht mehr
CONFIG_FINGERPRINT = "config_fingerprint"
SKIP_STATE_KEYS = ("raw_sha256", "etag", "last_modified")
# Felder ohne Einfluss auf die geschriebenen Daten (Änderung erzwingt kein Neueinlesen)
_FINGERPRINT_EXCLUDE = {"schedule", "enabled", "guards", "skip_if_unchanged"}
# Warnungen, die nur gelten, wenn das Ersetzen festgeschrieben wurde (bei ROLLBACK entfernt)
_UNAPPLIED_WARNINGS = {"invalid_existing", "geometry_retained"}


class RunLocked(RuntimeError):
    """Ein anderer Lauf hält die Lauf-Sperre."""


class Terminated(KeyboardInterrupt):
    """SIGTERM während eines Laufs.

    Unterklasse von ``KeyboardInterrupt``, damit psycopg eine laufende Abfrage wie bei Strg-C
    serverseitig abbricht, bevor die Transaktion zurückgerollt wird.
    """


# Abbruch beim Abruf oder Prüfen (Bieterantwort V5): Lieferung unvollständig oder fehlerhaft, nichts gelöscht
INCOMPLETE_KINDS = frozenset(
    {"network", "timeout", "http", "ogc_exception", "incomplete", "possibly_truncated", "paging", "parse",
     "too_many_errors", "too_large"}
)  # fmt: skip


@dataclass(frozen=True)
class _Ownership:
    """Ergebnis der Besitzprüfung eines Datensatzes vor dem Ersetzen (H7)."""

    metadata_id: Any  # bestehende Metadatenzeile oder None (wird angelegt)
    action: str  # new | owned | adopt | backfill
    created_run_id: str | None = None  # bei backfill: erster erfolgreicher Lauf laut Historie


class _GuardSkip(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class _DryRunRollback(Exception):
    pass


@contextmanager
def _sigterm_raises() -> Iterator[None]:
    """Während des Laufs löst SIGTERM ``Terminated`` aus (nur im Haupt-Thread möglich)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        raise Terminated("SIGTERM")

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def config_fingerprint(src: SourceBase) -> str:
    """Hash über die datenrelevante Konfiguration der Quelle und die Programmversion."""
    cfg = src.model_dump(mode="json", exclude=_FINGERPRINT_EXCLUDE)
    payload = json.dumps({"app_version": __version__, "source": cfg}, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def mark_invalid_keys(diff: DiffResult, errors: list[RecordError]) -> tuple[DiffResult, int]:
    """Bisherige Schlüssel, deren neuer Datensatz ungültig ist, nicht als „gelöscht“ zählen."""
    invalid = {e.record_key for e in errors if e.record_key}
    if not invalid or diff.keys.empty:
        return diff, 0
    mask = (diff.keys["change"] == CHANGE_DELETED) & diff.keys["key"].isin(invalid)
    n = int(mask.sum())
    if not n:
        return diff, 0
    keys = diff.keys.copy()
    keys.loc[mask, "change"] = CHANGE_INVALID
    return DiffResult(diff.added, diff.changed, diff.deleted - n, diff.unchanged, diff.compared, keys), n


def drop_skipped_keys(diff: DiffResult, skipped: list[str]) -> tuple[DiffResult, int]:
    """Objekte, die erst die Datenbank verworfen hat, nicht als neu/geändert/unverändert zählen.

    Neue Schlüssel entfallen aus dem Protokoll; bisherige werden wie ungültige Lieferungen behandelt
    (``invalid``, Rückgabe: deren Anzahl für ``invalid_existing``).
    """
    if not skipped or diff.keys.empty:
        return diff, 0
    keys = diff.keys
    hit = keys["key"].isin(skipped)
    change = keys["change"]

    def n(code: str) -> int:
        return int((hit & (change == code)).sum())

    added, changed, unchanged = n(CHANGE_ADDED), n(CHANGE_CHANGED), n(CHANGE_UNCHANGED)
    existing = int((hit & (change != CHANGE_ADDED)).sum())
    keys = keys[~(hit & (change == CHANGE_ADDED))].copy()
    keys.loc[keys["key"].isin(skipped), "change"] = CHANGE_INVALID
    out = DiffResult(
        diff.added - added,
        None if diff.changed is None else diff.changed - changed,
        diff.deleted,
        None if diff.unchanged is None else diff.unchanged - unchanged,
        diff.compared,
        keys,
    )
    return out, existing


@dataclass
class _SourceState:
    """Zwischenstand einer Quelle während ``_process``."""

    parsed: ParseResult | None = None
    diff: tuple[DiffResult, dict[str, str | None]] | None = None
    committed: bool = False  # Zieltabellen ersetzt (COMMIT erfolgt)
    persisted_warnings: int | None = None  # Ergebnis bereits in der Ersetzungs-Transaktion gespeichert


@dataclass
class RunOptions:
    """Steuerung eines Laufs (CLI-Optionen)."""

    source_ids: list[str] | None = None
    dry_run: bool = False
    force_replace: set[str] = field(default_factory=set)
    adopt: set[str] = field(default_factory=set)  # fremde Metadatenzeile bewusst übernehmen (H7)
    trigger: str = "manual"
    simulated: bool = False
    full: bool = False
    replay: dict[str, Path] = field(default_factory=dict)
    mutate: Callable[[SourceBase, ParseResult], list[PipelineWarning]] | None = None
    http_transport: Any = None


@dataclass
class RunOutcome:
    """Ergebnis für CLI und Tests."""

    report: dict[str, Any]
    exit_code: int
    json_path: Path | None = None
    md_path: Path | None = None


# ---------------------------------------------------------------------- Rohdaten
def save_fetch_info(raw_dir: Path, fetched: FetchResult) -> None:
    """Merkt sich Rohdateien und Abrufinfos eines Laufs (für erneutes Einlesen)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "info": fetched.info,
        "artifacts": [
            {"path": str(a.path), "sha256": a.sha256, "url": a.url, "info": a.info} for a in fetched.artifacts
        ],
        "saved_at": datetime.now(UTC).isoformat(),
    }
    (raw_dir / FETCH_INFO).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_fetch_info(raw_dir: Path) -> FetchResult:
    """Rohdaten eines früheren Laufs als ``FetchResult`` (Demo-Modus, Wiederherstellung)."""
    data = json.loads((raw_dir / FETCH_INFO).read_text(encoding="utf-8"))
    arts = []
    for a in data["artifacts"]:
        p = Path(a["path"])
        if not p.is_absolute() or not p.exists():
            p = raw_dir / Path(a["path"]).name
        if not p.exists():
            raise SourceFailure("replay", f"Rohdatei fehlt: {a['path']}")
        arts.append(RawArtifact(p, a["sha256"], a.get("url"), a.get("info") or {}))
    return FetchResult(arts, info=data.get("info") or {})


def latest_raw_dir(raw_root: Path, source_id: str) -> Path | None:
    """Jüngster Rohdaten-Ordner der Quelle mit vorhandenen Dateien."""
    base = raw_root / source_id
    if not base.is_dir():
        return None
    candidates = []
    for d in base.iterdir():
        info = d / FETCH_INFO
        if info.is_file():
            try:
                data = json.loads(info.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if data.get("artifacts"):
                candidates.append((data.get("saved_at", ""), d))
    return max(candidates)[1] if candidates else None


def cleanup_raw(raw_root: Path, retention_days: int) -> int:
    """Löscht Rohdaten-Ordner älter als ``retention_days``; der jüngste je Quelle mit Dateien bleibt."""
    if not raw_root.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for src_dir in raw_root.iterdir():
        if not src_dir.is_dir():
            continue
        keep = latest_raw_dir(raw_root, src_dir.name)
        for d in src_dir.iterdir():
            if d.is_dir() and d != keep and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    return removed


# ---------------------------------------------------------------------- Lauf
class Orchestrator:
    """Führt einen Lauf aus."""

    def __init__(self, cfg: LoadedConfig, opts: RunOptions) -> None:
        self.cfg = cfg
        self.opts = opts
        self.adapter: TargetAdapter = make_adapter(cfg.target)
        self.run_id = str(uuid.uuid4())
        self.started = datetime.now(UTC)
        self.feature_keys: dict[str, set[str]] = {}

    def select_sources(self) -> list[Any]:
        """Quellen dieses Laufs (aktiv, gefiltert nach ``--source``, doppelte Angaben einmal)."""
        if self.opts.source_ids:
            out = []
            for sid in dict.fromkeys(self.opts.source_ids):
                src = self.cfg.source(sid)
                if not src.enabled:
                    raise ConfigError(f"Quelle '{sid}' ist deaktiviert (enabled: false)")
                out.append(src)
            return out
        sources = self.cfg.enabled_sources()
        if not sources:
            raise ConfigError("Keine aktive Quelle ausgewählt")
        return sources

    def run(self) -> RunOutcome:
        """Lauf ausführen; schreibt immer einen Bericht (außer bei Konfigurationsfehlern vor Beginn)."""
        run_id_var.set(self.run_id)
        try:
            sources = self.select_sources()
            for sid in self.opts.adopt:
                self.cfg.source(sid)  # ConfigError bei Tippfehler – sonst würde --adopt still ignoriert
            report = RunReport(
                self.run_id,
                self.opts.trigger,
                self.started,
                __version__,
                self.cfg.config_hash,
                self.cfg.target.adapter,
                simulated=self.opts.simulated,
                dry_run=self.opts.dry_run,
            )
            report.warnings = [
                PipelineWarning("config", w).as_dict() for w in self.cfg.warnings if "deaktiviert" not in w
            ]
            with _sigterm_raises():
                return self._run_with_lock(report, sources)
        finally:
            run_id_var.set(None)

    def _connect(self, meta_db: bool = False) -> psycopg.Connection[Any]:
        p = self.cfg.pipeline
        return connect(p.meta.database if meta_db else None, autocommit=True, settings=p.db)

    def _run_with_lock(self, report: RunReport, sources: list[Any]) -> RunOutcome:
        try:
            lock_conn = self._connect()
        except DatabaseUnavailable as exc:
            return self._abort_before_start(report, sources, "database", str(exc), ExitCode.DB_UNAVAILABLE)
        try:
            try:
                self._acquire_run_lock(lock_conn, report)
            except psycopg.OperationalError as exc:
                msg = str(exc).strip()
                return self._abort_before_start(report, sources, "database", msg, ExitCode.DB_UNAVAILABLE)
            try:
                return self._run_connected(report, sources)
            finally:
                self._release_run_locks(lock_conn)
        finally:
            lock_conn.close()

    @staticmethod
    def _try_run_locks(conn: psycopg.Connection[Any]) -> bool:
        """Neue (zweiteilige) und alte (einteilige) Lauf-Sperre nehmen – beide oder keine."""
        row = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s), 0)", (RUN_LOCK,)).fetchone()
        if not (row and row[0]):
            return False
        row = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LEGACY_RUN_LOCK,)).fetchone()
        if row and row[0]:
            return True
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s), 0)", (RUN_LOCK,))
        return False

    @staticmethod
    def _release_run_locks(conn: psycopg.Connection[Any]) -> None:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s), 0)", (RUN_LOCK,))
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LEGACY_RUN_LOCK,))
        except psycopg.Error:
            pass  # Verbindung weg: Sperren sind mit ihr freigegeben

    def _acquire_run_lock(self, conn: psycopg.Connection[Any], report: RunReport) -> None:
        """Lauf-Sperre nehmen; geplante Läufe warten bis ``run.lock_wait_s`` auf einen aktiven Lauf."""
        wait = self.cfg.pipeline.run.lock_wait_s if self.opts.trigger == "schedule" else 0
        deadline = time.monotonic() + wait
        while True:
            if self._try_run_locks(conn):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event(log, "run_lock_wait", f"Lauf-Sperre belegt – warte bis zu {remaining:.0f} s", logging.WARNING)
            time.sleep(min(LOCK_POLL_S, remaining))
        msg = "Ein anderer Lauf ist aktiv (Lauf-Sperre belegt)" + (f", {wait} s gewartet" if wait else "")
        if self.opts.trigger == "schedule":  # sonst fiele ein geplanter Lauf unbemerkt aus
            report.status, report.finished_at = "locked", datetime.now(UTC)
            data = report.as_dict()
            data["run"]["message"] = msg
            self._notify(data)
        raise RunLocked(msg)

    def _run_connected(self, report: RunReport, sources: list[Any]) -> RunOutcome:
        p = self.cfg.pipeline
        target: psycopg.Connection[Any] | None = None
        meta_conn: psycopg.Connection[Any] | None = None
        try:
            # autocommit: jeder transaction()-Block ist ein echtes BEGIN/COMMIT (kein Savepoint in
            # einer impliziten Transaktion), Lesezugriffe dazwischen halten keine Transaktion offen.
            target = self._connect()
            meta_conn = target if p.meta.location == "schema" else self._connect(meta_db=True)
            meta = MetaRepository(meta_conn, p.meta.schema_name)
            aborted = self._preflight(report, sources, target, meta)
            if aborted is not None:
                return aborted
            stale = meta.abort_stale_runs(datetime.now(UTC))
            if stale:
                report.warnings.append(
                    PipelineWarning(
                        "stale_runs", f"{stale} frühere(r) Lauf/Läufe ohne Abschluss als 'aborted' markiert"
                    ).as_dict()
                )
            meta.start_run(
                self.run_id,
                self.started,
                self.opts.trigger,
                self.opts.simulated,
                self.opts.dry_run,
                __version__,
                self.cfg.config_hash,
            )
        except (DatabaseUnavailable, psycopg.OperationalError) as exc:
            return self._abort_before_start(report, sources, "database", str(exc).strip(), ExitCode.DB_UNAVAILABLE)
        except Exception as exc:
            log.exception("Unerwarteter Fehler vor Beginn des Laufs")
            msg = f"{type(exc).__name__}: {exc}"
            return self._abort_before_start(report, sources, "internal", msg, ExitCode.INTERNAL)
        else:
            return self._run_started(report, sources, target, meta)
        finally:
            if meta_conn is not None and meta_conn is not target:
                meta_conn.close()
            if target is not None:
                target.close()

    def _preflight(
        self, report: RunReport, sources: list[Any], target: psycopg.Connection[Any], meta: MetaRepository
    ) -> RunOutcome | None:
        """Zielschema und Lauf-Historie prüfen (nur lesend); bei Fehlern Abbruch vor dem ersten Schreiben."""
        issues = self.adapter.check(target)
        errors = [i for i in issues if i.level == "error"]
        report.warnings += [
            PipelineWarning("target_schema", i.message).as_dict() for i in issues if i.level == "warning"
        ]
        if errors:
            msg = "; ".join(i.message for i in errors)
            event(log, "target_mismatch", f"Zielschema passt nicht: {msg}", logging.ERROR)
            return self._abort_before_start(report, sources, "target_schema", msg, ExitCode.CONFIG)
        try:
            meta.check_schema()
        except MetaSchemaError as exc:
            event(log, "meta_schema", str(exc), logging.ERROR)
            return self._abort_before_start(report, sources, "meta_schema", str(exc), ExitCode.META_SCHEMA)
        return None

    def _run_started(
        self, report: RunReport, sources: list[Any], target: psycopg.Connection[Any], meta: MetaRepository
    ) -> RunOutcome:
        """Quellen verarbeiten; jeder Abbruch ab hier schließt den Lauf trotzdem ab."""
        p = self.cfg.pipeline
        event(log, "run_started", f"Lauf gestartet: {len(sources)} Quelle(n)", sources=[s.id for s in sources])
        try:
            http = HttpClient(p.http, transport=self.opts.http_transport)
            try:
                for src in sources:
                    sr = self._new_report(src)
                    report.sources.append(sr)  # schon vor dem Verarbeiten: auch bei Abbruch im Bericht
                    self._process(src, sr, target, meta, http)
            finally:
                http.close()
            self._after_import(report, target)
        except BaseException as exc:
            outcome = self._finish_aborted(report, sources, meta, exc)
            if not isinstance(exc, Exception):
                raise  # Strg-C/SIGTERM/SystemExit: Exit-Code setzt die CLI
            return outcome
        outcome = self._finish(report, meta, None)
        try:
            meta.prune(p.meta.keep_runs)
            cleanup_raw(p.storage.raw_dir, p.storage.retention_days)
        except Exception:  # Aufräumen darf den Lauf nicht scheitern lassen
            log.exception("Aufräumen fehlgeschlagen")
        return outcome

    def _after_import(self, report: RunReport, target: psycopg.Connection[Any]) -> None:
        """Materialisierte Sichten aktualisieren (Bieterantwort 75), wenn mindestens eine Quelle geschrieben hat.

        Die Daten sind dann schon festgeschrieben: Ein Fehler hier ist nur eine Warnung (``refresh_failed``).
        """
        if self.opts.dry_run or not self.cfg.target.refresh_views():
            return
        if not any(s.status == "ok" and not s.data_unchanged for s in report.sources):
            return
        try:
            report.refreshed_views = self.adapter.refresh_views(target)
            event(log, "views_refreshed", "Materialisierte Sichten aktualisiert", views=report.refreshed_views)
        except Exception as exc:
            log.exception("REFRESH MATERIALIZED VIEW fehlgeschlagen")
            report.warnings.append(
                PipelineWarning(
                    "refresh_failed",
                    f"Materialisierte Sicht nicht aktualisiert ({type(exc).__name__}: {exc}) – Daten sind geschrieben",
                ).as_dict()
            )

    def _abort_before_start(
        self, report: RunReport, sources: list[Any], kind: str, message: str, code: ExitCode
    ) -> RunOutcome:
        """Abbruch vor dem ersten Schreiben: alle Quellen fehlgeschlagen, Bericht und Benachrichtigung."""
        for src in sources:
            sr = self._new_report(src)
            sr.status, sr.finished_at = "failed", datetime.now(UTC)
            sr.error = {"kind": kind, "message": clean_text("Lauf vor dem ersten Schreiben abgebrochen: " + message)}
            report.sources.append(sr)
        return self._finish(report, None, code)

    def _finish_aborted(
        self, report: RunReport, sources: list[Any], meta: MetaRepository, exc: BaseException
    ) -> RunOutcome:
        """Lauf nach unerwartetem Abbruch abschließen (Status failed, Bericht, Benachrichtigung)."""
        if isinstance(exc, Terminated):
            code, text = ExitCode.TERMINATED, "SIGTERM"
        elif isinstance(exc, KeyboardInterrupt):
            code, text = ExitCode.INTERRUPTED, "Strg-C"
        elif isinstance(exc, psycopg.OperationalError):
            code, text = ExitCode.DB_UNAVAILABLE, f"Datenbankverbindung verloren: {exc}"
        else:
            code, text = ExitCode.INTERNAL, f"{type(exc).__name__}: {exc}"
        if isinstance(exc, Exception):
            log.error("Lauf abgebrochen: %s", text, exc_info=exc)
        else:
            event(log, "run_interrupted", f"Lauf abgebrochen ({text})", logging.ERROR)
        report.warnings.append(PipelineWarning("run_aborted", f"Lauf abgebrochen: {text}").as_dict())
        done = {sr.id for sr in report.sources}
        for src in sources:
            if src.id not in done:
                sr = self._new_report(src)
                sr.status, sr.finished_at = "failed", datetime.now(UTC)
                sr.error = {"kind": "run_aborted", "message": "Lauf vor dieser Quelle abgebrochen"}
                report.sources.append(sr)
        return self._finish(report, meta, code)

    def _finish(self, report: RunReport, meta: MetaRepository | None, exit_code: ExitCode | None) -> RunOutcome:
        report.finished_at = datetime.now(UTC)
        report.status, code = self._overall_status(report)
        if exit_code is not None:
            report.status, code = "failed", exit_code
        data = report.as_dict()
        # Erst die Lauf-Historie abschließen, dann Dateien und Benachrichtigung – jeder Schritt für sich,
        # damit ein Fehler im einen die anderen nicht verhindert.
        if meta is not None and not self._record_finish(meta, report, data):
            data["warnings"].append(
                PipelineWarning(
                    "meta_write_failed", "Lauf konnte in pipeline_meta nicht abgeschlossen werden"
                ).as_dict()
            )
        json_path: Path | None = None
        md_path: Path | None = None
        try:
            json_path, md_path = write_report(data, self.cfg.pipeline.storage.report_dir)
        except Exception:
            log.exception("Berichtsdateien konnten nicht geschrieben werden")
        event(
            log,
            "run_finished",
            f"Lauf beendet: {report.status} – Bericht {json_path or 'nicht als Datei gespeichert'}",
            status=report.status,
            summary=data["summary"],
        )
        self._notify(data)
        return RunOutcome(data, code, json_path, md_path)

    @staticmethod
    def _overall_status(report: RunReport) -> tuple[str, ExitCode]:
        statuses = [s.status for s in report.sources]
        bad = sum(1 for s in statuses if s in ("failed", "aborted_incomplete", "skipped_guard"))
        failed = statuses.count("failed") + statuses.count("aborted_incomplete")
        if failed == len(statuses):
            return ("aborted_incomplete" if "aborted_incomplete" in statuses else "failed"), ExitCode.ALL_FAILED
        if "aborted_incomplete" in statuses:
            # Teilabbruch: betroffene Quellen zurückgerollt, ihr Bestand unverändert (V5)
            return "aborted_incomplete", ExitCode.PARTIAL
        if bad:
            return "partial", ExitCode.PARTIAL
        return "ok", ExitCode.OK

    def _record_finish(self, meta: MetaRepository, report: RunReport, data: dict[str, Any]) -> bool:
        """``finish_run`` – bei Fehler noch einmal über eine neue Verbindung (alte evtl. abgerissen)."""
        assert report.finished_at is not None
        try:
            meta.finish_run(self.run_id, report.finished_at, report.status, data)
            return True
        except Exception:
            log.exception("Lauf-Historie konnte nicht abgeschlossen werden – neuer Versuch")
        try:
            with self._connect(meta_db=self.cfg.pipeline.meta.location == "database") as conn:
                MetaRepository(conn, meta.schema).finish_run(self.run_id, report.finished_at, report.status, data)
            return True
        except Exception:
            log.exception("Lauf-Historie konnte auch über eine neue Verbindung nicht abgeschlossen werden")
            return False

    def _notify(self, data: dict[str, Any]) -> None:
        """Webhook bei jedem Lauf, der nicht ok ist; Fehler dabei werden nur protokolliert."""
        env = self.cfg.pipeline.report.notify_webhook_env
        if not env or data["run"]["status"] == "ok":
            return
        url = os.environ.get(env)
        if not url:
            return
        import httpx

        try:
            resp = httpx.post(url, json={"run": data["run"], "summary": data["summary"]}, timeout=10)
        except Exception as exc:  # auch httpx.InvalidURL u. Ä. – nie den Lauf abbrechen
            log.warning("Benachrichtigung per Webhook fehlgeschlagen: %s", type(exc).__name__)
            return
        if not resp.is_success:
            log.warning("Benachrichtigung per Webhook: HTTP %s", resp.status_code)

    def _new_report(self, src: SourceBase) -> SourceReport:
        return SourceReport(
            id=src.id,
            type=getattr(src, "type", "?"),
            title=src.title,
            dataset=src.target.dataset,
            license=src.license,
            attribution=src.attribution,
            started_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ eine Quelle
    def _process(
        self, src: Any, sr: SourceReport, target: psycopg.Connection[Any], meta: MetaRepository, http: HttpClient
    ) -> None:
        token = source_id_var.set(src.id)
        st = _SourceState()
        try:
            event(log, "source_started", f"Quelle {src.id} ({src.type})")
            self._run_source(src, target, meta, http, sr, st)
        except _GuardSkip as exc:
            self._mark_failed(sr, st, "skipped_guard", None)
            sr.warnings.append(PipelineWarning("guard", str(exc)).as_dict())
            event(log, "source_skipped", f"Nicht ersetzt: {exc}", logging.WARNING)
        except SourceFailure as exc:
            status = "aborted_incomplete" if exc.kind in INCOMPLETE_KINDS else "failed"
            self._mark_failed(
                sr, st, status, {"kind": exc.kind, "message": str(exc), "http_status": exc.http_status, "url": exc.url}
            )
            event(log, "source_failed", f"Quelle fehlgeschlagen ({exc.kind}): {exc}", logging.ERROR)
        except psycopg.Error as exc:
            msg = f"Datenbankfehler, Transaktion zurückgerollt: {clean_text(str(exc).strip(), 500)}"
            self._mark_failed(sr, st, "failed", {"kind": "database", "message": msg})
            event(log, "source_failed", f"Datenbankfehler: {exc}", logging.ERROR)
        except Exception as exc:  # jede Quelle isoliert – nichts darf den Lauf abbrechen
            self._mark_failed(
                sr, st, "failed", {"kind": "internal", "message": clean_text(f"{type(exc).__name__}: {exc}", 500)}
            )
            log.exception("Unerwarteter Fehler in Quelle %s", src.id)
        except BaseException as exc:  # Strg-C/SIGTERM: Quelle noch festhalten, dann weiterreichen
            self._mark_failed(
                sr, st, "failed", {"kind": "interrupted", "message": f"Lauf abgebrochen ({type(exc).__name__})"}
            )
            raise
        finally:
            if not st.committed and sr.status not in ("dry_run", "unchanged"):
                self._drop_unapplied_counts(sr)
            if st.persisted_warnings is None:
                sr.finished_at = datetime.now(UTC)
            self._persist(src, sr, st, meta)
            source_id_var.reset(token)

    @staticmethod
    def _mark_failed(sr: SourceReport, st: _SourceState, status: str, error: dict[str, Any] | None) -> None:
        """Status setzen – nach dem COMMIT nie mehr herabstufen, dann nur eine Warnung."""
        if st.committed:
            msg = (error or {}).get("message") or status
            sr.warnings.append(PipelineWarning("after_commit", f"Problem nach dem Schreiben: {msg}").as_dict())
            return
        sr.status = status
        if error is not None:
            sr.error = error

    @staticmethod
    def _drop_unapplied_counts(sr: SourceReport) -> None:
        """Nicht angewendete Änderungen nicht als neu/geändert/gelöscht/ersetzt ausweisen (nur in ``info``)."""
        na: dict[str, Any] = {}
        if sr.added is not None or sr.deleted is not None:
            na.update(added=sr.added, changed=sr.changed, deleted=sr.deleted, unchanged=sr.unchanged)
            sr.added = sr.changed = sr.deleted = sr.unchanged = None
        if sr.rows_deleted or sr.rows_inserted or sr.rows_updated:
            na.update(rows_deleted=sr.rows_deleted, rows_inserted=sr.rows_inserted, rows_updated=sr.rows_updated)
            sr.rows_deleted = sr.rows_inserted = sr.rows_updated = 0
        for k in ("rows_deleted_by_table", "rows_inserted_by_table", "rows_updated_by_table"):
            if k in sr.info:
                na[k] = sr.info.pop(k)
        if sr.invalid_existing:
            na["invalid_existing"] = sr.invalid_existing
            sr.invalid_existing = 0
        sr.warnings = [w for w in sr.warnings if w.get("code") not in _UNAPPLIED_WARNINGS]
        if na:
            sr.info["not_applied"] = na

    def _run_source(
        self,
        src: Any,
        target: psycopg.Connection[Any],
        meta: MetaRepository,
        http: HttpClient,
        sr: SourceReport,
        st: _SourceState,
    ) -> None:
        p = self.cfg.pipeline
        raw_dir = p.storage.raw_dir / src.id / f"{self.started:%Y%m%dT%H%M%SZ}_{self.run_id[:8]}"
        state = StateStore(p.storage.state_dir, src.id)
        fingerprint = self._check_config_fingerprint(src, state)
        ctx = SourceContext(p, http, self.run_id, raw_dir, state, use_conditional_requests=not self.opts.full)
        plugin = source_class(src.type)(src, ctx)
        fetched = self._fetch(src, plugin, raw_dir, sr)
        if fetched.unchanged and src.id not in self.opts.force_replace:
            sr.status = "unchanged"
            sr.added = sr.changed = sr.deleted = 0
            sr.info.update(fetched.info)
            event(log, "source_unchanged", "Quelle seit dem letzten erfolgreichen Lauf unverändert – kein Ersetzen")
            self._update_state(state, {**fetched.state_updates, CONFIG_FINGERPRINT: fingerprint}, sr)
            return
        parsed = st.parsed = plugin.parse(fetched)
        self._collect_parsed(src, parsed, target, sr)
        st.diff = self._replace(src, parsed, target, meta, sr, st, plugin.location_url())
        if sr.status == "dry_run":
            return
        # ab hier ist der Bestand ersetzt: Fehler sind nur noch Warnungen
        if parsed.values is None:  # erst nach dem Ersetzen: CSV-Quellen prüfen gegen geschriebene Schlüssel
            self.feature_keys[src.id] = {f.key for f in parsed.features}
        self._update_state(
            state, {**fetched.state_updates, **parsed.state_updates, CONFIG_FINGERPRINT: fingerprint}, sr
        )

    def _check_config_fingerprint(self, src: Any, state: StateStore) -> str:
        """Bei geänderter Konfiguration/Version Hash und ETag vergessen, damit die Quelle neu eingelesen wird."""
        fingerprint = config_fingerprint(src)
        stored = state.load()
        if stored.get(CONFIG_FINGERPRINT) != fingerprint and any(k in stored for k in SKIP_STATE_KEYS):
            state.forget(SKIP_STATE_KEYS)
            event(log, "config_changed", "Konfiguration oder Programmversion geändert – Quelle wird neu eingelesen")
        return fingerprint

    @staticmethod
    def _update_state(state: StateStore, values: dict[str, Any], sr: SourceReport) -> None:
        try:
            state.update(values)
        except Exception as exc:
            log.exception("Zustand der Quelle konnte nicht gespeichert werden")
            sr.warnings.append(
                PipelineWarning(
                    "state_write_failed",
                    f"Zustand nicht gespeichert ({type(exc).__name__}) – der nächste Lauf liest die Quelle neu ein",
                ).as_dict()
            )

    def _fetch(self, src: Any, plugin: Any, raw_dir: Path, sr: SourceReport) -> FetchResult:
        if src.id in self.opts.replay:
            fetched = load_fetch_info(self.opts.replay[src.id])
            sr.info["replay"] = str(self.opts.replay[src.id])
        else:
            fetched = plugin.fetch()
            save_fetch_info(raw_dir, fetched)
        sr.warnings += [w.as_dict() for w in fetched.warnings]
        sr.raw = {
            "file": str(fetched.artifacts[0].path) if fetched.artifacts else None,
            "files": len(fetched.artifacts),
            "sha256": fetched.combined_sha256,
        }
        return fetched

    def _collect_parsed(self, src: Any, parsed: ParseResult, target: psycopg.Connection[Any], sr: SourceReport) -> None:
        """Verknüpfung prüfen, Kennzahlen übernehmen, Fehlerquote prüfen."""
        if self.opts.mutate:
            sr.warnings += [w.as_dict() for w in self.opts.mutate(src, parsed)]
        self._check_join(src, parsed, target, sr)
        sr.warnings += [w.as_dict() for w in parsed.warnings]
        sr.info.update(parsed.info)
        sr.fetched, sr.valid, sr.invalid, sr.repaired = parsed.fetched, parsed.valid, parsed.invalid, parsed.repaired
        sr.quality = quality_counts(parsed)
        sr.warnings += quality_warnings(sr.quality, quality_examples(parsed))
        sr.error_examples = [
            {"record_key": e.record_key, "reason_code": e.reason_code, "reason": e.reason} for e in parsed.errors[:20]
        ]
        total = parsed.valid + parsed.invalid
        max_ratio = (
            src.guards.max_error_ratio
            if src.guards.max_error_ratio is not None
            else self.cfg.pipeline.run.max_error_ratio
        )
        if total and parsed.invalid / total > max_ratio:
            raise SourceFailure(
                "too_many_errors",
                f"{parsed.invalid} von {total} Datensätzen fehlerhaft ({parsed.invalid / total:.1%} > {max_ratio:.0%})",
            )

    def _check_join(self, src: Any, parsed: ParseResult, target: psycopg.Connection[Any], sr: SourceReport) -> None:
        """CSV-Kennzahlen gegen die Schlüssel der Geometrie-Quelle prüfen (``geometry_join``)."""
        if not isinstance(src, CsvSource) or not src.geometry_join or parsed.values is None:
            return
        j = src.geometry_join
        keys = self.feature_keys.get(j.source)
        if keys is None:
            ref = self.cfg.source(j.source)
            keys = set(self.adapter.existing_feature_keys(target, ref.target.dataset)["key"].dropna())
        if not keys:
            parsed.warnings.append(
                PipelineWarning(
                    "join_skipped", f"Geometrie-Quelle '{j.source}' hat keine Objekte – Verknüpfung nicht prüfbar"
                )
            )
            return
        values = parsed.values
        miss = ~values["object_key"].isin(keys)
        n = int(miss.sum())
        if not n:
            return
        examples = sorted(values.loc[miss, "object_key"].dropna().unique().tolist())[:20]
        data = {"count": n, "distinct": int(values.loc[miss, "object_key"].nunique()), "examples": examples}
        if j.on_missing == "warn":
            parsed.warnings.append(
                PipelineWarning("unmatched_keys", f"{n} Zeilen ohne passende Geometrie in '{j.source}'", data)
            )
        elif j.on_missing == "drop":
            parsed.values = values[~miss].reset_index(drop=True)
            parsed.warnings.append(PipelineWarning("unmatched_keys", f"{n} Zeilen ohne Geometrie verworfen", data))
        else:
            for _, row in values[miss].head(10_000).iterrows():
                parsed.errors.append(
                    RecordError(
                        row["key"], "unmatched_key", f"Kein Objekt mit Schlüssel {row['object_key']} in '{j.source}'"
                    )
                )
            parsed.invalid_overflow += max(0, n - 10_000)
            if n > 10_000:
                parsed.overflow_reasons["unmatched_key"] = parsed.overflow_reasons.get("unmatched_key", 0) + n - 10_000
            parsed.values = values[~miss].reset_index(drop=True)

    def _diff(
        self,
        src: Any,
        kind: str,
        parsed: ParseResult,
        dctx: DatasetContext,
        existing: int,
        target: psycopg.Connection[Any],
        meta: MetaRepository,
        sr: SourceReport,
    ) -> tuple[DiffResult, dict[str, str | None]]:
        """Vergleich mit den vorhandenen Zieldaten (Bieterantwort 62); liefert Diff und alte Geometrien."""
        try:
            basis = self.adapter.diff_basis(target, dctx, parsed.features, parsed.values)
        except ProjectionUnavailable as exc:
            sr.warnings.append(
                PipelineWarning(
                    "diff_from_baseline",
                    f"Vergleich mit dem Zielbestand nicht möglich ({exc}) – verglichen wird mit dem letzten Lauf",
                ).as_dict()
            )
            return self._diff_baseline(src, kind, parsed.keys_and_hashes(), existing, target, meta, sr)
        old = basis.existing
        dups = int(old["key"].duplicated().sum())
        if dups:
            examples = sorted(old.loc[old["key"].duplicated(), "key"].astype(str).unique().tolist())[:20]
            old = old.drop_duplicates("key")
            sr.warnings.append(
                PipelineWarning(
                    "duplicate_existing_keys",
                    f"{dups} Schlüssel kommen im Zielbestand mehrfach vor – je Schlüssel einmal verglichen",
                    {"count": dups, "examples": examples},
                ).as_dict()
            )
        diff = compute_diff(basis.new, old, True)
        if len(old) and len(basis.new) and not diff.unchanged and not diff.changed:
            # kein einziger gemeinsamer Schlüssel: Abbildung der Schlüsselspalten passt vermutlich nicht
            sr.warnings.append(
                PipelineWarning(
                    "diff_keys_mismatch",
                    f"Kein Schlüssel der Lieferung ({len(basis.new)}) im Zielbestand ({len(old)}) gefunden – "
                    "Zuordnung der Schlüsselspalten in target.yaml prüfen",
                ).as_dict()
            )
        return diff, basis.old_geoms

    def _diff_baseline(
        self,
        src: Any,
        kind: str,
        new: Any,
        existing: int,
        target: psycopg.Connection[Any],
        meta: MetaRepository,
        sr: SourceReport,
    ) -> tuple[DiffResult, dict[str, str | None]]:
        """Rückfall: Vergleich mit ``pipeline_meta.diff_keys`` des letzten erfolgreichen Laufs."""
        with_geoms = self.cfg.pipeline.meta.diff_geoms and kind == "features"
        base = meta.baseline(src.id, with_geoms=with_geoms)
        if base is not None:
            return compute_diff(new, base[0], True), base[1]
        if existing == 0:
            return compute_diff(new, None, True), {}
        if kind == "features":
            diff = compute_diff(new, self.adapter.existing_feature_keys(target, src.target.dataset), False)
            sr.warnings.append(
                PipelineWarning(
                    "no_baseline",
                    "Keine Vergleichsbasis aus einem früheren Lauf – Änderungen werden ab dem nächsten Lauf gezählt",
                ).as_dict()
            )
            return diff, {}
        sr.warnings.append(
            PipelineWarning(
                "no_baseline",
                f"Keine Vergleichsbasis: Bestand {existing} Zeilen ersetzt durch {len(new)} – Zählung ab dem nächsten Lauf",
            ).as_dict()
        )
        return DiffResult(0, None, 0, None, False, new.assign(change=CHANGE_REPLACED)), {}

    def _guard_problems(self, src: Any, kind: str, n_new: int, existing: int) -> list[str]:
        g = src.guards
        minimum = g.min_features if kind == "features" else g.min_rows
        problems = []
        if n_new == 0 and existing:
            problems.append("keine gültigen Datensätze geliefert")
        if minimum is not None and n_new < minimum:
            problems.append(f"nur {n_new} gültige Datensätze (Minimum {minimum})")
        if g.max_drop_ratio is not None and existing and n_new < existing * (1 - g.max_drop_ratio):
            problems.append(f"Rückgang von {existing} auf {n_new} (> {g.max_drop_ratio:.0%})")
        return problems

    def _replace(
        self,
        src: Any,
        parsed: ParseResult,
        target: psycopg.Connection[Any],
        meta: MetaRepository,
        sr: SourceReport,
        st: _SourceState,
        url: str | None,
    ) -> tuple[DiffResult, dict[str, str | None]]:
        kind = "values" if parsed.values is not None else "features"
        dataset = src.target.dataset
        new = parsed.keys_and_hashes()
        try:
            with target.transaction():
                own = self._check_ownership(src, target, meta)
                dctx = DatasetContext(
                    dataset,
                    src,
                    url,
                    self.run_id,
                    self.started,
                    expected_metadata_id=own.metadata_id,
                    check_metadata_id=True,
                )
                existing = self.adapter.count_existing(target, dataset, kind)
                diff, old_geoms = self._diff(src, kind, parsed, dctx, existing, target, meta, sr)
                diff, sr.invalid_existing = mark_invalid_keys(diff, parsed.errors)
                counts = diff.counts()
                sr.added, sr.changed, sr.deleted, sr.unchanged = (
                    counts["added"],
                    counts["changed"],
                    counts["deleted"],
                    counts["unchanged"],
                )
                sr.compared = diff.compared
                sr.info["existing_before"] = existing
                problems = self._guard_problems(src, kind, len(new), existing)
                if problems:
                    if src.id not in self.opts.force_replace:
                        raise _GuardSkip("Bestand bleibt erhalten: " + "; ".join(problems))
                    sr.warnings.append(
                        PipelineWarning(
                            "guard_forced", "Schutzregel übersteuert (--force-replace): " + "; ".join(problems)
                        ).as_dict()
                    )
                if self.opts.dry_run:
                    sr.status = "dry_run"
                    sr.preview_sql = self.adapter.preview_sql(target, dctx, kind)
                    self._ownership_warning(src, sr, own, dry_run=True)  # kein Besitzeintrag im Probelauf
                    self._invalid_existing_warning(sr, dry_run=True)
                    raise _DryRunRollback
                try:
                    stats = self.adapter.replace(target, dctx, parsed.features, parsed.values)
                except OwnershipError as exc:
                    raise SourceFailure("ownership", str(exc)) from exc
                # Liegt pipeline_meta in einer eigenen Datenbank, ist der Eintrag vor dem COMMIT der
                # Zieltabellen schon festgeschrieben (verwaister Eintrag bei späterem ROLLBACK, s. _claim)
                self._claim(src, meta, own, stats.metadata_id)
                self._ownership_warning(src, sr, own)
                # gilt erst mit dem COMMIT; bei ROLLBACK verschiebt _drop_unapplied_counts nach not_applied
                sr.rows_deleted, sr.rows_inserted = stats.rows_deleted, stats.rows_inserted
                sr.rows_updated = stats.rows_updated
                sr.info["rows_deleted_by_table"] = stats.deleted
                sr.info["rows_inserted_by_table"] = stats.inserted
                if kind == "features":
                    sr.info["rows_updated_by_table"] = stats.updated
                self._retained_warning(sr, stats)
                diff = self._reconcile_db_geometries(sr, parsed, diff, stats)
                self._invalid_existing_warning(sr)
                geoms = self._diff_geoms(src, kind, parsed, diff, old_geoms)
                if meta.conn is target:  # Lauf-Historie in derselben Transaktion (atomar mit dem Ersetzen)
                    sr.status, sr.data_unchanged, sr.finished_at = "ok", False, datetime.now(UTC)
                    self._write_result(meta, src, sr, diff, geoms)
                    st.persisted_warnings = len(sr.warnings)
            st.committed = True
            sr.status, sr.data_unchanged = "ok", False
        except _DryRunRollback:
            geoms = {}
        except BaseException:
            sr.status, sr.data_unchanged = "running", True  # zurückgerollt: Status setzt _process
            st.persisted_warnings = None
            raise
        event(
            log,
            "source_replaced" if sr.status != "dry_run" else "source_dry_run",
            f"neu {sr.added}, geändert {sr.changed}, gelöscht {sr.deleted}, unverändert {sr.unchanged}",
            added=sr.added,
            changed=sr.changed,
            deleted=sr.deleted,
        )
        return diff, geoms

    def _check_ownership(self, src: Any, target: psycopg.Connection[Any], meta: MetaRepository) -> _Ownership:
        """Darf die Pipeline die Metadatenzeile des Datensatzes ändern? Sonst ``SourceFailure('ownership')``.

        Eigen ist eine Zeile, die laut ``owned_datasets`` (Ort und Primärschlüssel) von der Pipeline
        angelegt oder bewusst übernommen wurde. Altbestand aus der Zeit vor der Besitzerfassung wird über
        die Lauf-Historie erkannt; jede andere bestehende Zeile gehört der MOSIDI-Anwendung.
        Bewusst ohne ``FOR UPDATE``: Die Zeile wäre sonst während Diff und Aufbau der Ladezeilen gesperrt.
        ``replace()`` sperrt sie erst zum Schreiben und prüft dann erneut (``expected_metadata_id``).
        """
        dataset = src.target.dataset
        pk = self.adapter.metadata_id(target, dataset)
        if pk is None:
            return _Ownership(None, "new")
        owner = meta.owned_dataset(dataset)
        if owner is not None and owner.location == self._location(pk):
            return _Ownership(pk, "owned")
        if src.id in self.opts.adopt:
            return _Ownership(pk, "adopt")
        if owner is None and (legacy := meta.legacy_import_run(dataset)) is not None:
            return _Ownership(pk, "backfill", legacy)
        raise SourceFailure("ownership", self._ownership_message(src, pk, owner))

    def _location(self, pk: Any) -> tuple[str, str, str]:
        t = self.cfg.target
        return (t.schema_name, t.tables.metadata.name, str(pk))

    def _ownership_message(self, src: Any, pk: Any, owner: OwnedDataset | None) -> str:
        t = self.cfg.target
        m = t.tables.metadata
        msg = (
            f"Datensatz '{src.target.dataset}' hat bereits eine Metadatenzeile ({t.schema_name}.{m.name}, "
            f"{m.pk}={pk}), die nicht von der Pipeline angelegt wurde – Bestand bleibt unverändert."
        )
        if owner is not None:
            msg += f" Laut pipeline_meta gehört der Pipeline {owner.target_schema}.{owner.metadata_table} {owner.metadata_pk}."
        return (
            msg + " Zeile prüfen (z. B. target.dataset umbenennen) oder bewusst übernehmen mit "
            f"'pipeline run --source {src.id} --adopt {src.id}'."
        )

    def _claim(self, src: Any, meta: MetaRepository, own: _Ownership, metadata_id: Any) -> None:
        """Besitz vermerken – in der Ersetzungs-Transaktion, wenn pipeline_meta in der Zieldatenbank liegt.

        Sonst wird der Eintrag vor dem COMMIT der Zieltabellen festgeschrieben: Scheitert danach das COMMIT,
        bleibt er verwaist stehen. Bei neu angelegten Zeilen schadet das nicht (die Zeile gibt es nicht,
        der nächste Lauf ersetzt den Eintrag); bei ``--adopt`` gilt die Übernahme dann als erfolgt.
        """
        if own.action == "owned":
            return
        meta.claim_dataset(
            src.target.dataset,
            src.id,
            self._location(metadata_id),
            created_run_id=self.run_id if own.action == "new" else own.created_run_id,
            adopted_run_id=self.run_id if own.action == "adopt" else None,
            backfilled=own.action == "backfill",
        )

    def _ownership_warning(self, src: Any, sr: SourceReport, own: _Ownership, dry_run: bool = False) -> None:
        m = self.cfg.target.tables.metadata
        row = f"{self.cfg.target.schema_name}.{m.name} {m.pk}={own.metadata_id}"
        suffix = " (Probelauf: nicht vermerkt)" if dry_run else ""
        if own.action == "adopt":
            text = f"Bestehende Metadatenzeile {row} bewusst übernommen (--adopt){suffix}"
            sr.warnings.append(PipelineWarning("dataset_adopted", text).as_dict())
        elif own.action == "backfill":
            text = (
                f"Metadatenzeile {row} stammt laut Lauf-Historie von der Pipeline (Bestand vor der "
                f"Besitzerfassung) – als eigen vermerkt{suffix}"
            )
            sr.warnings.append(PipelineWarning("ownership_backfilled", text).as_dict())

    @staticmethod
    def _reconcile_db_geometries(
        sr: SourceReport, parsed: ParseResult, diff: DiffResult, stats: ReplaceStats
    ) -> DiffResult:
        """Ergebnis des Schutznetzes in der Datenbank in Bericht, Fehlerliste und Diff übernehmen (Antwort 63)."""
        if not (stats.db_repaired or stats.db_skipped):
            return diff
        repaired, skipped = set(stats.db_repaired_keys), set(stats.db_skipped_keys)
        for f in parsed.features:
            if f.key in repaired:
                f.repaired = True
        parsed.features = [f for f in parsed.features if f.key not in skipped]
        parsed.errors += [
            RecordError(
                k, "invalid_geometry", "Geometrie in der Datenbank ungültig und nicht reparierbar (ST_MakeValid)"
            )
            for k in stats.db_skipped_keys
        ]
        diff, existing = drop_skipped_keys(diff, stats.db_skipped_keys)
        counts = diff.counts()
        sr.added, sr.changed, sr.deleted, sr.unchanged = (
            counts["added"],
            counts["changed"],
            counts["deleted"],
            counts["unchanged"],
        )
        sr.invalid_existing += existing
        sr.valid, sr.invalid, sr.repaired = parsed.valid, parsed.invalid, parsed.repaired
        sr.quality = quality_counts(parsed)
        sr.warnings = [w for w in sr.warnings if w.get("code") not in QUALITY_CODES]
        sr.warnings += quality_warnings(sr.quality, quality_examples(parsed))
        sr.error_examples = [
            {"record_key": e.record_key, "reason_code": e.reason_code, "reason": e.reason} for e in parsed.errors[:20]
        ]
        return diff

    @staticmethod
    def _retained_warning(sr: SourceReport, stats: ReplaceStats) -> None:
        """Hinweis auf Geometrien, die in der Lieferung fehlen und erhalten bleiben (Bieterantwort 74)."""
        if not stats.retained:
            return
        sr.warnings.append(
            PipelineWarning(
                "geometry_retained",
                f"{stats.retained} Geometrien sind in der Lieferung nicht mehr enthalten und bleiben im Bestand "
                "erhalten (nur ihre Attribute dieser Quelle entfallen) – bei Bedarf fachlich prüfen",
                {"count": stats.retained, "examples": stats.retained_examples},
            ).as_dict()
        )

    @staticmethod
    def _invalid_existing_warning(sr: SourceReport, dry_run: bool = False) -> None:
        if not sr.invalid_existing:
            return
        effect = "würden im Bestand fehlen (Probelauf)" if dry_run else "sind im Bestand nicht mehr enthalten"
        sr.warnings.append(
            PipelineWarning(
                "invalid_existing",
                f"{sr.invalid_existing} bisher vorhandene Datensätze sind in dieser Lieferung ungültig "
                f"(siehe Fehlerliste) – nicht als gelöscht gezählt, {effect}",
            ).as_dict()
        )

    def _diff_geoms(
        self, src: Any, kind: str, parsed: ParseResult, diff: DiffResult, old_geoms: dict[str, str | None]
    ) -> dict[str, str | None]:
        """Geometrien für die Änderungskarte (nur ``meta.diff_geoms``); entfallene aus der Vergleichsbasis."""
        if not (self.cfg.pipeline.meta.diff_geoms and kind == "features"):
            return {}
        geoms = {f.key: ewkb_hex(f.geometry) for f in parsed.features}
        deleted_keys = diff.keys.loc[diff.keys["change"] == CHANGE_DELETED, "key"]
        geoms.update({k: old_geoms.get(k) for k in deleted_keys})
        return geoms

    def _result_row(self, src: Any, sr: SourceReport) -> dict[str, Any]:
        return {
            "source_id": src.id,
            "source_type": sr.type,
            "dataset": sr.dataset,
            "status": sr.status,
            "started_at": sr.started_at,
            "finished_at": sr.finished_at,
            "fetched": sr.fetched,
            "valid": sr.valid,
            "invalid": sr.invalid,
            "repaired": sr.repaired,
            "added": sr.added,
            "changed": sr.changed,
            "deleted": sr.deleted,
            "unchanged": sr.unchanged,
            "rows_deleted": sr.rows_deleted,
            "rows_inserted": sr.rows_inserted,
            "error_kind": (sr.error or {}).get("kind"),
            "error_message": (sr.error or {}).get("message"),
            "warnings": sr.warnings,
            "raw_file": (sr.raw or {}).get("file"),
            "raw_sha256": (sr.raw or {}).get("sha256"),
        }

    def _write_result(
        self,
        meta: MetaRepository,
        src: Any,
        sr: SourceReport,
        diff: DiffResult | None,
        geoms: dict[str, str | None],
    ) -> None:
        """Ergebnis und (bei Erfolg) Diff-Schlüssel gemeinsam schreiben (eine Transaktion bzw. ein Savepoint)."""
        with meta.conn.transaction():
            meta.write_source_result(self.run_id, self._result_row(src, sr))
            if diff is not None and sr.status == "ok":
                meta.write_diff_keys(self.run_id, src.id, diff.keys, geoms)

    def _persist(self, src: Any, sr: SourceReport, st: _SourceState, meta: MetaRepository) -> None:
        """Ergebnis, Fehler und (bei Erfolg) Diff-Schlüssel in ``pipeline_meta`` schreiben.

        Liegt die Lauf-Historie in der Zieldatenbank, ist das Ergebnis schon in der Ersetzungs-Transaktion
        gespeichert; hier werden nur später hinzugekommene Warnungen nachgetragen. Sonst gehen Ergebnis
        und Diff-Schlüssel gemeinsam in eine Transaktion, damit kein „ok“ ohne Vergleichsbasis stehen
        bleibt; scheitert das, wird das Ergebnis allein nachgetragen.
        """
        if st.persisted_warnings is not None:
            if len(sr.warnings) != st.persisted_warnings:
                try:
                    meta.update_source_warnings(self.run_id, src.id, sr.warnings)
                except Exception:
                    log.exception("Warnungen der Quelle %s konnten nicht nachgetragen werden", src.id)
        else:
            diff, geoms = st.diff if st.diff is not None else (None, {})
            try:
                self._write_result(meta, src, sr, diff, geoms)
            except Exception:
                log.exception("Lauf-Historie für %s konnte nicht geschrieben werden", src.id)
                sr.warnings.append(
                    PipelineWarning(
                        "meta_write_failed",
                        "Diff-Schlüssel konnten nicht geschrieben werden – der nächste Lauf vergleicht "
                        "mit der vorherigen Vergleichsbasis.",
                    ).as_dict()
                )
                try:
                    meta.write_source_result(self.run_id, self._result_row(src, sr))
                except Exception:
                    log.exception("Ergebnis der Quelle %s konnte nicht gespeichert werden", src.id)
        try:
            if st.parsed is not None and st.parsed.errors:
                meta.write_errors(self.run_id, src.id, st.parsed.errors)
        except Exception:
            log.exception("Fehlerdatensätze der Quelle %s konnten nicht gespeichert werden", src.id)
            sr.warnings.append(
                PipelineWarning("meta_write_failed", "Fehlerdatensätze konnten nicht gespeichert werden").as_dict()
            )


def run_pipeline(cfg: LoadedConfig, opts: RunOptions) -> RunOutcome:
    """Bequemer Einstieg für CLI und Tests."""
    return Orchestrator(cfg, opts).run()
