"""Fremdtexte (Serverantworten) in Bericht und pipeline_meta entschärfen (SECURITY_REVIEW S5/S6)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import jsonschema

from geoimport.meta.repository import _json, strip_nul
from geoimport.report.builder import RunReport, SourceReport, load_schema
from geoimport.report.render_md import render_markdown
from geoimport.sources.base import PipelineWarning, RecordError, SourceFailure, clean_text

OSC8 = "\x1b]8;;https://evil.example/\x1b\\Alles OK\x1b]8;;\x1b\\"
PAYLOAD = f"Fehler\x00 \x1b[2J{OSC8} <img src=x onerror=alert(1)> [klick](https://evil.example) ![b](https://evil.example/b.png) \u202eabc"


def test_clean_text_strips_controls_and_truncates() -> None:
    out = clean_text(PAYLOAD)
    assert "\x00" not in out and "\x1b" not in out and "\u202e" not in out
    assert clean_text("a\tb\r\nc") == "a\tb\nc"  # Tab und Zeilenumbruch bleiben
    long = clean_text("x" * 5000)
    assert len(long) == 2000 and long.endswith("…")


def test_boundary_classes_clean_external_text() -> None:
    exc = SourceFailure("http", PAYLOAD, 500, "https://h/\x1b[31m")
    assert "\x00" not in str(exc) and "\x1b" not in str(exc) and exc.url == "https://h/[31m"
    w = PipelineWarning("schema_drift", PAYLOAD, {"examples": ["a\x00b", {"k\x1b": "v\x00"}]})
    assert "\x1b" not in w.message and w.data == {"examples": ["ab", {"k": "v"}]}
    e = RecordError("12\x0034", "invalid_value", PAYLOAD)
    assert e.record_key == "1234" and "\x00" not in e.reason


def _report(error_message: str, data_unchanged: bool = True) -> dict:
    t0 = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
    r = RunReport("2f1d3c4b-0000-4000-8000-000000000001", "schedule", t0, "0.9.0", "sha256:abc", "demo_reference")
    bad = SourceReport(
        "wfs_x",
        "wfs",
        "WFS",
        "wfs_x",
        status="failed",
        started_at=t0,
        finished_at=t0 + timedelta(seconds=5),
        data_unchanged=data_unchanged,
        error={"kind": "ogc_exception", "message": error_message, "http_status": 400, "url": "https://h/?a=1&b=<x>"},
        warnings=[{"code": "schema_drift", "message": error_message}],
        error_examples=[{"record_key": "<b>k</b>", "reason_code": "x", "reason": error_message}],
    )
    r.sources = [bad]
    r.warnings = [{"code": "config", "message": error_message}]
    r.finished_at = t0 + timedelta(seconds=6)
    r.status = "failed"
    return r.as_dict()


def test_markdown_neutralises_injection() -> None:
    md = render_markdown(_report(PAYLOAD))
    assert "\x00" not in md and "\x1b" not in md and "\u202e" not in md
    assert not re.search(r"(?<!\\)<", md), "unmaskiertes HTML"
    assert not re.search(r"(?<!\\)\[", md) and "![" not in md, "unmaskierter Link/Bild"
    assert "\\<img src=x onerror=alert(1)\\>" in md  # als Text sichtbar
    assert "Bestand der Quelle unverändert" in md


def test_markdown_does_not_claim_unchanged_for_replaced_source() -> None:
    md = render_markdown(_report("Problem", data_unchanged=False))
    assert "Bestand der Quelle unverändert" not in md and "Bestand der Quelle wurde ersetzt" in md


def test_meta_json_without_nul() -> None:
    data = {"a\x00": ["x\x00y", {"m": "\x00"}], "n": 1, "o": object()}
    out = _json(data)
    assert "\\u0000" not in out and json.loads(out)["a"] == ["xy", {"m": ""}]
    assert strip_nul("a\x00b") == "ab" and strip_nul(None) is None
    assert json.loads(_json({"t": "\\u0000 literal"}))["t"] == "\\u0000 literal"  # Text bleibt erhalten


def test_summary_counts_only_written_sources() -> None:
    t0 = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
    r = RunReport("2f1d3c4b-0000-4000-8000-000000000002", "manual", t0, "0.9.0", "sha256:abc", "demo_reference")
    ok = SourceReport("a", "wfs", "A", "a", status="ok", added=2, changed=1, deleted=3, unchanged=4)
    skipped = SourceReport("b", "wfs", "B", "b", status="skipped_guard", added=0, changed=0, deleted=900, invalid=2)
    failed = SourceReport("c", "csv", "C", "c", status="failed", added=5, deleted=7, invalid=1)
    r.sources = [ok, skipped, failed]
    r.finished_at = t0
    s = r.as_dict()["summary"]
    assert (s["added"], s["changed"], s["deleted"]) == (2, 1, 3)
    assert s["errors"] == 3  # Fehler zählen weiterhin über alle Quellen
    jsonschema.validate(r.as_dict(), load_schema())
