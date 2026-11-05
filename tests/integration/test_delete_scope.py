"""Integrationstests zum Löschbereich ``delete_scope: sql`` (H6), zur Datensatz-Sperre und zum
Aufbau der Ladezeilen vor dem Löschen."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from geoimport.config.loader import load_config
from geoimport.orchestrator import RunOptions, run_pipeline
from geoimport.target.adapter import DatasetContext
from geoimport.target.introspect import schema_snapshot
from geoimport.target.table_mapping import TableMappingAdapter
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID, CSV_ID = "geobasis_bb_gemeinden", "inkar_gemeinden"

# Modell ohne FK auf die Metadaten und ohne source_column: Die Herkunft steht in einer Spalte,
# die per Vorlage befüllt wird – nur eigene DELETE-Anweisungen finden den Bestand eines Datensatzes.
TH_DDL = """
CREATE SCHEMA th;
CREATE TABLE th.datensaetze (nr integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                             kennung varchar(80) UNIQUE NOT NULL, bezeichnung text);
CREATE TABLE th.objekte (oid bigserial PRIMARY KEY, herkunft varchar(80) NOT NULL,
                         schluessel varchar(40) NOT NULL, bezeichnung text,
                         form geometry(MultiPolygon, 4326) NOT NULL);
CREATE TABLE th.merkmale (mid bigserial PRIMARY KEY, herkunft varchar(80) NOT NULL,
                          schluessel varchar(40), daten jsonb NOT NULL);
