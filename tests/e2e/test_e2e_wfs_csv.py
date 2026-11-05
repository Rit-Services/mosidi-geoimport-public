"""E2E über die CLI: 1 WFS + 1 CSV (Fixtures), drei Läufe, Bericht, Demo-Modus, Demo-Karte."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import psycopg
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from geoimport.cli import app
from geoimport.report.builder import load_schema
from tests.conftest import FakeWfs

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
runner = CliRunner()


def cli(config_dir: Path, *args: str) -> Any:
    return runner.invoke(app, ["--log-format", "text", *args, "--config-dir", str(config_dir)])


def latest(config_dir: Path) -> dict[str, Any]:
    return json.loads((config_dir.parent / "data" / "reports" / "latest.json").read_text(encoding="utf-8"))


def test_three_runs(config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]) -> None:
    assert cli(config_dir, "validate-config").exit_code == 0
    assert cli(config_dir, "init-meta").exit_code == 0
    r = cli(config_dir, "check-target")
    assert r.exit_code == 0, r.output

    # 1. Lauf: alles neu
    r = cli(config_dir, "run", "--trigger", "test")
    assert r.exit_code == 0, r.output
    rep = latest(config_dir)
    jsonschema.validate(rep, load_schema())
    assert rep["summary"]["added"] == 6 + 49 and rep["summary"]["ok"] == 2
    md = (config_dir.parent / "data" / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "© GeoBasis-DE/LGB, dl-de/by-2-0" in md

    # 2. Lauf (vollständig neu eingelesen): 0/0/0
    r = cli(config_dir, "run", "--full", "--trigger", "test")
    assert r.exit_code == 0 and "neu 0, geändert 0, gelöscht 0" in r.output, r.output

    # 3. Lauf mit geänderter Quelle: ein Attribut geändert
    fake_wfs.features = fake_wfs.features.replace(
        b"<app:gem_name>Brandenburg an der Havel</app:gem_name>",
        b"<app:gem_name>Brandenburg a. d. Havel</app:gem_name>",
    )
    r = cli(config_dir, "run", "--source", "geobasis_bb_gemeinden", "--trigger", "test")
    assert r.exit_code == 0, r.output
    s = latest(config_dir)["sources"][0]
    assert (s["added"], s["changed"], s["deleted"], s["unchanged"]) == (0, 1, 0, 5)
    assert (
        db.execute("SELECT name FROM ref.geometrien WHERE objekt_schluessel='12051000'").fetchone()[0]
        == "Brandenburg a. d. Havel"
    )

    # Bericht aus der Lauf-Historie, Quellenübersicht
    r = cli(config_dir, "report", "--format", "json")
    assert r.exit_code == 0 and json.loads(r.output)["run"]["run_id"] == latest(config_dir)["run"]["run_id"]
    r = cli(config_dir, "sources")
    assert r.exit_code == 0 and "geobasis_bb_gemeinden" in r.output


def test_exit_code_partial(config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any]) -> None:
    fake_wfs.fail_status = 503
    r = cli(config_dir, "run")
    assert r.exit_code == 1, r.output


def test_demo_mode_locks_and_simulation(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import load_yaml, write_yaml

    # 3 simulierte Fehler bei nur 6 Fixture-Objekten wären 33 % (> max_error_ratio 5 %); real: 3 von 413
    path = config_dir / "sources" / "geobasis_bb_gemeinden.yaml"
    src = load_yaml(path)
    src["guards"]["max_error_ratio"] = 0.5
    write_yaml(path, src)
    assert cli(config_dir, "run").exit_code == 0
    r = cli(config_dir, "demo", "--changes")
    assert r.exit_code == 3 and "PIPELINE_ENV=demo" in r.output  # Sperre 1
    monkeypatch.setenv("PIPELINE_ENV", "demo")
    r = cli(config_dir, "demo", "--changes")
    assert r.exit_code == 0, r.output  # fehlerhafte Einzeldatensätze lassen die Quelle nicht scheitern
    rep = latest(config_dir)
    assert rep["run"]["simulated"] and rep["run"]["trigger"] == "demo"
    w = next(s for s in rep["sources"] if s["id"] == "geobasis_bb_gemeinden")
    assert w["status"] == "ok" and w["changed"] >= 1 and w["deleted"] >= 1 and w["added"] >= 1 and w["invalid"] == 3
    # Warnungscodes aus Bieterantwort 63 mit Zählern je Quelle
    # Pflichtfeld leer + Schlüssel leer (auch ein Pflichtfeld laut Antwort 63)
    assert w["quality"] == {"geometry_repaired": 0, "geometry_skipped": 1, "missing_required_field": 2}
    codes = {x["code"]: x for x in w["warnings"]}
    assert codes["geometry_skipped"]["data"] == {"count": 1, "examples": ["SIMULATION_1"]}
    assert codes["missing_required_field"]["data"]["count"] == 2 and "geometry_repaired" not in codes
    assert (config_dir.parent / "data" / "reports" / "latest.md").read_text("utf-8").startswith("# SIMULATION")
    assert db.execute("SELECT simulated FROM pipeline_meta.import_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    # Reset stellt den echten Stand wieder her
    r = cli(config_dir, "demo", "--reset")
    assert r.exit_code == 0, r.output
    assert (
        db.execute(
            "SELECT count(*) FROM ref.geometrien g JOIN ref.metadaten m ON m.id=g.metadaten_id "
            "WHERE m.datensatz='geobasis_bb_gemeinden'"
        ).fetchone()[0]
        == 6
    )
    assert not latest(config_dir)["run"]["simulated"]


def test_demo_refuses_non_demo_adapter(
    config_dir: Path, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import load_yaml, write_yaml

    monkeypatch.setenv("PIPELINE_ENV", "demo")
    t = load_yaml(config_dir / "target.yaml")
    t["adapter"] = "table_mapping"
    write_yaml(config_dir / "target.yaml", t)
    r = cli(config_dir, "demo", "--changes")
    assert r.exit_code == 3 and "demo_reference" in r.output  # Sperre 2


def test_demo_refuses_non_demo_database(db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from geoimport.target.demo_reference import DemoDatabaseError, require_demo_database

    class Fake:
        def execute(self, *_: Any) -> Any:
            class R:
                def fetchone(self) -> tuple[str]:
                    return ("brandenburg",)

            return R()

    with pytest.raises(DemoDatabaseError, match="keine Demo-Datenbank"):  # Sperre 3
        require_demo_database(Fake())  # type: ignore[arg-type]
    assert require_demo_database(db).endswith("_demo")


def test_webmap_api(
    config_dir: Path, fake_wfs: FakeWfs, inkar_zip: Path, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli(config_dir, "run").exit_code == 0
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    from webmap import app as webmap

    webmap.config.cache_clear()
    client = TestClient(webmap.app)
    r = client.get("/")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/api/health").json()["status"] == "ok"
    ds = {d["dataset"]: d["objekte"] for d in client.get("/api/datasets").json()}
    assert ds["geobasis_bb_gemeinden"] == 6
    fc = client.get("/api/features", params={"dataset": "geobasis_bb_gemeinden"}).json()
    assert len(fc["features"]) == 6 and fc["features"][0]["geometry"]["type"] == "MultiPolygon"
    # Umkreissuche um den Campus: Brandenburg an der Havel liegt darin, Entfernung 0
    rad = client.get("/api/radius", params={"dataset": "geobasis_bb_gemeinden", "radius_m": 2000}).json()
    assert (
        rad["features"][0]["properties"]["key"] == "12051000" and rad["features"][0]["properties"]["entfernung_m"] == 0
    )
    assert (
        12_000_000 < rad["summe"]["flaeche_im_umkreis_m2"] < 12_700_000
    )  # ≈ π·2000² (Kreis vollständig im Stadtgebiet)
    assert client.get("/api/radius", params={"dataset": "x", "radius_m": 99999}).status_code == 422
    ch = client.get("/api/changes").json()
    assert ch["run"]["counts"]["added"] == 6 + 49 and len(ch["features"]) == 6
    err = client.get("/api/errors").json()
    assert len(err["ohne_geometrie"]) == 1
    ind = client.get("/api/indicators", params={"dataset": "inkar_gemeinden"}).json()
    assert {i["indikator"] for i in ind} >= {"Einwohnerdichte", "Arbeitslosenquote"}
    # Zwischenspeicher bis zum nächsten Lauf; unbekannte Datensätze werden nicht gemerkt
    stamp = webmap._indicator_cache["inkar_gemeinden"][0]
    assert client.get("/api/indicators", params={"dataset": "inkar_gemeinden"}).json() == ind
    assert client.get("/api/indicators", params={"dataset": "gibt_es_nicht"}).json() == []
    assert "gibt_es_nicht" not in webmap._indicator_cache
    assert cli(config_dir, "run").exit_code == 0
    assert client.get("/api/indicators", params={"dataset": "inkar_gemeinden"}).json() == ind
    assert webmap._indicator_cache["inkar_gemeinden"][0] != stamp
    cp = client.get(
        "/api/choropleth",
        params={
            "dataset": "inkar_gemeinden",
            "geometry_dataset": "geobasis_bb_gemeinden",
            "indicator": "Einwohnerdichte",
            "period": "2022",
        },
    ).json()
    assert sum(1 for f in cp["features"] if f["properties"]["wert"] is not None) == 6
    assert client.get("/api/features", params={"dataset": "x", "bbox": "1,2,3"}).status_code == 400
