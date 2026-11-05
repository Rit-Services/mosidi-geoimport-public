"""HTTP-Wiederholversuche, Log-Maskierung, Bericht und Schema, Plugin-Registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import jsonschema
import pytest
from pydantic import BaseModel
from pytest_httpserver import HTTPServer

from geoimport.config.models import HttpSettings
from geoimport.http import HttpClient, HttpError
from geoimport.logging import mask_secrets
from geoimport.report.builder import RunReport, SourceReport, load_schema, write_report
from geoimport.report.render_md import render_markdown
from geoimport.sources import _REGISTRY, register_source, registered_kinds, source_class
from geoimport.sources.base import Source


def test_retry_then_success(httpserver: HTTPServer) -> None:
    httpserver.expect_ordered_request("/x").respond_with_data("busy", status=503)
    httpserver.expect_ordered_request("/x").respond_with_data("ok")
    c = HttpClient(HttpSettings(retries=2, backoff_base_s=0))
    assert c.get(httpserver.url_for("/x")).text == "ok"


def test_retry_exhausted(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/x").respond_with_data("busy", status=503)
    c = HttpClient(HttpSettings(retries=2, backoff_base_s=0))
    with pytest.raises(HttpError) as exc:
        c.get(httpserver.url_for("/x"))
    assert exc.value.status == 503 and exc.value.attempts == 3


def test_no_retry_on_404(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/x").respond_with_data("nein", status=404)
    c = HttpClient(HttpSettings(retries=3, backoff_base_s=0))
    with pytest.raises(HttpError) as exc:
        c.get(httpserver.url_for("/x"))
    assert exc.value.status == 404 and len(httpserver.log) == 1


def test_network_error() -> None:
    c = HttpClient(HttpSettings(retries=0, backoff_base_s=0, timeout_s=2))
    with pytest.raises(HttpError) as exc:
        c.get("http://127.0.0.1:9/")
    assert exc.value.kind in ("network", "timeout")


def test_mask_secrets() -> None:
    s = mask_secrets("GET https://user:geheim@host/wfs?apikey=abc123&x=1 token=zzz")
    assert "geheim" not in s and "abc123" not in s and "zzz" not in s and "x=1" in s


def _report(simulated: bool = False) -> dict:
    t0 = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
    r = RunReport(
        "2f1d3c4b-0000-4000-8000-000000000001",
        "schedule",
        t0,
        "0.9.0",
        "sha256:abc",
        "demo_reference",
        simulated=simulated,
    )
    ok = SourceReport(
        "geobasis_bb_gemeinden",
        "wfs",
        "Gemeinden",
        "geobasis_bb_gemeinden",
        "dl-de/by-2-0",
        "© GeoBasis-DE/LGB, dl-de/by-2-0",
        "ok",
        t0,
        t0 + timedelta(seconds=41),
        413,
        413,
        0,
        1,
        0,
        2,
        0,
        411,
        warnings=[{"code": "schema_drift", "message": "Feld 'x' neu"}],
    )
    bad = SourceReport(
        "inkar_gemeinden",
        "csv",
        "INKAR | Test",
        "inkar_gemeinden",
        status="failed",
        started_at=t0,
        finished_at=t0 + timedelta(seconds=5),
        error={"kind": "http", "message": "HTTP 503 nach 5 Versuchen", "http_status": 503, "url": "https://x"},
    )
    r.sources = [ok, bad]
    r.finished_at = t0 + timedelta(seconds=60)
    r.status = "partial"
    return r.as_dict()


def test_report_matches_schema_and_renders(tmp_path: Path) -> None:
    data = _report()
    jsonschema.validate(data, load_schema())
    assert data["run"]["started_at"] == "2026-11-20T03:00:00+01:00"  # Europe/Berlin mit Offset
    assert data["summary"]["ok"] == 1 and data["summary"]["failed"] == 1 and data["summary"]["changed"] == 2
    md = render_markdown(data)
    assert "teilweise erfolgreich" in md and "HTTP 503" in md and "INKAR / Test" in md and "SIMULATION" not in md
    j, m = write_report(data, tmp_path)
    assert j.parent.name == "2026" and (tmp_path / "latest.json").exists() and m.exists()


def test_simulated_report_is_marked() -> None:
    md = render_markdown(_report(simulated=True))
    assert md.startswith("# SIMULATION") and "keine echten Änderungen" in md


def test_registry() -> None:
    assert {"wfs", "csv"} <= set(registered_kinds())

    class DummyConfig(BaseModel):
        type: Literal["dummy_test"]

    @register_source("dummy_test", DummyConfig)
    class Dummy(Source):
        def probe(self):  # type: ignore[no-untyped-def]
            return {}

        def fetch(self):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def parse(self, fetched):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    try:
        assert source_class("dummy_test") is Dummy
        assert "dummy_test" in registered_kinds()
    finally:
        _REGISTRY.pop("dummy_test")
    with pytest.raises(KeyError, match="Unbekannter Quelltyp"):
        source_class("gibtsnicht")
