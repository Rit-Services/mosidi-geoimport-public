"""Koordinatentransformation nach EPSG:4326 und Prüfung der Achsenreihenfolge.

Die Pipeline fordert Daten im Quell-CRS an und transformiert lokal mit pyproj
(``always_xy=True``, also immer Länge/Breite bzw. Rechts-/Hochwert). Hintergrund:
WFS 2.0 liefert EPSG:4326 als ``urn:ogc:def:crs:EPSG::4326`` in der Reihenfolge
Breite/Länge – eine häufige Fehlerquelle.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely.geometry.base import BaseGeometry

TARGET_CRS = "EPSG:4326"

_EPSG_RE = re.compile(r"(?i)(?:EPSG(?::|::|/0/|/)|crs:EPSG::?)(\d{4,6})\s*$")


class CrsError(ValueError):
    """CRS unbekannt oder Koordinaten unplausibel (z. B. vertauschte Achsen)."""


def normalize_crs(value: str) -> str:
    """``urn:ogc:def:crs:EPSG::25833`` / ``http://www.opengis.net/def/crs/EPSG/0/25833`` → ``EPSG:25833``."""
    value = value.strip()
    m = _EPSG_RE.search(value)
    if m:
        return f"EPSG:{m.group(1)}"
    try:
        epsg = CRS.from_user_input(value).to_epsg()
    except Exception as exc:  # pyproj wirft verschiedene Fehlerklassen
        raise CrsError(f"Koordinatensystem nicht erkannt: {value!r}") from exc
    if epsg is None:
        raise CrsError(f"Koordinatensystem ohne EPSG-Code: {value!r}")
    return f"EPSG:{epsg}"


def epsg_urn(crs: str) -> str:
    """``EPSG:25833`` → ``urn:ogc:def:crs:EPSG::25833`` (Schreibweise für WFS 2.0)."""
    return "urn:ogc:def:crs:EPSG::" + normalize_crs(crs).split(":")[1]


@lru_cache(maxsize=32)
def transformer(src_crs: str, dst_crs: str = TARGET_CRS) -> Transformer:
    """Gecachter Transformer mit Achsenreihenfolge x/y (Länge/Breite)."""
    return Transformer.from_crs(normalize_crs(src_crs), dst_crs, always_xy=True)


def to_target(geom: BaseGeometry, src_crs: str) -> BaseGeometry:
    """Geometrie aus ``src_crs`` nach EPSG:4326 transformieren."""
    if normalize_crs(src_crs) == TARGET_CRS:
        return geom
    t = transformer(src_crs)

    def _xy(coords: np.ndarray) -> np.ndarray:
        x, y = t.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, _xy)


def check_plausible(geom: BaseGeometry, bbox: tuple[float, float, float, float]) -> None:
    """Prüft, ob die Geometrie (EPSG:4326) im Plausibilitätsrahmen liegt.

    Liegt sie nur mit vertauschten Achsen darin, wird ein Achsentausch gemeldet – ohne
    stilles Korrigieren, denn dann ist die Konfiguration (CRS/Achsenreihenfolge) falsch.
    """
    minx, miny, maxx, maxy = geom.bounds
    bx0, by0, bx1, by1 = bbox
    if bx0 <= minx and maxx <= bx1 and by0 <= miny and maxy <= by1:
        return
    if bx0 <= miny and maxy <= bx1 and by0 <= minx and maxx <= by1:
        raise CrsError(
            "Koordinaten liegen nur mit vertauschten Achsen im erwarteten Gebiet (Breite/Länge statt Länge/Breite) – "
            "request_crs bzw. Achsenreihenfolge prüfen"
        )
    raise CrsError(
        f"Koordinaten außerhalb des Plausibilitätsrahmens {list(bbox)}: "
        f"[{minx:.5f}, {miny:.5f}, {maxx:.5f}, {maxy:.5f}]"
    )


def transform_bbox(bbox: tuple[float, float, float, float], src: str, dst: str) -> tuple[float, float, float, float]:
    """Begrenzungsrahmen zwischen zwei CRS umrechnen (mit Randverdichtung)."""
    t = Transformer.from_crs(normalize_crs(src), normalize_crs(dst), always_xy=True)
    try:
        x0, y0, x1, y1 = t.transform_bounds(*bbox, densify_pts=21)
    except ProjError as exc:
        raise CrsError(f"Rahmen {bbox} nicht von {src} nach {dst} umrechenbar: {exc}") from exc
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        raise CrsError(f"Rahmen {bbox} liegt außerhalb des Gültigkeitsbereichs von {dst}")
    return (x0, y0, x1, y1)


def is_geographic(crs: str) -> bool:
    """True für geografische CRS (Grad statt Meter)."""
    return bool(CRS.from_user_input(normalize_crs(crs)).is_geographic)
