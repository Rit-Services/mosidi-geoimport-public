"""Bieterantworten 74/75: eigenständiger Betrieb ohne MOSIDI-Funktionen.

Die Pipeline schreibt die Metadaten selbst in ``table_metadata`` (INKAR gekennzeichnet über
``dct_catalog_publisher``) und aktualisiert nach dem Import die materialisierte Sicht ``source_granularities``.
Die Tabellen hier sind ein Testmodell; die echten Spalten folgen aus dem Schema-Dump der TH.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from geoimport.config.loader import load_config
from geoimport.orchestrator import RunOptions, run_pipeline
from geoimport.target.introspect import schema_snapshot
from geoimport.target.table_mapping import TableMappingAdapter
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID, CSV_ID = "geobasis_bb_gemeinden", "inkar_gemeinden"

DDL = """
CREATE SCHEMA th;
CREATE TABLE th.table_metadata (id serial PRIMARY KEY, kennung text UNIQUE NOT NULL, titel text,
                                dct_catalog_publisher text);
CREATE TABLE th.geometrien (id bigserial PRIMARY KEY, metadaten_id int NOT NULL REFERENCES th.table_metadata,
                            schluessel text NOT NULL, geom geometry(Geometry, 4326) NOT NULL);
CREATE TABLE th.attribute (id bigserial PRIMARY KEY, metadaten_id int NOT NULL REFERENCES th.table_metadata,
                           geometrie_id bigint REFERENCES th.geometrien, schluessel text NOT NULL, name text NOT NULL,
                           wert text, zeitbezug text, zusatz jsonb);
CREATE MATERIALIZED VIEW th.source_granularities AS
  SELECT m.kennung, m.dct_catalog_publisher, count(a.id) AS werte
  FROM th.table_metadata m LEFT JOIN th.attribute a ON a.metadaten_id = m.id GROUP BY 1, 2;
CREATE UNIQUE INDEX ON th.source_granularities (kennung);
"""


@pytest.fixture
def th_config(config_dir: Path, db: psycopg.Connection[Any]) -> Path:
    db.execute(DDL)
    write_yaml(
        config_dir / "target.yaml",
        {
            "adapter": "table_mapping",
            "schema": "th",
            "tables": {
                "metadata": {
                    "name": "table_metadata",
                    "pk": "id",
                    "dataset_column": "kennung",
                    "columns": {"titel": "{source.title}", "dct_catalog_publisher": "{meta.publisher}"},
                },
                "geometries": {
                    "name": "geometrien",
                    "pk": "id",
                    "fk_metadata": "metadaten_id",
                    "object_key_column": "schluessel",
                    "geometry_column": "geom",
                },
                "attributes": {
                    "name": "attribute",
                    "fk_metadata": "metadaten_id",
                    "fk_geometry": "geometrie_id",
                    "object_key_column": "schluessel",
                    "layout": "key_value",
                    "name_column": "name",
                    "value_column": "wert",
                    "period_column": "zeitbezug",
                    "columns": {"zusatz": "{record.extra}"},
                },
            },
            "after_import": {"refresh_materialized_views": ["source_granularities"], "concurrently": True},
        },
    )
    return config_dir


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def view(db: psycopg.Connection[Any]) -> dict[str, tuple[Any, int]]:
    rows = db.execute("SELECT kennung, dct_catalog_publisher, werte FROM th.source_granularities").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_metadata_publisher_and_view_refreshed(
    th_config: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    before = schema_snapshot(db, "th")
    assert view(db) == {}
    out = run(th_config)
    assert out.exit_code == 0, out.report
    assert out.report["run"]["refreshed_views"] == ["th.source_granularities"]
    assert view(db) == {WFS_ID: ("LGB", 36), CSV_ID: ("BBSR", 49)}  # INKAR über dct_catalog_publisher erkennbar
    assert schema_snapshot(db, "th") == before  # REFRESH ändert keine Struktur

    # unveränderte Quellen: nichts geschrieben, kein REFRESH
    again = run(th_config)
    assert [s["status"] for s in again.report["sources"]] == ["unchanged", "unchanged"]
    assert "refreshed_views" not in again.report["run"]
    # Probelauf: kein REFRESH
    assert "refreshed_views" not in run(th_config, dry_run=True, full=True).report["run"]


def test_refresh_failure_is_warning_after_commit(
    th_config: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: TableMappingAdapter, conn: psycopg.Connection[Any]) -> list[str]:
        raise psycopg.errors.LockNotAvailable("Sperre nicht erhalten")

    monkeypatch.setattr(TableMappingAdapter, "refresh_views", broken)
    out = run(th_config, source_ids=[WFS_ID])
    s = out.report["sources"][0]
    assert s["status"] == "ok" and out.exit_code == 0  # Daten sind geschrieben, Lauf nicht herabgestuft
    assert any(w["code"] == "refresh_failed" and "LockNotAvailable" in w["message"] for w in out.report["warnings"])
    assert db.execute("SELECT count(*) FROM th.geometrien").fetchone() == (6,)


def test_missing_view_is_target_error(th_config: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    target = load_yaml(th_config / "target.yaml")
    target["after_import"]["refresh_materialized_views"] = ["gibt_es_nicht"]
    write_yaml(th_config / "target.yaml", target)
    issues = TableMappingAdapter(load_config(th_config).target).check(db)
    assert any("th.gibt_es_nicht" in i.message and i.level == "error" for i in issues)
    out = run(th_config, source_ids=[WFS_ID])
    assert out.exit_code == 3
    assert db.execute("SELECT count(*) FROM th.geometrien").fetchone() == (0,)