INSERT INTO th.datensaetze (kennung, bezeichnung) VALUES ('fremd', 'Fremddaten der MOSIDI-Anwendung');
INSERT INTO th.objekte (herkunft, schluessel, form)
SELECT 'fremd', 'f' || i, ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(13, 52), 4326), 0.01))
FROM generate_series(1, 2) i;
INSERT INTO th.merkmale (herkunft, schluessel, daten)
SELECT 'fremd', 'f1', jsonb_build_object('i', i) FROM generate_series(1, 3) i;
"""


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def src_report(outcome: Any, sid: str) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == sid)


def th_counts(db: psycopg.Connection[Any]) -> dict[str, tuple[int, int]]:
    rows = db.execute(
        """SELECT h, (SELECT count(*) FROM th.objekte WHERE herkunft = h),
                     (SELECT count(*) FROM th.merkmale WHERE herkunft = h)
           FROM (SELECT herkunft FROM th.objekte UNION SELECT herkunft FROM th.merkmale) x(h)"""
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@pytest.fixture
def sql_scope_config(config_dir: Path, db: psycopg.Connection[Any]) -> Path:
    """Zielmodell ``th`` mit Fremddaten und ``delete_scope: sql``."""
    db.execute(TH_DDL)
    write_yaml(
        config_dir / "target.yaml",
        {
            "adapter": "table_mapping",
            "schema": "th",
            "tables": {
                "metadata": {
                    "name": "datensaetze",
                    "pk": "nr",
                    "dataset_column": "kennung",
                    "columns": {"bezeichnung": "{source.title}"},
                },
                "geometries": {
                    "name": "objekte",
                    "pk": "oid",
                    "object_key_column": "schluessel",
                    "geometry_column": "form",
                    "columns": {"herkunft": "{dataset}", "bezeichnung": "{record.name}"},
                },
                "attributes": {
                    "name": "merkmale",
                    "object_key_column": "schluessel",
                    "layout": "jsonb",
                    "jsonb_column": "daten",
                    "columns": {"herkunft": "{dataset}"},
                },
            },
            "delete_scope": {
                "strategy": "sql",
                "sql": ["DELETE FROM th.merkmale WHERE herkunft = %(dataset)s;"],
                # Geometrien werden nie gelöscht, sondern über den Schlüssel abgeglichen (Bieterantwort 74)
                "geometry_scope": "herkunft = %(dataset)s -- Objekte des Datensatzes",
                "scope_sql": {
                    "features": "SELECT schluessel FROM th.objekte WHERE herkunft = %(dataset)s -- Objekte",
                    "values": "SELECT 1 FROM th.merkmale WHERE herkunft = %(dataset)s;",
                },
            },
        },
    )
    return config_dir


def test_sql_scope_deletes_only_own_rows(
    sql_scope_config: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    before = schema_snapshot(db, "th")
    out = run(sql_scope_config)
    assert out.exit_code == 0, out.report
    assert th_counts(db) == {"fremd": (2, 3), WFS_ID: (6, 6), CSV_ID: (0, 49)}

    # zweiter Lauf: Bestand wird über scope_sql gezählt (früher: AssertionError in _scope_geo)
    again = run(sql_scope_config, full=True)
    assert again.exit_code == 0, again.report
    w, c = src_report(again, WFS_ID), src_report(again, CSV_ID)
    assert (w["status"], w["info"]["existing_before"], w["added"], w["deleted"]) == ("ok", 6, 0, 0)
    assert (c["status"], c["info"]["existing_before"], c["added"]) == ("ok", 49, 0)
    # Attribute ohne FK/Quellspalte: Bestand nicht auf die Lieferung abbildbar → sichtbarer Rückfall (Antwort 62)
    fallback = [x for x in c["warnings"] if x["code"] == "diff_from_baseline"]
    assert fallback and "attributes braucht" in fallback[0]["message"]
    assert w["info"]["rows_deleted_by_table"] == {"sql_1": 6}
    assert w["info"]["rows_inserted_by_table"] == {"objekte": 0, "merkmale": 6}
    assert th_counts(db) == {"fremd": (2, 3), WFS_ID: (6, 6), CSV_ID: (0, 49)}
    assert db.execute("SELECT count(*) FROM th.datensaetze WHERE kennung = 'fremd'").fetchone() == (1,)
    assert schema_snapshot(db, "th") == before


def test_sql_scope_drop_guard_counts_deleted_scope(
    sql_scope_config: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    run(sql_scope_config, source_ids=[WFS_ID])
    gml = fake_wfs.features
    keep_end = gml.index(b"</wfs:member>") + len(b"</wfs:member>")
    fake_wfs.features = gml[:keep_end] + b"</wfs:FeatureCollection>"
    fake_wfs.hits = 1
    path = sql_scope_config / "sources" / f"{WFS_ID}.yaml"
    src = load_yaml(path)
    src["guards"]["min_features"] = 1
    write_yaml(path, src)
    out = run(sql_scope_config, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert s["status"] == "skipped_guard" and "Rückgang von 6 auf 1" in s["warnings"][-1]["message"]
    assert th_counts(db) == {"fremd": (2, 3), WFS_ID: (6, 6)}


def test_dataset_lock_separate_from_run_lock(config_dir: Path, db: psycopg.Connection[Any], db_env: str) -> None:
    """Ein Datensatz namens ``run`` darf nicht auf die Lauf-Sperre warten."""
    cfg = load_config(config_dir)
    adapter = TableMappingAdapter(cfg.target)
    ctx = DatasetContext("run", cfg.source(WFS_ID), None, "test", datetime.now(UTC))
    with psycopg.connect(db_env, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('geoimport:run'))")  # Lauf-Sperre (bisherige Form)
        other.execute("SELECT pg_advisory_lock(hashtext('geoimport:run-lock'), 0)")  # Lauf-Sperre (neue Form)
        with psycopg.connect(db_env) as conn:
            conn.execute("SET lock_timeout = '2s'")
            adapter.replace(conn, ctx, [], None)
            held = conn.execute(
                """SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()
                   AND objsubid = 2 AND classid = hashtext('geoimport:dataset')::oid AND objid = hashtext('run')::oid"""
            ).fetchone()
            assert held == (1,)
            preview = adapter.preview_sql(conn, ctx, "features")
            conn.rollback()
    assert "pg_advisory_xact_lock(hashtext('geoimport:dataset'), hashtext('run'))" in preview[0]


def test_rows_built_before_delete(
    config_dir: Path,
    fake_wfs: FakeWfs,
    inkar_zip: Path,
    db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Die Ladezeilen (bei INKAR ~1,3 Mio.) entstehen vor dem DELETE, nicht bei gehaltenen Zeilensperren."""
    events: list[str] = []
    orig_rows = TableMappingAdapter._attribute_rows
    orig_delete = TableMappingAdapter._delete

    def rows(self: TableMappingAdapter, *a: Any, **k: Any) -> Any:
        yield from orig_rows(self, *a, **k)
        events.append("rows")

    def delete(self: TableMappingAdapter, *a: Any, **k: Any) -> None:
        events.append("delete")
        orig_delete(self, *a, **k)

    monkeypatch.setattr(TableMappingAdapter, "_attribute_rows", rows)
    monkeypatch.setattr(TableMappingAdapter, "_delete", delete)
    out = run(config_dir)
    assert out.exit_code == 0, out.report
    # je Quelle: Zeilen für den Vergleich mit dem Bestand, Zeilen zum Schreiben, dann erst DELETE
    assert events == ["rows", "rows", "delete", "rows", "rows", "delete"]
    assert db.execute("SELECT count(*) FROM ref.attribute").fetchone() == (36 + 49,)
