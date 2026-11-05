"""Vom Quellobjekt zum Zieldatensatz: Fehlergründe, Dubletten, Mapping-Randfälle."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point, Polygon, box

from geoimport.config.models import ConcatExpr, ConstExpr, FieldExpr, KeyConfig, Mapping, PipelineConfig
from geoimport.transform.geometry import GeometryError, check_geometry
from geoimport.transform.mapping import MappingError, evaluate, mapping_fields, referenced_fields
from geoimport.transform.validate import RawFeature, prepare_features

CAMPUS = Point(12.5371959, 52.4107103)
MAPPING = Mapping(
    name="name", attributes={"ags": FieldExpr(field="ags", zfill=8), "flaeche": FieldExpr(field="fl", to_float=True)}
)


def _prepare(raw: list[RawFeature], **kw: object) -> tuple[list, list]:
    return prepare_features(
        raw,
        key=KeyConfig(fields=["ags"]),
        mapping=MAPPING,
        required=["name"],
        geometry_types=None,
        src_crs="EPSG:4326",
        pipeline=PipelineConfig(),
        **kw,  # type: ignore[arg-type]
    )


def _feat(
    ags: str | None = "12051000", name: str | None = "Brandenburg", fl: str = "229,7", geom: object = CAMPUS
) -> RawFeature:
    return RawFeature({"ags": ags, "name": name, "fl": fl}, geom)  # type: ignore[arg-type]


def test_gueltiges_objekt_wird_abgebildet() -> None:
    records, errors = _prepare([_feat(ags="12051000")])
    assert errors == []
    (rec,) = records
    assert rec.key == "12051000" and rec.name == "Brandenburg"
    assert rec.attributes == {"ags": "12051000", "flaeche": 229.7}
    assert rec.hash


@pytest.mark.parametrize(
    ("feature", "code"),
    [
        (_feat(ags=None), "missing_key"),
        (_feat(name=""), "missing_required_field"),
        (_feat(geom=None), "missing_geometry"),
        (_feat(geom=Point(100, 10)), "crs_implausible"),
        (_feat(fl="viel"), "mapping_error"),
    ],
)
def test_fehlergruende(feature: RawFeature, code: str) -> None:
    records, errors = _prepare([feature])
    assert records == []
    assert [e.reason_code for e in errors] == [code]


def test_doppelter_schluessel_ist_fehler() -> None:
    records, errors = _prepare([_feat(), _feat()])
    assert len(records) == 1
    assert [e.reason_code for e in errors] == ["duplicate_key"]


def test_identische_dublette_an_kachelgrenze_wird_verworfen() -> None:
    records, errors = _prepare([_feat(), _feat()], dedupe_identical=True)
    assert len(records) == 1 and errors == []
    _, errors = _prepare([_feat(), _feat(name="Anders")], dedupe_identical=True)
    assert [e.reason_code for e in errors] == ["duplicate_key"]


# ------------------------------------------------------------------ Geometrie
def test_reparatur_behaelt_geometriefamilie() -> None:
    # Selbstüberschneidende Fläche (Schleife) wird zu Flächen repariert, gültige Linie bleibt unverändert
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])
    geom, repaired = check_geometry(bowtie, None)
    assert repaired and geom.geom_type == "MultiPolygon"
    geom, repaired = check_geometry(LineString([(0, 0), (1, 1)]), ["LineString"])
    assert not repaired


def test_nicht_reparierbar() -> None:
    # Fläche ohne Inhalt: make_valid liefert nur eine Linie, keine Fläche
    flat = Polygon([(0, 0), (1, 1), (2, 2), (0, 0)])
    with pytest.raises(GeometryError) as exc:
        check_geometry(flat, None)
    assert exc.value.code == "invalid_geometry"


def test_erlaubter_typ_nach_reparatur() -> None:
    with pytest.raises(GeometryError, match="nicht erwartet"):
        check_geometry(box(0, 0, 1, 1), ["Point"])


def test_z_und_m_werden_auf_2d_reduziert() -> None:
    """Bieterantwort 63: nur 2D inkl. Multi-Geometrien."""
    from shapely import has_m, has_z
    from shapely.wkt import loads

    for wkt in (
        "MULTIPOLYGON Z (((0 0 1, 1 0 1, 1 1 1, 0 0 1)))",
        "POLYGON M ((0 0 7, 1 0 7, 1 1 7, 0 0 7))",
        "POINT ZM (1 2 3 4)",
    ):
        geom, repaired = check_geometry(loads(wkt), None)
        assert not has_z(geom) and not has_m(geom) and not repaired, wkt


# ------------------------------------------------------------------ Mapping
def test_umwandlungen() -> None:
    props = {"n": "3,9", "d": "31.12.2024", "leer": "", "a": "1", "b": "x"}
    with pytest.raises(MappingError):  # keine stille Abrundung mehr ("3,9" ist keine Ganzzahl)
        evaluate(FieldExpr(field="n", to_int=True), props)
    assert evaluate(FieldExpr(field="d", to_date="%d.%m.%Y"), props) == "2024-12-31"
    assert evaluate(FieldExpr(field="leer", to_int=True), props) is None
    assert evaluate(ConcatExpr(concat=["a", "fehlt"]), props) is None
    assert evaluate(ConcatExpr(concat=["a", ConstExpr(const="-"), "b"]), props) == "1-x"
    with pytest.raises(MappingError, match="nicht umwandelbar"):
        evaluate(FieldExpr(field="d", to_date="%Y"), props)
    with pytest.raises(MappingError, match="Unbekannter Ausdruck"):
        evaluate(42, props)


def test_referenzierte_felder() -> None:
    expr = ConcatExpr(concat=["krs", ConstExpr(const="-"), FieldExpr(field="gem")])
    assert referenced_fields(expr) == {"krs", "gem"}
    assert referenced_fields(ConstExpr(const=1)) == set()
    assert mapping_fields(MAPPING) == {"name", "ags", "fl"}


def test_zusammengesetzter_schluessel() -> None:
    records, errors = prepare_features(
        [_feat(ags="12051000", name="A"), _feat(ags="12051000", name="B")],
        key=KeyConfig(fields=["ags", "name"]),
        mapping=MAPPING,
        required=[],
        geometry_types=None,
        src_crs="EPSG:4326",
        pipeline=PipelineConfig(),
    )
    assert errors == [] and sorted(r.key for r in records) == ["12051000|A", "12051000|B"]
