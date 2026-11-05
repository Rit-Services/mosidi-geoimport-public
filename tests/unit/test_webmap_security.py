"""Härtung der Demo-Karte (SECURITY_REVIEW S7/S8) – ohne Datenbank."""

from __future__ import annotations

import asyncio
import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from webmap import app as webmap
from webmap import auth

PW = "ein-sicheres-kennwort-123"


def _reset() -> None:
    webmap.auth_config.cache_clear()
    webmap._limiter = None
    webmap._ready_cache.at = -math.inf
    webmap._ready_cache.ok = False


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MAP_AUTH_USER", "thb")
    monkeypatch.setenv("MAP_AUTH_PASSWORD", PW)
    monkeypatch.setenv("MAP_SESSION_SECRET", "x" * 40)
    _reset()
    yield
    _reset()


def _client(ip: str = "testclient", session: str | None = None) -> TestClient:
    c = TestClient(
        webmap.app, base_url="https://testserver", follow_redirects=False, raise_server_exceptions=False, client=(ip, 1)
    )
    if session:
        c.cookies.set(auth.COOKIE_NAME, session)
    return c


@pytest.fixture
def client(auth_env: None) -> TestClient:
    return _client()


def _csrf(c: TestClient) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/login").text)
    assert m, "Login-Formular ohne CSRF-Token"
    return m.group(1)


def _login(c: TestClient) -> str:
    r = c.post("/login", data={"username": "thb", "password": PW, "csrf": _csrf(c)})
    assert r.status_code == 303, r.text
    token = r.cookies.get(auth.COOKIE_NAME)
    assert token
    return token


def _raw_get(path: str, cookie: str | None = None) -> int:
    """GET mit unverändertem ASGI-Pfad (httpx würde ``..`` vorher auflösen)."""
    headers = [(b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", f"{auth.COOKIE_NAME}={cookie}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8", "surrogateescape"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    with _no_raise():
        asyncio.run(webmap.app(scope, receive, send))
    return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))


@contextmanager
def _no_raise() -> Iterator[None]:
    try:
        yield
    except Exception:  # noqa: BLE001 – Serverfehler zeigt sich am gesendeten Status 500
        pass


# ---------------------------------------------------------------- 1. Umgehung über /static/fonts/
@pytest.mark.parametrize(
    "path",
    [
        "/static/fonts/../map.js",  # auch ..%2f und %2e%2e: uvicorn dekodiert vor der App
        "/static/fonts/../index.html",
        "/static/fonts/x/../../map.js",
        "/static/fonts/./../map.js",
        "/static//fonts/../map.js",
        "/static/fonts/../vendor/leaflet/leaflet.js",
    ],
)
def test_keine_umgehung_ueber_schriften(auth_env: None, path: str) -> None:
    assert _raw_get(path) in (303, 401)


def test_schriften_und_stil_weiter_ohne_anmeldung(auth_env: None) -> None:
    assert _raw_get("/static/fonts/open-sans-latin-400-normal.woff2") == 200
    assert _raw_get("/static/style.css") == 200


def test_nul_im_pfad_400(client: TestClient) -> None:
    token = _login(client)
    assert _raw_get("/static/map\x00.js", cookie=token) == 400


