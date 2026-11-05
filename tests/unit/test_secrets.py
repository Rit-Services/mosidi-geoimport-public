"""Zugangsdaten bleiben aus Logs, Berichten, Zustand und Metadaten (SECURITY_REVIEW S9, CODE_REVIEW M3)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

import geoimport.logging as glog
from geoimport.config.loader import ConfigError, expand_env
from geoimport.config.models import WfsSource
from geoimport.db import conninfo
from geoimport.sources.base import SourceFailure, clean_text
from tests.conftest import FIX
from tests.unit.test_csv import _inkar, _plugin


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    glog._known.clear()


@pytest.mark.parametrize(
    ("text", "secret", "kept"),
    [
        ("GET https://user:geheim@host/wfs?apikey=abc123&x=1", "geheim", "x=1"),
        ("https://h/f.csv?client_secret=s3cr3t&year=2025", "s3cr3t", "year=2025"),
        ("https://b.s3/x?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA1", "deadbeef", "X-Amz-Signature=***"),
        ("https://b.s3/x?X-Amz-Credential=AKIA1&X-Amz-Security-Token=tok9", "AKIA1", "X-Amz-Credential=***"),
        ("https://h/x?sig=q1w2&authkey=k3y&access_key=a1", "q1w2", "authkey=***"),
        ("postgresql://geoimport:p@ss@w0rd@db:5432/x", "p@ss", "geoimport:***@db"),
        ("https://ghp_TOKEN@github.com/x", "ghp_TOKEN", "@github.com"),
        ("host=db password=hunter2 user=u", "hunter2", "user=u"),
        ("Authorization: Bearer eyJabc.def", "eyJabc", "Bearer ***"),
        ("Feld business_key=ags, key_normalize=upper", "", "business_key=ags"),
        ("Mail an a@b.de, https://host/wfs?SERVICE=WFS&typeNames=app:gem", "", "SERVICE=WFS&typeNames=app:gem"),
    ],
)
def test_mask_secrets(text: str, secret: str, kept: str) -> None:
    masked = glog.mask_secrets(text)
    assert (not secret or secret not in masked) and kept in masked, masked


def test_registrierte_werte_ueberall_maskiert() -> None:
    glog.register_secret("ganz-geheim-42")
    glog.register_secret("abc")  # zu kurz: würde beliebigen Text zerstückeln
    assert glog.mask_secrets("Fehler: ganz-geheim-42 abgelehnt (abc)") == "Fehler: *** abgelehnt (abc)"


def test_json_log_maskiert_auch_data(capsys: pytest.CaptureFixture[str]) -> None:
    glog.setup_logging("INFO", "json")
    glog.event(
        logging.getLogger("t"),
        "x",
        "Abruf https://u:pw1234@h/",
        url="https://h/?token=t0k3n",
        headers={"Password": "x"},
    )
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "pw1234" not in line["msg"] and line["data"] == {
        "url": "https://h/?token=***",
        "headers": {"Password": "***"},
    }


def test_bericht_und_meta_texte_maskiert() -> None:
    assert "t0k3n" not in clean_text("HTTP 403 für https://h/f.csv?token=t0k3n")
    exc = SourceFailure("http", "HTTP 403", 403, "https://h/f.csv?api_key=t0k3n")
    assert exc.url and "t0k3n" not in exc.url


def test_geheime_umgebungsvariable_wird_registriert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INKAR_API_TOKEN", "tok-aus-env-1")
    monkeypatch.setenv("LGB_KEY", "lgb-schluessel-2")
    monkeypatch.setenv("WFS_LAYER", "app:gem-sichtbar")
    missing: list[str] = []
    assert expand_env({"u": "https://h/?t=${INKAR_API_TOKEN}", "l": "${WFS_LAYER}"}, missing)["l"] == "app:gem-sichtbar"
    expand_env("${LGB_KEY}", missing)
    assert glog.mask_secrets("…t=tok-aus-env-1… lgb-schluessel-2") == "…t=***… ***"
    assert "app:gem-sichtbar" in glog.mask_secrets("app:gem-sichtbar")


def test_db_kennwort_wird_registriert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in {"POSTGRES_DB": "d", "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "db-kennwort-1"}.items():
        monkeypatch.setenv(k, v)
    conninfo()
    assert "db-kennwort-1" not in glog.mask_secrets('FATAL: password "db-kennwort-1" für Benutzer u')
    pw = tmp_path / "pw"
    pw.write_text("datei-kennwort-2\n")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(pw))
    conninfo()
    assert glog.mask_secrets("x datei-kennwort-2") == "x ***"


def test_db_tls_einstellbar(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {"POSTGRES_DB": "d", "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "pw-123456"}.items():
        monkeypatch.setenv(k, v)
    assert "sslmode" not in conninfo()  # ohne Angabe: libpq-Standard (PGSSLMODE)
    monkeypatch.setenv("POSTGRES_SSLMODE", "verify-full")
    monkeypatch.setenv("POSTGRES_SSLROOTCERT", "/run/secrets/db/ca.crt")
    info = conninfo()
    assert "sslmode=verify-full" in info and "sslrootcert=/run/secrets/db/ca.crt" in info
    monkeypatch.setenv("POSTGRES_SSLMODE", "verify_full")
    with pytest.raises(ConfigError, match="POSTGRES_SSLMODE"):
        conninfo()


@pytest.mark.parametrize("bad", ["TODO/../../x", "TODO_TH quelle", "TODO.."])
def test_quell_id_regel_auch_fuer_vorlagen(bad: str) -> None:
    base = {"type": "wfs", "enabled": False, "title": "t", "license": "l", "business_key": ["a"], "target": {"dataset": "d"},
            "wfs": {"url": "https://x/wfs", "type_name": "a:b"}}  # fmt: skip
    with pytest.raises(ValueError, match="id"):
        WfsSource.model_validate(base | {"id": bad})
    assert WfsSource.model_validate(base | {"id": "TODO_TH_wfs_quelle_2"}).id == "TODO_TH_wfs_quelle_2"


def test_csv_token_in_params_nie_gespeichert(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/d.zip", query_string={"token": "tok-csv-77"}).respond_with_data(
        (FIX / "csv" / "inkar_synthetisch.zip").read_bytes(), headers={"ETag": '"v1"'}
    )
    src = _inkar(FIX / "csv" / "inkar_synthetisch.zip")
    src["csv"]["location"] = {"url": httpserver.url_for("/d.zip"), "params": {"token": "tok-csv-77"}}
    p = _plugin(tmp_path, src)
    fetched = p.fetch()
    p.ctx.state.update(fetched.state_updates)
    stored = json.dumps([fetched.info, [a.url for a in fetched.artifacts], p.location_url()])
    stored += "".join(f.read_text() for f in (tmp_path / "state").rglob("*") if f.is_file())
    assert "tok-csv-77" not in stored and httpserver.url_for("/d.zip") in stored


def test_csv_token_in_url_maskiert_gespeichert(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/d.zip").respond_with_data((FIX / "csv" / "inkar_synthetisch.zip").read_bytes())
    src = _inkar(FIX / "csv" / "inkar_synthetisch.zip")
    src["csv"]["location"] = {"url": httpserver.url_for("/d.zip") + "?api_key=tok-url-88"}
    p = _plugin(tmp_path, src)
    fetched = p.fetch()
    assert httpserver.log[-1][0].args["api_key"] == "tok-url-88"  # Anfrage selbst unverändert
    stored = json.dumps([fetched.info, fetched.state_updates, [a.url for a in fetched.artifacts], p.location_url()])
    assert "tok-url-88" not in stored and "api_key=***" in stored
