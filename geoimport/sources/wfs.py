"""WFS-Quellen (OGC Web Feature Service 2.0.0 / 1.1.0).

Ablauf je Lauf:

1. GetCapabilities: FeatureType vorhanden? CRS, Ausgabeformate, ``ImplementsResultPaging``, ``CountDefault``.
2. DescribeFeatureType: Felder mit dem zuletzt bekannten Schema vergleichen → Warnung ``schema_drift``;
   fehlt ein gemapptes Feld, schlägt die Quelle fehl.
3. GetFeature mit der Paging-Strategie ``startindex``, ``bbox_tiles`` oder ``none`` (``auto`` wählt).
   Jede Antwortseite wird vor dem Speichern geprüft (Wurzel ``FeatureCollection`` im WFS-/GML-Namensraum
   bzw. GeoJSON-``FeatureCollection``) und als Rohdatei mit SHA-256 gespeichert.
4. Lesen: GML über GDAL (pyogrio), alle Attribute als Text (``GML_FIELDTYPES=ALWAYS_STRING``), weil GDAL
   Feldtypen sonst je Datei rät und identische Objekte verschiedene Hashes bekämen. GeoJSON wird ohne GDAL
   gelesen und selbst in Text umgewandelt (``null`` bleibt leer, ``123`` bleibt ``"123"``).

Vollständigkeit: Paging endet erst bei einer leeren Seite, beim Erreichen der Trefferzahl oder wenn der
Dienst kein ``next`` mehr nennt. Verglichen wird die Zahl *verschiedener* Objekte (``gml:id`` bzw.
Schlüssel) mit ``RESULTTYPE=hits``; Überschneidungen zwischen Seiten brechen den Abruf ab.

Beobachtung LGB ``vg_wfs`` (deegree, 29.10.2026): ``numberReturned="0"`` und ``numberMatched="unknown"``
auch bei gefüllten Antworten. Die Pipeline zählt deshalb die gelieferten Objekte selbst und vergleicht
mit ``RESULTTYPE=hits``.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET  # noqa: S405 – Antworten bekannter Dienste, keine Entity-Auflösung in expat
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pyogrio
import shapely
import shapely.geometry
from pyogrio.errors import DataSourceError

from geoimport import GDAL_SKIP_DRIVERS
from geoimport.config.models import WfsSource
from geoimport.http import HttpError
from geoimport.logging import event
from geoimport.sources import register_source
from geoimport.sources.base import FetchResult, ParseResult, PipelineWarning, RawArtifact, Source, SourceFailure
from geoimport.transform.crs import CrsError, epsg_urn, is_geographic, normalize_crs, transform_bbox
from geoimport.transform.mapping import mapping_fields
from geoimport.transform.validate import RawFeature, prepare_features

log = logging.getLogger(__name__)

pyogrio.set_gdal_config_options(
    {
        "GML_FIELDTYPES": "ALWAYS_STRING",  # deterministische Typen (siehe Modulkopf)
        "GML_DOWNLOAD_SCHEMA": "NO",  # keine Netzzugriffe beim Lesen der Rohdaten
        "GML_SKIP_RESOLVE_ELEMS": "ALL",
    }
)
# Wurde pyogrio vor geoimport importiert, greift GDAL_SKIP nicht (siehe geoimport/__init__.py)
_UNSAFE_DRIVERS = sorted(set(GDAL_SKIP_DRIVERS) & set(pyogrio.list_drivers()))

GEOJSON_FORMATS = ("application/geo+json", "application/json", "json", "geojson")
GML_PREFERENCE = (
    "application/gml+xml; version=3.2",
    "text/xml; subtype=gml/3.2.1",
    "text/xml; subtype=gml/3.2",
    "application/gml+xml; version=3.1",
    "text/xml; subtype=gml/3.1.1",
)
_OGC_NS = ("http://www.opengis.net/wfs", "http://www.opengis.net/gml")
_MEMBER_TAGS = ("member", "featureMember", "featureMembers")
_ROOT_RE = re.compile(rb"<(?![?!])(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _child_text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return None


def root_name(content: bytes) -> str:
    """Lokaler Name des XML-Wurzelelements (leer bei Nicht-XML)."""
    m = _ROOT_RE.search(content[:4000])
    return m.group(1).decode("ascii", "replace") if m else ""


def check_exception(content: bytes) -> None:
    """OGC-ExceptionReport erkennen – auch bei HTTP 200."""
    if root_name(content) in ("ExceptionReport", "ServiceExceptionReport"):
        try:
            root = ET.fromstring(content)  # noqa: S314
            texts = [
                (t.text or "").strip() for t in root.iter() if _local(t.tag) in ("ExceptionText", "ServiceException")
            ]
            codes = [t.get("exceptionCode", "") for t in root.iter() if _local(t.tag) == "Exception"]
            msg = "; ".join(x for x in texts if x) or "ohne Text"
            code = ",".join(x for x in codes if x)
        except ET.ParseError:
            msg, code = content[:300].decode("utf-8", "replace"), ""
        raise SourceFailure("ogc_exception", f"WFS-ExceptionReport{f' ({code})' if code else ''}: {msg[:500]}")


@dataclass
class PageInfo:
    """Ausgewertete Antwortseite (GetFeature)."""

    count: int = 0  # gelieferte Objekte (unabhängig von ``numberReturned``)
    ids: list[str | None] = field(default_factory=list)  # Identität je Objekt: gml:id/GeoJSON-id, sonst Schlüssel
    keys: list[str | None] = field(default_factory=list)  # fachlicher Schlüssel je Objekt (Rohwerte)
    number_matched: int | None = None
    has_next: bool = False


def _ns_local(tag: str) -> tuple[str, str]:
    ns, _, local = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
    return ns, local


def _gml_id(el: ET.Element) -> str | None:
    for name, value in el.attrib.items():
        ns, local = _ns_local(name)
        if local == "id" and ns.startswith("http://www.opengis.net/gml"):
            return value
    return el.get("fid")


def _join_key(values: list[str | None]) -> str | None:
    return None if any(v is None for v in values) else "\x1f".join(v or "" for v in values)


def _require_feature_collection(tag: str) -> None:
    ns, local = _ns_local(tag)
    if local != "FeatureCollection" or not ns.startswith(_OGC_NS):
        raise SourceFailure("parse", f"Keine WFS-FeatureCollection (Wurzelelement {tag[:120]})")


def check_root(path: Path) -> None:
    """Nur das Wurzelelement einer gespeicherten GML-Seite prüfen (liest nur den Dateianfang).

    Für Rohdateien, die ``fetch()`` vor dem Speichern bereits vollständig geprüft hat.
    """
    try:
        with path.open("rb") as fh:
            for _, el in ET.iterparse(fh, events=("start",)):  # noqa: S314 – siehe Import
                _require_feature_collection(el.tag)
                return
    except ET.ParseError as exc:
        raise SourceFailure("parse", f"Rohdatei {path.name} nicht lesbar: {exc}") from exc
    raise SourceFailure("parse", f"Rohdatei {path.name} ohne Wurzelelement")


def _inspect_xml(content: bytes, key_fields: tuple[str, ...]) -> PageInfo:
    info = PageInfo()
    root: ET.Element | None = None
    depth = 0
    in_members = False
    key_vals: dict[str, str] = {}
    fid: str | None = None
    try:
        for ev, el in ET.iterparse(io.BytesIO(content), events=("start", "end")):  # noqa: S314 – siehe Import
            if ev == "start":
                depth += 1
                ns, local = _ns_local(el.tag)
                if depth == 1:
                    _require_feature_collection(el.tag)
                    root = el
                    matched = el.get("numberMatched", "")
                    info.number_matched = int(matched) if matched.isascii() and matched.isdigit() else None
                    info.has_next = bool(el.get("next"))
                elif depth == 2:
                    if local == "truncatedResponse":  # WFS 2.0.2: Dienst meldet selbst eine gekürzte Antwort
                        raise SourceFailure("possibly_truncated", "Dienst meldet wfs:truncatedResponse")
                    in_members = local in _MEMBER_TAGS
                elif depth == 3 and in_members:
                    info.count += 1
                    fid, key_vals = _gml_id(el), {}
                continue
            if depth == 4 and in_members:
                local = _ns_local(el.tag)[1]
                if local in key_fields:
                    key_vals[local] = (el.text or "").strip()
            elif depth == 3 and in_members:
                key = _join_key([key_vals.get(k) for k in key_fields]) if key_fields else None
                info.keys.append(key)
                info.ids.append(fid or (None if key is None else f"key:{key}"))
                el.clear()
            elif depth == 2 and root is not None:
                del root[:]  # Speicher: abgearbeitete Objekte verwerfen
            depth -= 1
    except ET.ParseError as exc:
        raise SourceFailure("parse", f"GML-Antwort nicht lesbar: {exc}") from exc
    return info


def _json_text(value: Any) -> str | None:
    """JSON-Wert als Text wie bei GML: ``null`` → None, Zahlen in der gelieferten Schreibweise."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=float)


