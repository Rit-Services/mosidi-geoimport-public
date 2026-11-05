"""Bieterantwort 63: Geometrien reparieren statt die Quelle abzubrechen (Schutznetz in der Datenbank).

Die Geometrien werden schon in Python geprüft und mit ``make_valid`` repariert. PostGIS kann eine Geometrie
trotzdem anders bewerten (andere GEOS-Version). Dann gilt in der Datenbank: ``ST_MakeValid`` → übernommen
(``geometry_repaired``); nicht reparierbar oder leer → Datensatz übersprungen (``geometry_skipped``).
Gespeichert wird nur 2D, Z- und M-Werte werden entfernt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.wkt import loads

from geoimport.config.loader import load_config
from geoimport.orchestrator import RunOptions, run_pipeline
from geoimport.sources.base import FeatureRecord, ParseResult, PipelineWarning
from geoimport.target.adapter import DatasetContext
from geoimport.target.table_mapping import TableMappingAdapter
from tests.conftest import FakeWfs

pytestmark = pytest.mark.integration

WFS_ID = "geobasis_bb_gemeinden"
BOWTIE = Polygon([(13.0, 52.0), (13.01, 52.01), (13.01, 52.0), (13.0, 52.01)])  # Selbstüberschneidung
FLAT = Polygon([(13.0, 52.0), (13.01, 52.0), (13.02, 52.0)])  # Fläche 0 → nach ST_MakeValid eine Linie


def _feature(key: str, geom: BaseGeometry) -> FeatureRecord:
    return FeatureRecord(key, f"Objekt {key}", geom, {"ags": key}, "h")


def test_adapter_repairs_skips_and_forces_2d(config_dir: Path, db: psycopg.Connection[Any], db_env: str) -> None:
    cfg = load_config(config_dir)
    adapter = TableMappingAdapter(cfg.target)
    ctx = DatasetContext("reparatur", cfg.source(WFS_ID), None, "test", datetime.now(UTC))
    features = [
        _feature("ok3d", loads("POLYGON Z ((13 52 5, 13.01 52 5, 13.01 52.01 5, 13 52 5))")),
        _feature("bowtie", BOWTIE),
        _feature("flat", FLAT),
    ]
    with psycopg.connect(db_env, autocommit=True) as conn:
        with conn.transaction():
            stats = adapter.replace(conn, ctx, features, None)
        rows = dict(
            conn.execute(
                """SELECT g.objekt_schluessel, ST_GeometryType(g.geom) || ':' || ST_NDims(g.geom) || ':' || ST_IsValid(g.geom)
                   FROM ref.geometrien g JOIN ref.metadaten m ON m.id = g.metadaten_id WHERE m.datensatz = 'reparatur'"""
            ).fetchall()
        )
        attrs = {
            r[0]
            for r in conn.execute(
                "SELECT a.objekt_schluessel FROM ref.attribute a JOIN ref.metadaten m ON m.id = a.metadaten_id "
                "WHERE m.datensatz = 'reparatur'"
            ).fetchall()
        }
    assert rows == {"ok3d": "ST_Polygon:2:true", "bowtie": "ST_MultiPolygon:2:true"}
    assert (stats.db_repaired, stats.db_skipped, stats.db_skipped_keys) == (1, 1, ["flat"])
    assert attrs == {"ok3d", "bowtie"}  # Attribute des übersprungenen Objekts werden nicht geschrieben


def _inject(src: Any, parsed: ParseResult) -> list[PipelineWarning]:
    """Zwei Objekte an der Prüfung in Python vorbei verändern (wie bei abweichender GEOS-Version)."""
    if parsed.values is not None:
        return []
    feats = parsed.features
    feats[0].geometry = BOWTIE
    feats[1].geometry = LineString([(13.0, 52.0), (13.01, 52.0)]).buffer(0)  # leer
    return []


def test_pipeline_reports_db_repair_and_skip(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    out = run_pipeline(load_config(config_dir), RunOptions(trigger="test", source_ids=[WFS_ID], mutate=_inject))
    s = next(x for x in out.report["sources"] if x["id"] == WFS_ID)
    assert out.exit_code == 0, out.report
    assert s["status"] == "ok"
    assert s["quality"] == {"geometry_repaired": 1, "geometry_skipped": 1, "missing_required_field": 0}
    assert (s["valid"], s["invalid"], s["added"]) == (5, 1, 5)
    codes = {w["code"]: w for w in s["warnings"]}
    assert codes["geometry_skipped"]["data"]["count"] == 1 and codes["geometry_repaired"]["data"]["count"] == 1
    skipped = codes["geometry_skipped"]["data"]["examples"][0]
    assert db.execute("SELECT count(*) FROM ref.geometrien").fetchone() == (5,)
    assert db.execute(
        "SELECT reason_code FROM pipeline_meta.import_errors WHERE record_key = %s", (skipped,)
    ).fetchone() == ("invalid_geometry",)
    assert (
        db.execute("SELECT change FROM pipeline_meta.diff_keys WHERE record_key = %s", (skipped,)).fetchall() == []
    )  # nicht als neu protokolliert
