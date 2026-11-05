"""CSV-Lesekorrektheit: Kodierungserkennung, Dezimal-/Tausendertrennzeichen, Datumsformat, ZIP-Inhalt."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from geoimport.config.models import ColumnSpec
from geoimport.sources.csv_source import convert_column, detect_encoding, open_table
from tests.conftest import FIX
from tests.unit.test_csv import _plugin


# ------------------------------------------------------------------ Kodierung
@pytest.mark.parametrize(
    "text",
    [
        "12;Müncheberg\n",  # früher cp1006 → „M³ncheberg“
        "x;Preis in €\n1;5\n",  # früher cp437
        "Merkmal\nAusgaben – Kultur\n",  # früher cp1125
        "Merkmal\n„Kultur“\n",
    ],
)
def test_kurze_cp1252_stichprobe_ist_cp1252(text: str) -> None:
    assert detect_encoding(text.encode("cp1252")) == "cp1252"


def test_undefinierte_cp1252_bytes_ergeben_latin1() -> None:
    # 0x81/0x8D/0x8F/0x90/0x9D sind in cp1252 nicht belegt
    assert detect_encoding(b"a;b\n1;Br\xfcck\x81\n") == "latin-1"


def test_utf8_und_bom() -> None:
    assert detect_encoding("a;b\n1;Müncheberg – „x“ €\n".encode()) == "utf-8"
    assert detect_encoding("a;b\n1;Müncheberg\n".encode("utf-8-sig")) == "utf-8-sig"


def _sonderzeichen_src(path: Path, encoding: str | None = None) -> dict:
    csv: dict = {"location": {"path": str(path)}, "delimiter": ";", "decimal": ","}
    if encoding:
        csv["encoding"] = encoding
    return {
        "id": "sonderzeichen",
        "type": "csv",
        "title": "Test",
        "license": "synthetisch",
        "csv": csv,
        "columns": {
            "Kennziffer": {"to": "region_key"},
            "Gemeinde": {"to": "region_name"},
            "Merkmal": {"to": "indicator"},
            "Wert": {"to": "value", "type": "float"},
        },
        "record": {"period": None},
        "business_key": ["region_key", "indicator"],
        "target": {"dataset": "sonderzeichen"},
    }


FIX_SONDER = FIX / "csv" / "statistik_cp1252_sonderzeichen.csv"


def test_cp1252_fixture_mit_bytes_0x80_bis_0x9f(tmp_path: Path) -> None:
    raw = FIX_SONDER.read_bytes()
    assert any(0x80 <= b <= 0x9F for b in raw)  # –, €, „, “ liegen in cp1252 genau dort
    p = _plugin(tmp_path, _sonderzeichen_src(FIX_SONDER))
    parsed = p.parse(p.fetch())
    assert parsed.info["detected_encoding"] == "cp1252"
    v = parsed.values
    assert v is not None
    assert set(v["name"]) == {"Ausgaben je Einwohner – „Kultur“ in €", "Größe – Fläche in km²"}
    assert "Müncheberg, Stadt" in " ".join(v["extra"])
    enc = [w for w in parsed.warnings if w.code == "encoding"]
    assert len(enc) == 1 and "cp1252" in enc[0].message


def test_utf8_ohne_kodierungswarnung(tmp_path: Path) -> None:
    path = tmp_path / "utf8.csv"
    path.write_bytes(FIX_SONDER.read_bytes().decode("cp1252").encode("utf-8"))
    p = _plugin(tmp_path, _sonderzeichen_src(path))
    parsed = p.parse(p.fetch())
    assert parsed.info["detected_encoding"] == "utf-8"
    assert not [w for w in parsed.warnings if w.code == "encoding"]


def test_explizite_kodierung_ohne_warnung(tmp_path: Path) -> None:
    p = _plugin(tmp_path, _sonderzeichen_src(FIX_SONDER, encoding="cp1252"))
    parsed = p.parse(p.fetch())
    assert parsed.info["detected_encoding"] == "cp1252"
    assert "Ausgaben je Einwohner – „Kultur“ in €" in set(parsed.values["name"])  # type: ignore[index]
    assert not [w for w in parsed.warnings if w.code == "encoding"]


# ------------------------------------------------------------------ Dezimal-/Tausendertrennzeichen
def _conv(values: list[str], typ: str, decimal: str = ",", thousands: str | None = None) -> tuple[list, list]:
    num, bad = convert_column(pd.Series(values), ColumnSpec(to="x", type=typ), decimal, thousands)  # type: ignore[arg-type]
    return [None if pd.isna(x) else x for x in num], list(bad)


def test_dezimalkomma_lehnt_punkt_ab() -> None:
    num, bad = _conv(["1.234", "1,5", "12", "1.234,5"], "float")
    assert bad == [True, False, False, True]  # „1.234“ wäre sonst still 1,234 statt 1234
    assert num[1:3] == [1.5, 12.0]


def test_dezimalkomma_mit_tausenderpunkt() -> None:
    num, bad = _conv(["1.234", "1.234,5", "1,5", "1.234.567"], "float", thousands=".")
    assert bad == [False] * 4 and num == [1234.0, 1234.5, 1.5, 1234567.0]
    num, bad = _conv(["1.234", "3,9", "7"], "int", thousands=".")
    assert bad == [False, True, False] and num[0] == 1234 and num[2] == 7


def test_int_mit_nachkommastellen_ist_fehler() -> None:
    _, bad = _conv(["3,9", "3,0", "4"], "int")
    assert bad == [True, False, False]


def test_dezimalpunkt_unveraendert() -> None:
    num, bad = _conv(["1.5", "1,234"], "float", decimal=".")
    assert bad == [False, True] and num[0] == 1.5
    num, bad = _conv(["1,234.5"], "float", decimal=".", thousands=",")
    assert bad == [False] and num == [1234.5]


def test_tausenderpunkt_ohne_konfiguration_wird_fehlerzeile(tmp_path: Path) -> None:
    path = tmp_path / "werte.csv"
    path.write_text("Kennziffer;Gemeinde;Merkmal;Wert\n1;A;m;1.234\n2;B;m;12,5\n", encoding="utf-8")
    p = _plugin(tmp_path, _sonderzeichen_src(path))
    parsed = p.parse(p.fetch())
    v = parsed.values
    assert v is not None and list(v["value"]) == [12.5]
    assert [e.reason_code for e in parsed.errors] == ["invalid_value"]
    assert "1.234" in parsed.errors[0].reason


def test_koordinaten_mit_punkt_bei_dezimalkomma(tmp_path: Path) -> None:
    csv = tmp_path / "punkte.csv"
    csv.write_text(
        "id;name;rw;hw;flaeche\n1;Gut;333400,5;5808700,1;12,5\n2;Punkt;333.400;5808700;1\n", encoding="utf-8"
    )
    src = {
        "id": "punkte",
        "type": "csv",
        "title": "Punkte",
        "license": "synthetisch",
        "csv": {"location": {"path": str(csv)}},
        "columns": {"id": {"to": "id"}, "name": {"to": "name"}, "flaeche": {"to": "flaeche"}},
        "coordinates": {"x": "rw", "y": "hw", "crs": "EPSG:25833"},
        "mapping": {"name": "name", "attributes": {"flaeche": {"field": "flaeche", "to_float": True}}},
        "business_key": ["id"],
        "target": {"dataset": "punkte"},
    }
    p = _plugin(tmp_path, src)
    parsed = p.parse(p.fetch())
    assert len(parsed.features) == 1 and parsed.features[0].attributes["flaeche"] == 12.5
    assert [e.reason_code for e in parsed.errors] == ["invalid_value"]


def test_mapping_uebernimmt_dezimalkomma_der_csv(tmp_path: Path) -> None:
    csv = tmp_path / "punkte.csv"
    csv.write_text("id;name;rw;hw;flaeche\n1;A;333400,5;5808700,1;1.234\n", encoding="utf-8")
    src = {
        "id": "punkte",
        "type": "csv",
        "title": "Punkte",
        "license": "synthetisch",
        "csv": {"location": {"path": str(csv)}},
        "columns": {"id": {"to": "id"}, "name": {"to": "name"}, "flaeche": {"to": "flaeche"}},
        "coordinates": {"x": "rw", "y": "hw", "crs": "EPSG:25833"},
        "mapping": {"name": "name", "attributes": {"flaeche": {"field": "flaeche", "to_float": True}}},
        "business_key": ["id"],
        "target": {"dataset": "punkte"},
    }
    p = _plugin(tmp_path, src)
    parsed = p.parse(p.fetch())
    assert not parsed.features
    assert [e.reason_code for e in parsed.errors] == ["mapping_error"]


# ------------------------------------------------------------------ Datum
def test_datum_ohne_format_ist_konfigurationsfehler() -> None:
    with pytest.raises(ValidationError, match="date_format"):
        ColumnSpec(to="stichtag", type="date")
    assert ColumnSpec(to="stichtag", type="date", date_format="%d.%m.%Y").date_format == "%d.%m.%Y"


def test_datum_mit_format_eindeutig() -> None:
    spec = ColumnSpec(to="d", type="date", date_format="%d.%m.%Y")
    out, bad = convert_column(pd.Series(["01.02.2024", "13.02.2024", "2024-02-01"]), spec, ",", None)
    assert list(out[:2]) == ["2024-02-01", "2024-02-13"] and list(bad) == [False, False, True]


# ------------------------------------------------------------------ ZIP-Inhalt
def test_zip_ignoriert_macosx_und_ressourcendateien(tmp_path: Path) -> None:
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("daten/x.csv", "a;b\n1;2\n")
        zf.writestr("__MACOSX/daten/._x.csv", b"\x00\x05\x16\x07Mac OS X")
        zf.writestr("daten/._x.csv", b"\x00\x05\x16\x07")
    with open_table(z, None) as (fh, name):
        assert name == "daten/x.csv" and fh.read() == b"a;b\n1;2\n"
    with open_table(z, "x*.csv") as (_, name):
        assert name == "daten/x.csv"