# ---------------------------------------------------------------- 2. Lebenszeichen und Bereitschaft
def test_livez_ohne_db_und_ohne_anmeldung(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt() -> None:
        raise AssertionError("livez darf die Datenbank nicht abfragen")

    monkeypatch.setattr(webmap, "_db_ping", kaputt)
    r = client.get("/livez")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_zwischengespeichert_ohne_version(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(webmap, "_db_ping", lambda: calls.append(1))
    for path in ("/readyz", "/readyz", "/api/health", "/readyz"):
        r = client.get(path)  # ohne Sitzung: kubelet und Compose-Healthcheck melden sich nicht an
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert "version" not in r.text
    assert len(calls) == 1  # höchstens eine DB-Abfrage je Zwischenspeicher-Intervall


def test_readyz_meldet_db_ausfall(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def down() -> None:
        raise psycopg.OperationalError("weg")

    monkeypatch.setattr(webmap, "_db_ping", down)
    assert client.get("/readyz").status_code == 503
    assert client.get("/api/health").status_code == 503
    assert client.get("/livez").status_code == 200


# ---------------------------------------------------------------- 3. 400 statt 500
def test_ungueltige_eingaben_kein_serverfehler(client: TestClient) -> None:
    _login(client)
    assert client.get("/api/changes", params={"run_id": "not-a-uuid"}).status_code == 422
    assert client.get("/api/errors", params={"run_id": "zzzz"}).status_code == 422
    for path, params in [
        ("/api/features", {"dataset": "abc\x00def"}),
        ("/api/indicators", {"dataset": "x\x00"}),
        ("/api/choropleth", {"dataset": "a", "geometry_dataset": "b", "indicator": "c\x00", "period": "2022"}),
    ]:
        assert client.get(path, params=params).status_code == 400, path


def test_data_error_wird_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, Any]:
        raise psycopg.errors.InvalidTextRepresentation("invalid input syntax for type uuid")

    monkeypatch.setattr(webmap, "_names", boom)
    _login(client)
    r = client.get("/api/datasets")
    assert r.status_code == 400 and "uuid" not in r.text  # keine DB-Meldung nach außen


# ---------------------------------------------------------------- 4. CSRF, Abmelden, Cookie, OpenAPI
def test_openapi_abgeschaltet(client: TestClient) -> None:
    _login(client)
    assert client.get("/openapi.json").status_code == 404


def test_anmelden_braucht_csrf_token(client: TestClient) -> None:
    assert client.post("/login", data={"username": "thb", "password": PW}).status_code == 403
    assert client.post("/login", data={"username": "thb", "password": PW, "csrf": "0" * 64}).status_code == 403
    # Token aus einer fremden Sitzung (Angreifer holt sich selbst eines) passt nicht zum Cookie des Opfers
    fremd = _csrf(_client("6.6.6.6"))
    opfer = _client()
    opfer.get("/login")
    assert opfer.post("/login", data={"username": "thb", "password": PW, "csrf": fremd}).status_code == 403
    assert _login(client)


def test_abmelden_nur_per_post_mit_token(client: TestClient) -> None:
    token = _login(client)
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", headers={"X-CSRF-Token": "f" * 64}).status_code == 403
    csrf = auth.csrf_token(webmap.auth_config(), "logout", token)  # type: ignore[arg-type]
    r = client.post("/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert f'{auth.COOKIE_NAME}=""' in r.headers["set-cookie"]


def test_info_liefert_abmelde_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(target=SimpleNamespace(adapter="demo_reference"), enabled_sources=lambda: [])
    monkeypatch.setattr(webmap, "config", lambda: cfg)
    token = _login(client)
    info = client.get("/api/info").json()
    assert info["csrf"] == auth.csrf_token(webmap.auth_config(), "logout", token)  # type: ignore[arg-type]


def test_cookie_secure_standard_und_abschaltbar(auth_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    r = c.post("/login", data={"username": "thb", "password": PW, "csrf": _csrf(c)})
    assert "secure" in r.headers["set-cookie"].lower()
    monkeypatch.setenv("MAP_COOKIE_SECURE", "false")
    _reset()
    c = TestClient(webmap.app, follow_redirects=False)  # http://, z. B. lokale Vorführung
    r = c.post("/login", data={"username": "thb", "password": PW, "csrf": _csrf(c)})
    assert r.status_code == 303 and "secure" not in r.headers["set-cookie"].lower()


# ---------------------------------------------------------------- 5. Globale Begrenzung der Fehlversuche
def test_globale_sperre_ueber_alle_adressen() -> None:
    lim = auth.LoginLimiter(max_attempts=5, lock_seconds=900, global_max=3)
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        lim.record_failure(ip, now=0)
    assert lim.is_locked("4.4.4.4", now=1)  # XFF-Rotation bringt keine weiteren Versuche
    assert not lim.is_locked("4.4.4.4", now=61)  # nur ein gleitendes Minutenfenster, keine Dauersperre


def test_globale_sperre_am_endpunkt(auth_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAP_LOGIN_GLOBAL_MAX", "2")
    _reset()
    for ip in ("1.1.1.1", "2.2.2.2"):
        c = _client(ip)
        assert c.post("/login", data={"username": "thb", "password": "falsch", "csrf": _csrf(c)}).status_code == 401
    c = _client("3.3.3.3")
    assert c.post("/login", data={"username": "thb", "password": PW, "csrf": _csrf(c)}).status_code == 429


# ---------------------------------------------------------------- 6. DoS
def test_simplify_mindestwert(client: TestClient) -> None:
    _login(client)
    assert client.get("/api/features", params={"dataset": "x", "simplify": "0"}).status_code == 422
    assert webmap.MAX_FEATURES <= 5000


def _fake_indicators(monkeypatch: pytest.MonkeyPatch, blocked: str) -> tuple[threading.Event, threading.Event]:
    started, release = threading.Event(), threading.Event()
    attrs = SimpleNamespace(
        layout="key_value", name_column="name", value_column="wert", period_column="zeitbezug", fk_metadata="m"
    )
    cfg = SimpleNamespace(
        target=SimpleNamespace(tables=SimpleNamespace(attributes=attrs)),
        enabled_sources=lambda: [SimpleNamespace(target=SimpleNamespace(dataset=d)) for d in ("a", "b")],
    )
    monkeypatch.setattr(webmap, "config", lambda: cfg)
    names = {k: sql.Identifier(k) for k in ("attr", "meta", "m_pk", "m_ds", "pm")}
    monkeypatch.setattr(webmap, "_names", lambda: names)

    class Cur:
        ds: str | None = None

        def execute(self, q: Any, params: Any = None) -> Cur:
            self.ds = params[0] if params else None
            return self

        def fetchone(self) -> dict[str, Any]:
            return {"s": None}

        def fetchall(self) -> list[dict[str, Any]]:
            if self.ds == blocked:
                started.set()
                release.wait(10)
            return [{"indikator": self.ds, "jahre": [], "werte": 1}]

    @contextmanager
    def fake_cursor(timeout_ms: int = 0) -> Iterator[Cur]:
        yield Cur()

    monkeypatch.setattr(webmap, "cursor", fake_cursor)
    webmap._indicator_cache.clear()
    return started, release


def test_kennzahlen_sperre_je_datensatz(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _login(client)
    started, release = _fake_indicators(monkeypatch, blocked="a")
    slow = threading.Thread(target=lambda: _client(session=token).get("/api/indicators", params={"dataset": "a"}))
    slow.start()
    try:
        assert started.wait(5)
        result: list[int] = []
        fast = threading.Thread(
            target=lambda: result.append(
                _client(session=token).get("/api/indicators", params={"dataset": "b"}).status_code
            )
        )
        fast.start()
        fast.join(3)
        assert result == [200], "Datensatz b wartet auf die Berechnung von a (globale Sperre)"
    finally:
        release.set()
        slow.join(5)
        webmap._indicator_cache.clear()


def test_kennzahlen_unbekannter_datensatz_ohne_db(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _login(client)
    _fake_indicators(monkeypatch, blocked="-")

    @contextmanager
    def no_db(timeout_ms: int = 0) -> Iterator[None]:
        raise AssertionError("unbekannter Datensatz darf keine DB-Verbindung belegen")
        yield

    monkeypatch.setattr(webmap, "cursor", no_db)
    r = _client(session=token).get("/api/indicators", params={"dataset": "gibt_es_nicht"})
    assert r.status_code == 200 and r.json() == []


def test_parallele_anmeldungen_umgehen_sperre_nicht(auth_env: None) -> None:
    """Viele Anfragen warten mit zurückgehaltenem Körper, bis alle die erste Sperrprüfung hinter sich haben."""
    c = _client("9.9.9.9")
    token = _csrf(c)
    nonce = c.cookies.get(auth.CSRF_COOKIE)
    assert nonce
    statuses: list[int] = []
    waiting: list[int] = []

    async def one(release: asyncio.Event, password: str) -> None:
        body = f"username=thb&password={password}&csrf={token}".encode()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "https", "path": "/login", "raw_path": b"/login", "query_string": b"", "root_path": "",
            "headers": [
                (b"host", b"testserver"), (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()), (b"cookie", f"{auth.CSRF_COOKIE}={nonce}".encode()),
            ],
            "client": ("9.9.9.9", 1), "server": ("testserver", 443),
        }  # fmt: skip

        async def receive() -> dict[str, Any]:
            waiting.append(1)
            await release.wait()
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        await webmap.app(scope, receive, send)

    async def main() -> None:
        release = asyncio.Event()
        # 19 falsche Versuche, zuletzt das richtige Kennwort: Es darf trotz Sperre nicht durchkommen
        tasks = [asyncio.create_task(one(release, "falsch")) for _ in range(19)]
        tasks.append(asyncio.create_task(one(release, PW)))
        for _ in range(10_000):  # bis alle Anfragen auf ihren Körper warten (erste Sperrprüfung vorbei)
            if len(waiting) >= len(tasks):
                break
            await asyncio.sleep(0)
        assert len(waiting) >= len(tasks)
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(main())
    assert 303 not in statuses and statuses.count(401) <= 5 and statuses.count(429) >= 15, statuses
