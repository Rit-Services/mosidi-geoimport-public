"""Anmeldung der Demo-Karte (Demonstrator im Internet) – ohne Datenbank."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from webmap import app as webmap
from webmap import auth

PW = "ein-sicheres-kennwort-123"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MAP_AUTH_USER", "thb")
    monkeypatch.setenv("MAP_AUTH_PASSWORD", PW)
    monkeypatch.setenv("MAP_SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("MAP_LOGIN_MAX_ATTEMPTS", "3")
    webmap.auth_config.cache_clear()
    webmap._limiter = None
    yield TestClient(webmap.app, base_url="https://testserver", follow_redirects=False)
    webmap.auth_config.cache_clear()
    webmap._limiter = None


def _form(client: TestClient, password: str) -> dict[str, str]:
    """Formularfelder inkl. CSRF-Token der Login-Seite (setzt dabei das CSRF-Cookie)."""
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/login").text)
    assert m
    return {"username": "thb", "password": password, "csrf": m.group(1)}


def test_ohne_sitzung_zur_anmeldung(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/datasets").status_code == 401
    assert client.get("/static/map.js").status_code == 303
    page = client.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.text and "keine Seite der Technischen" in page.text
    assert client.get("/static/style.css").status_code == 200  # Login-Seite braucht ihren Stil
    assert client.get("/static/fonts/open-sans-latin-400-normal.woff2").status_code == 200


def test_anmelden_und_abmelden(client: TestClient) -> None:
    r = client.post("/login", data=_form(client, PW))
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.cookies.get(auth.COOKIE_NAME)
    assert (
        cookie
        and "httponly" in r.headers["set-cookie"].lower()
        and "samesite=strict" in r.headers["set-cookie"].lower()
    )
    assert client.get("/", cookies={auth.COOKIE_NAME: cookie}).status_code == 200
    assert client.get("/login", cookies={auth.COOKIE_NAME: cookie}).status_code == 303  # schon angemeldet
    cfg = webmap.auth_config()
    assert cfg is not None
    out = client.post("/logout", headers={"X-CSRF-Token": auth.csrf_token(cfg, "logout", cookie)})
    assert out.status_code == 303 and auth.COOKIE_NAME in out.headers["set-cookie"]


def test_falsches_kennwort_und_sperre(client: TestClient) -> None:
    r = client.post("/login", data=_form(client, "falsch"))
    assert r.status_code == 401 and "falsch" in r.text and auth.COOKIE_NAME not in r.headers.get("set-cookie", "")
    client.post("/login", data=_form(client, "falsch"))
    assert client.post("/login", data=_form(client, "falsch")).status_code == 429
    # gesperrt: auch das richtige Kennwort hilft vorerst nicht
    assert client.post("/login", data=_form(client, PW)).status_code == 429


def test_gefaelschte_sitzung(client: TestClient) -> None:
    cfg = webmap.auth_config()
    assert cfg is not None
    token = auth.create_session_token(cfg)
    forged = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    assert client.get("/", cookies={auth.COOKIE_NAME: forged}).status_code == 303
    expired = auth.create_session_token(cfg, now=0)
    assert not auth.verify_session_token(cfg, expired)


def test_konfiguration() -> None:
    assert auth.load_auth_config({}) is None  # Compose-Setup: Anmeldung aus
    with pytest.raises(RuntimeError):
        auth.load_auth_config({"MAP_AUTH_REQUIRED": "true"})  # Kubernetes: ohne Zugangsdaten kein Start
    with pytest.raises(RuntimeError):
        auth.load_auth_config({"MAP_AUTH_USER": "thb", "MAP_AUTH_PASSWORD": "kurz"})
    with pytest.raises(RuntimeError):
        auth.load_auth_config({"MAP_AUTH_USER": "thb", "MAP_AUTH_PASSWORD": PW, "MAP_SESSION_SECRET": "zu-kurz"})


def test_manipulierte_eingaben_ohne_serverfehler(client: TestClient) -> None:
    cfg = webmap.auth_config()
    assert cfg is not None
    for cookie in ("1.ä.x", "²³.a.b", "9999999999.a.ü"):
        assert not auth.verify_session_token(cfg, cookie)
    raw_cookie = f"{auth.COOKIE_NAME}=1.ä.x".encode("latin-1")
    assert client.get("/", headers={"cookie": raw_cookie}).status_code == 303  # type: ignore[dict-item]
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert client.post("/login", content=b"a=1&" * 2000, headers=form).status_code == 413
    assert client.post("/login", content=b"a=1&b=2&c=3&d=4&e=5", headers=form).status_code == 400
