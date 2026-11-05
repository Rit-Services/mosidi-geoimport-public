"""pipeline_meta: Versionierte Migrationen nur über init-meta, ein Lauf führt kein DDL aus (CODE_REVIEW H8)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from typer.testing import CliRunner

from geoimport.cli import app
from geoimport.config.loader import load_config
from geoimport.db import connect
from geoimport.exitcodes import ExitCode
from geoimport.meta.repository import MetaRepository, expected_version
from geoimport.orchestrator import RunOptions, run_pipeline
from tests.conftest import FakeWfs

pytestmark = pytest.mark.integration
AUTO_INIT_META = False  # siehe tests/integration/conftest.py

WFS_ID = "geobasis_bb_gemeinden"
runner = CliRunner()

# Stand von schema.sql bis 0.10 (ohne Versionstabelle, ohne Spalte dry_run)
LEGACY_DDL = """
CREATE SCHEMA pipeline_meta;
CREATE TABLE pipeline_meta.import_runs (run_id uuid PRIMARY KEY, started_at timestamptz NOT NULL,
  finished_at timestamptz, trigger text NOT NULL, status text NOT NULL, simulated boolean NOT NULL DEFAULT false,
  app_version text, config_hash text, report jsonb);
CREATE TABLE pipeline_meta.import_source_results (run_id uuid REFERENCES pipeline_meta.import_runs ON DELETE CASCADE,
  source_id text, source_type text, dataset text, status text, started_at timestamptz, finished_at timestamptz,
  fetched int, valid int, invalid int, repaired int, added int, changed int, deleted int, unchanged int,
  rows_deleted bigint, rows_inserted bigint, error_kind text, error_message text, warnings jsonb,
  raw_file text, raw_sha256 text, PRIMARY KEY (run_id, source_id));
"""


def cli(config_dir: Path, *args: str) -> Any:
    return runner.invoke(app, ["--log-format", "text", *args, "--config-dir", str(config_dir)])


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def test_run_without_init_meta_fails_clearly(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == ExitCode.META_SCHEMA and out.report["run"]["status"] == "failed"
    err = out.report["sources"][0]["error"]
    assert err["kind"] == "meta_schema" and "init-meta" in err["message"]
    assert out.json_path is not None and out.json_path.exists()  # Bericht trotzdem als Datei
    assert db.execute("SELECT to_regnamespace('pipeline_meta')").fetchone()[0] is None  # nichts angelegt
    assert db.execute("SELECT count(*) FROM ref.metadaten").fetchone()[0] == 0
    r = cli(config_dir, "run", "--source", WFS_ID, "--trigger", "test")
    assert r.exit_code == ExitCode.META_SCHEMA, r.output


def test_init_meta_then_run_and_idempotent(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    r = cli(config_dir, "init-meta")
    assert r.exit_code == 0 and f"Version {expected_version()}" in r.output, r.output
    assert db.execute("SELECT max(version) FROM pipeline_meta.schema_version").fetchone()[0] == expected_version()
    r = cli(config_dir, "init-meta")
    assert r.exit_code == 0 and "keine Änderung nötig" in r.output, r.output
    assert run(config_dir, source_ids=[WFS_ID]).exit_code == 0


def test_legacy_installation_upgraded_by_init_meta(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    db.execute(LEGACY_DDL)
    old = str(uuid.uuid4())
    db.execute(
        "INSERT INTO pipeline_meta.import_runs (run_id, started_at, trigger, status) VALUES (%s, now(), 'manual', 'ok')",
        (old,),
    )
    assert MetaRepository(db, "pipeline_meta").schema_version() == 0
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == ExitCode.META_SCHEMA and "Version 0" in out.report["sources"][0]["error"]["message"]
    assert cli(config_dir, "init-meta").exit_code == 0
    assert db.execute("SELECT count(*) FROM pipeline_meta.import_runs WHERE run_id=%s", (old,)).fetchone()[0] == 1
    assert db.execute("SELECT to_regclass('pipeline_meta.diff_keys')").fetchone()[0] is not None
    out = run(config_dir, source_ids=[WFS_ID], dry_run=True)  # Spalte dry_run nachgerüstet
    assert out.exit_code == 0, out.report


def test_newer_schema_refused(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    assert cli(config_dir, "init-meta").exit_code == 0
    db.execute(
        "INSERT INTO pipeline_meta.schema_version (version, name) VALUES (%s, 'zukunft')", (expected_version() + 1,)
    )
    out = run(config_dir, source_ids=[WFS_ID])
    assert (
        out.exit_code == ExitCode.META_SCHEMA and "Image aktualisieren" in out.report["sources"][0]["error"]["message"]
    )


def test_run_works_with_least_privilege_role(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schreibrolle ohne DDL-Rechte (Betriebshandbuch §4): Lauf und init-meta ohne ausstehende Migration gehen."""
    assert cli(config_dir, "init-meta").exit_code == 0
    role = "gi_core_test_writer"
    dbname = db.execute("SELECT current_database()").fetchone()[0]
    ident = sql.Identifier(role)
    if db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
        db.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(ident))
    db.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident))
    db.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'writer'").format(ident))
    try:
        db.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(sql.Identifier(dbname), ident))
        for schema in ("ref", "pipeline_meta"):
            s = sql.Identifier(schema)
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, ident))
            db.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(s, ident)
            )
            db.execute(sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(s, ident))
        monkeypatch.setenv("POSTGRES_USER", role)
        monkeypatch.setenv("POSTGRES_PASSWORD", "writer")
        out = run(config_dir, source_ids=[WFS_ID])
        assert out.exit_code == 0, out.report
        with connect(settings=load_config(config_dir).pipeline.db) as conn:  # init-meta ohne Ausstehendes: kein DDL
            assert MetaRepository(conn, "pipeline_meta").migrate() == []
    finally:
        db.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(ident))
        db.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident))