def _load_feature_collection(content: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(content, parse_float=Decimal)
    except ValueError as exc:
        raise SourceFailure("parse", f"GeoJSON-Antwort nicht lesbar: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" or not isinstance(doc.get("features"), list):
        raise SourceFailure("parse", "Keine GeoJSON-FeatureCollection")
    if not all(isinstance(f, dict) for f in doc["features"]):
        raise SourceFailure("parse", "GeoJSON-FeatureCollection mit ungültigem Objekt")
    return doc


def _props(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties")
    return props if isinstance(props, dict) else {}


def _inspect_json(content: bytes, key_fields: tuple[str, ...]) -> PageInfo:
    doc = _load_feature_collection(content)
    info = PageInfo(count=len(doc["features"]))
    for f in doc["features"]:
        props = _props(f)
        key = _join_key([_json_text(props.get(k)) for k in key_fields]) if key_fields else None
        fid = None if f.get("id") is None else str(f["id"])
        info.keys.append(key)
        info.ids.append(fid or (None if key is None else f"key:{key}"))
    matched = doc.get("numberMatched", doc.get("totalFeatures"))
    info.number_matched = matched if isinstance(matched, int) and not isinstance(matched, bool) else None
    links = doc.get("links")
    info.has_next = isinstance(links, list) and any(isinstance(lk, dict) and lk.get("rel") == "next" for lk in links)
    return info


def inspect_page(content: bytes, is_json: bool, key_fields: tuple[str, ...] = ()) -> PageInfo:
    """Antwortseite streng prüfen und auswerten – vor dem Speichern und vor dem Lesen über GDAL.

    Anderes als eine FeatureCollection (z. B. eine OGR-VRT, die GDAL am Inhalt erkennen würde) →
    ``SourceFailure("parse")``. Kommentare zählen nicht als Objekte.
    """
    return _inspect_json(content, key_fields) if is_json else _inspect_xml(content, key_fields)


def count_members(content: bytes, is_json: bool) -> int:
    """Anzahl gelieferter Objekte einer Antwortseite (unabhängig von ``numberReturned``)."""
    return inspect_page(content, is_json).count


@dataclass
class FeatureTypeInfo:
    """Angaben zu einem FeatureType aus GetCapabilities."""

    name: str
    default_crs: str | None
    other_crs: list[str] = field(default_factory=list)
    wgs84_bbox: tuple[float, float, float, float] | None = None


@dataclass
class Capabilities:
    """Ausgewertete GetCapabilities-Antwort."""

    version: str | None
    title: str | None
    feature_types: dict[str, FeatureTypeInfo]
    output_formats: list[str]
    implements_paging: bool | None
    count_default: int | None
    implements_sorting: bool | None

    def find(self, type_name: str) -> FeatureTypeInfo | None:
        """FeatureType nach vollem Namen oder lokalem Teil (ohne Präfix)."""
        if type_name in self.feature_types:
            return self.feature_types[type_name]
        local = type_name.split(":")[-1]
        matches = [ft for n, ft in self.feature_types.items() if n.split(":")[-1] == local]
        return matches[0] if len(matches) == 1 else None


def parse_capabilities(content: bytes) -> Capabilities:
    """GetCapabilities (WFS 2.0/1.1) namensraumunabhängig auswerten."""
    check_exception(content)
    try:
        root = ET.fromstring(content)  # noqa: S314
    except ET.ParseError as exc:
        raise SourceFailure("parse", f"GetCapabilities nicht lesbar: {exc}") from exc
    if _local(root.tag) != "WFS_Capabilities":
        raise SourceFailure("parse", f"Keine WFS-Capabilities (Wurzelelement {_local(root.tag)})")
    title = None
    for el in root.iter():
        if _local(el.tag) == "ServiceIdentification":
            title = _child_text(el, "Title")
            break
    fts: dict[str, FeatureTypeInfo] = {}
    for el in root.iter():
        if _local(el.tag) != "FeatureType":
            continue
        name = _child_text(el, "Name")
        if not name:
            continue
        default = _child_text(el, "DefaultCRS") or _child_text(el, "DefaultSRS")
        others = [(c.text or "").strip() for c in el if _local(c.tag) in ("OtherCRS", "OtherSRS")]
        bbox = None
        for bb in _children(el, "WGS84BoundingBox"):
            lc, uc = _child_text(bb, "LowerCorner"), _child_text(bb, "UpperCorner")
            if lc and uc:
                x0, y0 = (float(v) for v in lc.split()[:2])
                x1, y1 = (float(v) for v in uc.split()[:2])
                bbox = (x0, y0, x1, y1)
        fts[name] = FeatureTypeInfo(name, default, others, bbox)
    formats: list[str] = []
    global_formats: list[str] = []
    paging = sorting = None
    count_default = None
    for om in root.iter():
        if _local(om.tag) == "OperationsMetadata":
            for param in _children(om, "Parameter"):  # global für alle Operationen
                if param.get("name", "").lower() == "outputformat":
                    global_formats += [(v.text or "").strip() for v in param.iter() if _local(v.tag) == "Value"]
    for op in root.iter():
        if _local(op.tag) == "Operation" and op.get("name") == "GetFeature":
            for param in _children(op, "Parameter"):
                if param.get("name", "").lower() == "outputformat":
                    formats += [(v.text or "").strip() for v in param.iter() if _local(v.tag) == "Value"]
        if _local(op.tag) == "Constraint":
            val = next(
                (((v.text or "").strip()) for v in op.iter() if _local(v.tag) in ("DefaultValue", "Value")), None
            )
            cname = op.get("name")
            if cname == "ImplementsResultPaging" and val:
                paging = val.upper() == "TRUE"
            elif cname == "ImplementsSorting" and val:
                sorting = val.upper() == "TRUE"
            elif cname == "CountDefault" and val and val.isdigit():
                count_default = int(val)
    return Capabilities(root.get("version"), title, fts, formats or global_formats, paging, count_default, sorting)


def parse_describe(content: bytes, type_name: str) -> dict[str, str]:
    """Felder (Name → XSD-Typ) eines FeatureTypes aus DescribeFeatureType.

    Manche Dienste (z. B. LGB ``vg_wfs``) liefern das Schema aller FeatureTypes, teils mit
    anonymen ``complexType``-Definitionen im Element – beides wird unterstützt.
    """
    check_exception(content)
    root = ET.fromstring(content)  # noqa: S314
    local = type_name.split(":")[-1]
    complex_types = {ct.get("name"): ct for ct in root if _local(ct.tag) == "complexType" and ct.get("name")}
    container: ET.Element | None = None
    for el in root:
        if _local(el.tag) == "element" and el.get("name") == local:
            ref = (el.get("type") or "").split(":")[-1]
            container = complex_types.get(ref) if ref else el
            break
    if container is None:
        container = complex_types.get(f"{local}Type")
    if container is None and len(complex_types) == 1:
        container = next(iter(complex_types.values()))
    if container is None:
        return {}
    fields: dict[str, str] = {}
    for el in container.iter():
        if el is container or _local(el.tag) != "element" or not el.get("name"):
            continue
        typ = el.get("type")
        if not typ:
            base = next((r.get("base") for r in el.iter() if _local(r.tag) == "restriction"), None)
            typ = base or "complex"
        fields[el.get("name", "")] = typ.split(":")[-1]
    return fields


def schema_drift(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Unterschiede zwischen zwei Feldlisten als lesbare Meldungen."""
    out = [f"Feld '{f}' neu" for f in new if f not in old]
    out += [f"Feld '{f}' entfallen" for f in old if f not in new]
    out += [f"Feld '{f}' Typ geändert: {old[f]} → {new[f]}" for f in new if f in old and old[f] != new[f]]
    return out


def equality_filter(conditions: dict[str, str], version: str) -> str:
    """Einfache Gleichheitsfilter als FES-/OGC-Filter-XML."""
    if version.startswith("2"):
        ns, prop = 'xmlns:fes="http://www.opengis.net/fes/2.0"', "fes:ValueReference"
        p = "fes"
    else:
        ns, prop = 'xmlns:ogc="http://www.opengis.net/ogc"', "ogc:PropertyName"
        p = "ogc"
    parts = [
        f"<{p}:PropertyIsEqualTo><{prop}>{escape(k)}</{prop}><{p}:Literal>{escape(v)}</{p}:Literal></{p}:PropertyIsEqualTo>"
        for k, v in conditions.items()
    ]
    body = parts[0] if len(parts) == 1 else f"<{p}:And>{''.join(parts)}</{p}:And>"
    return f"<{p}:Filter {ns}>{body}</{p}:Filter>"


def tile_count(bbox: tuple[float, float, float, float], size: float) -> int:
    """Anzahl der Kacheln, ohne sie anzulegen (Prüfung gegen ``max_pages``)."""
    x0, y0, x1, y1 = bbox
    return max(1, math.ceil((x1 - x0) / size)) * max(1, math.ceil((y1 - y0) / size))


def tiles(bbox: tuple[float, float, float, float], size: float) -> list[tuple[float, float, float, float]]:
    """Zerlegt einen Rahmen in Kacheln der Kantenlänge ``size``."""
    x0, y0, x1, y1 = bbox
    nx, ny = max(1, math.ceil((x1 - x0) / size)), max(1, math.ceil((y1 - y0) / size))
    return [
        (x0 + i * size, y0 + j * size, min(x1, x0 + (i + 1) * size), min(y1, y0 + (j + 1) * size))
        for j in range(ny)
        for i in range(nx)
    ]


def _float_coords(value: Any) -> Any:
    if isinstance(value, list):
        return [_float_coords(v) for v in value]
    return float(value) if isinstance(value, (int, Decimal)) else value


def _float_geom(geom: Any) -> Any:
    """GeoJSON-Geometrie mit ``Decimal``-Koordinaten (siehe ``_load_feature_collection``) für shapely."""
    if not isinstance(geom, dict):
        return geom
    if "geometries" in geom:
        return {**geom, "geometries": [_float_geom(g) for g in geom["geometries"] or []]}
    return {**geom, "coordinates": _float_coords(geom.get("coordinates"))}


def _shape(geom: Any) -> Any:
    if not isinstance(geom, dict):
        return None
    try:
        # 2D wie früher pyogrio.raw.read(force_2d=True): Zielspalten sind 2D, Hashes bleiben vergleichbar
        return shapely.force_2d(shapely.geometry.shape(_float_geom(geom)))
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, shapely.errors.GEOSException):
        return None  # ungültige Geometrie → Datensatzfehler in prepare_features


def read_geojson(path: Path) -> list[RawFeature]:
    """Liest eine GeoJSON-Rohdatei ohne GDAL; Eigenschaften werden selbst in Text umgewandelt."""
    return _geojson_features(_load_feature_collection(path.read_bytes()))


def _geojson_features(doc: dict[str, Any]) -> list[RawFeature]:
    out: list[RawFeature] = []
    for f in doc["features"]:
        props = {str(k): _json_text(v) for k, v in _props(f).items()}
        fid = None if f.get("id") is None else str(f["id"])
        out.append(RawFeature(props, _shape(f.get("geometry")), fid))
    return out


def read_features(path: Path, is_json: bool | None = None) -> list[RawFeature]:
    """Liest eine Rohdatei: GML über GDAL (Attribute als Text), GeoJSON ohne GDAL."""
    if is_json if is_json is not None else path.suffix.lower() in (".json", ".geojson"):
        return read_geojson(path)
    if _UNSAFE_DRIVERS:
        raise SourceFailure(
            "config",
            f"GDAL-Treiber {', '.join(_UNSAFE_DRIVERS)} aktiv: pyogrio wurde vor geoimport importiert, "
            "GDAL_SKIP greift nicht – Rohdateien werden nicht gelesen",
        )
    try:
        meta, _, geoms, field_data = pyogrio.raw.read(path, force_2d=True, WRITE_GFS="NO")
    except DataSourceError as exc:
        raise SourceFailure("parse", f"Rohdatei {path.name} nicht lesbar: {exc}") from exc
    names = list(meta["fields"])
    shapes = shapely.from_wkb(geoms) if geoms is not None else [None] * (len(field_data[0]) if field_data else 0)
    out: list[RawFeature] = []
    for i, geom in enumerate(shapes):
        props = {n: (None if col[i] is None else str(col[i])) for n, col in zip(names, field_data, strict=True)}
        out.append(RawFeature(props, geom, props.get("gml_id")))
    return out


@register_source("wfs", WfsSource)
class WfsSourcePlugin(Source):
    """Plugin für ``type: wfs``."""

    cfg: WfsSource

    # ------------------------------------------------------------------ Hilfen
    def location_url(self) -> str | None:  # noqa: D102
        return self.cfg.wfs.url

    def _get(self, params: dict[str, Any]) -> bytes:
        try:
            response = self.ctx.http.get(self.cfg.wfs.url, params={**params, **self.cfg.wfs.params})
        except HttpError as exc:
            raise SourceFailure(exc.kind, str(exc), exc.status, exc.url) from exc
        content = response.content
        check_exception(content)
        return content

    def _base(self, request: str) -> dict[str, Any]:
        return {"SERVICE": "WFS", "VERSION": self.cfg.wfs.version, "REQUEST": request}

    def capabilities(self) -> Capabilities:
        """GetCapabilities laden und auswerten."""
        return parse_capabilities(self._get(self._base("GetCapabilities")))

    def describe(self) -> dict[str, str]:
        """DescribeFeatureType laden und auswerten."""
        p = self._base("DescribeFeatureType")
        p["TYPENAMES" if self.cfg.wfs.version.startswith("2") else "TYPENAME"] = self.cfg.wfs.type_name
        return parse_describe(self._get(p), self.cfg.wfs.type_name)

    def _crs(self, ft: FeatureTypeInfo) -> str:
        try:
            if self.cfg.wfs.request_crs:
                return normalize_crs(self.cfg.wfs.request_crs)
            if ft.default_crs:
                return normalize_crs(ft.default_crs)
        except CrsError as exc:
            raise SourceFailure("config", str(exc)) from exc
        raise SourceFailure("config", "Kein CRS: request_crs angeben (Dienst nennt kein Default-CRS)")

    def _format(self, caps: Capabilities) -> tuple[str | None, bool]:
        """Ausgabeformat und ob es GeoJSON ist."""
        wanted = self.cfg.wfs.output_format
        if wanted != "auto":
            return wanted, any(f in wanted.lower() for f in ("json",))
        # GML zuerst: dort liest GDAL alle Attribute als Text; GeoJSON nur, wenn kein GML angeboten wird
        lower = {f.lower(): f for f in caps.output_formats}
        for f in GML_PREFERENCE:
            if f in lower:
                return lower[f], False
        for f in GEOJSON_FORMATS:
            if f in lower:
                return lower[f], True
        return None, False

    def _strategy(self, caps: Capabilities) -> str:
        s = self.cfg.wfs.paging.strategy
        if s != "auto":
            return s
        if caps.implements_paging:
            return "startindex"
        if caps.count_default is None or caps.count_default >= 10_000:
            return "none"
        return "bbox_tiles"

    def _getfeature_params(
        self, crs: str, fmt: str | None, bbox: tuple[float, float, float, float] | None, sort_by: str | None = None
    ) -> dict[str, str]:
        w = self.cfg.wfs
        p = self._base("GetFeature")
        v2 = w.version.startswith("2")
        p["TYPENAMES" if v2 else "TYPENAME"] = w.type_name
        p["SRSNAME"] = epsg_urn(crs) if v2 else crs
        if fmt:
            p["OUTPUTFORMAT"] = fmt
        flt = w.filter
        if isinstance(flt, dict):
            flt = equality_filter(flt, w.version)
        if flt and bbox:
            raise SourceFailure("config", "bbox und filter gleichzeitig: BBOX bitte in den OGC-Filter aufnehmen")
        if flt:
            p["FILTER"] = flt
        elif bbox:
            # WFS 2.0 mit URN-CRS: Achsenreihenfolge laut EPSG, bei geografischen CRS also Breite vor Länge
            order = (bbox[1], bbox[0], bbox[3], bbox[2]) if v2 and is_geographic(crs) else bbox
            p["BBOX"] = ",".join(f"{v:.6f}" for v in order) + "," + (epsg_urn(crs) if v2 else crs)
        if sort_by or w.sort_by:
            p["SORTBY"] = sort_by or w.sort_by or ""
        return p

    def _tile_plan(
        self, ft: FeatureTypeInfo, crs: str, bbox: tuple[float, float, float, float] | None
    ) -> tuple[tuple[float, float, float, float], list[tuple[float, float, float, float]]]:
        """Kachelrahmen und Kacheln – geprüft gegen ``paging.max_pages``, bevor eine Kachel angefragt wird.

        Ohne ``bbox`` bestimmt der Dienst die Ausdehnung; sie wird auf ``crs.plausibility_bbox_4326``
        beschnitten, damit eine Welt-Ausdehnung nicht Millionen Anfragen auslöst (SECURITY_REVIEW S4).
        """
        paging = self.cfg.wfs.paging
        if bbox is None:
            if not ft.wgs84_bbox:
                raise SourceFailure("config", "bbox_tiles braucht 'bbox' (Dienst nennt keine Ausdehnung)")
            sx0, sy0, sx1, sy1 = ft.wgs84_bbox
            px0, py0, px1, py1 = self.ctx.pipeline.crs.plausibility_bbox_4326
            clipped = (max(sx0, px0), max(sy0, py0), min(sx1, px1), min(sy1, py1))
            if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
                raise SourceFailure(
                    "config",
                    f"Ausdehnung des Dienstes {ft.wgs84_bbox} liegt außerhalb von crs.plausibility_bbox_4326 – "
                    "wfs.bbox angeben",
                )
            try:
                bbox = transform_bbox(clipped, "EPSG:4326", crs)
            except CrsError as exc:
                raise SourceFailure("config", f"bbox_tiles: {exc} – wfs.bbox angeben") from exc
        size = paging.tile_size_m / 111_000 if is_geographic(crs) else paging.tile_size_m
        try:
            count = tile_count(bbox, size)
        except (OverflowError, ValueError) as exc:
            raise SourceFailure("config", f"bbox_tiles: Rahmen {bbox} unbrauchbar – wfs.bbox prüfen") from exc
        if count > paging.max_pages:
            raise SourceFailure(
                "paging",
                f"{count} Kacheln > paging.max_pages {paging.max_pages} – tile_size_m vergrößern, wfs.bbox "
                "einschränken oder max_pages erhöhen",
            )
        return bbox, tiles(bbox, size)

    def _hits_detail(self, crs: str, bbox: tuple[float, float, float, float] | None) -> tuple[int | None, str]:
        """Trefferzahl per ``RESULTTYPE=hits`` und, falls sie fehlt, der Grund."""
        p = self._getfeature_params(crs, None, bbox)
        p["RESULTTYPE"] = "hits"
        try:
            content = self._get(p)
        except SourceFailure as exc:
            log.info("RESULTTYPE=hits nicht verfügbar: %s", exc)
            return None, f"Anfrage fehlgeschlagen ({exc.kind}: {str(exc)[:200]})"
        m = re.search(rb'number(?:Matched|OfFeatures)="(\d+)"', content[:4000])
        if m:
            return int(m.group(1)), ""
        return None, "Dienst nennt keine Zahl (numberMatched fehlt oder 'unknown')"

    def hits(self, crs: str, bbox: tuple[float, float, float, float] | None) -> int | None:
        """Trefferzahl per ``RESULTTYPE=hits`` (``None``, wenn der Dienst sie nicht nennt)."""
        return self._hits_detail(crs, bbox)[0]

    # ------------------------------------------------------------------ Plugin-API
    def probe(self) -> dict[str, Any]:  # noqa: D102
        caps = self.capabilities()
        ft = caps.find(self.cfg.wfs.type_name)
        out: dict[str, Any] = {
            "url": self.cfg.wfs.url,
            "titel": caps.title,
            "version": caps.version,
            "featuretype_vorhanden": ft is not None,
            "featuretypes": sorted(caps.feature_types)[:50],
            "ausgabeformate": caps.output_formats[:20],
            "implements_result_paging": caps.implements_paging,
            "count_default": caps.count_default,
        }
        if ft:
            crs = self._crs(ft)
            out |= {
                "default_crs": ft.default_crs,
                "request_crs": crs,
                "paging_strategie": self._strategy(caps),
                "treffer": self.hits(crs, self.cfg.wfs.bbox),
                "felder": self.describe(),
            }
        return out

    def fetch(self) -> FetchResult:  # noqa: D102
        w = self.cfg.wfs
        caps = self.capabilities()
        ft = caps.find(w.type_name)
        if ft is None:
            raise SourceFailure(
                "featuretype_missing",
                f"FeatureType '{w.type_name}' nicht in GetCapabilities (vorhanden: {', '.join(sorted(caps.feature_types)[:15])})",
            )
        crs = self._crs(ft)
        fmt, is_json = self._format(caps)
        strategy = self._strategy(caps)
        warnings: list[PipelineWarning] = []

        # Schema prüfen
        fields: dict[str, str] = {}
        try:
            fields = self.describe()
        except (SourceFailure, ET.ParseError) as exc:
            warnings.append(PipelineWarning("schema_check_skipped", f"DescribeFeatureType nicht auswertbar: {exc}"))
        if fields:
            needed = mapping_fields(self.cfg.mapping) | set(self.cfg.key.fields) | set(self.cfg.guards.required)
            missing = sorted(f for f in needed if f not in fields)
            if missing:
                raise SourceFailure("schema_mismatch", f"Gemappte Felder fehlen im Dienst: {', '.join(missing)}")
            old = self.ctx.state.load().get("schema")
            if old:
                for msg in schema_drift(old, fields):
                    warnings.append(PipelineWarning("schema_drift", msg))

        bbox: tuple[float, float, float, float] | None = tuple(w.bbox) if w.bbox else None  # type: ignore[assignment]
        extent = bbox
        tile_list: list[tuple[float, float, float, float]] = []
        if strategy == "bbox_tiles":
            extent, tile_list = self._tile_plan(ft, crs, bbox)
        # Stabile Reihenfolge beim Blättern (H5): nach dem ersten Schlüsselfeld sortieren, wenn der Dienst kann
        sort = w.sort_by
        if strategy == "startindex" and not sort and caps.implements_sorting:
            sort = self.cfg.business_key[0]
        # Bei Kacheln die Trefferzahl des ganzen Kachelrahmens: deckt auch von Kacheln verschluckte Objekte auf
        expected, hits_problem = self._hits_detail(crs, extent)
        key_fields = tuple(self.cfg.key.fields)
        self.ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        ext = "json" if is_json else "gml"
        artifacts: list[RawArtifact] = []
        seen_ids: set[str] = set()  # verschiedene Objekte über alle Seiten/Kacheln
        anonymous = 0  # Objekte ohne gml:id und ohne Schlüssel (nicht unterscheidbar)

        def inspect(content: bytes) -> PageInfo:
            nonlocal anonymous
            page = inspect_page(content, is_json, key_fields)
            anonymous += sum(1 for i in page.ids if i is None)
            return page

        def save(content: bytes, label: str, info: dict[str, Any]) -> RawArtifact:
            import hashlib

            path = self.ctx.raw_dir / f"{label}.{ext}"
            path.write_bytes(content)
            art = RawArtifact(path, hashlib.sha256(content).hexdigest(), None, info)
            artifacts.append(art)
            return art

        def throttle() -> None:
            if w.paging.throttle_ms:
                time.sleep(w.paging.throttle_ms / 1000)

        total = 0
        if strategy == "none":
            content = self._get(self._getfeature_params(crs, fmt, bbox, sort))
            page = inspect(content)
            n = page.count
            seen_ids.update(i for i in page.ids if i)
            save(content, "page_0001", {"features": n})
            total = n
            if expected is None and page.number_matched is not None:
                expected = page.number_matched
            if caps.count_default and n >= caps.count_default:
                msg = f"{n} Objekte = CountDefault {caps.count_default} des Dienstes – Antwort vermutlich abgeschnitten"
                if self.cfg.guards.strict_count:
                    raise SourceFailure("possibly_truncated", msg + " (paging.strategy bbox_tiles verwenden)")
                warnings.append(PipelineWarning("possibly_truncated", msg))
        elif strategy == "startindex":
            # Mehr als CountDefault liefert der Dienst je Seite nicht
            size = min(w.paging.page_size, caps.count_default) if caps.count_default else w.paging.page_size
            start, page_no = 0, 1
            uses_next = False
            while True:
                p = self._getfeature_params(crs, fmt, bbox, sort)
                p["COUNT" if w.version.startswith("2") else "MAXFEATURES"] = str(size)
                p["STARTINDEX"] = str(start)
                content = self._get(p)
                page = inspect(content)
                n = page.count
                if n == 0:  # Ende erst bei einer leeren Seite – eine kurze Seite kann eine Obergrenze des Dienstes sein
                    if page_no == 1:
                        save(content, f"page_{page_no:04d}", {"features": 0, "startindex": start})
                    break
                ids = {i for i in page.ids if i}
                if ids and ids <= seen_ids:
                    raise SourceFailure(
                        "paging", f"Seite {page_no} wiederholt eine frühere Seite – STARTINDEX wird ignoriert"
                    )
                # Identität = gml:id/GeoJSON-id, nur ohne ID der Schlüssel. Gleicher Schlüssel bei verschiedenen
                # IDs ist eine Dublette in den Daten (duplicate_key in parse) – wie innerhalb einer Seite.
                dup = sorted(ids & seen_ids)
                if dup:
                    raise SourceFailure(
                        "incomplete",
                        f"Seite {page_no} enthält {len(dup)} Objekt(e) früherer Seiten "
                        f"(z. B. {dup[0].replace(chr(31), '|')!r}) – Reihenfolge des Dienstes nicht stabil "
                        f"({'SORTBY ' + sort if sort else 'ohne SORTBY'}); Lieferung unvollständig",
                    )
                seen_ids |= ids
                if expected is None and page_no == 1 and page.number_matched is not None:
                    expected = page.number_matched
                save(content, f"page_{page_no:04d}", {"features": n, "startindex": start})
                total += n
                uses_next = uses_next or page.has_next
                if expected is not None and len(seen_ids) + anonymous >= expected:
                    break
                if uses_next and not page.has_next:  # WFS 2.0: Dienst nennt Folgeseiten, diese ist die letzte
                    break
                start += n
                page_no += 1
                if page_no > w.paging.max_pages:
                    raise SourceFailure("paging", f"Mehr als {w.paging.max_pages} Seiten – Abbruch")
                throttle()
            if len(artifacts) > 1 and not sort:
                warnings.append(
                    PipelineWarning(
                        "unsorted_paging",
                        "Seitenweiser Abruf ohne SORTBY (Dienst meldet kein ImplementsSorting, wfs.sort_by "
                        "nicht gesetzt): Überschneidungen werden erkannt, Lücken nur über die Trefferzahl",
                    )
                )
        else:  # bbox_tiles
            for i, tile in enumerate(tile_list, start=1):
                content = self._get(self._getfeature_params(crs, fmt, tile))
                page = inspect(content)
                n = page.count
                if caps.count_default and n >= caps.count_default:
                    raise SourceFailure(
                        "possibly_truncated", f"Kachel {i}: {n} Objekte = CountDefault – tile_size_m verkleinern"
                    )
                seen_ids.update(x for x in page.ids if x)  # Objekte an Kachelgrenzen zählen einmal
                if n:
                    save(content, f"tile_{i:04d}", {"features": n, "bbox": tile})
                total += n
                throttle()

        unique = len(seen_ids) + anonymous
        if expected is None:
            msg = f"Trefferzahl (RESULTTYPE=hits) nicht verfügbar: {hits_problem} – Vollständigkeit nicht prüfbar"
            if self.cfg.guards.strict_count:
                raise SourceFailure("possibly_truncated", msg + " (guards.strict_count: false importiert mit Warnung)")
            warnings.append(PipelineWarning("count_unverified", msg))
        elif unique != expected:
            msg = f"{unique} verschiedene Objekte erhalten ({total} geliefert), der Dienst meldet per RESULTTYPE=hits {expected}"
            # Kacheln: mehr als gemeldet ist kein Verlust (z. B. gml:id je Antwort neu vergeben)
            if self.cfg.guards.strict_count and not (strategy == "bbox_tiles" and unique > expected):
                raise SourceFailure("incomplete", "Unvollständige Lieferung: " + msg)
            warnings.append(PipelineWarning("count_mismatch", msg))
        event(
            log,
            "wfs_fetched",
            f"{total} Objekte in {len(artifacts)} Datei(en), Strategie {strategy}",
            strategy=strategy,
            features=total,
            files=len(artifacts),
        )
        result = FetchResult(
            artifacts,
            info={
                "request_crs": crs,
                "output_format": fmt or "Standard",
                "paging": strategy,
                "hits": expected,
                "features_delivered": total,
                "features_unique": unique,
                "sort_by": sort,
                "is_json": is_json,
            },
            warnings=warnings,
            state_updates={"schema": fields} if fields else {},
        )
        prev_sha = self.ctx.state.load().get("raw_sha256")
        result.state_updates["raw_sha256"] = result.combined_sha256
        if self.cfg.skip_if_unchanged and self.ctx.use_conditional_requests and prev_sha == result.combined_sha256:
            result.unchanged = True
        return result

    def parse(self, fetched: FetchResult) -> ParseResult:  # noqa: D102
        raw: list[RawFeature] = []
        for art in fetched.artifacts:
            if fetched.info.get("is_json", art.path.suffix == ".json"):
                # ein Durchgang prüft (FeatureCollection) und liest
                raw.extend(_geojson_features(_load_feature_collection(art.path.read_bytes())))
                continue
            count = art.info.get("features")
            if isinstance(count, int):
                check_root(art.path)  # volle Prüfung lief in fetch() vor dem Speichern
            else:  # Rohdatei ohne Angaben (Demo, Wiederherstellung) → vollständig prüfen
                count = inspect_page(art.path.read_bytes(), False).count
            if count == 0:
                continue  # leere Seite: hat in GDAL keinen Layer
            raw.extend(read_features(art.path, False))
        crs = fetched.info.get("request_crs") or normalize_crs(self.cfg.wfs.request_crs or "EPSG:4326")
        features, errors = prepare_features(
            raw,
            key=self.cfg.key,
            mapping=self.cfg.mapping,
            required=self.cfg.guards.required,
            geometry_types=self.cfg.guards.geometry_types,
            src_crs=crs,
            pipeline=self.ctx.pipeline,
            dedupe_identical=fetched.info.get("paging") == "bbox_tiles",
        )
        return ParseResult(features=features, errors=errors, fetched=len(raw), info=dict(fetched.info))
