"""Fehler bleiben sichtbar: Abschluss in pipeline_meta, Bericht und Webhook auch bei Abbruch (CODE_REVIEW H9)."""

from __future__ import annotations

import os
import signal
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest_httpserver import HTTPServer
from typer.testing import CliRunner

import geoimport.orchestrator as orch_mod
from geoimport.cli import app
from geoimport.config.loader import load_config
from geoimport.db import connect
from geoimport.exitcodes import ExitCode
from geoimport.http import HttpClient
from geoimport.meta.repository import MetaRepository
from geoimport.orchestrator import RunLocked, RunOptions, run_pipeline
from geoimport.sources import source_class
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID, CSV_ID = "geobasis_bb_gemeinden", "inkar_gemeinden"
LOCK_SQL = "SELECT pg_advisory_lock(hashtext('geoimport:run-lock'), 0)"


def run(config_dir: Path, trigger: str = "test", **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger=trigger, **opts))


def run_row(db: psycopg.Connection[Any]) -> tuple[str, Any]:
    row = db.execute("SELECT status, report FROM pipeline_meta.import_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert row is not None
    return row[0], row[1]


def latest(config_dir: Path) -> Path:
    return config_dir.parent / "data" / "reports" / "latest.json"


def set_pipeline(config_dir: Path, section: str, **values: Any) -> None:
    p = load_yaml(config_dir / "pipeline.yaml")
    p[section].update(values)
    write_yaml(config_dir / "pipeline.yaml", p)


@pytest.fixture
def webhook(config_dir: Path, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[Any]]:
    """Webhook auf den Testserver; liefert eine Funktion, die die empfangenen JSON-Nutzlasten liefert."""
    httpserver.expect_request("/hook", method="POST").respond_with_data("ok")
    set_pipeline(config_dir, "report", notify_webhook_env="GI_TEST_WEBHOOK")
    monkeypatch.setenv("GI_TEST_WEBHOOK", httpserver.url_for("/hook"))
    return lambda: [r.get_json() for r, _ in httpserver.log if r.path == "/hook"]


def test_plugin_constructor_error_is_isolated(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = source_class

    def broken(kind: str) -> Any:
        if kind != "wfs":
            return real(kind)

        class Broken(real(kind)):  # type: ignore[misc]
            def __init__(self, *a: Any, **k: Any) -> None:
                raise ValueError("Plugin kaputt")

        return Broken

    monkeypatch.setattr(orch_mod, "source_class", broken)
    out = run(config_dir)
    w = next(s for s in out.report["sources"] if s["id"] == WFS_ID)
    assert w["status"] == "failed" and "Plugin kaputt" in w["error"]["message"]
    assert next(s for s in out.report["sources"] if s["id"] == CSV_ID)["status"] == "ok"
    assert out.exit_code == ExitCode.PARTIAL and run_row(db)[0] == "partial"


def test_unexpected_error_still_finishes_run(
    config_dir: Path,
    fake_wfs: FakeWfs,
    db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    webhook: Callable[[], list[Any]],
) -> None:
    def boom(self: Any) -> None:
        raise RuntimeError("unerwartet")

    monkeypatch.setattr(HttpClient, "close", boom)
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == ExitCode.INTERNAL and out.report["run"]["status"] == "failed"
    assert any(w["code"] == "run_aborted" and "unerwartet" in w["message"] for w in out.report["warnings"])
    assert run_row(db)[0] == "failed" and latest(config_dir).exists()
    assert [h["run"]["status"] for h in webhook()] == ["failed"]


def test_keyboard_interrupt_recorded_then_reraised(
    config_dir: Path,
    fake_wfs: FakeWfs,
    inkar_zip: Path,
    db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    webhook: Callable[[], list[Any]],
) -> None:
    def interrupted(self: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(source_class("wfs"), "fetch", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(config_dir)
    status, report = run_row(db)
    assert status == "failed" and report["run"]["status"] == "failed"
    srcs = {s["id"]: s for s in report["sources"]}
    assert srcs[WFS_ID]["error"]["kind"] == "interrupted" and srcs[CSV_ID]["error"]["kind"] == "run_aborted"
    res = db.execute("SELECT status, error_kind FROM pipeline_meta.import_source_results").fetchall()
    assert res == [("failed", "interrupted")]
    assert latest(config_dir).exists() and [h["run"]["status"] for h in webhook()] == ["failed"]


def test_sigterm_exit_code_143(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def terminated(self: Any) -> Any:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM nicht zugestellt")

    monkeypatch.setattr(source_class("wfs"), "fetch", terminated)
    r = CliRunner().invoke(
        app, ["--log-format", "text", "run", "--source", WFS_ID, "--trigger", "test", "--config-dir", str(config_dir)]
    )
    assert r.exit_code == ExitCode.TERMINATED, r.output
    assert run_row(db)[0] == "failed"
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # Handler wiederhergestellt


def test_finish_run_retried_on_fresh_connection(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = MetaRepository.finish_run
    calls: list[int] = []

    def drop_connection_first(self: MetaRepository, *a: Any, **k: Any) -> None:
        calls.append(self.conn.info.backend_pid)
        if len(calls) == 1:  # Verbindung bricht weg, bevor der Lauf abgeschlossen ist
            db.execute("SELECT pg_terminate_backend(%s)", (self.conn.info.backend_pid,))
        original(self, *a, **k)

    monkeypatch.setattr(MetaRepository, "finish_run", drop_connection_first)
    out = run(config_dir, source_ids=[WFS_ID])
    assert len(calls) == 2 and calls[0] != calls[1]
    assert out.exit_code == 0 and run_row(db)[0] == "ok" and out.json_path is not None


def test_finish_run_with_nul_in_report(config_dir: Path, db: psycopg.Connection[Any]) -> None:
    repo = MetaRepository(db, "pipeline_meta")
    repo.migrate()
    run_id = str(uuid.uuid4())
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    repo.start_run(run_id, now, "test", False, False, "x", "y")
    repo.finish_run(run_id, now, "failed", {"msg": "Server sagt \x00\x1b[2J", "k\x00": ["\x00"]})
    repo.write_source_result(run_id, {"source_id": "s\x00", "status": "failed", "error_message": "a\x00b"})
    assert run_row(db) == ("failed", {"msg": "Server sagt \x1b[2J", "k": [""]})
    assert db.execute("SELECT source_id, error_message FROM pipeline_meta.import_source_results").fetchone() == (
        "s",
        "ab",
    )


def test_stale_running_rows_marked_aborted(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    MetaRepository(db, "pipeline_meta").migrate()
    stale = str(uuid.uuid4())
    db.execute(
        "INSERT INTO pipeline_meta.import_runs (run_id, started_at, trigger, status) "
        "VALUES (%s, now() - interval '1 day', 'schedule', 'running')",
        (stale,),
    )
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == 0 and any(w["code"] == "stale_runs" for w in out.report["warnings"])
    row = db.execute("SELECT status, finished_at FROM pipeline_meta.import_runs WHERE run_id=%s", (stale,)).fetchone()
    assert row[0] == "aborted" and row[1] is not None


def test_database_unavailable_exit_5(
    config_dir: Path, fake_wfs: FakeWfs, db_env: str, monkeypatch: pytest.MonkeyPatch, webhook: Callable[[], list[Any]]
) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "1")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT", "2")
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == ExitCode.DB_UNAVAILABLE and out.report["sources"][0]["error"]["kind"] == "database"
    assert latest(config_dir).exists() and [h["run"]["status"] for h in webhook()] == ["failed"]
    r = CliRunner().invoke(app, ["check-target", "--quick", "--config-dir", str(config_dir)])
    assert r.exit_code == ExitCode.DB_UNAVAILABLE, r.output


def test_invalid_webhook_url_does_not_break_run(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    set_pipeline(config_dir, "report", notify_webhook_env="GI_TEST_WEBHOOK")
    monkeypatch.setenv("GI_TEST_WEBHOOK", "http://[kaputt")  # httpx.InvalidURL ist kein httpx.HTTPError
    fake_wfs.fail_status = 500
    out = run(config_dir, source_ids=[WFS_ID])
    assert out.exit_code == ExitCode.ALL_FAILED and run_row(db)[0] == "aborted_incomplete"


def test_duplicate_source_option(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    out = run(config_dir, source_ids=[WFS_ID, WFS_ID])
    assert out.exit_code == 0 and [s["id"] for s in out.report["sources"]] == [WFS_ID]


def test_scheduled_run_waits_for_lock(
    config_dir: Path, fake_wfs: FakeWfs, db_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch_mod, "LOCK_POLL_S", 0.1)
    with psycopg.connect(db_env, autocommit=True) as other:
        other.execute(LOCK_SQL)
        with pytest.raises(RunLocked):  # manueller Lauf wartet nicht
            run(config_dir, source_ids=[WFS_ID])
        timer = threading.Timer(0.5, lambda: other.execute("SELECT pg_advisory_unlock_all()"))
        timer.start()
        try:
            out = run(config_dir, trigger="schedule", source_ids=[WFS_ID])
        finally:
            timer.join()
    assert out.exit_code == 0


def test_scheduled_run_gives_up_and_notifies(
    config_dir: Path, fake_wfs: FakeWfs, db_env: str, monkeypatch: pytest.MonkeyPatch, webhook: Callable[[], list[Any]]
) -> None:
    monkeypatch.setattr(orch_mod, "LOCK_POLL_S", 0.1)
    set_pipeline(config_dir, "run", lock_wait_s=1)
    with psycopg.connect(db_env, autocommit=True) as other:
        other.execute(LOCK_SQL)
        with pytest.raises(RunLocked, match="1 s gewartet"):
            run(config_dir, trigger="schedule", source_ids=[WFS_ID])
    assert [h["run"]["status"] for h in webhook()] == ["locked"]


def test_connections_have_timeouts(config_dir: Path, db_env: str) -> None:
    cfg = load_config(config_dir)
    with connect(settings=cfg.pipeline.db) as conn:
        assert conn.execute("SHOW lock_timeout").fetchone()[0] == "1min"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "30min"
    set_pipeline(config_dir, "db", lock_timeout_s=5)
    with connect(settings=load_config(config_dir).pipeline.db) as conn:
        assert conn.execute("SHOW lock_timeout").fetchone()[0] == "5s"
