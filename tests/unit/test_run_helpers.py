"""Bausteine des Laufs ohne Datenbank: Exit-Codes, Fingerabdruck, ungültige Schlüssel, Webhook, Migrationen."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest
from pytest_httpserver import HTTPServer

from geoimport.config.loader import load_config
from geoimport.db import session_conninfo
from geoimport.diff import compute_diff
from geoimport.exitcodes import ExitCode
from geoimport.meta.repository import CHANGE_INVALID, expected_version, migrations
from geoimport.orchestrator import Orchestrator, RunOptions, config_fingerprint, mark_invalid_keys
from geoimport.sources.base import RecordError, StateStore
from tests.conftest import load_yaml, write_yaml

WFS_ID = "geobasis_bb_gemeinden"


def test_exit_codes_keep_existing_values() -> None:
    assert [ExitCode.OK, ExitCode.PARTIAL, ExitCode.ALL_FAILED, ExitCode.CONFIG, ExitCode.LOCKED] == [0, 1, 2, 3, 4]
    assert len({int(c) for c in ExitCode}) == len(ExitCode)
    assert {ExitCode.DB_UNAVAILABLE, ExitCode.META_SCHEMA, ExitCode.INTERNAL} == {5, 6, 7}


def test_config_fingerprint(config_dir: Path) -> None:
    src = load_config(config_dir).source(WFS_ID)
    fp = config_fingerprint(src)
    assert fp == config_fingerprint(load_config(config_dir).source(WFS_ID))
    assert config_fingerprint(src.model_copy(update={"schedule": "weekly"})) == fp  # ohne Einfluss auf Daten
    assert config_fingerprint(src.model_copy(update={"title": "Anders"})) != fp


def test_mark_invalid_keys_not_counted_as_deleted() -> None:
    old = pd.DataFrame({"key": ["a", "b", "c"], "hash": ["1", "2", "3"]})
    new = pd.DataFrame({"key": ["a"], "hash": ["1"]})
    diff = compute_diff(new, old, True)
    assert diff.deleted == 2
    out, n = mark_invalid_keys(diff, [RecordError("b", "invalid_value", "kaputt"), RecordError(None, "x", "y")])
    assert n == 1 and out.deleted == 1 and out.counts()["deleted"] == 1
    assert out.keys.set_index("key").loc["b", "change"] == CHANGE_INVALID
    assert mark_invalid_keys(diff, [])[0] is diff


def test_state_forget(tmp_path: Path) -> None:
    st = StateStore(tmp_path, "s")
    st.update({"raw_sha256": "x", "etag": "e", "schema": {"a": 1}})
    st.forget(("raw_sha256", "etag", "last_modified"))
    data = st.load()
    assert "raw_sha256" not in data and "etag" not in data and data["schema"] == {"a": 1}


def test_select_sources_dedupes(config_dir: Path) -> None:
    orch = Orchestrator(load_config(config_dir), RunOptions(source_ids=[WFS_ID, WFS_ID]))
    assert [s.id for s in orch.select_sources()] == [WFS_ID]


def _orch_with_webhook(config_dir: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> Orchestrator:
    p = load_yaml(config_dir / "pipeline.yaml")
    p["report"]["notify_webhook_env"] = "GI_TEST_WEBHOOK"
    write_yaml(config_dir / "pipeline.yaml", p)
    monkeypatch.setenv("GI_TEST_WEBHOOK", url)
    return Orchestrator(load_config(config_dir), RunOptions())


def test_notify_never_raises(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    orch = _orch_with_webhook(config_dir, monkeypatch, "http://[kaputt")  # httpx.InvalidURL, kein HTTPError
    with caplog.at_level(logging.WARNING):
        orch._notify({"run": {"status": "failed"}, "summary": {}})
    assert "Webhook fehlgeschlagen" in caplog.text


def test_notify_logs_http_status(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/hook", method="POST").respond_with_data("nein", status=500)
    orch = _orch_with_webhook(config_dir, monkeypatch, httpserver.url_for("/hook"))
    with caplog.at_level(logging.WARNING):
        orch._notify({"run": {"status": "failed"}, "summary": {}})
    assert "HTTP 500" in caplog.text


def test_migrations_numbered_and_without_extension_ddl() -> None:
    ms = migrations()
    assert [m.version for m in ms] == list(range(1, expected_version() + 1))
    first = ms[0].sql_for("pipeline_meta").upper()
    assert "CREATE EXTENSION" not in first and "CREATE SCHEMA" not in first
    assert 'CREATE TABLE IF NOT EXISTS "PIPELINE_META".IMPORT_RUNS' in first


def test_session_conninfo_timeouts_and_keepalives() -> None:
    from geoimport.config.models import DatabaseSettings

    info = session_conninfo("host=h dbname=d", DatabaseSettings(lock_timeout_s=5, statement_timeout_s=0))
    assert "lock_timeout=5000" in info and "statement_timeout=0" in info and "keepalives=1" in info
    assert "lock_timeout=60000" in session_conninfo("host=h") and "statement_timeout=1800000" in session_conninfo(
        "host=h"
    )


def test_ressourcengrenzen_gelten_als_unvollstaendiger_abruf() -> None:
    """Zu große Antworten (HTTP-Grenze) und entpackte CSV-Grenzen ersetzen nie den Bestand: Teilabbruch."""
    from geoimport.orchestrator import INCOMPLETE_KINDS

    assert {"too_large", "incomplete", "timeout"} <= INCOMPLETE_KINDS
    assert not {"tls", "config", "insecure_redirect", "ownership"} & INCOMPLETE_KINDS
