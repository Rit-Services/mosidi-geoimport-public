"""CSV: Kodierung, Dezimalkomma, Filter, wide→long, ZIP, Schlüssel, INKAR-„latest“."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from geoimport.config.loader import load_config
from geoimport.http import HttpClient
from geoimport.sources.base import SourceContext, SourceFailure, StateStore
from geoimport.sources.csv_source import CsvSourcePlugin, detect_encoding, open_table
from tests.conftest import CONFIG, FIX, load_yaml, write_yaml


def test_detect_encoding() -> None:
    assert detect_encoding("Kennziffer;Gemeinde\n12051000;Brück\n".encode()) == "utf-8"
    assert detect_encoding("﻿a;b\n".encode()) == "utf-8-sig"
    assert detect_encoding("Gemeindeschlüssel;Größe\n12;Müncheberg Straße\n".encode("cp1252")) == "cp1252"


def _plugin(tmp_path: Path, source: dict, httpserver: HTTPServer | None = None) -> CsvSourcePlugin:
    cfg_dir = tmp_path / "config"
    write_yaml(cfg_dir / "target.yaml", load_yaml(CONFIG / "target.demo.yaml"))
    write_yaml(cfg_dir / "sources" / "s.yaml", source)
    cfg = load_config(cfg_dir)
    s = cfg.source(source["id"])
    http = HttpClient(cfg.pipeline.http.model_copy(update={"retries": 0, "backoff_base_s": 0}))
    ctx = SourceContext(cfg.pipeline, http, "t", tmp_path / "raw", StateStore(tmp_path / "state", s.id))
    return CsvSourcePlugin(s, ctx)


def _inkar(path: Path) -> dict:
    src = load_yaml(CONFIG / "sources" / "inkar_gemeinden.yaml")
    src["csv"]["location"] = {"path": str(path)}
    src["geometry_join"] = None
    src["guards"]["min_rows"] = 1
    return src


def test_inkar_structure_from_zip(tmp_path: Path) -> None:
    p = _plugin(tmp_path, _inkar(FIX / "csv" / "inkar_synthetisch.zip"))
    parsed = p.parse(p.fetch())
    v = parsed.values
    assert v is not None
    # 6 Gemeinden × 4 Indikatoren × 2 Jahre + 1 unbekannte Gemeinde; Kreise/NUTS/Flensburg gefiltert
    assert len(v) == 49
    assert parsed.info["detected_encoding"] == "utf-8" and parsed.info["member"] == "inkar_synthetisch.csv"
    assert set(v["object_key"]) >= {"12051000", "12999999"}
    row = v[(v.object_key == "12051000") & (v.name == "Einwohnerdichte") & (v.period == "2022")].iloc[0]
    assert row.value == pytest.approx(320.07)  # Dezimalkomma
    assert row.key == "12051000|LRB|bev_dichte|Einwohnerdichte|2022"
    assert '"domain": "LRB"' in row.extra and "region_name" not in row.extra  # keep: false
    # "Bevölkerung"/"Bildung" mit gleichem Kürzel sind über den Schlüssel unterscheidbar (keine Dubletten)
    assert not any(e.reason_code == "duplicate_key" for e in parsed.errors)
    # "n/a" ist keine Zahl → Fehlerzeile, nicht still verworfen
    assert [e.reason_code for e in parsed.errors] == ["invalid_value"]
    assert parsed.state_updates["columns"][0] == "Bereich"


def test_pflichtfelder_wert_und_zeitbezug(tmp_path: Path) -> None:
    """Bieterantwort 63: Schlüssel, Raumbezug, Zeitbezug und Wert sind Pflicht – auch ohne guards.required."""
    from geoimport.quality import quality_counts

    text = (FIX / "csv" / "inkar_synthetisch.csv").read_text("utf-8")
    text += (
        "LRB;1;bev_dichte;Einwohnerdichte;Gemeinden;12051000;Brandenburg an der Havel;2024;\n"  # ohne Wert
        "LRB;1;bev_dichte;Einwohnerdichte;Gemeinden;12052000;Cottbus;;12,5\n"  # ohne Zeitbezug (Teil des Schlüssels)
    )
    path = tmp_path / "inkar.csv"
    path.write_text(text, "utf-8")
    src = _inkar(path)
    src["guards"]["required"] = []  # Pflicht gilt unabhängig von der Konfiguration
    p = _plugin(tmp_path, src)
    parsed = p.parse(p.fetch())
    assert parsed.values is not None and len(parsed.values) == 49
    reasons = sorted(e.reason_code for e in parsed.errors)
    assert reasons == ["invalid_value", "missing_key", "missing_required_field"]
    missing = next(e for e in parsed.errors if e.reason_code == "missing_required_field")
    assert missing.record_key == "12051000|LRB|bev_dichte|Einwohnerdichte|2024" and missing.reason.endswith(": value")
    assert quality_counts(parsed)["missing_required_field"] == 2


def test_inkar_konfiguration_nennt_alle_pflichtfelder() -> None:
    src = load_yaml(CONFIG / "sources" / "inkar_gemeinden.yaml")
    assert {"region_key", "indicator", "period", "value"} <= set(src["guards"]["required"])


def test_duplicate_keys_become_errors(tmp_path: Path) -> None:
    src = _inkar(FIX / "csv" / "inkar_synthetisch.csv")
    src["business_key"] = ["region_key", "indicator_code", "period"]  # zu grober Schlüssel
    p = _plugin(tmp_path, src)
    parsed = p.parse(p.fetch())
    dups = [e for e in parsed.errors if e.reason_code == "duplicate_key"]
    assert len(dups) == 24  # a_bb_4G: Bevölkerung + Bildung je Gemeinde und Jahr


def test_missing_mapped_column(tmp_path: Path) -> None:
    src = _inkar(FIX / "csv" / "inkar_synthetisch.csv")
    src["columns"]["Fehlt"] = {"to": "x"}
    p = _plugin(tmp_path, src)
    with pytest.raises(SourceFailure) as exc:
        p.parse(p.fetch())
    assert exc.value.kind == "schema_mismatch"


def test_statistik_cp1252_wide_with_footer(tmp_path: Path) -> None:
    src = {
        "id": "statistik_test",
        "type": "csv",
        "title": "Test",
        "license": "synthetisch",
        "csv": {
            "location": {"path": str(FIX / "csv" / "statistik_cp1252_wide.csv")},
            "delimiter": ";",
            "decimal": ",",
            "thousands": ".",
            "footer_rows": 1,
            "na_values": ["-"],
        },
        "reshape": {
            "wide_to_long": {
                "id_columns": ["Gemeindeschlüssel", "Gemeinde"],
                "var_name": "period",
                "value_name": "value",
                "indicator": "Einwohner",
            }
        },
        "columns": {
            "Gemeindeschlüssel": {"to": "region_key", "normalize": {"zfill": 8}},
            "value": {"to": "value", "type": "float"},
        },
        "business_key": ["region_key", "indicator", "period"],
        "target": {"dataset": "statistik_test"},
    }
    p = _plugin(tmp_path, src)
    parsed = p.parse(p.fetch())
    v = parsed.values
    assert parsed.info["detected_encoding"] == "cp1252"
    assert len(v) == 3 and set(v["name"]) == {"Einwohner"}
    assert v[(v.object_key == "12051000") & (v.period == "2021")].value.iloc[0] == pytest.approx(1234.5)
    # "-" = kein Wert: Wert ist Pflicht (Bieterantwort 63) → Zeile übersprungen, nicht mit leerem Wert gespeichert
    assert [(e.record_key, e.reason_code) for e in parsed.errors] == [
        ("12069017|Einwohner|2021", "missing_required_field")
    ]


def test_points_from_coordinates(tmp_path: Path) -> None:
    csv = tmp_path / "punkte.csv"
    csv.write_text("id;name;rw;hw\n1;Campus;333400,5;5808700,1\n2;Kaputt;abc;5808700\n", encoding="utf-8")
    src = {
        "id": "punkte",
        "type": "csv",
        "title": "Punkte",
        "license": "synthetisch",
        "csv": {"location": {"path": str(csv)}},
        "columns": {"id": {"to": "id"}, "name": {"to": "name"}},
        "coordinates": {"x": "rw", "y": "hw", "crs": "EPSG:25833"},
        "mapping": {"name": "name"},
        "business_key": ["id"],
        "target": {"dataset": "punkte"},
    }
    p = _plugin(tmp_path, src)
    parsed = p.parse(p.fetch())
    assert len(parsed.features) == 1 and parsed.features[0].geometry.geom_type == "Point"
    assert 12.4 < parsed.features[0].geometry.x < 12.6
    assert [e.reason_code for e in parsed.errors] == ["invalid_value"]


def test_archive_member_must_be_unique(tmp_path: Path) -> None:
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inkar_a.csv", "x")
        zf.writestr("inkar_b.csv", "y")
    with pytest.raises(SourceFailure, match="2 Dateien"), open_table(z, "inkar*.csv"):
        pass


def test_latest_year_search(httpserver: HTTPServer, tmp_path: Path) -> None:
    year = date.today().year
    httpserver.expect_request(f"/inkar_{year - 1}.zip", method="HEAD").respond_with_data("", status=200)
    src = _inkar(FIX / "csv" / "inkar_synthetisch.zip")
    src["csv"]["location"] = {"url_template": httpserver.url_for("/inkar_{year}.zip"), "year": "latest"}
    assert _plugin(tmp_path, src).resolve_url().endswith(f"inkar_{year - 1}.zip")


def test_download_conditional_and_unchanged(httpserver: HTTPServer, tmp_path: Path) -> None:
    data = (FIX / "csv" / "inkar_synthetisch.zip").read_bytes()
    httpserver.expect_request("/d.zip").respond_with_data(data, headers={"ETag": '"v1"'})
    src = _inkar(FIX / "csv" / "inkar_synthetisch.zip")
    src["csv"]["location"] = {"url": httpserver.url_for("/d.zip")}
    p = _plugin(tmp_path, src)
    first = p.fetch()
    assert not first.unchanged and first.artifacts[0].path.read_bytes() == data
    p.ctx.state.update(first.state_updates)
    second = p.fetch()
    assert second.unchanged  # gleicher Hash
    assert httpserver.log[-1][0].headers.get("If-None-Match") == '"v1"'


def test_not_modified_304(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/d.zip").respond_with_data("", status=304)
    src = _inkar(FIX / "csv" / "inkar_synthetisch.zip")
    src["csv"]["location"] = {"url": httpserver.url_for("/d.zip")}
    p = _plugin(tmp_path, src)
    p.ctx.state.update({"url": httpserver.url_for("/d.zip"), "etag": '"v1"'})
    r = p.fetch()
    assert r.unchanged and not r.artifacts


def test_gzip(tmp_path: Path) -> None:
    import gzip

    gz = tmp_path / "x.csv.gz"
    gz.write_bytes(gzip.compress(b"a;b\n1;2\n"))
    with open_table(gz, None) as (fh, name):
        assert io.BytesIO(fh.read()).read() == b"a;b\n1;2\n" and name == "x.csv"


def test_ascii_stichprobe_cp1252_spaeter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Große Datei: Die Stichprobe ist reines ASCII, die ersten Umlaute (cp1252) folgen erst danach
    import geoimport.sources.csv_source as csv_source

    text = (FIX / "csv" / "inkar_synthetisch.csv").read_text("utf-8")
    path = tmp_path / "spaet_cp1252.csv"
    path.write_bytes(text.encode("cp1252"))
    monkeypatch.setattr(csv_source, "SAMPLE_BYTES", 600)  # vor dem ersten Umlaut (Byte 683)
    p = _plugin(tmp_path, _inkar(path))
    parsed = p.parse(p.fetch())
    assert parsed.info["detected_encoding"] == "utf-8"
    assert any(w.code == "encoding" for w in parsed.warnings)
    v = parsed.values
    assert v is not None and len(v) == 49
    assert "Bevölkerung" in " ".join(v["key"])  # korrekt als cp1252 gelesen, nicht verstümmelt


def test_abgeschnittene_zip_ist_unvollstaendig(tmp_path: Path) -> None:
    # V5: abgebrochener Download (ZIP ohne Ende) → Abbruch beim Lesen, Bestand bleibt unverändert
    from geoimport.orchestrator import INCOMPLETE_KINDS

    data = (FIX / "csv" / "inkar_synthetisch.zip").read_bytes()
    cut = tmp_path / "abgeschnitten.zip"
    cut.write_bytes(data[: len(data) // 2])
    p = _plugin(tmp_path, _inkar(cut))
    with pytest.raises(SourceFailure) as exc:
        p.parse(p.fetch())
    assert exc.value.kind in INCOMPLETE_KINDS


def test_abgeschnittene_csv_ist_unvollstaendig(tmp_path: Path) -> None:
    # Datei mitten in der letzten Zeile abgeschnitten: nicht still übernehmen, sondern als unvollständig abbrechen
    text = (FIX / "csv" / "inkar_synthetisch.csv").read_text("utf-8").rstrip("\n")
    cut = tmp_path / "abgeschnitten.csv"
    cut.write_text(text[: text.rfind(";")], encoding="utf-8")  # letzte Spalte (Wert) fehlt
    p = _plugin(tmp_path, _inkar(cut))
    with pytest.raises(SourceFailure, match="abgeschnitten") as exc:
        p.parse(p.fetch())
    assert exc.value.kind == "incomplete"


def _afs(path: Path) -> dict:
    src = load_yaml(CONFIG / "sources" / "afs_bb.yaml")
    src["csv"]["location"] = {"path": str(path)}
    src["geometry_join"] = None
    src["guards"]["min_rows"] = 1
    return src


def test_afs_aufbau_mit_kopf_und_fussnoten(tmp_path: Path) -> None:
    p = _plugin(tmp_path, _afs(FIX / "csv" / "afs_synthetisch.csv"))
    parsed = p.parse(p.fetch())
    v = parsed.values
    assert v is not None
    # 5 Zeilen Brandenburgs × 3 Merkmale; andere Länder und Fußnoten gefiltert
    assert sorted(set(v["object_key"])) == ["12051000", "12054000", "12060005", "12060020"]
    assert parsed.info["detected_encoding"] == "cp1252"  # ISO-8859-1 dekodiert in cp1252 identisch (0xA0–0xFF)
    row = v[(v.object_key == "12054000") & (v.name == "weiblich")].iloc[0]
    assert row.value == 92000 and row.key == "12054000|weiblich"
    # aufgelöste Gemeinde: „-“ = kein Wert → Pflichtfeld Wert fehlt, Zeilen übersprungen (Bieterantwort 63)
    assert len(v) == 12 and "12073603" not in set(v["object_key"])
    assert sorted(e.record_key for e in parsed.errors if e.reason_code == "missing_required_field") == [
        "12073603|insgesamt",
        "12073603|maennlich",
        "12073603|weiblich",
    ]


def test_afs_ohne_fussnotenmarke_ist_unvollstaendig(tmp_path: Path) -> None:
    data = (FIX / "csv" / "afs_synthetisch.csv").read_bytes()
    cut = tmp_path / "afs_abgeschnitten.csv"
    cut.write_bytes(data[: data.index(b"__________")])
    p = _plugin(tmp_path, _afs(cut))
    with pytest.raises(SourceFailure, match="Fußnotenmarke") as exc:
        p.parse(p.fetch())
    assert exc.value.kind == "incomplete"


def test_afs_ersatzdatei_bei_downloadfehler(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/afs.csv").respond_with_data("nicht da", status=404)
    src = _afs(FIX / "csv" / "afs_synthetisch.csv")
    src["csv"]["location"] = {
        "url": httpserver.url_for("/afs.csv"),
        "fallback_path": str(FIX / "csv" / "afs_synthetisch.csv"),
    }
    p = _plugin(tmp_path, src, httpserver)
    fetched = p.fetch()
    assert [w.code for w in fetched.warnings] == ["fallback_file"]
    assert len(p.parse(fetched).values) == 12  # type: ignore[arg-type]
