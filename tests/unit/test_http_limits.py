"""HTTP gegen feindliche Dienste: Größen-/Entpackungsgrenze, Gesamtfrist, Retry-After, keine sinnlosen Wiederholungen."""

from __future__ import annotations

import gzip
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from geoimport.config.models import HttpSettings
from geoimport.http import HttpClient, HttpError

MIB = 1024 * 1024
BOMB = gzip.compress(b"\0" * (64 * MIB), 9)  # ~64 KiB auf der Leitung, 64 MiB entpackt


def _client(
    handler: Callable[[httpx.Request], httpx.Response], **settings: Any
) -> tuple[HttpClient, list[httpx.Request], list[float]]:
    seen: list[httpx.Request] = []
    slept: list[float] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = HttpClient(
        HttpSettings(**({"retries": 0, "backoff_base_s": 0} | settings)), transport=httpx.MockTransport(wrapped)
    )
    client.sleep = slept.append
    return client, seen, slept


def _bomb(request: httpx.Request) -> httpx.Response:
    # in Stücken wie aus dem Netz: httpx entpackt beim Lesen
    chunks = iter([BOMB[i : i + 65536] for i in range(0, len(BOMB), 65536)])
    return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=chunks)


# ------------------------------------------------------------------ S2: Grenze für entpackte Bytes
def test_get_gzip_bomb_capped() -> None:
    client, _, _ = _client(_bomb, max_response_mb=8)
    with pytest.raises(HttpError) as exc:
        client.get("https://dienst.example/ows")
    assert exc.value.kind == "too_large" and "8 MiB" in str(exc.value)


def test_download_gzip_bomb_capped(tmp_path: Path) -> None:
    client, seen, _ = _client(_bomb, max_download_mb=8, retries=2)
    with pytest.raises(HttpError) as exc:
        client.download("https://dienst.example/f.csv", tmp_path / "f.csv")
    assert exc.value.kind == "too_large" and len(seen) == 1  # nicht wiederholt
    assert not list(tmp_path.iterdir())  # keine Reste


def test_download_cap_per_call(tmp_path: Path) -> None:
    client, _, _ = _client(lambda r: httpx.Response(200, content=b"x" * (3 * MIB)))
    with pytest.raises(HttpError) as exc:
        client.download("https://dienst.example/f.csv", tmp_path / "f.csv", max_bytes=2 * MIB)
    assert exc.value.kind == "too_large"
    assert client.download("https://dienst.example/f.csv", tmp_path / "f.csv").size == 3 * MIB


def test_decompression_ratio_capped() -> None:
    client, _, _ = _client(_bomb, max_response_mb=1024, max_decompression_ratio=100)
    with pytest.raises(HttpError) as exc:
        client.get("https://dienst.example/ows")
    assert exc.value.kind == "too_large" and "Verhältnis" in str(exc.value) and " aus 0 KiB" not in str(exc.value)


def test_normal_gzip_response_passes() -> None:
    body = b"<wfs:FeatureCollection/>" * 1000
    client, _, _ = _client(
        lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(body))
    )
    r = client.get("https://dienst.example/ows")
    assert r.content == body and r.status_code == 200


# ------------------------------------------------------------------ Gesamtfrist (tröpfelnder Dienst)
def _drip(request: httpx.Request) -> httpx.Response:
    def chunks() -> Iterator[bytes]:
        for _ in range(100):
            time.sleep(0.05)
            yield b"x"

    return httpx.Response(200, content=chunks())


def test_get_deadline() -> None:
    client, _, _ = _client(_drip, deadline_s=0.3)
    t0 = time.monotonic()
    with pytest.raises(HttpError) as exc:
        client.get("https://dienst.example/ows")
    assert exc.value.kind == "timeout" and time.monotonic() - t0 < 2


def test_download_deadline(tmp_path: Path) -> None:
    client, _, _ = _client(_drip, deadline_s=0.3)
    with pytest.raises(HttpError) as exc:
        client.download("https://dienst.example/f.csv", tmp_path / "f.csv")
    assert exc.value.kind == "timeout" and not list(tmp_path.iterdir())


# ------------------------------------------------------------------ Retry-After
def _busy_then_ok(retry_after: str) -> Callable[[httpx.Request], httpx.Response]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": retry_after})
        return httpx.Response(200, content=b"ok")

    return handler


def test_retry_after_seconds() -> None:
    client, seen, slept = _client(_busy_then_ok("7"), retries=1)
    assert client.get("https://dienst.example/ows").content == b"ok"
    assert len(seen) == 2 and slept == [7.0]


def test_retry_after_http_date() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    client, _, slept = _client(_busy_then_ok(when), retries=1)
    client.get("https://dienst.example/ows")
    assert len(slept) == 1 and 25 <= slept[0] <= 30


def test_retry_after_capped(tmp_path: Path) -> None:
    client, _, slept = _client(_busy_then_ok("86400"), retries=1, max_retry_after_s=60)
    client.download("https://dienst.example/f.csv", tmp_path / "f.csv")
    assert slept == [60.0]


# ------------------------------------------------------------------ keine Wiederholung bei bleibenden Fehlern
def _raising(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "tls"),
        (httpx.UnsupportedProtocol("Request URL has an unsupported protocol 'ftp://'"), "config"),
        (httpx.LocalProtocolError("Illegal header value"), "config"),
    ],
)
def test_permanent_transport_errors_not_retried(exc: Exception, kind: str) -> None:
    client, seen, slept = _client(_raising(exc), retries=3)
    with pytest.raises(HttpError) as err:
        client.get("https://dienst.example/ows")
    assert err.value.kind == kind and len(seen) == 1 and not slept


def test_transient_connect_error_retried() -> None:
    client, seen, slept = _client(_raising(httpx.ConnectError("Connection refused")), retries=2)
    with pytest.raises(HttpError) as err:
        client.get("https://dienst.example/ows")
    assert err.value.kind == "network" and len(seen) == 3 and len(slept) == 2


def test_invalid_url_becomes_http_error() -> None:
    client, _, _ = _client(lambda r: httpx.Response(200))
    with pytest.raises(HttpError) as err:
        client.get("https://dienst.example:abc/ows")
    assert err.value.kind == "config"


# ------------------------------------------------------------------ Nachprüfung Schritte 4–5
def test_verschachtelte_kodierung_abgelehnt() -> None:
    """httpx entpackt ``gzip, gzip`` ohne Längengrenze: wenige Bytes würden vor jeder Prüfung Gigabytes."""
    doppelt = gzip.compress(gzip.compress(b"\0" * MIB, 9), 9)
    client, seen, _ = _client(
        lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip, gzip"}, content=doppelt), retries=2
    )
    with pytest.raises(HttpError) as exc:
        client.get("https://dienst.example/ows")
    assert exc.value.kind == "too_large" and "Content-Encoding" in str(exc.value) and len(seen) == 1


def test_einfache_kodierung_mit_identity_bleibt_erlaubt() -> None:
    client, _, _ = _client(
        lambda r: httpx.Response(200, headers={"Content-Encoding": "identity, gzip"}, content=gzip.compress(b"ok"))
    )
    assert client.get("https://dienst.example/ows").content == b"ok"


@pytest.mark.parametrize("value", [b"\xb2", b"\xd9\xa3", b"1e9x", b"-5"])
def test_retry_after_unbrauchbar_kein_absturz(value: bytes) -> None:
    from geoimport.http import _retry_after

    assert _retry_after(httpx.Response(503, headers=[(b"Retry-After", value)])) is None
