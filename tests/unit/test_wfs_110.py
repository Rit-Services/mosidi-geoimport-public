"""Bieterantwort 67: WFS 2.0.0 und 1.1.0 müssen unterstützt sein (Antwort 69: Abruf moderat und sequentiell).

Testdienst nach WFS 1.1.0: Namensraum ``http://www.opengis.net/wfs``, GML 3.1.1, Objekte in
``gml:featureMember``, ``numberOfFeatures`` statt ``numberMatched``, Parameter ``TYPENAME`` und ``MAXFEATURES``
(``STARTINDEX`` als verbreitete Erweiterung, z. B. GeoServer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from geoimport.config.models import HttpSettings, PipelineConfig, WfsSource
from geoimport.http import HttpClient
from geoimport.sources.base import SourceContext, SourceFailure, StateStore
from geoimport.sources.wfs import WfsSourcePlugin

CAPS_110 = b"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs" xmlns:ows="http://www.opengis.net/ows"
  xmlns:ogc="http://www.opengis.net/ogc" version="1.1.0">
 <ows:ServiceIdentification><ows:Title>Testdienst 1.1.0</ows:Title></ows:ServiceIdentification>
 <ows:OperationsMetadata>
  <ows:Operation name="GetFeature">
   <ows:Parameter name="outputFormat"><ows:Value>text/xml; subtype=gml/3.1.1</ows:Value></ows:Parameter>
   <ows:Parameter name="resultType"><ows:Value>results</ows:Value><ows:Value>hits</ows:Value></ows:Parameter>
  </ows:Operation>
 </ows:OperationsMetadata>
 <wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>app:gem</wfs:Name><wfs:Title>Gemeinden</wfs:Title>
  <wfs:DefaultSRS>urn:ogc:def:crs:EPSG::25833</wfs:DefaultSRS>
  <ows:WGS84BoundingBox><ows:LowerCorner>11.2 51.3</ows:LowerCorner><ows:UpperCorner>14.8 53.6</ows:UpperCorner>
  </ows:WGS84BoundingBox></wfs:FeatureType></wfs:FeatureTypeList>
</wfs:WFS_Capabilities>"""


def _member(i: int) -> str:
    return (
        f'<gml:featureMember><app:gem gml:id="gem.{i}"><app:ags>{12000000 + i}</app:ags>'
        f"<app:gem_name>Gemeinde {i}</app:gem_name><app:geom>"
        f'<gml:Point srsName="urn:ogc:def:crs:EPSG::25833"><gml:pos>{370000 + i} 5800000</gml:pos></gml:Point>'
        "</app:geom></app:gem></gml:featureMember>"
    )


@dataclass
class Wfs110:
    """Antwortet wie ein WFS 1.1.0; ``cap`` = Obergrenze je Antwort (z. B. maxFeatures des Servers)."""

    n: int = 1200
    cap: int = 10**9
    requests: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k.upper(): v for k, v in request.url.params.items()}
        self.requests.append(q)
        if q.get("VERSION") != "1.1.0":
            return httpx.Response(400, content=b"nur 1.1.0")
        if q["REQUEST"] == "GetCapabilities":
            return httpx.Response(200, content=CAPS_110)
        if q["REQUEST"] == "DescribeFeatureType":
            return httpx.Response(404)
        assert "TYPENAMES" not in q and "COUNT" not in q, q  # Parameter von WFS 2.0
        if q.get("RESULTTYPE") == "hits":
            return httpx.Response(
                200,
                content=f'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" numberOfFeatures="{self.n}"/>'.encode(),
            )
        start = int(q.get("STARTINDEX", 0))
        size = min(int(q.get("MAXFEATURES", 10**9)), self.cap)
        ids = list(range(self.n))[start : start + size]
        body = (
            '<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" xmlns:gml="http://www.opengis.net/gml" '
            f'xmlns:app="http://x/app" numberOfFeatures="{len(ids)}">'  # 1.1.0: Zahl dieser Antwort, nicht gesamt
            + "".join(_member(i) for i in ids)
            + "</wfs:FeatureCollection>"
        )
        return httpx.Response(200, content=body.encode())

    def getfeature(self) -> list[dict[str, str]]:
        return [r for r in self.requests if r.get("REQUEST") == "GetFeature" and "RESULTTYPE" not in r]


def _plugin(tmp_path: Path, server: Wfs110, **paging: Any) -> WfsSourcePlugin:
    src = WfsSource.model_validate(
        {
            "id": "w11", "type": "wfs", "title": "t", "license": "l", "business_key": ["ags"],
            "target": {"dataset": "d"}, "guards": {"geometry_types": None},
            "mapping": {"geometry": "geom", "name": "gem_name", "attributes": {"ags": "ags"}},
            "wfs": {
                "url": "https://x/wfs", "version": "1.1.0", "type_name": "app:gem", "request_crs": "EPSG:25833",
                "paging": {"page_size": 500, "throttle_ms": 0} | paging,
            },
        }
    )  # fmt: skip
    http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(server))
    ctx = SourceContext(PipelineConfig(), http, "r", tmp_path / "raw", StateStore(tmp_path / "s", "w11"))
    return WfsSourcePlugin(src, ctx)


def test_getfeature_110_with_paging(tmp_path: Path) -> None:
    server = Wfs110()
    plugin = _plugin(tmp_path, server, strategy="startindex")
    fetched = plugin.fetch()
    parsed = plugin.parse(fetched)
    assert len(parsed.features) == 1200 and not parsed.errors
    assert {f.key for f in parsed.features} == {str(12000000 + i) for i in range(1200)}
    f0 = next(f for f in parsed.features if f.key == "12000000")
    assert f0.name == "Gemeinde 0" and f0.geometry is not None and 13.0 < f0.geometry.x < 13.2  # nach EPSG:4326
    pages = server.getfeature()
    assert [(r["STARTINDEX"], r["MAXFEATURES"]) for r in pages] == [("0", "500"), ("500", "500"), ("1000", "500")]
    assert all(r["TYPENAME"] == "app:gem" and r["SRSNAME"] == "EPSG:25833" for r in pages)
    assert all(r["OUTPUTFORMAT"] == "text/xml; subtype=gml/3.1.1" for r in pages)


def test_maxfeatures_cap_without_paging_is_incomplete(tmp_path: Path) -> None:
    """Obergrenze des Dienstes bei einem Abruf ohne Paging: nicht still abschneiden (Trefferzahl 1.1.0)."""
    server = Wfs110(cap=500)
    plugin = _plugin(tmp_path, server, strategy="none")
    with pytest.raises(SourceFailure) as exc:
        plugin.fetch()
    assert exc.value.kind in ("incomplete", "possibly_truncated")
    assert len(server.getfeature()) == 1


def test_requests_are_sequential(tmp_path: Path) -> None:
    """Antwort 69: moderat abrufen – eine Anfrage nach der anderen, Seitengröße aus der Konfiguration."""
    server = Wfs110(n=1000)
    _plugin(tmp_path, server, strategy="startindex", page_size=250).fetch()
    starts = [int(r["STARTINDEX"]) for r in server.getfeature()]
    assert starts == sorted(starts) == [0, 250, 500, 750]  # Trefferzahl erreicht → Ende
