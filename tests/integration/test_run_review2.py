"""Nachprüfung: Nur angewendete Zahlen ausweisen, kurze Zeilensperre, Lauf-Sperre kompatibel mit Altversionen."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from geoimport.config.loader import load_config
from geoimport.meta.repository import MetaRepository
from geoimport.orchestrator import RunLocked, RunOptions, run_pipeline
from geoimport.sources.base import ParseResult, PipelineWarning, RecordError
from geoimport.target.table_mapping import TableMappingAdapter
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID = "geobasis_bb_gemeinden"
KEY = "12051000"


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def wfs_report(outcome: Any) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == WFS_ID)


def allow_errors(config_dir: Path) -> None:
    path = config_dir / "sources" / f"{WFS_ID}.yaml"
    src = load_yaml(path)
    src["guards"]["max_error_ratio"] = 0.5
    write_yaml(path, src)


def invalidate(src: Any, parsed: ParseResult) -> list[PipelineWarning]:
    parsed.features = [f for f in parsed.features if f.key != KEY]
    parsed.errors.append(RecordError(KEY, "invalid_geometry", "Geometrie ungültig"))
    return []


def codes(s: dict[str, Any]) -> list[str]:
    return [w["code"] for w in s["warnings"]]


def test_rolled_back_replace_reports_nothing_applied(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_errors(config_dir)
    run(config_dir, source_ids=[WFS_ID])

    def broken(*a: Any, **k: Any) -> None:
        raise psycopg.errors.DiskFull("simuliert: Diff-Schlüssel nicht schreibbar")

    monkeypatch.setattr(MetaRepository, "write_diff_keys", broken)
    s = wfs_report(run(config_dir, source_ids=[WFS_ID], full=True, mutate=invalidate))
    assert s["status"] == "failed" and s["data_unchanged"]
    assert (s["rows_deleted"], s["rows_inserted"], s["invalid_existing"]) == (0, 0, 0)
    assert "rows_deleted_by_table" not in s["info"] and "invalid_existing" not in codes(s)
    na = s["info"]["not_applied"]
    assert na["rows_deleted"] > 0 and na["rows_inserted"] > 0 and na["invalid_existing"] == 1
    assert db.execute("SELECT count(*) FROM ref.geometrien").fetchone()[0] == 6


def test_guard_skip_and_dry_run_invalid_existing(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    allow_errors(config_dir)
    run(config_dir, source_ids=[WFS_ID])
    s = wfs_report(run(config_dir, source_ids=[WFS_ID], full=True, mutate=invalidate, dry_run=True))
    assert s["status"] == "dry_run" and s["invalid_existing"] == 1
    w = next(w for w in s["warnings"] if w["code"] == "invalid_existing")
    assert "würde" in w["message"]
    path = config_dir / "sources" / f"{WFS_ID}.yaml"
    src = load_yaml(path)
    src["guards"]["min_features"] = 100
    write_yaml(path, src)
    s = wfs_report(run(config_dir, source_ids=[WFS_ID], full=True, mutate=invalidate))
    assert s["status"] == "skipped_guard" and s["invalid_existing"] == 0 and "invalid_existing" not in codes(s)
    assert s["info"]["not_applied"]["invalid_existing"] == 1


def test_metadata_row_not_locked_while_preparing(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any], db_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(config_dir, source_ids=[WFS_ID])
    original = TableMappingAdapter.count_existing
    probes: list[bool] = []

    def probe(self: Any, conn: Any, dataset: str, kind: str) -> int:
        with psycopg.connect(db_env) as other:  # z. B. die MOSIDI-Anwendung ändert den Titel
            try:
                other.execute("SELECT 1 FROM ref.metadaten WHERE datensatz=%s FOR UPDATE NOWAIT", (dataset,))
                probes.append(True)
            except psycopg.errors.LockNotAvailable:
                probes.append(False)
        return original(self, conn, dataset, kind)

    monkeypatch.setattr(TableMappingAdapter, "count_existing", probe)
    out = run(config_dir, source_ids=[WFS_ID], full=True)
    assert out.exit_code == 0 and probes == [True]


def test_legacy_run_lock_blocks_new_run(config_dir: Path, fake_wfs: FakeWfs, db_env: str) -> None:
    with psycopg.connect(db_env, autocommit=True) as old_version:
        old_version.execute("SELECT pg_advisory_lock(hashtext('geoimport:run'))")  # Lauf-Sperre bis 0.10
        with pytest.raises(RunLocked):
            run(config_dir, source_ids=[WFS_ID])
        # die neue Version hält während des Laufs beide Sperren
        old_version.execute("SELECT pg_advisory_unlock_all()")
    seen: list[tuple[bool, bool]] = []
    original = MetaRepository.abort_stale_runs

    def check_locks(self: MetaRepository, *a: Any) -> int:
        with psycopg.connect(db_env, autocommit=True) as probe:
            legacy = probe.execute("SELECT pg_try_advisory_lock(hashtext('geoimport:run'))").fetchone()[0]
            new = probe.execute("SELECT pg_try_advisory_lock(hashtext('geoimport:run-lock'), 0)").fetchone()[0]
            seen.append((legacy, new))
        return original(self, *a)

    mp = pytest.MonkeyPatch()
    mp.setattr(MetaRepository, "abort_stale_runs", check_locks)
    try:
        assert run(config_dir, source_ids=[WFS_ID]).exit_code == 0
    finally:
        mp.undo()
    assert seen == [(False, False)]
