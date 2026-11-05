"""Zugriff auf das eigene Schema ``pipeline_meta`` (Lauf-Historie, Fehler, Diff-Schlüssel, Besitz der Datensätze).

Liegt standardmäßig als eigenes Schema in der Zieldatenbank (``meta.location: schema``) oder,
falls die TH kein zusätzliches Schema wünscht, in einer eigenen Datenbank auf demselben Server
(``meta.location: database``). Tabellen der TH werden hier nie berührt.

Schemaänderungen sind nummerierte Migrationen (``migrations/NNNN_name.sql``), angewendet nur von
``pipeline init-meta``; die erreichte Version steht in ``schema_version``. Ein Lauf prüft nur die
Version und braucht daher keine Rechte zum Ändern der Struktur (Betriebshandbuch, Abschnitt 4).
Neue Migration: nächste Nummer als Datei ablegen, ``{schema}`` als Platzhalter für das Schema.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
import shapely
from psycopg import sql
from shapely.geometry.base import BaseGeometry

from geoimport.config.models import IDENT_RE
from geoimport.sources.base import RecordError

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
MIGRATION_LOCK = "geoimport:meta-migrate"
CHANGE_INVALID = "invalid"  # Schlüssel, dessen neuer Datensatz ungültig war (nicht „entfallen“)


class MetaSchemaError(RuntimeError):
    """``pipeline_meta`` fehlt oder hat nicht die erwartete Version."""


@dataclass(frozen=True)
class Migration:
    """Eine Schemaänderung (Datei ``NNNN_name.sql``)."""

    version: int
    name: str
    path: Path

    def sql_for(self, schema: str) -> str:
        """SQL mit eingesetztem Schemanamen."""
        return self.path.read_text(encoding="utf-8").replace("{schema}", '"' + schema + '"')


@dataclass(frozen=True)
class OwnedDataset:
    """Eintrag in ``owned_datasets``: Metadatenzeile, die der Pipeline gehört."""

    dataset: str
    source_id: str
    target_schema: str
    metadata_table: str
    metadata_pk: str
    adopted: bool
    backfilled: bool

    @property
    def location(self) -> tuple[str, str, str]:
        """Schema, Metadatentabelle und Primärschlüssel (Text)."""
        return (self.target_schema, self.metadata_table, self.metadata_pk)


def migrations() -> list[Migration]:
    """Alle Migrationen, aufsteigend und lückenlos ab 1."""
    found = []
    for p in MIGRATIONS_DIR.glob("*.sql"):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            raise RuntimeError(f"Migrationsdatei {p.name}: Name muss NNNN_name.sql sein")
        found.append(Migration(int(m.group(1)), m.group(2), p))
    found.sort(key=lambda m: m.version)
    if [m.version for m in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("Migrationen müssen lückenlos ab 0001 nummeriert sein")
    return found


def expected_version() -> int:
    """Schemaversion, die diese Programmversion erwartet."""
    return len(migrations())


def strip_nul(value: Any) -> Any:
    """NUL-Zeichen aus allen Texten entfernen (PostgreSQL lehnt sie in text/jsonb ab)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(v) for v in value]
    return value


def _json(value: Any) -> str:
    """JSON für jsonb-Spalten, ohne NUL (auch nicht aus ``str()`` fremder Objekte)."""
    return json.dumps(strip_nul(value), ensure_ascii=False, default=lambda o: str(o).replace("\x00", ""))


def ewkb_hex(geom: BaseGeometry | None) -> str | None:
    """Geometrie als Hex-EWKB mit SRID 4326 (für COPY in geometry-Spalten)."""
    if geom is None or geom.is_empty:
        return None
    return str(shapely.to_wkb(shapely.set_srid(geom, 4326), hex=True, include_srid=True))


