"""HTTP: keine Umleitung von https auf http, Query-String der URL bleibt bei ``params`` erhalten."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from geoimport.config.models import HttpSettings
from geoimport.http import HttpClient, HttpError


def _client(handler: object) -> tuple[HttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator, no-any-return]

    settings = HttpSettings(retries=0, backoff_base_s=0)
    return HttpClient(settings, transport=httpx.MockTransport(wrapped)), seen


def _redirect_to(target: str) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=b"ok")

    return handler


def test_redirect_https_to_http_blocked_get() -> None:
    client, seen = _client(_redirect_to("http://evil.example/ziel"))
    with pytest.raises(HttpError) as exc:
        client.get("https://dienst.example/start", params={"a": "1"})
    assert exc.value.kind == "insecure_redirect"
    assert [str(r.url.host) for r in seen] == ["dienst.example"]  # Ziel nie angefragt


def test_redirect_https_to_http_blocked_download(tmp_path: Path) -> None:
    client, seen = _client(_redirect_to("http://evil.example/datei.zip"))
    with pytest.raises(HttpError) as exc:
        client.download("https://dienst.example/start", tmp_path / "d.zip")
    assert exc.value.kind == "insecure_redirect"
    assert not (tmp_path / "d.zip").exists() and len(seen) == 1


def test_redirect_https_to_http_head_ok_false() -> None:
    client, _ = _client(_redirect_to("http://evil.example/x"))
    assert client.head_ok("https://dienst.example/start") is False


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("https://dienst.example/start", "https://anderer.example/ziel"),
        ("https://dienst.example/start", "/ziel"),  # relativ bleibt https
        ("http://dienst.example/start", "http://dienst.example/ziel"),
        ("http://dienst.example/start", "https://dienst.example/ziel"),
    ],
)
def test_redirect_allowed(start: str, target: str) -> None:
    client, _ = _client(_redirect_to(target))
    assert client.get(start).content == b"ok"


def test_query_string_in_url_is_kept() -> None:
    client, seen = _client(lambda r: httpx.Response(200, content=b"ok"))
    client.get(
        "https://dienst.example/wfs?map=/x.map&apikey=K&service=wms",
        params={"SERVICE": "WFS", "REQUEST": "GetCapabilities"},
    )
    items = seen[0].url.params.multi_items()
    assert ("map", "/x.map") in items and ("apikey", "K") in items
    # Request-Parameter gewinnen, Groß-/Kleinschreibung egal (OGC-KVP)
    assert ("SERVICE", "WFS") in items and not any(k == "service" for k, _ in items)
    assert ("REQUEST", "GetCapabilities") in items


def test_query_string_without_params_unchanged() -> None:
    client, seen = _client(lambda r: httpx.Response(200, content=b"ok"))
    client.get("https://dienst.example/wfs?map=/x.map")
    assert seen[0].url.params.multi_items() == [("map", "/x.map")]
