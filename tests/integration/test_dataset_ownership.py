"""Besitz der Datensätze (CODE_REVIEW H7): Die Pipeline übernimmt keine fremde Metadatenzeile."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from geoimport.cli import app
from geoimport.config.loader import load_config
from geoimport.orchestrator import RunOptions, run_pipeline
from geoimport.target.adapter import DatasetContext, OwnershipError
from geoimport.target.table_mapping import TableMappingAdapter
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID = "geobasis_bb_gemeinden"  # target.dataset ist gleich der Quell-ID
OWNED = "SELECT dataset, source_id, target_schema, metadata_table, metadata_pk, created_run_id::text, adopted, adopted_run_id::text, backfilled FROM pipeline_meta.owned_datasets"


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def src_report(outcome: Any, sid: str = WFS_ID) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == sid)


def owned(db: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return db.execute(OWNED + " ORDER BY dataset").fetchall()


@pytest.fixture
def foreign_row(db: psycopg.Connection[Any]) -> int:
    """Metadatenzeile der MOSIDI-Anwendung mit demselben Datensatznamen wie die WFS-Quelle."""
    row = db.execute(
        "INSERT INTO ref.metadaten (datensatz, titel) VALUES (%s, 'Pflege durch MOSIDI') RETURNING id", (WFS_ID,)
    ).fetchone()
    db.execute(
        "INSERT INTO ref.geometrien (metadaten_id, objekt_schluessel, geom) "
        "VALUES (%s, 'mosidi-1', ST_SetSRID(ST_MakePoint(13, 52), 4326))",
        (row[0],),
    )
    return int(row[0])


def foreign_untouched(db: psycopg.Connection[Any], pk: int) -> bool:
    title = db.execute("SELECT titel FROM ref.metadaten WHERE id = %s", (pk,)).fetchone()
    geoms = db.execute("SELECT array_agg(objekt_schluessel) FROM ref.geometrien WHERE metadaten_id = %s", (pk,))
    return title == ("Pflege durch MOSIDI",) and geoms.fetchone() == (["mosidi-1"],)


def test_new_dataset_is_claimed(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == 0, out.report
    pk = db.execute("SELECT id::text FROM ref.metadaten WHERE datensatz = %s", (WFS_ID,)).fetchone()[0]
    assert owned(db) == [(WFS_ID, WFS_ID, "ref", "metadaten", pk, out.report["run"]["run_id"], False, None, False)]
    again = run(config_dir, source_ids=[WFS_ID], full=True)
    assert src_report(again)["status"] == "ok"
    assert owned(db)[0][5] == out.report["run"]["run_id"]  # Besitz bleibt beim ersten Lauf


def test_foreign_metadata_row_refused(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], foreign_row: int
) -> None:
    out = run(config_dir, source_ids=[WFS_ID])
    s = src_report(out)
    assert s["status"] == "failed" and s["data_unchanged"] and out.exit_code == 2
    assert s["error"]["kind"] == "ownership"
    assert "nicht von der Pipeline angelegt" in s["error"]["message"] and "--adopt" in s["error"]["message"]
    assert foreign_untouched(db, foreign_row)
    assert owned(db) == []


def test_adopt_opt_in(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], foreign_row: int) -> None:
    out = run(config_dir, source_ids=[WFS_ID], adopt={WFS_ID})
    s = src_report(out)
    assert s["status"] == "ok", out.report
    assert any(w["code"] == "dataset_adopted" for w in s["warnings"])
    run_id = out.report["run"]["run_id"]
    assert owned(db) == [(WFS_ID, WFS_ID, "ref", "metadaten", str(foreign_row), None, True, run_id, False)]
    # Geometrien werden nie gelöscht (Bieterantwort 74): die übernommene Geometrie bleibt, Hinweis im Bericht
    assert db.execute("SELECT count(*) FROM ref.geometrien WHERE metadaten_id = %s", (foreign_row,)).fetchone() == (7,)
    retained = [w for w in s["warnings"] if w["code"] == "geometry_retained"]
    assert retained and retained[0]["data"] == {"count": 1, "examples": ["mosidi-1"]}
    # danach gehört der Datensatz der Pipeline – ohne erneutes --adopt
    assert src_report(run(config_dir, source_ids=[WFS_ID], full=True))["status"] == "ok"


def test_adopt_via_cli(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], foreign_row: int) -> None:
    args = ["--log-format", "text", "run", "--source", WFS_ID, "--adopt", WFS_ID, "--config-dir", str(config_dir)]
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    assert owned(db)[0][6] is True
    bad = CliRunner().invoke(app, [*args[:-2], "--adopt", "gibt_es_nicht", "--config-dir", str(config_dir)])
    assert bad.exit_code == 3


def test_replaced_metadata_row_refused(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    """Die eigene Zeile wurde gelöscht und von MOSIDI unter demselben Namen neu angelegt."""
    run(config_dir, source_ids=[WFS_ID])
    for table in ("attribute", "geometrien"):
        db.execute(
            f"DELETE FROM ref.{table} WHERE metadaten_id IN (SELECT id FROM ref.metadaten WHERE datensatz = %s)",
            (WFS_ID,),
        )
    db.execute("DELETE FROM ref.metadaten WHERE datensatz = %s", (WFS_ID,))
    db.execute("INSERT INTO ref.metadaten (datensatz, titel) VALUES (%s, 'Pflege durch MOSIDI')", (WFS_ID,))
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    assert src_report(out)["error"]["kind"] == "ownership"


def test_legacy_rows_backfilled(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    """Bestand aus der Zeit vor der Besitzerfassung (Demonstrator): erfolgreicher Lauf in der Historie."""
    first = run(config_dir, source_ids=[WFS_ID])
    first_id = first.report["run"]["run_id"]
    pk = db.execute("SELECT id::text FROM ref.metadaten WHERE datensatz = %s", (WFS_ID,)).fetchone()[0]
    db.execute("DELETE FROM pipeline_meta.owned_datasets")
    # Migration 2 lief hier im ersten Lauf (Testfixture); im Betrieb läuft init-meta vorher
    db.execute("UPDATE pipeline_meta.schema_version SET applied_at = now() - interval '1 day' WHERE version = 2")
    # ohne Besitzeintrag, aber die Historie stammt aus der Zeit NACH Migration 2 → verdächtig, abgelehnt
    assert src_report(run(config_dir, source_ids=[WFS_ID], full=True))["error"]["kind"] == "ownership"
    # Probelauf vermerkt nichts
    db.execute("UPDATE pipeline_meta.schema_version SET applied_at = now() WHERE version = 2")
    dry = run(config_dir, source_ids=[WFS_ID], full=True, dry_run=True)
    assert src_report(dry)["status"] == "dry_run" and owned(db) == []
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out)
    assert s["status"] == "ok", out.report
    assert [w["code"] for w in s["warnings"]].count("ownership_backfilled") == 1
    assert owned(db) == [(WFS_ID, WFS_ID, "ref", "metadaten", pk, first_id, False, None, True)]


def test_dry_run_writes_no_claim(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], foreign_row: int
) -> None:
    refused = run(config_dir, source_ids=[WFS_ID], dry_run=True)
    assert src_report(refused)["error"]["kind"] == "ownership"  # Probelauf zeigt die Ablehnung
    out = run(config_dir, source_ids=[WFS_ID], dry_run=True, adopt={WFS_ID})
    assert src_report(out)["status"] == "dry_run", out.report
    assert owned(db) == [] and foreign_untouched(db, foreign_row)


def test_claim_in_separate_meta_database(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], db_env: str
) -> None:
    db.execute("DROP DATABASE IF EXISTS geoimport_meta_test")
    db.execute("CREATE DATABASE geoimport_meta_test")
    p = load_yaml(config_dir / "pipeline.yaml")
    p["meta"].update({"location": "database", "database": "geoimport_meta_test"})
    write_yaml(config_dir / "pipeline.yaml", p)
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == 0, out.report
    with psycopg.connect(db_env.rsplit("/", 1)[0] + "/geoimport_meta_test") as m:
        assert m.execute("SELECT dataset, adopted FROM pipeline_meta.owned_datasets").fetchall() == [(WFS_ID, False)]


def test_replace_rechecks_metadata_row(config_dir: Path, db: psycopg.Connection[Any], db_env: str) -> None:
    """Zwischen Besitzprüfung und Schreiben angelegte Zeile (z. B. durch MOSIDI) wird nicht übernommen."""
    cfg = load_config(config_dir)
    adapter = TableMappingAdapter(cfg.target)
    ctx = DatasetContext(
        WFS_ID, cfg.source(WFS_ID), None, "test", datetime.now(UTC), expected_metadata_id=None, check_metadata_id=True
    )
    db.execute("INSERT INTO ref.metadaten (datensatz, titel) VALUES (%s, 'Pflege durch MOSIDI')", (WFS_ID,))
    with psycopg.connect(db_env) as conn:
        with pytest.raises(OwnershipError):
            adapter.replace(conn, ctx, [], None)
        conn.rollback()
    assert db.execute("SELECT titel FROM ref.metadaten WHERE datensatz = %s", (WFS_ID,)).fetchone() == (
        "Pflege durch MOSIDI",
    )
