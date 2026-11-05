"""Bieterantwort 74: Beim Ersetzen einer Quelle werden nur die Attribute gelöscht und neu geschrieben.

Geometrien (Raumbezüge, z. B. Gemeinden über den AGS) bleiben erhalten und werden über ihren fachlichen
Schlüssel aktualisiert. Fehlt ein Objekt in der neuen Lieferung, bleibt seine Geometrie stehen und der
Bericht meldet es als Hinweis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from geoimport.config.loader import ConfigError, load_config
from geoimport.orchestrator import RunOptions, run_pipeline
from tests.conftest import FakeWfs, load_yaml, write_yaml

pytestmark = pytest.mark.integration

WFS_ID = "geobasis_bb_gemeinden"


def run(config_dir: Path, **opts: Any) -> Any:
    return run_pipeline(load_config(config_dir), RunOptions(trigger="test", source_ids=[WFS_ID], **opts))


def src_report(outcome: Any) -> dict[str, Any]:
    return next(s for s in outcome.report["sources"] if s["id"] == WFS_ID)


def geometry_ids(db: psycopg.Connection[Any]) -> dict[str, int]:
    return dict(db.execute("SELECT objekt_schluessel, id FROM ref.geometrien").fetchall())


def drop_last_member(fake_wfs: FakeWfs) -> bytes:
    """Letztes Objekt aus der WFS-Antwort entfernen; liefert dessen GML."""
    start = fake_wfs.features.rindex(b"<wfs:member>")
    end = fake_wfs.features.rindex(b"</wfs:member>") + len(b"</wfs:member>")
    member = fake_wfs.features[start:end]
    fake_wfs.features = fake_wfs.features[:start] + fake_wfs.features[end:]
    fake_wfs.hits -= 1
    return member


def test_second_run_keeps_geometry_ids_and_replaces_attributes(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    assert run(config_dir).exit_code == 0
    ids = geometry_ids(db)
    attr_ids = {r[0] for r in db.execute("SELECT id FROM ref.attribute").fetchall()}
    assert len(ids) == 6 and len(attr_ids) == 36

    fake_wfs.features = fake_wfs.features.replace(
        b"<app:gem_art>Kreisfreie Stadt</app:gem_art>", b"<app:gem_art>Stadt (geaendert)</app:gem_art>"
    )
    out = run(config_dir)
    s = src_report(out)
    assert out.exit_code == 0, out.report
    assert geometry_ids(db) == ids  # dieselben Geometrie-IDs, nichts gelöscht und neu angelegt
    new_attr_ids = {r[0] for r in db.execute("SELECT id FROM ref.attribute").fetchall()}
    assert len(new_attr_ids) == 36 and not (new_attr_ids & attr_ids)  # Attribute vollständig ersetzt
    assert (
        db.execute("SELECT count(*) FROM ref.attribute WHERE name='gem_art' AND wert='Stadt (geaendert)'").fetchone()[0]
        >= 1
    )
    # Attribute hängen weiter an ihrer (unveränderten) Geometrie
    assert db.execute(
        "SELECT count(*) FROM ref.attribute a JOIN ref.geometrien g ON g.id = a.geometrie_id"
    ).fetchone() == (36,)
    assert s["info"]["rows_deleted_by_table"] == {"attribute": 36}
    assert s["info"]["rows_inserted_by_table"] == {"geometrien": 0, "attribute": 36}
    assert s["info"]["rows_updated_by_table"] == {"geometrien": 0}  # Geometrie und Name unverändert
    assert s["rows_updated"] == 0


def test_changed_geometry_is_updated_in_place(config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]) -> None:
    run(config_dir)
    ids = geometry_ids(db)
    db.execute("UPDATE ref.geometrien SET name = 'alt' WHERE objekt_schluessel = '12051000'")
    out = run(config_dir, full=True)
    s = src_report(out)
    assert s["info"]["rows_updated_by_table"] == {"geometrien": 1}
    assert geometry_ids(db) == ids
    assert db.execute("SELECT name FROM ref.geometrien WHERE objekt_schluessel = '12051000'").fetchone() == (
        "Brandenburg an der Havel",
    )


def test_missing_area_keeps_geometry_and_reports_hint(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    run(config_dir)
    ids = geometry_ids(db)
    drop_last_member(fake_wfs)
    out = run(config_dir)
    s = src_report(out)
    assert out.exit_code == 0, out.report
    assert geometry_ids(db) == ids  # Geometrie des fehlenden Gebiets bleibt
    gone = [
        k for k in ids if k not in {f for (f,) in db.execute("SELECT DISTINCT objekt_schluessel FROM ref.attribute")}
    ]
    assert len(gone) == 1  # nur seine Attribute sind entfallen
    hints = [w for w in s["warnings"] if w["code"] == "geometry_retained"]
    assert len(hints) == 1 and hints[0]["data"]["count"] == 1 and hints[0]["data"]["examples"] == gone
    assert s["deleted"] == 1  # in der Lieferung entfallen
    deleted = db.execute(
        "SELECT record_key, geom IS NOT NULL FROM pipeline_meta.diff_keys WHERE change='deleted'"
    ).fetchall()
    assert deleted == [(gone[0], True)]  # Änderungskarte zeigt das entfallene Gebiet weiterhin

    # kommt das Gebiet zurück, wird dieselbe Geometrie wieder verwendet
    fake_wfs.features = FakeWfs().features
    fake_wfs.hits = 6
    back = run(config_dir)
    # Vergleich mit dem Bestand (Antwort 62): die Geometrie war noch da, nur ohne Attribute → geändert
    assert (src_report(back)["added"], src_report(back)["changed"], src_report(back)["deleted"]) == (0, 1, 0)
    assert geometry_ids(db) == ids


def test_foreign_attributes_referencing_geometries_survive(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    """Fremde Attribute (z. B. der MOSIDI-Anwendung), die auf eine Geometrie zeigen, bleiben gültig."""
    run(config_dir)
    db.execute("INSERT INTO ref.metadaten (datensatz, titel) VALUES ('fremd', 'Fremddaten')")
    db.execute(
        """INSERT INTO ref.attribute (metadaten_id, geometrie_id, objekt_schluessel, name, wert)
           SELECT m.id, g.id, g.objekt_schluessel, 'eigene_kennzahl', '42'
           FROM ref.metadaten m, ref.geometrien g WHERE m.datensatz = 'fremd' AND g.objekt_schluessel = '12051000'"""
    )
    out = run(config_dir, full=True)
    assert out.exit_code == 0, out.report
    row = db.execute(
        """SELECT g.objekt_schluessel FROM ref.attribute a JOIN ref.geometrien g ON g.id = a.geometrie_id
           WHERE a.name = 'eigene_kennzahl'"""
    ).fetchone()
    assert row == ("12051000",)


def test_duplicate_keys_in_existing_geometries_abort(
    config_dir: Path, fake_wfs: FakeWfs, db: psycopg.Connection[Any]
) -> None:
    """Doppelte Schlüssel im Bestand: nicht raten, welche Geometrie gemeint ist – Abbruch, Bestand bleibt."""
    run(config_dir)
    db.execute(
        """INSERT INTO ref.geometrien (metadaten_id, objekt_schluessel, name, geom)
           SELECT metadaten_id, objekt_schluessel, name, geom FROM ref.geometrien WHERE objekt_schluessel = '12051000'"""
    )
    before = db.execute("SELECT count(*) FROM ref.attribute").fetchone()
    out = run(config_dir, full=True)
    s = src_report(out)
    assert s["status"] == "failed" and s["data_unchanged"]
    assert "12051000" in s["error"]["message"] and "mehrfach" in s["error"]["message"]
    assert db.execute("SELECT count(*) FROM ref.attribute").fetchone() == before


def test_sql_scope_must_not_delete_geometries(config_dir: Path) -> None:
    target = load_yaml(config_dir / "target.yaml")
    target["delete_scope"] = {
        "strategy": "sql",
        "sql": [
            "DELETE FROM ref.attribute WHERE metadaten_id = %(meta_id)s",
            "DELETE FROM ref.geometrien WHERE metadaten_id = %(meta_id)s",
        ],
        "scope_sql": {
            "features": "SELECT objekt_schluessel FROM ref.geometrien WHERE metadaten_id = %(meta_id)s",
            "values": "SELECT 1 FROM ref.attribute WHERE metadaten_id = %(meta_id)s",
        },
    }
    write_yaml(config_dir / "target.yaml", target)
    with pytest.raises(ConfigError, match="Geometrien werden nicht gelöscht"):
        load_config(config_dir)
