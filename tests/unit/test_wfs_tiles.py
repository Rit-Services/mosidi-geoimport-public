"""S4: Der Dienst darf die Zahl der Kachel-Anfragen nicht bestimmen (Welt-Ausdehnung, fehlendes max_pages)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from geoimport.config.models import HttpSettings, PipelineConfig, WfsSource
from geoimport.http import HttpClient
from geoimport.sources.base import SourceContext, SourceFailure, StateStore
from geoimport.sources.wfs import WfsSourcePlugin
from geoimport.transform.crs import transform_bbox

ROOT = Path(__file__).resolve().parents[2]
GUARD = 300  # Testdienst bricht danach ab, damit ein Fehler nicht Millionen Anfragen auslöst


class TileServer:
    """WFS ohne Paging, CountDefault 1000, Ausdehnung laut Capabilities frei wählbar."""

    def __init__(self, wgs84: tuple[float, float, float, float]) -> None:
        self.wgs84 = wgs84
        self.requests: list[dict[str, str]] = []

    def caps(self) -> bytes:
        x0, y0, x1, y1 = self.wgs84
        return f"""<?xml version="1.0"?><wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0"><ows:OperationsMetadata>
<ows:Constraint name="ImplementsResultPaging"><ows:NoValues/><ows:DefaultValue>FALSE</ows:DefaultValue></ows:Constraint>
<ows:Constraint name="CountDefault"><ows:NoValues/><ows:DefaultValue>1000</ows:DefaultValue></ows:Constraint>
</ows:OperationsMetadata><wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>app:gem</wfs:Name>
<wfs:DefaultCRS>urn:ogc:def:crs:EPSG::25833</wfs:DefaultCRS><ows:WGS84BoundingBox>
<ows:LowerCorner>{x0} {y0}</ows:LowerCorner><ows:UpperCorner>{x1} {y1}</ows:UpperCorner></ows:WGS84BoundingBox>
</wfs:FeatureType></wfs:FeatureTypeList></wfs:WFS_Capabilities>""".encode()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k.upper(): v for k, v in request.url.params.items()}
        self.requests.append(q)
        if len(self.requests) > GUARD:
            raise RuntimeError("Testdienst: zu viele Anfragen")
        if q.get("REQUEST") == "GetCapabilities":
            return httpx.Response(200, content=self.caps())
        if q.get("RESULTTYPE") == "hits":
            return httpx.Response(
                200, content=b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" numberMatched="0"/>'
            )
        if q.get("REQUEST") == "GetFeature":
            return httpx.Response(200, content=b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"/>')
        return httpx.Response(404)

    def getfeature(self) -> list[dict[str, str]]:
        return [r for r in self.requests if r.get("REQUEST") == "GetFeature"]


def _plugin(tmp_path: Path, server: TileServer, **paging: Any) -> WfsSourcePlugin:
    bbox = paging.pop("bbox", None)
    src = WfsSource.model_validate(
        {
            "id": "tt", "type": "wfs", "title": "t", "license": "l", "business_key": ["ags"],
            "target": {"dataset": "d"},
            "wfs": {"url": "https://x/wfs", "type_name": "app:gem", "bbox": bbox,
                    "paging": {"strategy": "auto", "throttle_ms": 0} | paging},
        }
    )  # fmt: skip
    http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(server))
    ctx = SourceContext(PipelineConfig(), http, "r", tmp_path / "raw", StateStore(tmp_path / "s", "tt"))
    return WfsSourcePlugin(src, ctx)


def test_world_extent_too_many_tiles_fails_before_requests(tmp_path: Path) -> None:
    server = TileServer((-180, -90, 180, 90))
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, server, max_pages=100).fetch()
    assert exc.value.kind == "paging" and "Kacheln" in str(exc.value) and "max_pages" in str(exc.value)
    assert server.getfeature() == []  # keine einzige GetFeature-Anfrage


def test_world_extent_clipped_to_plausibility_bbox(tmp_path: Path) -> None:
    server = TileServer((-180, -90, 180, 90))
    fetched = _plugin(tmp_path, server, max_pages=100, tile_size_m=200_000).fetch()
    plaus = transform_bbox(PipelineConfig().crs.plausibility_bbox_4326, "EPSG:4326", "EPSG:25833")
    hits = next(r for r in server.requests if r.get("RESULTTYPE") == "hits")
    assert [round(float(v)) for v in hits["BBOX"].split(",")[:4]] == [round(v) for v in plaus]
    tiles = [r for r in server.getfeature() if "RESULTTYPE" not in r]
    assert 0 < len(tiles) <= 100 and fetched.info["paging"] == "bbox_tiles"


def test_configured_bbox_too_many_tiles_fails(tmp_path: Path) -> None:
    server = TileServer((12, 52, 13, 53))
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, server, max_pages=3, bbox=[300000, 5800000, 400000, 5900000]).fetch()
    assert exc.value.kind == "paging" and server.getfeature() == []


def test_extent_outside_plausibility_bbox_fails(tmp_path: Path) -> None:
    server = TileServer((100, -40, 110, -30))
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, server).fetch()
    assert exc.value.kind == "config" and "plausibility_bbox_4326" in str(exc.value)


def test_shipped_wfs_configs_pin_paging_strategy() -> None:
    strategies = {}
    for path in sorted((ROOT / "config" / "sources").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data.get("type") == "wfs":
            strategies[path.name] = data["wfs"].get("paging", {}).get("strategy", "auto")
    assert strategies and "auto" not in strategies.values()
    assert strategies["geobasis_bb_gemeinden.yaml"] == "none"  # LGB: ohne Paging, CountDefault 300000


def test_unbrauchbare_umrechnung_wird_konfigurationsfehler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ProjError oder unendliche Werte aus transform_bounds: kein interner Fehler (Exit 7), sondern ``config``."""
    import math

    from pyproj.exceptions import ProjError

    import geoimport.transform.crs as crs_mod

    class Fake:
        def __init__(self, result: Any) -> None:
            self.result = result

        def transform_bounds(self, *a: Any, **k: Any) -> Any:
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    for result in (ProjError("außerhalb"), (math.inf, 0.0, 1.0, 1.0)):
        monkeypatch.setattr(crs_mod.Transformer, "from_crs", staticmethod(lambda *a, r=result, **k: Fake(r)))
        server = TileServer((12, 52, 13, 53))
        with pytest.raises(SourceFailure) as exc:
            _plugin(tmp_path, server, strategy="bbox_tiles")._tile_plan(  # type: ignore[arg-type]
                SimpleFt(), "EPSG:25833", None
            )
        assert exc.value.kind == "config" and "wfs.bbox" in str(exc.value)


class SimpleFt:
    wgs84_bbox = (12.0, 52.0, 13.0, 53.0)
