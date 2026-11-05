"""S1: Eine als WFS-Seite getarnte OGR-VRT darf weder durchgelassen noch von GDAL gelesen werden."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from geoimport import _merge_gdal_skip
from geoimport.config.models import HttpSettings, PipelineConfig, WfsSource
from geoimport.http import HttpClient
from geoimport.sources import wfs
from geoimport.sources.base import FetchResult, RawArtifact, SourceContext, SourceFailure, StateStore
from geoimport.sources.wfs import WfsSourcePlugin, inspect_page, read_features
from tests.conftest import FIX


def _vrt(secret: Path) -> bytes:
    # Kommentar-Trick: ein Regex-Zähler sähe hier ein <wfs:member>
    return f"""<OGRVRTDataSource><!-- <wfs:member> --><OGRVRTLayer name="x">
<SrcDataSource>CSV:{secret}</SrcDataSource><OpenOptions><OOI key="HEADERS">NO</OOI></OpenOptions>
<SrcSQL dialect="SQLITE">SELECT '12051000' AS ags, field_1 AS gem_name,
 'POLYGON((13 52,13.1 52,13.1 52.1,13 52))' AS wkt FROM {secret.stem}</SrcSQL>
<GeometryField encoding="WKT" field="wkt"/></OGRVRTLayer></OGRVRTDataSource>""".encode()


def test_gdal_skip_active_in_process() -> None:
    import pyogrio  # erst nach geoimport importieren (GDAL_SKIP muss vorher gesetzt sein)

    skipped = os.environ["GDAL_SKIP"].split(",")
    assert {"OGR_VRT", "GPSBabel", "LIBKML", "KML", "WFS", "OAPIF"} <= set(skipped)
    drivers = pyogrio.list_drivers()
    assert "OGR_VRT" not in drivers and "GML" in drivers


def test_merge_gdal_skip_keeps_operator_entries() -> None:
    assert _merge_gdal_skip(None).split(",")[:2] == ["OGR_VRT", "GPSBabel"]
    assert _merge_gdal_skip("HDF4 netCDF").split(",") == [
        "HDF4", "netCDF", "OGR_VRT", "GPSBabel", "LIBKML", "KML", "WFS", "OAPIF"
    ]  # fmt: skip
    assert _merge_gdal_skip("ESRI Shapefile,KML").split(",") == [
        "ESRI Shapefile", "KML", "OGR_VRT", "GPSBabel", "LIBKML", "WFS", "OAPIF"
    ]  # fmt: skip


def test_gdal_skip_set_before_pyogrio_import() -> None:
    # Frischer Prozess: nur "import geoimport.sources.wfs" – der VRT-Treiber darf nicht registriert sein
    env = {k: v for k, v in os.environ.items() if k != "GDAL_SKIP"} | {"GDAL_SKIP": "HDF4"}
    code = (
        "import sys, zipfile; sys.modules.setdefault('zipfile_deflate64', zipfile)\n"
        "import geoimport.sources.wfs, pyogrio, os\n"
        "print('OGR_VRT' in pyogrio.list_drivers(), os.environ['GDAL_SKIP'])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True, cwd=Path.cwd()
    ).stdout
    assert out.startswith("False HDF4,OGR_VRT")


def test_read_features_refuses_vrt_without_root_check(tmp_path: Path) -> None:
    secret = tmp_path / "geheim.csv"
    secret.write_text("TOP-SECRET-TOKEN\n")
    page = tmp_path / "page_0001.gml"
    page.write_bytes(_vrt(secret))
    with pytest.raises(SourceFailure) as exc:
        read_features(page)
    assert exc.value.kind == "parse"


def test_inspect_page_rejects_vrt(tmp_path: Path) -> None:
    with pytest.raises(SourceFailure) as exc:
        inspect_page(_vrt(tmp_path / "geheim.csv"), False)
    assert exc.value.kind == "parse" and "OGRVRTDataSource" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Wartung</body></html>",
        b'<FeatureCollection xmlns="urn:fremd"/>',  # richtiger Name, falscher Namensraum
        # Zweites Wurzelelement hinter einer gültigen FeatureCollection
        b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"/><OGRVRTDataSource/>',
        b"kein XML",
    ],
)
def test_inspect_page_rejects_non_featurecollection_xml(body: bytes) -> None:
    with pytest.raises(SourceFailure) as exc:
        inspect_page(body, False)
    assert exc.value.kind == "parse"


@pytest.mark.parametrize(
    "body", [b'{"type": "Feature", "geometry": null, "properties": {}}', b"[1, 2]", b"{kaputt", b'{"features": []}']
)
def test_inspect_page_rejects_non_featurecollection_json(body: bytes) -> None:
    with pytest.raises(SourceFailure) as exc:
        inspect_page(body, True)
    assert exc.value.kind == "parse"


def test_inspect_page_accepts_lgb_fixture_and_ignores_comments() -> None:
    gml = (FIX / "wfs" / "vg_wfs_gem_brandenburg.gml").read_bytes()
    page = inspect_page(gml, False)
    assert page.count == 6 and len(set(page.ids)) == 6 and "gem_0" in page.ids
    commented = gml.replace(b"<wfs:member>", b"<!-- <wfs:member> --><wfs:member>", 1)
    assert inspect_page(commented, False).count == 6


CAPS = b"""<?xml version="1.0"?><wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0"><ows:OperationsMetadata>
<ows:Operation name="GetFeature"><ows:Parameter name="outputFormat"><ows:AllowedValues>
<ows:Value>application/gml+xml; version=3.2</ows:Value></ows:AllowedValues></ows:Parameter></ows:Operation>
</ows:OperationsMetadata><wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>app:gem</wfs:Name>
<wfs:DefaultCRS>urn:ogc:def:crs:EPSG::25833</wfs:DefaultCRS></wfs:FeatureType></wfs:FeatureTypeList>
</wfs:WFS_Capabilities>"""


def test_fetch_rejects_vrt_page(tmp_path: Path) -> None:
    payload = _vrt(tmp_path / "geheim.csv")

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k.upper(): v for k, v in request.url.params.items()}
        if q.get("REQUEST") == "GetCapabilities":
            return httpx.Response(200, content=CAPS)
        if q.get("RESULTTYPE") == "hits":
            return httpx.Response(
                200, content=b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" numberMatched="1"/>'
            )
        if q.get("REQUEST") == "GetFeature":
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    src = WfsSource.model_validate(
        {
            "id": "tt", "type": "wfs", "title": "t", "license": "l", "business_key": ["ags"],
            "target": {"dataset": "d"},
            "wfs": {"url": "https://x/wfs", "type_name": "app:gem", "paging": {"strategy": "none"}},
        }
    )  # fmt: skip
    http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handler))
    ctx = SourceContext(PipelineConfig(), http, "r", tmp_path / "raw", StateStore(tmp_path / "s", "tt"))
    with pytest.raises(SourceFailure) as exc:
        WfsSourcePlugin(src, ctx).fetch()
    assert exc.value.kind == "parse"
    assert not list((tmp_path / "raw").glob("*"))  # nichts gespeichert


# ------------------------------------------------------------------ parse(): günstige Prüfung, S1 bleibt dicht
def _parse_plugin(tmp_path: Path) -> WfsSourcePlugin:
    src = WfsSource.model_validate(
        {
            "id": "tt", "type": "wfs", "title": "t", "license": "l", "business_key": ["ags"],
            "target": {"dataset": "d"}, "guards": {"geometry_types": None},
            "wfs": {"url": "https://x/wfs", "type_name": "app:gem"},
        }
    )  # fmt: skip
    http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    ctx = SourceContext(PipelineConfig(), http, "r", tmp_path / "raw", StateStore(tmp_path / "s", "tt"))
    return WfsSourcePlugin(src, ctx)


def _fetched(path: Path, info: dict[str, object] | None, is_json: bool = False) -> FetchResult:
    return FetchResult(
        [RawArtifact(path, "x", None, info or {})], info={"is_json": is_json, "request_crs": "EPSG:25833"}
    )


def test_parse_skips_full_inspection_when_count_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = wfs._inspect_xml
    monkeypatch.setattr(wfs, "_inspect_xml", lambda c, k: calls.append(1) or real(c, k))
    page = tmp_path / "page_0001.gml"
    page.write_bytes((FIX / "wfs" / "vg_wfs_gem_brandenburg.gml").read_bytes())
    plugin = _parse_plugin(tmp_path)
    assert len(plugin.parse(_fetched(page, {"features": 6})).features) == 6
    assert calls == []  # Anzahl aus fetch() übernommen
    # Wiedereingelesene Rohdatei ohne Angaben → volle Prüfung
    assert len(plugin.parse(_fetched(page, None)).features) == 6
    assert calls == [1]


def test_parse_root_check_still_rejects_vrt(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.gml"
    page.write_bytes(_vrt(tmp_path / "geheim.csv"))  # nachträglich ausgetauschte Rohdatei
    with pytest.raises(SourceFailure) as exc:
        _parse_plugin(tmp_path).parse(_fetched(page, {"features": 1}))
    assert exc.value.kind == "parse" and "OGRVRTDataSource" in str(exc.value)


def test_parse_geojson_loads_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = wfs._load_feature_collection
    monkeypatch.setattr(wfs, "_load_feature_collection", lambda c: calls.append(1) or real(c))
    page = tmp_path / "page_0001.json"
    page.write_bytes((FIX / "wfs" / "geojson_int_null.json").read_bytes())
    parsed = _parse_plugin(tmp_path).parse(_fetched(page, None, is_json=True))
    assert parsed.fetched == 3 and calls == [1]
    page.write_bytes(b'{"type": "Feature"}')
    with pytest.raises(SourceFailure):
        _parse_plugin(tmp_path).parse(_fetched(page, {"features": 1}, is_json=True))
