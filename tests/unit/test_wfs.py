"""WFS: Capabilities, Schema, ExceptionReport, Paging-Strategien, Lesen über GDAL."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from geoimport.config.loader import load_config
from geoimport.http import HttpClient
from geoimport.sources.base import SourceContext, SourceFailure, StateStore
from geoimport.sources.wfs import (
    WfsSourcePlugin,
    check_exception,
    count_members,
    equality_filter,
    parse_capabilities,
    parse_describe,
    read_features,
    schema_drift,
    tiles,
)
from tests.conftest import FIX, FakeWfs, load_yaml, write_yaml

GML = FIX / "wfs" / "vg_wfs_gem_brandenburg.gml"


def test_capabilities_lgb() -> None:
    caps = parse_capabilities((FIX / "wfs" / "vg_wfs_capabilities.xml").read_bytes())
    assert caps.version == "2.0.0"
    assert caps.implements_paging is False and caps.count_default == 300000
    ft = caps.find("app:gem")
    assert ft and ft.default_crs == "urn:ogc:def:crs:EPSG::25833"
    assert caps.find("gem") is ft  # ohne Präfix
    assert "application/gml+xml; version=3.2" in caps.output_formats
    assert not any("json" in f for f in caps.output_formats)


def test_describe_all_types_anonymous_complex_types() -> None:
    xsd = (FIX / "wfs" / "vg_wfs_describe.xsd").read_bytes()
    gem = parse_describe(xsd, "app:gem")
    assert gem["ags"] == "string" and gem["gem_aktualitaet"] == "date" and "geom" in gem
    assert "krs_name" not in gem


def test_schema_drift_messages() -> None:
    msgs = schema_drift({"a": "string", "b": "string"}, {"a": "date", "c": "string"})
    assert "Feld 'c' neu" in msgs and "Feld 'b' entfallen" in msgs and any("Typ geändert" in m for m in msgs)


def test_exception_report_with_http_200() -> None:
    body = b"""<?xml version='1.0'?><ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
      <ows:Exception exceptionCode="InvalidParameterValue"><ows:ExceptionText>Unknown type app:xyz</ows:ExceptionText>
      </ows:Exception></ows:ExceptionReport>"""
    with pytest.raises(SourceFailure) as exc:
        check_exception(body)
    assert exc.value.kind == "ogc_exception" and "Unknown type" in str(exc.value)
    check_exception(GML.read_bytes()[:5000])


def test_count_members_ignores_number_returned() -> None:
    content = GML.read_bytes()
    assert b'numberReturned="0"' in content  # deegree-Eigenheit
    assert count_members(content, False) == 6


def test_read_features_text_attributes(tmp_path: Path) -> None:
    feats = read_features(GML)
    assert len(feats) == 6
    assert all(isinstance(v, str) or v is None for f in feats for v in f.props.values())
    assert {f.props["ags"] for f in feats} >= {"12051000"}
    assert not list(GML.parent.glob("*.gfs"))


def test_equality_filter_and_tiles() -> None:
    xml = equality_filter({"krss": "12051", "gem_art": "Stadt <A&B>"}, "2.0.0")
    assert "fes:And" in xml and "&lt;A&amp;B&gt;" in xml
    t = tiles((0, 0, 25, 10), 10)
    assert len(t) == 3 and t[-1] == (20, 0, 25, 10)


# ------------------------------------------------------------------ Plugin gegen Testserver
def _plugin(config_dir: Path, tmp_path: Path, **wfs_over: object) -> WfsSourcePlugin:
    src_path = config_dir / "sources" / "geobasis_bb_gemeinden.yaml"
    src = load_yaml(src_path)
    src["wfs"].update(wfs_over)
    write_yaml(src_path, src)
    cfg = load_config(config_dir)
    s = cfg.source("geobasis_bb_gemeinden")
    http = HttpClient(cfg.pipeline.http)
    ctx = SourceContext(cfg.pipeline, http, "test", tmp_path / "raw", StateStore(tmp_path / "state", s.id))
    return WfsSourcePlugin(s, ctx)


def test_fetch_and_parse_strategy_none(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    plugin = _plugin(config_dir, tmp_path)
    fetched = plugin.fetch()
    assert fetched.info["paging"] == "none" and fetched.info["hits"] == 6
    assert len(fetched.artifacts) == 1 and fetched.artifacts[0].path.exists()
    getfeature = [r for r in fake_wfs.requests if r.get("REQUEST") == "GetFeature" and "RESULTTYPE" not in r][0]
    assert getfeature["SRSNAME"] == "urn:ogc:def:crs:EPSG::25833"
    assert getfeature["OUTPUTFORMAT"] == "application/gml+xml; version=3.2"
    parsed = plugin.parse(fetched)
    assert parsed.fetched == 6 and len(parsed.features) == 6 and not parsed.errors
    brb = next(f for f in parsed.features if f.key == "12051000")
    assert brb.name == "Brandenburg an der Havel"
    assert set(brb.attributes) == {"ags", "ars", "gvbs", "krss", "gem_art", "aktualitaet"}
    minx, miny, maxx, maxy = brb.geometry.bounds
    assert 12.2 < minx < maxx < 12.8 and 52.3 < miny < maxy < 52.6  # EPSG:4326, Länge/Breite


def test_incomplete_delivery_fails(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    fake_wfs.hits = 7
    with pytest.raises(SourceFailure) as exc:
        _plugin(config_dir, tmp_path).fetch()
    assert exc.value.kind == "incomplete"


def test_featuretype_missing(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    with pytest.raises(SourceFailure) as exc:
        _plugin(config_dir, tmp_path, type_name="app:gibtsnicht").fetch()
    assert exc.value.kind == "featuretype_missing"


def test_schema_mismatch_when_mapped_field_missing(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    src_path = config_dir / "sources" / "geobasis_bb_gemeinden.yaml"
    src = load_yaml(src_path)
    src["mapping"]["attributes"]["neu"] = "feld_gibt_es_nicht"
    write_yaml(src_path, src)
    with pytest.raises(SourceFailure) as exc:
        _plugin(config_dir, tmp_path).fetch()
    assert exc.value.kind == "schema_mismatch" and "feld_gibt_es_nicht" in str(exc.value)


def test_schema_drift_warning_against_state(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    plugin = _plugin(config_dir, tmp_path)
    plugin.ctx.state.update({"schema": {"ags": "string", "gem_name": "string", "alt": "string"}})
    codes = [w.code for w in plugin.fetch().warnings]
    assert "schema_drift" in codes


def test_truncation_at_count_default(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    fake_wfs.caps = fake_wfs.caps.replace(
        b"<ows:DefaultValue>300000</ows:DefaultValue>", b"<ows:DefaultValue>6</ows:DefaultValue>"
    )
    with pytest.raises(SourceFailure) as exc:
        _plugin(config_dir, tmp_path, paging={"strategy": "none", "throttle_ms": 0}).fetch()
    assert exc.value.kind == "possibly_truncated"


def _members(gml: bytes) -> list[bytes]:
    start = gml.index(b"<wfs:member>")
    end = gml.rindex(b"</wfs:member>") + len(b"</wfs:member>")
    body = gml[start:end]
    return [b"<wfs:member>" + m for m in body.split(b"<wfs:member>")[1:]]


def test_startindex_paging(httpserver: HTTPServer, config_dir: Path, tmp_path: Path) -> None:
    wfs = FakeWfs()
    gml = wfs.features
    head, members = gml[: gml.index(b"<wfs:member>")], _members(gml)
    tail = b"</wfs:FeatureCollection>"

    def handler(request: Request) -> Response:
        p = {k.upper(): v for k, v in request.args.items()}
        if p.get("REQUEST") == "GetFeature" and "STARTINDEX" in p:
            start, count = int(p["STARTINDEX"]), int(p["COUNT"])
            return Response(head + b"".join(members[start : start + count]) + tail, content_type="text/xml")
        return wfs(request)

    httpserver.expect_request("/ows/vg_wfs").respond_with_handler(handler)
    plugin = _plugin(config_dir, tmp_path, paging={"strategy": "startindex", "page_size": 4, "throttle_ms": 0})
    fetched = plugin.fetch()
    assert len(fetched.artifacts) == 2 and fetched.info["features_delivered"] == 6
    assert len(plugin.parse(fetched).features) == 6


def test_startindex_ignored_is_detected(httpserver: HTTPServer, config_dir: Path, tmp_path: Path) -> None:
    wfs = FakeWfs()
    gml = wfs.features
    head, members = gml[: gml.index(b"<wfs:member>")], _members(gml)

    def handler(request: Request) -> Response:
        p = {k.upper(): v for k, v in request.args.items()}
        if p.get("REQUEST") == "GetFeature" and "STARTINDEX" in p:  # ignoriert STARTINDEX
            return Response(head + b"".join(members[:2]) + b"</wfs:FeatureCollection>", content_type="text/xml")
        return wfs(request)

    httpserver.expect_request("/ows/vg_wfs").respond_with_handler(handler)
    with pytest.raises(SourceFailure, match="STARTINDEX"):
        _plugin(config_dir, tmp_path, paging={"strategy": "startindex", "page_size": 2, "throttle_ms": 0}).fetch()


def test_bbox_tiles_dedupes_boundary_objects(httpserver: HTTPServer, config_dir: Path, tmp_path: Path) -> None:
    wfs = FakeWfs()
    httpserver.expect_request("/ows/vg_wfs").respond_with_handler(wfs)  # jede Kachel liefert alle 6 Objekte
    plugin = _plugin(
        config_dir,
        tmp_path,
        bbox=[330000, 5800000, 350000, 5815000],
        paging={"strategy": "bbox_tiles", "tile_size_m": 10000, "throttle_ms": 0},
    )
    fetched = plugin.fetch()
    assert len(fetched.artifacts) == 4
    parsed = plugin.parse(fetched)
    assert parsed.fetched == 24 and len(parsed.features) == 6 and not parsed.errors


def test_http_error_becomes_source_failure(config_dir: Path, tmp_path: Path, fake_wfs: FakeWfs) -> None:
    fake_wfs.fail_status = 503
    with pytest.raises(SourceFailure) as exc:
        _plugin(config_dir, tmp_path).fetch()
    assert exc.value.kind == "http" and exc.value.http_status == 503


def test_count_members_gml31_feature_members() -> None:
    gml = (
        b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" xmlns:gml="http://www.opengis.net/gml" '
        b'xmlns:app="urn:x"><gml:featureMembers><app:gem gml:id="a"/><app:gem gml:id="b"/><app:gem gml:id="c"/>'
        b"</gml:featureMembers></wfs:FeatureCollection>"
    )
    assert count_members(gml, False) == 3


def test_bbox_axis_order_wfs2_geographic(config_dir: Path, tmp_path: Path) -> None:
    plugin = _plugin(config_dir, tmp_path)
    bbox = (12.5, 52.4, 12.6, 52.5)
    geo = plugin._getfeature_params("EPSG:4326", None, bbox)["BBOX"]
    assert geo.startswith("52.400000,12.500000,52.500000,12.600000,")  # Breite vor Länge
    utm = plugin._getfeature_params("EPSG:25833", None, (1.0, 2.0, 3.0, 4.0))["BBOX"]
    assert utm.startswith("1.000000,2.000000,3.000000,4.000000,")


def test_abbruch_auf_seite_2_ist_unvollstaendig(httpserver: HTTPServer, config_dir: Path, tmp_path: Path) -> None:
    # V5: Bricht der Abruf auf einer Folgeseite ab, gilt die ganze Lieferung als unvollständig – nichts wird ersetzt
    from geoimport.orchestrator import INCOMPLETE_KINDS

    wfs = FakeWfs()
    gml = wfs.features
    head, members = gml[: gml.index(b"<wfs:member>")], _members(gml)

    def handler(request: Request) -> Response:
        p = {k.upper(): v for k, v in request.args.items()}
        if p.get("REQUEST") == "GetFeature" and "STARTINDEX" in p:
            if int(p["STARTINDEX"]) > 0:
                return Response("Dienst überlastet", status=400)
            return Response(head + b"".join(members[:4]) + b"</wfs:FeatureCollection>", content_type="text/xml")
        return wfs(request)

    httpserver.expect_request("/ows/vg_wfs").respond_with_handler(handler)
    plugin = _plugin(config_dir, tmp_path, paging={"strategy": "startindex", "page_size": 4, "throttle_ms": 0})
    with pytest.raises(SourceFailure) as exc:
        plugin.fetch()
    assert exc.value.kind in INCOMPLETE_KINDS
