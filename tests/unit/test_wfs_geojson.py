"""WFS mit GeoJSON-Ausgabe: alles bleibt Text wie bei GML (null → None, 123 → "123"); ``auto`` bevorzugt GML."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from geoimport.config.models import HttpSettings, PipelineConfig, WfsSource
from geoimport.http import HttpClient
from geoimport.sources.base import SourceContext, StateStore
from geoimport.sources.wfs import Capabilities, WfsSourcePlugin, inspect_page, read_features
from tests.conftest import FIX

GEOJSON = FIX / "wfs" / "geojson_int_null.json"


def test_read_geojson_keeps_json_values_as_text() -> None:
    feats = read_features(GEOJSON)
    a, b, c = (f.props for f in feats)
    assert a["ags"] == "12064000" and a["einwohner"] == "5432" and a["flaeche"] == "12.50"
    assert a["aktiv"] == "true" and json.loads(a["extra"]) == {"a": 1}
    assert b["einwohner"] is None and b["flaeche"] == "3" and b["aktiv"] == "false"
    assert c["ags"] is None and c["flaeche"] is None
    assert [f.fid for f in feats] == ["gem.1", "gem.2", "gem.3"]
    assert feats[0].geometry is not None and feats[0].geometry.geom_type == "Polygon"
    assert feats[1].geometry is not None and feats[1].geometry.coords[0] == (14.5, 52.4)
    assert feats[2].geometry is None


def test_inspect_geojson_page() -> None:
    page = inspect_page(GEOJSON.read_bytes(), True, ("ags",))
    assert page.count == 3 and page.number_matched == 3
    assert page.ids == ["gem.1", "gem.2", "gem.3"] and page.keys == ["12064000", "12064001", None]


def _source(tmp_path: Path, output_format: str, handler: object) -> WfsSourcePlugin:
    src = WfsSource.model_validate(
        {
            "id": "tt", "type": "wfs", "title": "t", "license": "l", "business_key": ["ags"],
            "target": {"dataset": "d"}, "guards": {"geometry_types": None},
            "wfs": {"url": "https://x/wfs", "type_name": "app:gem", "output_format": output_format,
                    "paging": {"strategy": "none"}, "request_crs": "EPSG:4326"},
            "mapping": {"name": "gem_name", "attributes": {"einwohner": "einwohner"}},
        }
    )  # fmt: skip
    http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    ctx = SourceContext(PipelineConfig(), http, "r", tmp_path / "raw", StateStore(tmp_path / "s", "tt"))
    return WfsSourcePlugin(src, ctx)


def test_auto_prefers_gml_over_geojson(tmp_path: Path) -> None:
    plugin = _source(tmp_path, "auto", lambda r: httpx.Response(404))
    caps = Capabilities(
        "2.0.0", None, {}, ["application/json", "application/gml+xml; version=3.2", "text/xml"], True, None, True
    )
    assert plugin._format(caps) == ("application/gml+xml; version=3.2", False)
    only_json = Capabilities("2.0.0", None, {}, ["application/json"], True, None, True)
    assert plugin._format(only_json) == ("application/json", True)


def test_fetch_and_parse_geojson(tmp_path: Path) -> None:
    caps = b"""<?xml version="1.0"?><wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0"><wfs:FeatureTypeList><wfs:FeatureType>
<wfs:Name>app:gem</wfs:Name><wfs:DefaultCRS>urn:ogc:def:crs:EPSG::4326</wfs:DefaultCRS></wfs:FeatureType>
</wfs:FeatureTypeList></wfs:WFS_Capabilities>"""

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k.upper(): v for k, v in request.url.params.items()}
        if q.get("REQUEST") == "GetCapabilities":
            return httpx.Response(200, content=caps)
        if q.get("REQUEST") == "GetFeature":
            return httpx.Response(200, content=GEOJSON.read_bytes())  # nennt numberMatched selbst
        return httpx.Response(404)

    plugin = _source(tmp_path, "application/json", handler)
    fetched = plugin.fetch()
    assert fetched.info["is_json"] and fetched.artifacts[0].path.suffix == ".json"
    parsed = plugin.parse(fetched)
    by_key = {f.key: f for f in parsed.features}
    assert set(by_key) == {"12064000", "12064001"}  # nicht "12064000.0"
    assert by_key["12064000"].attributes["einwohner"] in ("5432", 5432)
    assert by_key["12064001"].attributes["einwohner"] is None  # nicht "nan"
    assert len(parsed.errors) == 1  # Objekt ohne Schlüssel


def test_geojson_3d_coordinates_forced_to_2d(tmp_path: Path) -> None:
    # wie früher pyogrio.raw.read(force_2d=True): Zielspalten sind 2D, Hashes bleiben vergleichbar
    path = tmp_path / "page_0001.json"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"ags": "1"}, "geometry": {"type": "Point", "coordinates": [13.4, 52.5, 0]}},
                    {"type": "Feature", "properties": {"ags": "2"}, "geometry": {"type": "GeometryCollection", "geometries": [
                        {"type": "LineString", "coordinates": [[13.4, 52.5, 1], [13.5, 52.6, 2]]}]}},
                ],
            }
        )
    )  # fmt: skip
    a, b = (f.geometry for f in read_features(path))
    assert a is not None and not a.has_z and a.wkt == "POINT (13.4 52.5)"
    assert b is not None and not b.has_z