class MetaRepository:
    """Lesen und Schreiben der Lauf-Historie."""

    def __init__(self, conn: psycopg.Connection[Any], schema: str) -> None:
        if not IDENT_RE.fullmatch(schema):
            raise ValueError(f"ungültiger Schemaname {schema!r}")
        self.conn = conn
        self.schema = schema

    def _t(self, table: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(table))

    # ------------------------------------------------------------------ Schema
    def _regclass(self, table: str) -> bool:
        row = self.conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f'"{self.schema}"."{table}"',)).fetchone()
        return bool(row and row[0])

    def schema_version(self) -> int | None:
        """Erreichte Schemaversion; ``None`` ohne Schema, 0 bei Installationen ohne Versionstabelle."""
        if self._regclass("schema_version"):
            row = self.conn.execute(sql.SQL("SELECT max(version) FROM {}").format(self._t("schema_version"))).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        return 0 if self._regclass("import_runs") else None

    def check_schema(self) -> None:
        """Vor jedem Lauf: Schema vorhanden und in der erwarteten Version? Sonst ``MetaSchemaError``."""
        current, expected = self.schema_version(), expected_version()
        if current == expected:
            return
        if current is None:
            raise MetaSchemaError(
                f"Schema {self.schema} fehlt – einmalig 'pipeline init-meta' ausführen "
                "(mit einer Rolle, die das Schema anlegen darf)"
            )
        if current > expected:
            raise MetaSchemaError(
                f"Schema {self.schema} hat Version {current}, diese Programmversion kennt nur {expected} – "
                "Image aktualisieren"
            )
        raise MetaSchemaError(
            f"Schema {self.schema} hat Version {current}, erwartet {expected} – 'pipeline init-meta' ausführen"
        )

    def migrate(self, app_version: str | None = None) -> list[int]:
        """Fehlende Migrationen anwenden (nur ``pipeline init-meta``); liefert die angewendeten Versionen.

        Ohne ausstehende Migration wird kein DDL ausgeführt, damit ``init-meta`` vor jedem Lauf auch
        mit der Schreibrolle unschädlich ist. Parallele Aufrufe warten auf eine Sperre.
        """
        current = self.schema_version()
        if current == expected_version():
            return []
        applied: list[int] = []
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 0)", (MIGRATION_LOCK,))
            # Nur anlegen, was fehlt: CREATE … IF NOT EXISTS prüft die Rechte vor dem Vorhandensein
            if not self.conn.execute("SELECT 1 FROM pg_extension WHERE extname='postgis'").fetchone():
                self.conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            if not self.conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (self.schema,)).fetchone():
                self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            self.conn.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} (version int PRIMARY KEY, name text NOT NULL, "
                    "applied_at timestamptz NOT NULL DEFAULT now(), app_version text)"
                ).format(self._t("schema_version"))
            )
            current = self.schema_version() or 0
            for m in migrations():
                if m.version <= current:
                    continue
                self.conn.execute(m.sql_for(self.schema))
                self.conn.execute(
                    sql.SQL("INSERT INTO {} (version, name, app_version) VALUES (%s, %s, %s)").format(
                        self._t("schema_version")
                    ),
                    (m.version, m.name, app_version),
                )
                applied.append(m.version)
        return applied

    def exists(self) -> bool:
        """True, wenn ``import_runs`` vorhanden ist."""
        return self._regclass("import_runs")

    # ------------------------------------------------------------------ Läufe
    def start_run(
        self,
        run_id: str,
        started_at: datetime,
        trigger: str,
        simulated: bool,
        dry_run: bool,
        app_version: str,
        config_hash: str,
    ) -> None:
        """Lauf mit Status ``running`` anlegen."""
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL(
                    "INSERT INTO {} (run_id, started_at, trigger, status, simulated, dry_run, app_version, config_hash) "
                    "VALUES (%s, %s, %s, 'running', %s, %s, %s, %s)"
                ).format(self._t("import_runs")),
                (run_id, started_at, trigger, simulated, dry_run, app_version, config_hash),
            )

    def abort_stale_runs(self, finished_at: datetime) -> int:
        """Läufe, die noch auf ``running`` stehen, als ``aborted`` markieren (nur unter der Lauf-Sperre aufrufen)."""
        with self.conn.transaction():
            cur = self.conn.execute(
                sql.SQL("UPDATE {} SET status='aborted', finished_at=%s WHERE status='running'").format(
                    self._t("import_runs")
                ),
                (finished_at,),
            )
            return cur.rowcount

    def finish_run(self, run_id: str, finished_at: datetime, status: str, report: dict[str, Any]) -> None:
        """Lauf abschließen und Bericht ablegen."""
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL("UPDATE {} SET finished_at=%s, status=%s, report=%s::jsonb WHERE run_id=%s").format(
                    self._t("import_runs")
                ),
                (finished_at, status, _json(report), run_id),
            )

    def write_source_result(self, run_id: str, r: dict[str, Any]) -> None:
        """Ergebnis einer Quelle speichern."""
        cols = [
            "source_id", "source_type", "dataset", "status", "started_at", "finished_at", "fetched", "valid",
            "invalid", "repaired", "added", "changed", "deleted", "unchanged", "rows_deleted", "rows_inserted",
            "error_kind", "error_message", "warnings", "raw_file", "raw_sha256",
        ]  # fmt: skip
        values = [strip_nul(r.get(c)) for c in cols]
        values[cols.index("warnings")] = _json(r.get("warnings") or [])
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL("INSERT INTO {} (run_id, {}) VALUES (%s, {})").format(
                    self._t("import_source_results"),
                    sql.SQL(", ").join(map(sql.Identifier, cols)),
                    sql.SQL(", ").join(sql.Placeholder() * len(cols)),
                ),
                [run_id, *values],
            )

    def update_source_warnings(self, run_id: str, source_id: str, warnings: list[dict[str, Any]]) -> None:
        """Warnungen einer bereits gespeicherten Quelle nachtragen (Probleme nach dem Schreiben)."""
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL("UPDATE {} SET warnings=%s::jsonb WHERE run_id=%s AND source_id=%s").format(
                    self._t("import_source_results")
                ),
                (_json(warnings), run_id, source_id),
            )

    def write_errors(self, run_id: str, source_id: str, errors: list[RecordError]) -> None:
        """Fehlerhafte Einzeldatensätze speichern (COPY)."""
        if not errors:
            return
        q = sql.SQL("COPY {} (run_id, source_id, record_key, reason_code, reason, geom, raw) FROM STDIN").format(
            self._t("import_errors")
        )
        with self.conn.transaction(), self.conn.cursor() as cur, cur.copy(q) as cp:
            for e in errors:
                cp.write_row(
                    [
                        run_id,
                        source_id,
                        strip_nul(e.record_key),
                        strip_nul(e.reason_code),
                        strip_nul(e.reason)[:2000],
                        ewkb_hex(e.geometry),
                        _json(e.raw),
                    ]
                )

    def write_diff_keys(
        self, run_id: str, source_id: str, keys: pd.DataFrame, geoms: dict[str, str | None] | None = None
    ) -> None:
        """Schlüssel, Hash und Änderungsart je Datensatz (Vergleichsbasis des nächsten Laufs)."""
        q = sql.SQL("COPY {} (run_id, source_id, record_key, hash, change, geom) FROM STDIN").format(
            self._t("diff_keys")
        )
        geoms = geoms or {}
        with self.conn.transaction(), self.conn.cursor() as cur, cur.copy(q) as cp:
            for key, h, change in (
                keys[["key", "hash"]].assign(change=keys["change"]).itertuples(index=False, name=None)
            ):
                cp.write_row([run_id, source_id, key, None if pd.isna(h) else h, change, geoms.get(key)])

    # ------------------------------------------------------------------ Vergleichsbasis
    def _baselines(self) -> sql.Composed:
        """Vergleichsbasis je Quelle: letzter erfolgreicher, schreibender Lauf, zu dem Diff-Schlüssel vorliegen.

        Ohne Schlüssel (Schreiben der Lauf-Historie gescheitert) wäre die Basis leer und der nächste
        Vergleich meldete alles als neu – dann gilt der davor liegende Lauf.
        """
        return sql.SQL(
            """SELECT DISTINCT ON (r.source_id) r.source_id, r.run_id
               FROM {res} r JOIN {runs} u USING (run_id)
               WHERE r.status = 'ok' AND NOT u.dry_run
                 AND EXISTS (SELECT 1 FROM {keys} k WHERE k.source_id = r.source_id AND k.run_id = r.run_id)
               ORDER BY r.source_id, u.started_at DESC"""
        ).format(res=self._t("import_source_results"), runs=self._t("import_runs"), keys=self._t("diff_keys"))

    def baseline_run(self, source_id: str) -> str | None:
        """Lauf-ID der Vergleichsbasis der Quelle (siehe ``_baselines``)."""
        row = self.conn.execute(
            sql.SQL("SELECT b.run_id::text FROM (") + self._baselines() + sql.SQL(") b WHERE b.source_id = %s"),
            (source_id,),
        ).fetchone()
        return str(row[0]) if row else None

    def baseline(self, source_id: str, with_geoms: bool = False) -> tuple[pd.DataFrame, dict[str, str | None]] | None:
        """Schlüssel/Hashes des Bestands laut letztem erfolgreichem Lauf (ohne entfallene und ungültige)."""
        run_id = self.baseline_run(source_id)
        if run_id is None:
            return None
        geom_col = sql.SQL(", encode(ST_AsEWKB(geom), 'hex')") if with_geoms else sql.SQL("")
        rows = self.conn.execute(
            sql.SQL(
                "SELECT record_key, hash{} FROM {} WHERE run_id=%s AND source_id=%s AND change NOT IN ('deleted', %s)"
            ).format(geom_col, self._t("diff_keys")),
            (run_id, source_id, CHANGE_INVALID),
        ).fetchall()
        df = pd.DataFrame(
            {
                "key": pd.Series([r[0] for r in rows], dtype="string"),
                "hash": pd.Series([r[1] for r in rows], dtype="string"),
            }
        )
        geoms = {r[0]: r[2] for r in rows} if with_geoms else {}
        return df, geoms

    # ------------------------------------------------------------------ Besitz der Datensätze (H7)
    def owned_dataset(self, dataset: str) -> OwnedDataset | None:
        """Besitzeintrag des Datensatzes (``None``, wenn die Pipeline ihn nicht als eigen kennt)."""
        row = self.conn.execute(
            sql.SQL(
                "SELECT dataset, source_id, target_schema, metadata_table, metadata_pk, adopted, backfilled "
                "FROM {} WHERE dataset = %s"
            ).format(self._t("owned_datasets")),
            (dataset,),
        ).fetchone()
        return OwnedDataset(*row) if row else None

    def legacy_import_run(self, dataset: str) -> str | None:
        """Erster erfolgreicher, schreibender Lauf des Datensatzes aus der Zeit vor der Besitzerfassung.

        Nur Läufe vor Migration 2 zählen: Danach legt jeder erfolgreiche Lauf einen Besitzeintrag an,
        ein fehlender Eintrag ist dann kein Altbestand, sondern verdächtig.
        """
        row = self.conn.execute(
            sql.SQL(
                """SELECT r.run_id::text FROM {res} r JOIN {runs} u USING (run_id)
                   WHERE r.dataset = %s AND r.status IN ('ok', 'unchanged') AND NOT u.dry_run
                     AND u.started_at < (SELECT applied_at FROM {ver} WHERE version = 2)
                   ORDER BY u.started_at LIMIT 1"""
            ).format(res=self._t("import_source_results"), runs=self._t("import_runs"), ver=self._t("schema_version")),
            (dataset,),
        ).fetchone()
        return str(row[0]) if row else None

    def claim_dataset(
        self,
        dataset: str,
        source_id: str,
        location: tuple[str, str, str],
        *,
        created_run_id: str | None = None,
        adopted_run_id: str | None = None,
        backfilled: bool = False,
    ) -> None:
        """Datensatz als eigen vermerken (``location``: Schema, Metadatentabelle, PK als Text).

        Ein älterer Eintrag desselben Datensatzes (Zeile inzwischen gelöscht und neu angelegt) wird ersetzt.
        """
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL(
                    """INSERT INTO {} (dataset, source_id, target_schema, metadata_table, metadata_pk,
                                       created_run_id, adopted, adopted_run_id, backfilled)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (dataset) DO UPDATE SET source_id = EXCLUDED.source_id,
                         target_schema = EXCLUDED.target_schema, metadata_table = EXCLUDED.metadata_table,
                         metadata_pk = EXCLUDED.metadata_pk, created_run_id = EXCLUDED.created_run_id,
                         created_at = now(), adopted = EXCLUDED.adopted, adopted_run_id = EXCLUDED.adopted_run_id,
                         backfilled = EXCLUDED.backfilled"""
                ).format(self._t("owned_datasets")),
                (dataset, source_id, *location, created_run_id, adopted_run_id is not None, adopted_run_id, backfilled),
            )

    # ------------------------------------------------------------------ Aufräumen
    def prune(self, keep_runs: int) -> None:
        """Details älterer Läufe löschen; Lauf-Köpfe und Quellergebnisse bleiben.

        Zusätzlich werden unveränderte/ersetzte Schlüssel älterer Läufe entfernt, sofern sie
        nicht mehr Vergleichsbasis sind – bei großen Kennzahl-Quellen (INKAR) spart das Platz.
        """
        runs = self._t("import_runs")
        keys = self._t("diff_keys")
        old = sql.SQL("SELECT run_id FROM {} ORDER BY started_at DESC OFFSET %s").format(runs)
        # Basis je Quelle einmal bestimmen (nicht je Zeile von diff_keys – bei INKAR Millionen Zeilen)
        not_baseline = (
            sql.SQL(" AND NOT EXISTS (SELECT 1 FROM (")
            + self._baselines()
            + sql.SQL(") b WHERE b.source_id = d.source_id AND b.run_id = d.run_id)")
        )
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL("DELETE FROM {} WHERE run_id IN (").format(self._t("import_errors")) + old + sql.SQL(")"),
                (keep_runs,),
            )
            # Die Vergleichsbasis jeder Quelle bleibt erhalten, auch wenn sie älter ist (z. B. jährliche INKAR-Daten)
            self.conn.execute(
                sql.SQL("DELETE FROM {} d WHERE d.run_id IN (").format(keys) + old + sql.SQL(")") + not_baseline,
                (keep_runs,),
            )
            self.conn.execute(
                sql.SQL("DELETE FROM {} d WHERE d.change IN ('unchanged', 'replaced')").format(keys) + not_baseline
            )

    # ------------------------------------------------------------------ Abfragen für CLI/Karte
    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Letzte Läufe."""
        cur = self.conn.execute(
            sql.SQL(
                "SELECT run_id::text, started_at, finished_at, trigger, status, simulated, dry_run "
                "FROM {} ORDER BY started_at DESC LIMIT %s"
            ).format(self._t("import_runs")),
            (limit,),
        )
        names = [d.name for d in cur.description or []]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def report(self, run_id: str | None = None) -> dict[str, Any] | None:
        """Gespeicherter Bericht eines Laufs (Standard: letzter abgeschlossener)."""
        if run_id:
            q = sql.SQL("SELECT report FROM {} WHERE run_id=%s").format(self._t("import_runs"))
            row = self.conn.execute(q, (run_id,)).fetchone()
        else:
            q = sql.SQL("SELECT report FROM {} WHERE report IS NOT NULL ORDER BY started_at DESC LIMIT 1").format(
                self._t("import_runs")
            )
            row = self.conn.execute(q).fetchone()
        return row[0] if row else None

    def last_results(self) -> dict[str, dict[str, Any]]:
        """Letztes Ergebnis je Quelle."""
        cur = self.conn.execute(
            sql.SQL(
                "SELECT DISTINCT ON (r.source_id) r.source_id, r.status, r.finished_at, r.added, r.changed, r.deleted, "
                "r.error_message FROM {} r JOIN {} u USING (run_id) ORDER BY r.source_id, u.started_at DESC"
            ).format(self._t("import_source_results"), self._t("import_runs"))
        )
        names = [d.name for d in cur.description or []]
        return {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}
