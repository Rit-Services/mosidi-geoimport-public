"""Geometrieprüfung und -reparatur.

Z- und M-Werte werden entfernt, gespeichert wird nur 2D (Bieterantwort 63). Ungültige Geometrien
werden mit ``shapely.make_valid`` repariert. Das Ergebnis muss
dem erwarteten Geometrietyp entsprechen (aus einer GeometryCollection werden z. B. die
Flächen extrahiert); sonst gilt das Objekt als „ungültige Geometrie (nicht reparierbar)“.
"""

from __future__ import annotations

import shapely
from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

_FAMILY = {
    "Point": "point",
    "MultiPoint": "point",
    "LineString": "line",
    "MultiLineString": "line",
    "LinearRing": "line",
    "Polygon": "polygon",
    "MultiPolygon": "polygon",
}


class GeometryError(ValueError):
    """Geometrie fehlt, ist leer oder nicht reparierbar. ``code`` für ``import_errors.reason_code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _extract_family(geom: BaseGeometry, family: str) -> BaseGeometry | None:
    parts = [g for g in getattr(geom, "geoms", [geom]) if _FAMILY.get(g.geom_type) == family and not g.is_empty]
    flat: list[BaseGeometry] = []
    for p in parts:
        flat.extend(getattr(p, "geoms", [p]))
    if not flat:
        return None
    if family == "polygon":
        return flat[0] if len(flat) == 1 else MultiPolygon(flat)
    if family == "line":
        return flat[0] if len(flat) == 1 else MultiLineString(flat)
    return flat[0] if len(flat) == 1 else MultiPoint(flat)


def check_geometry(geom: BaseGeometry | None, allowed_types: list[str] | None) -> tuple[BaseGeometry, bool]:
    """Liefert (gültige Geometrie, repariert?) oder wirft ``GeometryError``."""
    if geom is None:
        raise GeometryError("missing_geometry", "Geometrie fehlt")
    if geom.is_empty:
        raise GeometryError("empty_geometry", "Geometrie ist leer")
    if shapely.has_z(geom) or shapely.has_m(geom):
        geom = shapely.force_2d(geom)  # nur 2D (Bieterantwort 63): Z- und M-Werte entfallen
    repaired = False
    if not geom.is_valid:
        reason = explain_validity(geom)
        fixed = shapely.make_valid(geom)
        family = _FAMILY.get(geom.geom_type)
        if fixed.geom_type == "GeometryCollection" or (family and _FAMILY.get(fixed.geom_type) != family):
            fixed = _extract_family(fixed, family) if family else None
        if fixed is None or fixed.is_empty or not fixed.is_valid:
            raise GeometryError("invalid_geometry", f"ungültige Geometrie (nicht reparierbar): {reason}")
        geom, repaired = fixed, True
    if allowed_types and geom.geom_type not in allowed_types:
        raise GeometryError(
            "unexpected_geometry_type",
            f"Geometrietyp {geom.geom_type} nicht erwartet (erlaubt: {', '.join(allowed_types)})",
        )
    return geom, repaired
