"""WFS-Paging: kein stilles Abschneiden (H4), stabile Reihenfolge und eindeutige Objekte (H5), Trefferzahl."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from geoimport.config.models import HttpSettings, PipelineConfig, WfsSource
from geoimport.http import HttpClient
from geoimport.sources.base import FetchResult, SourceContext, SourceFailure, StateStore
from geoimport.sources.wfs import WfsSourcePlugin


def _caps(paging: bool = True, sorting: bool = False) -> bytes:
    def constraint(name: str, on: bool) -> str:
        return (
            f'<ows:Constraint name="{name}"><ows:NoValues/>'
            f"<ows:DefaultValue>{'TRUE' if on else 'FALSE'}</ows:DefaultValue></ows:Constraint>"
        )

    return f"""<?xml version="1.0"?><wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0"><ows:OperationsMetadata>
<ows:Operation name="GetFeature"><ows:Parameter name="outputFormat"><ows:AllowedValues>
<ows:Value>application/gml+xml; version=3.2</ows:Value></ows:AllowedValues></ows:Parameter></ows:Operation>
{constraint("ImplementsResultPaging", paging)}{constraint("ImplementsSorting", sorting)}
</ows:OperationsMetadata><wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>app:gem</wfs:Name>
<wfs:DefaultCRS>urn:ogc:def:crs:EPSG::25833</wfs:DefaultCRS></wfs:FeatureType></wfs:FeatureTypeList>
</wfs:WFS_Capabilities>""".encode()


def _feat(i: int, key: int | None = None, with_id: bool = True) -> str:
    k = 12000000 + i if key is None else key
    gml_id = f' gml:id="g{i}"' if with_id else ""
    return (
        f"<wfs:member><app:gem{gml_id}><app:ags>{k}</app:ags><app:geom><gml:Point "
        f'srsName="urn:ogc:def:crs:EPSG::25833"><gml:pos>{370000 + i} 5800000</gml:pos></gml:Point></app:geom>'
        "</app:gem></wfs:member>"
    )


@dataclass
class FakeServer:
    """WFS mit einstellbaren Eigenheiten: Obergrenze je Antwort, Trefferzahl, Reihenfolge, next-Links."""

    n: int = 1200
    cap: int = 10**9  # Obergrenze je Antwort, nicht als CountDefault angekündigt
    hits: int | str | None = None  # None: Anfrage schlägt fehl; "unknown": keine Zahl
    paging: bool = True
    sorting: bool = False
    next_links: bool = False
    order: Callable[[int], list[int]] | None = None  # Reihenfolge der IDs je STARTINDEX
    feat: Callable[[int], str] = _feat
    requests: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k.upper(): v for k, v in request.url.params.items()}
        self.requests.append(q)
        req = q.get("REQUEST")
        if req == "GetCapabilities":
            return httpx.Response(200, content=_caps(self.paging, self.sorting))
        if req == "DescribeFeatureType":
            return httpx.Response(404)
        if q.get("RESULTTYPE") == "hits":
            if self.hits is None:
                return httpx.Response(400, content=b"hits nicht unterstuetzt")
            return httpx.Response(
                200,
                content=f'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
                f'numberMatched="{self.hits}" numberReturned="0"/>'.encode(),
            )
        start = int(q.get("STARTINDEX", 0))
        count = min(int(q.get("COUNT", 10**9)), self.cap)
        ids = (self.order(start) if self.order else list(range(self.n)))[start : start + count]
        nxt = ""
        if self.next_links and start + len(ids) < self.n:
            nxt = f' next="https://x/wfs?STARTINDEX={start + len(ids)}"'
        body = (
            '<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
            f'xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:app="http://x/app" numberMatched="unknown"{nxt}>'
            + "".join(self.feat(i) for i in ids)
            + "</wfs:FeatureCollection>"
        )
        return httpx.Response(200, content=body.encode())

    def getfeature(self) -> list[dict[str, str]]:
        return [r for r in self.requests if r.get("REQUEST") == "GetFeature" and "RESULTTYPE" not in r]


def _plugin(tmp_path: Path, server: FakeServer, strict: bool = True, **wfs: Any) -> WfsSourcePlugin:
    paging = {"page_size": 1000, "throttle_ms": 0} | wfs.pop("paging", {})
    src = WfsSource.model_validate(
        {
            "id": "tt", "type": "wfs", "title": "t", "license": "l", "business_key": ["ags"],
            "target": {"dataset": "d"}, "guards": {"strict_count": strict, "geometry_types": None},
            "wfs": {"url": "https://x/wfs", "type_name": "app:gem", "paging": paging} | wfs,
        }
    )  # fmt: skip
    http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(server))
    ctx = SourceContext(PipelineConfig(), http, "r", tmp_path / "raw", StateStore(tmp_path / "s", "tt"))
    return WfsSourcePlugin(src, ctx)


def _codes(f: FetchResult) -> list[str]:
    return [w.code for w in f.warnings if w.code != "schema_check_skipped"]  # Testdienst ohne DescribeFeatureType


# ------------------------------------------------------------------ H4: keine stille Kürzung
def test_server_cap_without_hits_is_not_truncated(tmp_path: Path) -> None:
    server = FakeServer(cap=500, hits="unknown")
    plugin = _plugin(tmp_path, server, strict=False)
    fetched = plugin.fetch()
    assert fetched.info["features_delivered"] == 1200
    assert len(plugin.parse(fetched).features) == 1200
    assert "count_unverified" in _codes(fetched)
    assert [r["STARTINDEX"] for r in server.getfeature()] == ["0", "500", "1000", "1200"]  # bis zur leeren Seite


def test_server_cap_without_hits_fails_when_strict(tmp_path: Path) -> None:
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, FakeServer(cap=500, hits="unknown")).fetch()
    assert exc.value.kind == "possibly_truncated" and "nicht prüfbar" in str(exc.value)


def test_server_cap_with_hits_stops_at_hits(tmp_path: Path) -> None:
    server = FakeServer(cap=500, hits=1200, sorting=True)
    fetched = _plugin(tmp_path, server).fetch()
    assert fetched.info["features_unique"] == 1200 and not _codes(fetched)
    assert len(server.getfeature()) == 3  # keine zusätzliche leere Seite nötig


def test_next_link_ends_paging(tmp_path: Path) -> None:
    server = FakeServer(cap=500, hits="unknown", next_links=True)
    fetched = _plugin(tmp_path, server, strict=False).fetch()
    assert fetched.info["features_delivered"] == 1200
    assert len(server.getfeature()) == 3  # letzte Seite ohne next → Ende ohne leere Seite


def test_failed_hits_request_is_reported(tmp_path: Path) -> None:
    fetched = _plugin(tmp_path, FakeServer(n=10, hits=None), strict=False, paging={"strategy": "none"}).fetch()
    msg = next(w.message for w in fetched.warnings if w.code == "count_unverified")
    assert "Anfrage fehlgeschlagen" in msg


def test_failed_hits_request_fails_when_strict(tmp_path: Path) -> None:
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, FakeServer(n=10, hits=None), paging={"strategy": "none"}).fetch()
    assert exc.value.kind == "possibly_truncated"


def test_number_matched_on_page_used_when_hits_fails(tmp_path: Path) -> None:
    server = FakeServer(n=10, hits=None)
    plain = server.__call__

    def with_matched(request: httpx.Request) -> httpx.Response:
        r = plain(request)
        return httpx.Response(r.status_code, content=r.content.replace(b'"unknown"', b'"10"'))

    plugin = _plugin(tmp_path, server, paging={"strategy": "none"})
    plugin.ctx.http = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(with_matched))
    fetched = plugin.fetch()
    assert fetched.info["hits"] == 10 and not _codes(fetched)


def test_tiles_capped_silently_detected(tmp_path: Path) -> None:
    # Jede Kachel liefert höchstens 3 Objekte, ohne CountDefault anzukündigen
    server = FakeServer(n=10, cap=3, hits=10, paging=False)
    plugin = _plugin(
        tmp_path,
        server,
        bbox=[330000, 5800000, 350000, 5815000],
        paging={"strategy": "bbox_tiles", "tile_size_m": 10000},
    )
    with pytest.raises(SourceFailure) as exc:
        plugin.fetch()
    assert exc.value.kind == "incomplete"
    hits_req = next(r for r in server.requests if r.get("RESULTTYPE") == "hits")
    assert hits_req["BBOX"].startswith("330000.000000,5800000.000000,350000.000000,5815000.000000")


# ------------------------------------------------------------------ H5: Reihenfolge, eindeutige Objekte
def _unstable(start: int) -> list[int]:
    # Seite 2 sieht eine verschobene Reihenfolge: 5 Objekte doppelt, 5 (1195–1199) fehlen
    ids = list(range(1200))
    return ids if start == 0 else ids[:995] + ids[995:1000] + ids[995:1000] + ids[1000:1195]


def test_unstable_order_fails_incomplete(tmp_path: Path) -> None:
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, FakeServer(hits=1200, order=_unstable)).fetch()
    assert exc.value.kind == "incomplete" and "früherer Seiten" in str(exc.value)


def _dup_key(i: int) -> int | None:
    return 19999999 if i in (1, 4) else None  # Objekte 1 und 4: verschiedene gml:id, gleicher Schlüssel


@pytest.mark.parametrize("page_size", [2, 3, 4, 6])
def test_duplicate_key_with_distinct_ids_is_record_error(tmp_path: Path, page_size: int) -> None:
    # Gleiches Ergebnis unabhängig davon, ob die Dubletten auf derselben oder auf verschiedenen Seiten liegen
    server = FakeServer(n=6, hits=6, sorting=True, feat=lambda i: _feat(i, key=_dup_key(i)))
    plugin = _plugin(tmp_path, server, paging={"page_size": page_size})
    fetched = plugin.fetch()
    assert fetched.info["features_unique"] == 6
    parsed = plugin.parse(fetched)
    assert len(parsed.features) == 5 and [e.reason_code for e in parsed.errors] == ["duplicate_key"]


def test_repeated_gml_id_across_pages_fails(tmp_path: Path) -> None:
    # Seite 2 liefert Objekt 2 (gml:id g2) erneut → Reihenfolge instabil
    server = FakeServer(n=6, hits=6, order=lambda s: [0, 1, 2, 3, 4, 5] if s == 0 else [0, 1, 2, 2, 4, 5])
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, server, paging={"page_size": 3}).fetch()
    assert exc.value.kind == "incomplete" and "g2" in str(exc.value)


@pytest.mark.parametrize("page_size", [3, 6])
def test_duplicate_key_without_ids_fails(tmp_path: Path, page_size: int) -> None:
    # Ohne gml:id ist der Schlüssel die einzige Identität: Dublette nicht von instabilem Paging unterscheidbar
    server = FakeServer(n=6, hits=6, feat=lambda i: _feat(i, key=_dup_key(i), with_id=False))
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, server, paging={"page_size": page_size}).fetch()
    assert exc.value.kind == "incomplete"


def test_sortby_business_key_when_supported(tmp_path: Path) -> None:
    server = FakeServer(n=6, hits=6, sorting=True)
    fetched = _plugin(tmp_path, server, paging={"page_size": 4}).fetch()
    assert {r.get("SORTBY") for r in server.getfeature()} == {"ags"}
    assert fetched.info["sort_by"] == "ags" and "unsorted_paging" not in _codes(fetched)


def test_configured_sort_by_wins(tmp_path: Path) -> None:
    server = FakeServer(n=6, hits=6, sorting=True)
    _plugin(tmp_path, server, sort_by="gml:id", paging={"page_size": 4}).fetch()
    assert {r.get("SORTBY") for r in server.getfeature()} == {"gml:id"}


def test_unsorted_paging_warns(tmp_path: Path) -> None:
    server = FakeServer(n=6, hits=6, sorting=False)
    fetched = _plugin(tmp_path, server, paging={"page_size": 4}).fetch()
    assert all("SORTBY" not in r for r in server.getfeature())
    assert "unsorted_paging" in _codes(fetched)


def test_unique_count_compared_against_hits(tmp_path: Path) -> None:
    # Strategie none: 6 Objekte geliefert, eines doppelt → 5 verschiedene, Dienst meldet 6
    server = FakeServer(n=6, hits=6, order=lambda s: [0, 1, 2, 3, 4, 4])
    with pytest.raises(SourceFailure) as exc:
        _plugin(tmp_path, server, paging={"strategy": "none"}).fetch()
    assert exc.value.kind == "incomplete" and "5 verschiedene" in str(exc.value)
