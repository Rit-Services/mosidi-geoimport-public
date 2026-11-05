"""Gemeinsame Test-Fixtures: Konfiguration im Temp-Verzeichnis, simulierter WFS, Testdatenbank."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
import pytest
import yaml
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

import geoimport  # noqa: F401 – setzt GDAL_SKIP, bevor ein Testmodul pyogrio importiert (SECURITY_REVIEW S1)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
CONFIG = ROOT / "config"


@pytest.fixture(scope="session")
def httpserver_listen_address() -> tuple[str, int]:
    # 127.0.0.1 statt localhost: unter Windows würde sonst jede Verbindung erst IPv6 versuchen
    return ("127.0.0.1", 0)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TARGET_CONFIG", "PIPELINE_ENV", "CONFIG_DIR", "GEOIMPORT_PLUGINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TZ", "Europe/Berlin")


# ---------------------------------------------------------------------- simulierter WFS
class FakeWfs:
    """Antwortet auf GetCapabilities/DescribeFeatureType/GetFeature mit den aufgezeichneten LGB-Antworten."""

    def __init__(self) -> None:
        self.caps = (FIX / "wfs" / "vg_wfs_capabilities.xml").read_bytes()
        self.describe = (FIX / "wfs" / "vg_wfs_describe.xsd").read_bytes()
        self.features = (FIX / "wfs" / "vg_wfs_gem_brandenburg.gml").read_bytes()
        self.hits = 6
        self.fail_status: int | None = None
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: Request) -> Response:
        params = {k.upper(): v for k, v in request.args.items()}
        self.requests.append(params)
        if self.fail_status:
            return Response("Fehler", status=self.fail_status)
        req = params.get("REQUEST", "")
        if req == "GetCapabilities":
            return Response(self.caps, content_type="text/xml")
        if req == "DescribeFeatureType":
            return Response(self.describe, content_type="text/xml")
        if req == "GetFeature":
            if params.get("RESULTTYPE") == "hits":
                body = (
                    '<?xml version="1.0"?><wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
                    f'numberMatched="{self.hits}" numberReturned="0"/>'
                )
                return Response(body, content_type="text/xml")
            return Response(self.features, content_type="text/xml")
        return Response("unbekannt", status=400)


@pytest.fixture
def fake_wfs(httpserver: HTTPServer) -> FakeWfs:
    wfs = FakeWfs()
    httpserver.expect_request("/ows/vg_wfs").respond_with_handler(wfs)
    return wfs


# ---------------------------------------------------------------------- Konfiguration
def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def config_dir(tmp_path: Path, httpserver: HTTPServer) -> Path:
    """Konfiguration wie im Repo, aber Daten im Temp-Verzeichnis und Quellen auf den lokalen Testserver."""
    cfg = tmp_path / "config"
    (cfg / "sources").mkdir(parents=True)
    pipeline = load_yaml(CONFIG / "pipeline.yaml")
    data = tmp_path / "data"
    pipeline["storage"] = {
        "raw_dir": str(data / "raw"),
        "report_dir": str(data / "reports"),
        "state_dir": str(data / "state"),
        "retention_days": 30,
    }
    pipeline["http"].update({"retries": 1, "backoff_base_s": 0, "timeout_s": 10})
    pipeline["meta"]["diff_geoms"] = True
    write_yaml(cfg / "pipeline.yaml", pipeline)
    shutil.copy(CONFIG / "target.demo.yaml", cfg / "target.yaml")
    wfs = load_yaml(CONFIG / "sources" / "geobasis_bb_gemeinden.yaml")
    wfs["wfs"]["url"] = httpserver.url_for("/ows/vg_wfs")
    wfs["wfs"]["paging"]["throttle_ms"] = 0
    wfs["guards"]["min_features"] = 5
    write_yaml(cfg / "sources" / "geobasis_bb_gemeinden.yaml", wfs)
    csv = load_yaml(CONFIG / "sources" / "inkar_gemeinden.yaml")
    csv["csv"]["location"] = {"url": httpserver.url_for("/inkar/inkar_2025.zip")}
    csv["csv"]["chunksize"] = 1000
    csv["guards"]["min_rows"] = 10
    write_yaml(cfg / "sources" / "inkar_gemeinden.yaml", csv)
    return cfg


@pytest.fixture
def inkar_zip(httpserver: HTTPServer) -> Path:
    path = FIX / "csv" / "inkar_synthetisch.zip"
    httpserver.expect_request("/inkar/inkar_2025.zip").respond_with_data(
        path.read_bytes(), content_type="application/zip", headers={"ETag": '"fixture-1"'}
    )
    return path


# ---------------------------------------------------------------------- Datenbank
def _db_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def db_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Setzt POSTGRES_* auf die Testdatenbank und leert sie (Schemas ref, pipeline_meta, th)."""
    url = _db_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL nicht gesetzt (docker compose -f compose.test.yaml up -d --wait)")
    u = urlparse(url)
    assert u.path.endswith("_demo"), "Testdatenbank muss auf _demo enden"
    monkeypatch.setenv("POSTGRES_HOST", u.hostname or "localhost")
    monkeypatch.setenv("POSTGRES_PORT", str(u.port or 5432))
    monkeypatch.setenv("POSTGRES_DB", u.path.lstrip("/"))
    monkeypatch.setenv("POSTGRES_USER", u.username or "")
    monkeypatch.setenv("POSTGRES_PASSWORD", u.password or "")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_unlock_all()")
        for schema in ("ref", "pipeline_meta", "th", "geoimport_meta_test"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute((ROOT / "sql" / "demo_reference_model.sql").read_text(encoding="utf-8"))
    return url


@pytest.fixture
def db(db_env: str) -> psycopg.Connection[Any]:
    with psycopg.connect(db_env, autocommit=True) as conn:
        yield conn
