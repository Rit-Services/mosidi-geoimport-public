"""Integrationstests gegen PostGIS (TEST_DATABASE_URL) mit simuliertem WFS und INKAR-Fixture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import psycopg
import pytest

from geoimport.config.loader import load_config
from geoimport.orchestrator import RunLocked, RunOptions, run_pipeline
from geoimport.report.builder import load_schema
from geoimport.target.introspect import schema_snapshot
from geoimport.target.table_mapping import TableMappingAdapter
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID, CSV_ID = "geobasis_bb_gemeinden", "inkar_gemeinden"


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    return dict(
        conn.execute(
            """SELECT m.datensatz, (SELECT count(*) FROM ref.geometrien g WHERE g.metadaten_id = m.id)
                                 + (SELECT count(*) FROM ref.attribute a WHERE a.metadaten_id = m.id)
               FROM ref.metadaten m"""
        ).fetchall()
    )


def src_report(outcome: Any, sid: str) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == sid)


def test_full_run_wfs_and_csv(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    before = schema_snapshot(db, "ref")
    out = run(config_dir)
    assert out.exit_code == 0, out.report
    jsonschema.validate(out.report, load_schema())
    w, c = src_report(out, WFS_ID), src_report(out, CSV_ID)
    assert (w["status"], w["valid"], w["added"], w["changed"], w["deleted"]) == ("ok", 6, 6, 0, 0)
    assert (c["status"], c["valid"], c["added"]) == ("ok", 49, 49)
    assert c["invalid"] == 1 and c["error_examples"][0]["reason_code"] == "invalid_value"
    unmatched = [x for x in c["warnings"] if x["code"] == "unmatched_keys"]
    assert unmatched and unmatched[0]["data"]["examples"] == ["12999999"]  # Gemeinde ohne Geometrie

    assert db.execute(
        "SELECT count(*), count(*) FILTER (WHERE ST_SRID(geom) = 4326 AND ST_IsValid(geom)) FROM ref.geometrien"
    ).fetchone() == (6, 6)
    meta = db.execute(
        "SELECT titel, lizenz, quellenvermerk, quelltyp, importiert_am IS NOT NULL FROM ref.metadaten WHERE datensatz=%s",
        (WFS_ID,),
    ).fetchone()
    assert meta == (
        "Gemeinden Brandenburg (LGB, Verwaltungsgrenzen)",
        "dl-de/by-2-0",
        "© GeoBasis-DE/LGB, dl-de/by-2-0",
        "wfs",
        True,
    )
    # WFS-Attribute hängen an ihrer Geometrie; CSV-Kennzahlen über den Schlüssel (AGS)
    assert (
        db.execute("SELECT count(*) FROM ref.attribute a JOIN ref.geometrien g ON g.id = a.geometrie_id").fetchone()[0]
        == 36
    )
    row = db.execute(
        """SELECT a.wert, a.zeitbezug, a.zusatz->>'indicator_code', g.name
           FROM ref.attribute a JOIN ref.metadaten m ON m.id = a.metadaten_id AND m.datensatz = %s
           JOIN ref.geometrien g ON g.objekt_schluessel = a.objekt_schluessel
           WHERE a.objekt_schluessel = '12051000' AND a.name = 'Einwohnerdichte' AND a.zeitbezug = '2022'""",
        (CSV_ID,),
    ).fetchone()
    assert row == ("320.07", "2022", "bev_dichte", "Brandenburg an der Havel")

    # Lauf-Historie
    assert db.execute(
        "SELECT status, trigger, report->>'schema_version' FROM pipeline_meta.import_runs"
    ).fetchone() == ("ok", "test", "1.0")
    assert db.execute("SELECT count(*) FROM pipeline_meta.diff_keys WHERE change='added'").fetchone()[0] == 55
    assert db.execute("SELECT count(*) FROM pipeline_meta.diff_keys WHERE geom IS NOT NULL").fetchone()[0] == 6
    assert db.execute("SELECT count(*) FROM pipeline_meta.import_errors").fetchone()[0] == 1
    assert (
        out.json_path and out.json_path.exists() and out.md_path and "Import-Bericht" in out.md_path.read_text("utf-8")
    )
    # Struktur des Zielschemas unverändert (A6)
    assert schema_snapshot(db, "ref") == before


def test_second_run_idempotent(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    run(config_dir)
    first = counts(db)
    again = run(config_dir)  # Quellen unverändert → kein Ersetzen
    assert again.exit_code == 0
    assert [s["status"] for s in again.report["sources"]] == ["unchanged", "unchanged"]
    full = run(config_dir, full=True)  # trotzdem neu einlesen → 0/0/0
    for s in full.report["sources"]:
        assert (s["status"], s["added"], s["changed"], s["deleted"]) == ("ok", 0, 0, 0)
        assert s["unchanged"] == s["valid"]
    assert counts(db) == first


def test_changed_source_counts(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    run(config_dir, source_ids=[WFS_ID])
    gml = fake_wfs.features
    fake_wfs.features = gml.replace(
        b"<app:gem_art>Kreisfreie Stadt</app:gem_art>", b"<app:gem_art>Stadt (geaendert)</app:gem_art>"
    )
    # ein Objekt entfernen
    start = fake_wfs.features.rindex(b"<wfs:member>")
    end = fake_wfs.features.rindex(b"</wfs:member>") + len(b"</wfs:member>")
    fake_wfs.features = fake_wfs.features[:start] + fake_wfs.features[end:]
    fake_wfs.hits = 5
    out = run(config_dir, source_ids=[WFS_ID])
    s = src_report(out, WFS_ID)
    assert (s["added"], s["changed"], s["deleted"], s["unchanged"]) == (0, 1, 1, 4)
    # Geometrien werden nie gelöscht (Bieterantwort 74): das entfallene Gebiet bleibt, nur seine Attribute entfallen
    assert db.execute("SELECT count(*) FROM ref.geometrien").fetchone()[0] == 6
    assert any(w["code"] == "geometry_retained" for w in s["warnings"])
    deleted = db.execute(
        "SELECT record_key, geom IS NOT NULL FROM pipeline_meta.diff_keys WHERE change='deleted'"
    ).fetchall()
    assert len(deleted) == 1 and deleted[0][1]  # Geometrie des entfallenen Objekts für die Änderungskarte


def test_other_sources_untouched_and_fault_isolation(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    db.execute("INSERT INTO ref.metadaten (datensatz, titel) VALUES ('fremd', 'Fremddaten')")
    db.execute(
        "INSERT INTO ref.geometrien (metadaten_id, objekt_schluessel, geom) "
        "SELECT id, 'x', ST_SetSRID(ST_MakePoint(13, 52), 4326) FROM ref.metadaten WHERE datensatz='fremd'"
    )
    run(config_dir)
    before = counts(db)
    fake_wfs.fail_status = 503
    out = run(config_dir, full=True)
    assert out.exit_code == 1 and out.report["run"]["status"] == "aborted_incomplete"  # Teilabbruch (V5)
    w = src_report(out, WFS_ID)
    assert w["status"] == "aborted_incomplete" and w["error"]["http_status"] == 503 and w["data_unchanged"]
    assert src_report(out, CSV_ID)["status"] == "ok"
    assert counts(db) == before and before["fremd"] == 1
    md = out.md_path.read_text("utf-8")
    assert "Nicht erreichbare oder fehlgeschlagene Quellen" in md and "HTTP 503" in md


def test_all_failed_exit_2(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], httpserver: Any) -> None:
    httpserver.expect_request("/inkar/inkar_2025.zip").respond_with_data("", status=500)
    fake_wfs.fail_status = 500
    out = run(config_dir)
    assert out.exit_code == 2 and out.report["run"]["status"] == "aborted_incomplete"


def test_rollback_keeps_old_data(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(config_dir, source_ids=[WFS_ID])
    before = counts(db)

    def boom(*a: Any, **k: Any) -> int:
        raise psycopg.errors.CheckViolation("simulierter Fehler beim Einfügen")

    monkeypatch.setattr(TableMappingAdapter, "_insert_attributes", boom)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert s["status"] == "failed" and s["error"]["kind"] == "database" and "zurückgerollt" in s["error"]["message"]
    assert counts(db) == before  # DELETE + INSERT der Geometrien wurden zurückgerollt


def test_guard_min_features_and_force(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    run(config_dir, source_ids=[WFS_ID])
    path = config_dir / "sources" / f"{WFS_ID}.yaml"
    src = load_yaml(path)
    src["guards"]["min_features"] = 100
    write_yaml(path, src)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert s["status"] == "skipped_guard" and out.exit_code == 1 and s["data_unchanged"]
    assert any(w["code"] == "guard" and "Minimum 100" in w["message"] for w in s["warnings"])
    forced = run(config_dir, source_ids=[WFS_ID], full=True, force_replace={WFS_ID})
    assert src_report(forced, WFS_ID)["status"] == "ok"


def test_guard_drop_ratio(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    run(config_dir, source_ids=[WFS_ID])
    gml = fake_wfs.features
    first = gml.index(b"<wfs:member>")
    keep_end = gml.index(b"</wfs:member>") + len(b"</wfs:member>")
    fake_wfs.features = gml[:keep_end] + b"</wfs:FeatureCollection>"
    fake_wfs.hits = 1
    assert first < keep_end
    path = config_dir / "sources" / f"{WFS_ID}.yaml"
    src = load_yaml(path)
    src["guards"]["min_features"] = 1
    write_yaml(path, src)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert s["status"] == "skipped_guard" and "Rückgang von 6 auf 1" in s["warnings"][-1]["message"]
    assert db.execute("SELECT count(*) FROM ref.geometrien").fetchone()[0] == 6


def test_dry_run_writes_nothing(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    out = run(config_dir, dry_run=True)
    assert out.exit_code == 0 and out.report["run"]["dry_run"]
    for s in out.report["sources"]:
        assert s["status"] == "dry_run" and s["added"] == s["valid"] and s["preview_sql"]
    assert db.execute("SELECT count(*) FROM ref.metadaten").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM pipeline_meta.diff_keys").fetchone()[0] == 0


def test_run_lock(config_dir: Path, fake_wfs: FakeWfs, db_env: str) -> None:
    with psycopg.connect(db_env, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('geoimport:run-lock'), 0)")
        with pytest.raises(RunLocked):
            run(config_dir, source_ids=[WFS_ID])


def test_check_target_detects_problems(config_dir: Path, db: psycopg.Connection[Any]) -> None:
    cfg = load_config(config_dir)
    adapter = TableMappingAdapter(cfg.target)
    assert [i for i in adapter.check(db) if i.level == "error"] == []
    target = load_yaml(config_dir / "target.yaml")
    target["tables"]["attributes"]["value_column"] = "wert_gibt_es_nicht"
    write_yaml(config_dir / "target.yaml", target)
    issues = TableMappingAdapter(load_config(config_dir).target).check(db)
    assert any("wert_gibt_es_nicht" in i.message for i in issues)


def test_placeholder_target_aborts_with_exit_3(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    from tests.conftest import CONFIG

    write_yaml(config_dir / "target.yaml", load_yaml(CONFIG / "target.yaml"))
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == 3
    assert "Platzhalter" in out.report["sources"][0]["error"]["message"]
    assert db.execute("SELECT count(*) FROM ref.metadaten").fetchone()[0] == 0


def test_generic_adapter_other_model(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    """Ein anders aufgebautes Modell (Quellspalte statt FK, Attribute als jsonb) – nur per target.yaml."""
    db.execute("""
        CREATE SCHEMA th;
        CREATE TABLE th.datensaetze (nr integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kennung varchar(80) UNIQUE NOT NULL,
                                     bezeichnung text, stand timestamptz);
        CREATE TABLE th.objekte (oid bigserial PRIMARY KEY, quelle varchar(80) NOT NULL, schluessel varchar(40) NOT NULL,
                                 bezeichnung text, form geometry(MultiPolygon, 4326) NOT NULL);
        CREATE TABLE th.merkmale (mid bigserial PRIMARY KEY, quelle varchar(80) NOT NULL, objekt bigint REFERENCES th.objekte,
                                  schluessel varchar(40), daten jsonb NOT NULL);
    """)
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
                    "columns": {"bezeichnung": "{source.title}", "stand": "{run.started_at}"},
                },
                "geometries": {
                    "name": "objekte",
                    "pk": "oid",
                    "source_column": "quelle",
                    "object_key_column": "schluessel",
                    "geometry_column": "form",
                    "columns": {"bezeichnung": "{record.name}"},
                },
                "attributes": {
                    "name": "merkmale",
                    "source_column": "quelle",
                    "fk_geometry": "objekt",
                    "object_key_column": "schluessel",
                    "layout": "jsonb",
                    "jsonb_column": "daten",
                },
            },
            "delete_scope": {"strategy": "by_source_column"},
        },
    )
    before = schema_snapshot(db, "th")
    out = run(config_dir)
    assert out.exit_code == 0, out.report
    assert db.execute("SELECT count(*), min(ST_GeometryType(form)) FROM th.objekte").fetchone() == (
        6,
        "ST_MultiPolygon",
    )
    assert db.execute("SELECT count(*) FROM th.merkmale WHERE objekt IS NOT NULL").fetchone()[0] == 6
    assert (
        db.execute(
            "SELECT daten->>'gem_art' FROM th.merkmale m JOIN th.objekte o ON o.oid=m.objekt WHERE o.schluessel='12051000'"
        ).fetchone()[0]
        == "Kreisfreie Stadt"
    )
    assert (
        db.execute("SELECT count(*) FROM th.merkmale WHERE quelle=%s AND daten ? 'value'", (CSV_ID,)).fetchone()[0]
        == 49
    )
    out2 = run(config_dir, full=True)
    assert all(s["added"] == 0 and s["changed"] == 0 for s in out2.report["sources"])
    assert db.execute("SELECT count(*) FROM th.objekte").fetchone()[0] == 6
    assert schema_snapshot(db, "th") == before


def test_meta_in_separate_database(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], db_env: str
) -> None:
    db.execute("DROP DATABASE IF EXISTS geoimport_meta_test")
    db.execute("CREATE DATABASE geoimport_meta_test")
    p = load_yaml(config_dir / "pipeline.yaml")
    p["meta"].update({"location": "database", "database": "geoimport_meta_test"})
    write_yaml(config_dir / "pipeline.yaml", p)
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == 0
    assert db.execute("SELECT to_regclass('pipeline_meta.import_runs')").fetchone()[0] is None
    with psycopg.connect(db_env.rsplit("/", 1)[0] + "/geoimport_meta_test") as m:
        assert m.execute("SELECT count(*) FROM pipeline_meta.import_runs").fetchone()[0] == 1
