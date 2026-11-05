"""CRS, Geometrie, Schlüssel und Mapping-Ausdrücke."""

from __future__ import annotations

import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box

from geoimport.config.models import ConcatExpr, ConstExpr, FieldExpr, Mapping, NormalizeSpec
from geoimport.transform.crs import CrsError, check_plausible, epsg_urn, normalize_crs, to_target
from geoimport.transform.geometry import GeometryError, check_geometry
from geoimport.transform.keys import build_key, normalize
from geoimport.transform.mapping import MappingError, apply_mapping, evaluate

CAMPUS = (12.5371959, 52.4107103)
BBOX_DE = (5.5, 47.0, 15.5, 55.2)


# ------------------------------------------------------------------ CRS
def test_epsg25833_to_4326_campus() -> None:
    from pyproj import Transformer

    x, y = Transformer.from_crs(4326, 25833, always_xy=True).transform(*CAMPUS)
    back = to_target(Point(x, y), "urn:ogc:def:crs:EPSG::25833")
    assert back.x == pytest.approx(CAMPUS[0], abs=1e-6)
    assert back.y == pytest.approx(CAMPUS[1], abs=1e-6)


@pytest.mark.parametrize(
    "value",
    ["EPSG:25833", "urn:ogc:def:crs:EPSG::25833", "http://www.opengis.net/def/crs/EPSG/0/25833", "epsg:25833"],
)
def test_normalize_crs(value: str) -> None:
    assert normalize_crs(value) == "EPSG:25833"
    assert epsg_urn(value) == "urn:ogc:def:crs:EPSG::25833"


def test_unknown_crs() -> None:
    with pytest.raises(CrsError):
        normalize_crs("kein-crs")


def test_axis_swap_detected() -> None:
    swapped = Point(CAMPUS[1], CAMPUS[0])  # Breite/Länge statt Länge/Breite
    with pytest.raises(CrsError, match="vertauschten Achsen"):
        check_plausible(swapped, BBOX_DE)
    check_plausible(Point(*CAMPUS), BBOX_DE)


def test_outside_bbox() -> None:
    with pytest.raises(CrsError, match="außerhalb"):
        check_plausible(Point(100, 10), BBOX_DE)


# ------------------------------------------------------------------ Geometrie
def test_valid_geometry_unchanged() -> None:
    g, repaired = check_geometry(box(0, 0, 1, 1), ["Polygon", "MultiPolygon"])
    assert not repaired and g.equals(box(0, 0, 1, 1))


def test_bowtie_is_repaired_to_polygons() -> None:
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    g, repaired = check_geometry(bowtie, ["Polygon", "MultiPolygon"])
    assert repaired and g.is_valid and g.geom_type in ("Polygon", "MultiPolygon")
    assert g.area == pytest.approx(0.5)


def test_unexpected_type() -> None:
    with pytest.raises(GeometryError) as exc:
        check_geometry(Point(0, 0), ["MultiPolygon"])
    assert exc.value.code == "unexpected_geometry_type"


def test_missing_and_empty() -> None:
    with pytest.raises(GeometryError, match="fehlt"):
        check_geometry(None, None)
    with pytest.raises(GeometryError, match="leer"):
        check_geometry(MultiPolygon(), None)


# ------------------------------------------------------------------ Schlüssel
def test_normalize_keeps_leading_zeros() -> None:
    assert normalize("1001000", NormalizeSpec(zfill=8)) == "01001000"
    assert normalize(" 12 051 000 ", NormalizeSpec(zfill=8)) == "12051000"
    assert normalize(12051000.0, None) == "12051000"
    assert normalize("", None) is None
    assert normalize("ab", NormalizeSpec(upper=True)) == "AB"


def test_build_key() -> None:
    assert build_key(["12051000", "q_alo", "2023"]) == "12051000|q_alo|2023"
    assert build_key(["12051000", None]) is None


# ------------------------------------------------------------------ Mapping
def test_expressions() -> None:
    props = {"ags": "12051000", "krss": "12051", "flaeche": "12,5", "datum": "02.07.2026", "leer": ""}
    assert evaluate("ags", props) == "12051000"
    assert evaluate(FieldExpr(field="krss", zfill=8), props) == "00012051"
    assert evaluate(FieldExpr(field="flaeche", to_float=True), props) == 12.5
    assert evaluate(FieldExpr(field="datum", to_date="%d.%m.%Y"), props) == "2026-07-02"
    assert evaluate(FieldExpr(field="leer", to_int=True), props) is None
    assert evaluate(ConstExpr(const="BB"), props) == "BB"
    assert evaluate(ConcatExpr(concat=["krss", ConstExpr(const="-"), "ags"]), props) == "12051-12051000"
    with pytest.raises(MappingError):
        evaluate(FieldExpr(field="ags", to_date="%d.%m.%Y"), props)


def test_zahlen_im_mapping() -> None:
    props = {"a": "1.234", "b": "3,9", "c": "1.234,5", "d": 12.5, "e": 3.0, "f": "12.5", "g": "12"}
    # ohne decimal: genau ein Trennzeichen, Punkt oder Komma (WFS liefert Punkt, deutsche Texte Komma)
    assert evaluate(FieldExpr(field="f", to_float=True), props) == 12.5
    assert evaluate(FieldExpr(field="d", to_float=True), props) == 12.5
    assert evaluate(FieldExpr(field="e", to_int=True), props) == 3
    assert evaluate(FieldExpr(field="g", to_int=True), props) == 12
    with pytest.raises(MappingError):
        evaluate(FieldExpr(field="c", to_float=True), props)  # Punkt und Komma: mehrdeutig
    # to_int schneidet nicht mehr ab
    for field in ("b", "a"):
        with pytest.raises(MappingError):
            evaluate(FieldExpr(field=field, to_int=True), props)
    # decimal ",": Punkt ist nur als Tausendertrennzeichen erlaubt
    with pytest.raises(MappingError):
        evaluate(FieldExpr(field="a", to_float=True, decimal=","), props)
    assert evaluate(FieldExpr(field="a", to_float=True, decimal=",", thousands="."), props) == 1234.0
    assert evaluate(FieldExpr(field="c", to_float=True, decimal=",", thousands="."), props) == 1234.5
    assert evaluate(FieldExpr(field="a", to_int=True, decimal=",", thousands="."), props) == 1234
    assert evaluate(FieldExpr(field="f", to_float=True, decimal="."), props) == 12.5


def test_apply_mapping_without_attributes_takes_all_but_technical() -> None:
    name, attrs = apply_mapping(Mapping(name="n"), {"n": "X", "gml_id": "g1", "lowerCorner": "1 2", "a": "1"})
    assert name == "X" and attrs == {"n": "X", "a": "1"}
