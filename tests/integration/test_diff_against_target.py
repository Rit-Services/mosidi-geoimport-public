"""Bieterantwort 62: fachlicher Diff gegen den vorhandenen Datenbestand.

Vergleichsbasis sind die aktuell vorhandenen Zieldaten, nicht ``pipeline_meta.diff_keys`` eines früheren Laufs.
Der erste Lauf gegen einen befüllten Bestand muss deshalb korrekte Zahlen für neu, geändert und entfallen liefern.
INKAR-Schlüssel: Kennziffer + Bereich + Kürzel + Indikator + Zeitbezug.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from geoimport.config.loader import load_config
from geoimport.orchestrator import RunOptions, run_pipeline
from geoimport.sources.base import ParseResult, PipelineWarning
from tests.conftest import FakeWfs

pytestmark = pytest.mark.integration

WFS_ID, CSV_ID = "geobasis_bb_gemeinden", "inkar_gemeinden"
KEY = "12051000|LRB|bev_dichte|Einwohnerdichte|2022"


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", **opts))


def src_report(outcome: Any, sid: str) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == sid)


def forget_history(db: psycopg.Connection[Any]) -> None:
    """Bestand bleibt, die Lauf-Historie der Diff-Schlüssel fehlt (z. B. Bestand aus der Vorarbeit)."""
    db.execute("DELETE FROM pipeline_meta.diff_keys")


def counts(s: dict[str, Any]) -> tuple[Any, ...]:
    return s["added"], s["changed"], s["deleted"], s["unchanged"], s["compared"]


def test_first_run_against_filled_target_wfs(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    run(config_dir, source_ids=[WFS_ID])
    forget_history(db)
    fake_wfs.features = fake_wfs.features.replace(
        b"<app:gem_art>Kreisfreie Stadt</app:gem_art>", b"<app:gem_art>Stadt (geaendert)</app:gem_art>"
    )
    start = fake_wfs.features.rindex(b"<wfs:member>")
    end = fake_wfs.features.rindex(b"</wfs:member>") + len(b"</wfs:member>")
    fake_wfs.features = fake_wfs.features[:start] + fake_wfs.features[end:]
    fake_wfs.hits = 5
    out = run(config_dir, source_ids=[WFS_ID])
    s = src_report(out, WFS_ID)
    assert counts(s) == (0, 1, 1, 4, True), s
    assert not any(w["code"] in ("no_baseline", "diff_from_baseline") for w in s["warnings"])
    # Änderungskarte: entfallenes Gebiet mit Geometrie aus dem Zielbestand
    assert db.execute(
        "SELECT count(*) FROM pipeline_meta.diff_keys WHERE change='deleted' AND geom IS NOT NULL"
    ).fetchone() == (1,)


def _change_values(src: Any, parsed: ParseResult) -> list[PipelineWarning]:
    if parsed.values is None:
        return []
    df = parsed.values
    df.loc[df["key"] == KEY, "value"] = 999.5  # ein Wert geändert
    parsed.values = df[df["key"] != "12051000|LRB|bev_dichte|Einwohnerdichte|2023"].reset_index(drop=True)
    return []


def test_first_run_against_filled_target_inkar(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    run(config_dir)
    forget_history(db)
    full = run(config_dir, source_ids=[CSV_ID], full=True)
    assert counts(src_report(full, CSV_ID)) == (0, 0, 0, 49, True)  # Schlüssel aus den Zielspalten zusammengesetzt

    forget_history(db)
    out = run(config_dir, source_ids=[CSV_ID], full=True, mutate=_change_values)
    s = src_report(out, CSV_ID)
    assert counts(s) == (0, 1, 1, 47, True), s
    changed = db.execute("SELECT record_key FROM pipeline_meta.diff_keys WHERE change='changed'").fetchall()
    assert changed == [(KEY,)]


def test_number_format_in_target_is_not_a_change(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    """Der Bestand kann Werte anders formatiert enthalten (z. B. von der Vorarbeit geschrieben)."""
    run(config_dir)
    db.execute("UPDATE ref.attribute SET wert = wert || '0' WHERE wert ~ '^[0-9]+[.][0-9]+$'")
    db.execute(
        "UPDATE ref.attribute SET zusatz = jsonb_build_object('indicator_code', zusatz->'indicator_code', 'domain', zusatz->'domain') WHERE zusatz IS NOT NULL"
    )
    out = run(config_dir, full=True)
    for sid in (WFS_ID, CSV_ID):
        s = src_report(out, sid)
        assert s["changed"] == 0 and s["unchanged"] == s["valid"], (sid, s)


def test_rows_from_other_datasets_do_not_count(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]
) -> None:
    run(config_dir)
    db.execute("INSERT INTO ref.metadaten (datensatz) VALUES ('fremd')")
    db.execute(
        """INSERT INTO ref.attribute (metadaten_id, objekt_schluessel, name, wert, zeitbezug, zusatz)
           SELECT (SELECT id FROM ref.metadaten WHERE datensatz='fremd'), objekt_schluessel, name, wert, zeitbezug, zusatz
           FROM ref.attribute"""
    )
    out = run(config_dir, full=True)
    for sid in (WFS_ID, CSV_ID):
        s = src_report(out, sid)
        assert (s["added"], s["changed"], s["deleted"]) == (0, 0, 0), (sid, s)
