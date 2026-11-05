"""Ersetzen und Lauf-Historie atomar, Zählung nur geschriebener Quellen, ungültige Datensätze, Konfig-Fingerabdruck."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from geoimport.config.loader import load_config
from geoimport.meta.repository import MetaRepository
from geoimport.orchestrator import CONFIG_FINGERPRINT, RunOptions, run_pipeline
from geoimport.sources.base import ParseResult, PipelineWarning, RecordError, StateStore
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID, CSV_ID = "geobasis_bb_gemeinden", "inkar_gemeinden"
KEY = "12051000"  # Brandenburg an der Havel
NEW_NAME = b"<app:gem_name>Brandenburg a. d. Havel</app:gem_name>"
OLD_NAME = b"<app:gem_name>Brandenburg an der Havel</app:gem_name>"


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def src_report(outcome: Any, sid: str) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == sid)


def name_in_target(db: psycopg.Connection[Any]) -> str:
    return db.execute("SELECT name FROM ref.geometrien WHERE objekt_schluessel=%s", (KEY,)).fetchone()[0]


def update_source(config_dir: Path, sid: str, **values: Any) -> None:
    path = config_dir / "sources" / f"{sid}.yaml"
    src = load_yaml(path)
    for k, v in values.items():
        if isinstance(v, dict):
            src.setdefault(k, {}).update(v)
        else:
            src[k] = v
    write_yaml(path, src)


def test_state_error_after_commit_is_only_a_warning(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(self: StateStore, values: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(StateStore, "update", disk_full)
    out = run(config_dir, source_ids=[WFS_ID])
    s = src_report(out, WFS_ID)
    assert s["status"] == "ok" and not s["data_unchanged"] and out.exit_code == 0
    assert any(w["code"] == "state_write_failed" for w in s["warnings"])
    assert db.execute("SELECT count(*) FROM ref.geometrien").fetchone()[0] == 6
    row = db.execute("SELECT status, warnings FROM pipeline_meta.import_source_results").fetchone()
    assert row[0] == "ok" and any(w["code"] == "state_write_failed" for w in row[1])  # nachgetragen
    assert "Bestand der Quelle unverändert" not in out.md_path.read_text("utf-8")


def test_meta_write_is_part_of_replace_transaction(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(config_dir, source_ids=[WFS_ID]).exit_code == 0
    fake_wfs.features = fake_wfs.features.replace(OLD_NAME, NEW_NAME)

    def broken(*a: Any, **k: Any) -> None:
        raise psycopg.errors.DiskFull("simuliert: pipeline_meta voll")

    original = MetaRepository.write_diff_keys
    monkeypatch.setattr(MetaRepository, "write_diff_keys", broken)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert s["status"] == "failed" and s["error"]["kind"] == "database" and s["data_unchanged"]
    assert name_in_target(db) == "Brandenburg an der Havel"  # Ersetzen mit zurückgerollt
    monkeypatch.setattr(MetaRepository, "write_diff_keys", original)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert (s["status"], s["changed"], s["unchanged"]) == ("ok", 1, 5)  # Basis = 1. Lauf
    assert name_in_target(db) == "Brandenburg a. d. Havel"


def test_guard_skipped_source_reports_no_changes(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    run(config_dir, source_ids=[WFS_ID])
    gml = fake_wfs.features
    fake_wfs.features = gml[: gml.index(b"</wfs:member>") + len(b"</wfs:member>")] + b"</wfs:FeatureCollection>"
    fake_wfs.hits = 1
    update_source(config_dir, WFS_ID, guards={"min_features": 1})
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert s["status"] == "skipped_guard" and s["deleted"] is None and s["added"] is None
    assert s["info"]["not_applied"]["deleted"] == 5
    assert out.report["summary"]["deleted"] == 0
    row = db.execute("SELECT deleted FROM pipeline_meta.import_source_results WHERE status='skipped_guard'").fetchone()
    assert row == (None,)


def test_invalid_records_not_counted_as_deleted(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    update_source(config_dir, WFS_ID, guards={"max_error_ratio": 0.5})
    run(config_dir, source_ids=[WFS_ID])

    def invalidate(src: Any, parsed: ParseResult) -> list[PipelineWarning]:
        parsed.features = [f for f in parsed.features if f.key != KEY]
        parsed.errors.append(RecordError(KEY, "invalid_geometry", "Geometrie ungültig"))
        return []

    out = run(config_dir, source_ids=[WFS_ID], full=True, mutate=invalidate)
    s = src_report(out, WFS_ID)
    assert (s["status"], s["deleted"], s["invalid_existing"], s["invalid"]) == ("ok", 0, 1, 1)
    assert any(w["code"] == "invalid_existing" for w in s["warnings"])
    assert out.report["summary"]["deleted"] == 0
    change = db.execute(
        "SELECT change FROM pipeline_meta.diff_keys WHERE record_key=%s ORDER BY run_id::text", (KEY,)
    ).fetchall()
    assert ("invalid",) in change
    # wieder gültig: Geometrie blieb im Bestand (Antwort 74), Attribute fehlten → „geändert“ (Antwort 62)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    s = src_report(out, WFS_ID)
    assert (s["added"], s["changed"], s["deleted"], s["unchanged"]) == (0, 1, 0, 5)


def test_config_change_defeats_skip_if_unchanged(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    assert run(config_dir).exit_code == 0
    assert src_report(run(config_dir, source_ids=[CSV_ID]), CSV_ID)["status"] == "unchanged"
    state = json.loads((config_dir.parent / "data" / "state" / f"{CSV_ID}.json").read_text("utf-8"))
    assert state[CONFIG_FINGERPRINT].startswith("sha256:")
    update_source(config_dir, CSV_ID, title="INKAR – Kennzahlen (geänderter Titel)")
    out = run(config_dir, source_ids=[CSV_ID])
    assert src_report(out, CSV_ID)["status"] == "ok", src_report(out, CSV_ID)
    assert src_report(run(config_dir, source_ids=[CSV_ID]), CSV_ID)["status"] == "unchanged"
