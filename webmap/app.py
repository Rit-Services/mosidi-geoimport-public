"""Demo-Karte: FastAPI mit GeoJSON-Endpunkten, nur lesend.

Liest die Zieltabellen über dieselbe ``target.yaml`` wie die Pipeline (keine fest codierten
Tabellennamen) und die Lauf-Historie aus ``pipeline_meta``. Jede Abfrage läuft in einer
Read-only-Transaktion mit ``statement_timeout``. Für den Betrieb ist eine eigene DB-Rolle
mit reinen Leserechten vorgesehen (``MAP_DB_USER``).

Funktionen: Datensätze, Änderungskarte „Letzter Import“, Fehler-Marker, Umkreissuche um den
Campus der TH Brandenburg, Kennzahlenkarte (CSV-Kennzahl je Gemeinde über den AGS).

Betrieb: ``/livez`` (Prozess lebt, ohne Datenbank) und ``/readyz`` (Datenbank erreichbar, höchstens alle
``READY_CACHE_SECONDS`` geprüft); beide ohne Anmeldung. ``/api/health`` verhält sich wie ``/readyz``.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import posixpath
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.rows import dict_row

from geoimport import __version__
from geoimport.config.loader import LoadedConfig, load_config
from geoimport.db import conninfo
from webmap import auth

log = logging.getLogger("webmap")

STATIC = Path(__file__).with_name("static")
# Campus TH Brandenburg, Magdeburger Straße 50 – OSM/Nominatim way 86953877, abgefragt 29.10.2026 (© OSM-Mitwirkende, ODbL)
CAMPUS = {"lat": 52.4107103, "lon": 12.5371959, "name": "Technische Hochschule Brandenburg"}
# Die Karte zeigt Gemeinden/Kreise eines Landes (Brandenburg: 413 Gemeinden); mehr braucht die Oberfläche nicht.
MAX_FEATURES = 5_000
# Kleinste Vereinfachung in Grad (≈ 7–11 m in Brandenburg); 0 hieße volle Auflösung aller Stützpunkte.
MIN_SIMPLIFY = 0.0001
STATEMENT_TIMEOUT_MS = int(os.environ.get("MAP_STATEMENT_TIMEOUT_MS", "20000"))
# Die Indikatorliste aggregiert den ganzen CSV-Bestand (INKAR: ca. 1,3 Mio. Zeilen); sie wird bis zum nächsten
# abgeschlossenen Lauf zwischengespeichert und darf einmalig länger rechnen.
INDICATORS_TIMEOUT_MS = int(os.environ.get("MAP_INDICATORS_TIMEOUT_MS", "180000"))
_indicator_cache: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
# Sperre je Datensatz: gleichzeitige Aufrufe starten dieselbe Aggregation nicht mehrfach, andere Datensätze warten nicht
_indicator_locks: dict[str, threading.Lock] = {}
_indicator_locks_guard = threading.Lock()
# Höchstzahl gleichzeitiger DB-Verbindungen der Karte (schützt max_connections, die sich die Pipeline teilt)
MAX_DB_CONNECTIONS = int(os.environ.get("MAP_MAX_DB_CONNECTIONS", "8"))
_db_slots = threading.BoundedSemaphore(MAX_DB_CONNECTIONS)
MAX_LOGIN_BODY = 4096
# Bereitschaft (DB erreichbar) höchstens so oft wirklich prüfen – öffentlich erreichbar, darf keine DB-Plätze belegen
READY_CACHE_SECONDS = 5.0

app = FastAPI(
    title="mosidi-geoimport – Demo-Karte", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


@lru_cache(maxsize=1)
def auth_config() -> auth.AuthConfig | None:
    """Anmeldung nur, wenn MAP_AUTH_PASSWORD gesetzt ist (Demonstrator im Internet)."""
    return auth.load_auth_config()


_limiter: auth.LoginLimiter | None = None
# Ohne Anmeldung erreichbar: Login-Seite mit ihrem Stil und ihren Schriften
_PUBLIC = {"/login", "/logout", "/favicon.ico", "/static/style.css"}
# Proben für Kubernetes/Compose: ohne Anmeldung und ohne Prüfung der Zugangsdaten im Middleware
_PROBES = {"/livez", "/readyz", "/api/health"}


def limiter() -> auth.LoginLimiter:
    """Sperre für Fehlversuche (eine Instanz je Prozess)."""
    global _limiter
    cfg = auth_config()
    if _limiter is None and cfg:
        _limiter = auth.LoginLimiter(cfg.max_attempts, cfg.lock_minutes * 60, cfg.global_max)
    return _limiter or auth.LoginLimiter()


def _is_public(path: str) -> bool:
    """Öffentlich nur in kanonischer Form: ``/static/fonts/../map.js`` (auch ``..%2f``, ``%2e%2e`` – uvicorn
    dekodiert vorher) würde sonst die Anmeldung umgehen, weil StaticFiles die Punkte erst danach auflöst."""
    if posixpath.normpath(path) != path:
        return False
    return path in _PUBLIC or path.startswith("/static/fonts/")


@app.middleware("http")
async def require_login(request: Request, call_next: Any) -> Response:
    """Ohne gültige Sitzung: Seiten → /login, API → 401."""
    path: str = request.scope["path"]
    if path in _PROBES:
        probe: Response = await call_next(request)
        return probe
    cfg = auth_config()
    if cfg is None or _is_public(path) or auth.verify_session_token(cfg, request.cookies.get(auth.COOKIE_NAME)):
        response: Response = await call_next(request)
        return response
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def reject_nul(request: Request, call_next: Any) -> Response:
    """NUL-Zeichen in Pfad oder Parametern → 400 (PostgreSQL-Text und Dateipfade erlauben sie nicht)."""
    if "\x00" in request.scope["path"] or any(
        "\x00" in k or "\x00" in v for k, v in request.query_params.multi_items()
    ):
        return JSONResponse({"detail": "Ungültiges Zeichen (NUL) in der Anfrage"}, status_code=400)
    response: Response = await call_next(request)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    """Sicherheits-Header für alle Antworten."""
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # Die OSM-Kachelserver verlangen einen Referer (Tile Usage Policy), sonst HTTP 403 –
    # deshalb nur den Ursprung senden, nie Pfad oder Parameter.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.exception_handler(psycopg.DataError)
async def data_error(request: Request, exc: psycopg.DataError) -> JSONResponse:
    """Ungültige Parameterwerte (z. B. Typumwandlung in der Datenbank) sind ein Fehler der Anfrage, kein 500."""
    log.info("Ungültiger Parameter bei %s: %s", request.url.path, type(exc).__name__)
    return JSONResponse({"detail": "Ungültiger Parameter"}, status_code=400)


@lru_cache(maxsize=1)
def config() -> LoadedConfig:
    """Konfiguration einmal laden (wie die Pipeline)."""
    return load_config()


@contextmanager
def cursor(timeout_ms: int = STATEMENT_TIMEOUT_MS) -> Iterator[psycopg.Cursor[dict[str, Any]]]:
    """Nur lesende Transaktion mit Zeitlimit; höchstens ``MAX_DB_CONNECTIONS`` gleichzeitig."""
    if not _db_slots.acquire(timeout=10):
        raise HTTPException(503, "Datenbank ausgelastet, bitte gleich noch einmal versuchen")
    try:
        with psycopg.connect(conninfo(), row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_ms)))
                yield cur
    finally:
        _db_slots.release()


def _t(schema: str, name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def _names() -> dict[str, Any]:
    cfg = config()
    t = cfg.target.tables
    s = cfg.target.schema_name
    g = t.geometries
    name_col = next((c for c, tpl in g.columns.items() if tpl == "{record.name}"), None)
    if not g.fk_metadata:
        raise HTTPException(501, "Die Demo-Karte erwartet geometries.fk_metadata (Referenzmodell)")
    return {
        "meta": _t(s, t.metadata.name),
        "geo": _t(s, g.name),
        "attr": _t(s, t.attributes.name),
        "m_pk": sql.Identifier(t.metadata.pk),
        "m_ds": sql.Identifier(t.metadata.dataset_column),
        "g_pk": sql.Identifier(g.pk),
        "g_fk": sql.Identifier(g.fk_metadata),
        "g_key": sql.Identifier(g.object_key_column),
        "g_geom": sql.Identifier(g.geometry_column),
        "g_name": sql.SQL("g.{}").format(sql.Identifier(name_col)) if name_col else sql.SQL("NULL::text"),
        "pm": sql.Identifier(cfg.pipeline.meta.schema_name),
    }


def _json(content: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    """JSON-Antwort mit Umwandlung von Datum/Decimal."""
    return JSONResponse(jsonable_encoder(content), headers=headers)


def _fc(rows: list[dict[str, Any]]) -> dict[str, Any]:
    feats = []
    for r in rows:
        geom = r.pop("geometry")
        feats.append({"type": "Feature", "geometry": geom, "properties": r})
    return {"type": "FeatureCollection", "features": feats}


def _bbox(bbox: str | None) -> list[float] | None:
    if not bbox:
        return None
    try:
        vals = [float(v) for v in bbox.split(",")]
    except ValueError as exc:
        raise HTTPException(400, "bbox: vier Zahlen minx,miny,maxx,maxy") from exc
    if (
        len(vals) != 4
        or not all(math.isfinite(v) for v in vals)
        or not (-180 <= vals[0] <= vals[2] <= 180)
        or not (-90 <= vals[1] <= vals[3] <= 90)
    ):
        raise HTTPException(400, "bbox außerhalb des gültigen Bereichs (EPSG:4326)")
    return vals


# ---------------------------------------------------------------------- Seiten
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Karte."""
    return FileResponse(STATIC / "index.html")


def _login_page(cfg: auth.AuthConfig, request: Request, error: str = "", status: int = 200) -> HTMLResponse:
    """Login-Seite mit CSRF-Token (Double-Submit: Zufallswert im Cookie, HMAC davon im Formular)."""
    nonce = request.cookies.get(auth.CSRF_COOKIE) or ""
    if not auth.is_csrf_nonce(nonce):
        nonce = auth.new_csrf_nonce()
    html = (Path(__file__).with_name("login.html")).read_text(encoding="utf-8")
    msg = f'<p class="login-error" role="alert">{escape(error)}</p>' if error else ""
    html = html.replace("<!--FEHLER-->", msg).replace("<!--CSRF-->", escape(auth.csrf_token(cfg, "login", nonce)))
    response = HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        auth.CSRF_COOKIE, nonce, path="/login", httponly=True, samesite="strict", secure=cfg.cookie_secure
    )
    return response


def _client(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"


@app.get("/login", include_in_schema=False)
def login_form(request: Request) -> Response:
    """Anmeldeseite (bei abgeschalteter Anmeldung direkt zur Karte)."""
    cfg = auth_config()
    if cfg is None or auth.verify_session_token(cfg, request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return _login_page(cfg, request)


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request) -> Response:
    """Formular auswerten (application/x-www-form-urlencoded, ohne Zusatzpaket)."""
    cfg = auth_config()
    if cfg is None:
        return RedirectResponse("/", status_code=303)
    client = _client(request)
    if limiter().is_locked(client):
        return _login_page(cfg, request, "Zu viele Fehlversuche. Bitte in einigen Minuten erneut versuchen.", 429)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_LOGIN_BODY:
            return _login_page(cfg, request, "Anfrage zu groß.", 413)
    try:
        form = parse_qs(body.decode("utf-8", errors="replace"), max_num_fields=4)
    except ValueError:
        return _login_page(cfg, request, "Ungültige Anfrage.", 400)
    if not auth.check_csrf(cfg, "login", request.cookies.get(auth.CSRF_COOKIE), form.get("csrf", [""])[0]):
        return _login_page(cfg, request, "Formular abgelaufen. Bitte erneut anmelden.", 403)
    # Erneut prüfen: Während der Körper eintraf, können parallele Anfragen die Sperre ausgelöst haben.
    # Zwischen dieser Prüfung und record_failure liegt kein await.
    if limiter().is_locked(client):
        return _login_page(cfg, request, "Zu viele Fehlversuche. Bitte in einigen Minuten erneut versuchen.", 429)
    user = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    if not auth.check_credentials(cfg, user, password):
        locked = limiter().record_failure(client)
        text = (
            "Zu viele Fehlversuche. Bitte in einigen Minuten erneut versuchen."
            if locked
            else "Benutzername oder Kennwort ist falsch."
        )
        return _login_page(cfg, request, text, 429 if locked else 401)
    limiter().reset(client)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_token(cfg),
        max_age=int(cfg.session_hours * 3600),
        httponly=True,
        samesite="strict",
        secure=cfg.cookie_secure,
    )
    response.delete_cookie(auth.CSRF_COOKIE, path="/login", httponly=True, samesite="strict", secure=cfg.cookie_secure)
    return response


@app.post("/logout", include_in_schema=False)
def logout(request: Request) -> Response:
    """Sitzung beenden – nur per POST mit dem an die Sitzung gebundenen Token (Kopfzeile X-CSRF-Token)."""
    cfg = auth_config()
    response = RedirectResponse("/login", status_code=303)
    session = request.cookies.get(auth.COOKIE_NAME)
    if cfg is None or not auth.verify_session_token(cfg, session):
        return response  # nichts abzumelden; fremde Seiten können so kein Cookie löschen
    if not auth.check_csrf(cfg, "logout", session, request.headers.get("x-csrf-token")):
        return JSONResponse({"detail": "CSRF-Token fehlt oder ist ungültig"}, status_code=403)
    response.delete_cookie(auth.COOKIE_NAME, httponly=True, samesite="strict", secure=cfg.cookie_secure)
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Kein Favicon (vermeidet 404 im Browser-Log)."""
    return Response(status_code=204)


@dataclass
class _ReadyCache:
    at: float = -math.inf
    ok: bool = False


_ready_cache = _ReadyCache()
_ready_lock = threading.Lock()


def _db_ping() -> None:
    """Eigene kurze Verbindung, nicht über die Abfrage-Plätze: volle Plätze bedeuten nicht „nicht bereit“."""
    with psycopg.connect(conninfo(), connect_timeout=3) as conn:
        conn.execute("SELECT 1")


def _is_ready() -> bool:
    """Datenbank erreichbar und Zugangsdaten gültig; höchstens eine Prüfung je ``READY_CACHE_SECONDS``."""
    if time.monotonic() - _ready_cache.at < READY_CACHE_SECONDS or not _ready_lock.acquire(blocking=False):
        return _ready_cache.ok  # frisch geprüft oder Prüfung läuft gerade: letztes Ergebnis
    try:
        try:
            auth_config()  # ungültige Zugangsdaten: Pod wird nicht bereit
            _db_ping()
            ok = True
        except Exception as exc:  # noqa: BLE001 – jeder Fehler heißt „nicht bereit“
            log.warning("Demo-Karte nicht bereit: %s", type(exc).__name__)
            ok = False
        _ready_cache.ok = ok  # zuerst das Ergebnis: wer ein frisches ``at`` sieht, sieht auch das neue ``ok``
        _ready_cache.at = time.monotonic()
        return ok
    finally:
        _ready_lock.release()


def _readiness() -> JSONResponse:
    if _is_ready():
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"status": "nicht bereit"}, status_code=503, headers={"Cache-Control": "no-store"})


@app.get("/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    """Lebenszeichen ohne Datenbank (async: läuft auch, wenn alle Worker-Threads belegt sind)."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    """Bereitschaft: Datenbank erreichbar (zwischengespeichert)."""
    return _readiness()


@app.get("/api/health")
def health() -> JSONResponse:
    """Wie /readyz – für den Compose-Healthcheck und bestehende Aufrufer; ohne Versionsangabe."""
    return _readiness()


@app.get("/api/info")
def info(request: Request) -> dict[str, Any]:
    """Campus-Koordinate, Modus, Zielmodell; bei Anmeldung das Token zum Abmelden."""
    cfg = config()
    acfg = auth_config()
    session = request.cookies.get(auth.COOKIE_NAME)
    return {
        "csrf": auth.csrf_token(acfg, "logout", session) if acfg and session else None,
        "campus": CAMPUS,
        "demo_env": os.environ.get("PIPELINE_ENV") == "demo",
        "anmeldung": auth_config() is not None,
        "adapter": cfg.target.adapter,
        "hinweis_modell": "Referenzmodell von RIT (Annahme), nicht das Datenmodell der TH"
        if cfg.target.adapter == "demo_reference"
        else None,
        "quellen": [
            {
                "id": s.id,
                "titel": s.title,
                "dataset": s.target.dataset,
                "typ": s.type,
                "lizenz": s.license,
                "quellenvermerk": s.attribution,
            }
            for s in cfg.enabled_sources()
        ],
    }


@app.get("/api/datasets")
def datasets() -> list[dict[str, Any]]:
    """Datensätze mit Anzahl Objekte und Attributzeilen."""
    n = _names()
    q = sql.SQL(
        """SELECT m.{m_ds} AS dataset,
                  (SELECT count(*) FROM {geo} g WHERE g.{g_fk} = m.{m_pk}) AS objekte
           FROM {meta} m ORDER BY 1"""
    ).format(**n)
    with cursor() as cur:
        return list(cur.execute(q).fetchall())


@app.get("/api/features")
def features(
    dataset: str = Query(..., max_length=100),
    bbox: str | None = None,
    simplify: float = Query(0.0002, ge=MIN_SIMPLIFY, le=0.01),
) -> JSONResponse:
    """Objekte eines Datensatzes als GeoJSON (Geometrie für die Anzeige vereinfacht)."""
    n = _names()
    box = _bbox(bbox)
    where = sql.SQL("m.{m_ds} = %(ds)s").format(**n)
    if box:
        where += sql.SQL(" AND g.{g_geom} && ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326)").format(**n)
    q = (
        sql.SQL(
            """SELECT g.{g_key}::text AS key, {g_name} AS name,
                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(g.{g_geom}, %(tol)s), 6)::json AS geometry
           FROM {geo} g JOIN {meta} m ON m.{m_pk} = g.{g_fk} WHERE """
        ).format(**n)
        + where
        + sql.SQL(" LIMIT %(lim)s")
    )
    params = {"ds": dataset, "tol": simplify, "lim": MAX_FEATURES}
    if box:
        params.update(dict(zip(("x0", "y0", "x1", "y1"), box, strict=True)))
    with cursor() as cur:
        rows = cur.execute(q, params).fetchall()
    return _json(_fc(rows))


@app.get("/api/runs")
def runs(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    """Letzte Läufe mit Kennzahlen."""
    n = _names()
    q = sql.SQL(
        """SELECT run_id::text, started_at, finished_at, trigger, status, simulated, dry_run,
                  report->'summary' AS summary
           FROM {pm}.import_runs WHERE report IS NOT NULL ORDER BY started_at DESC LIMIT %s"""
    ).format(**n)
    with cursor() as cur:
        return list(cur.execute(q, (limit,)).fetchall())


def _run_id(cur: psycopg.Cursor[dict[str, Any]], run_id: uuid.UUID | None) -> dict[str, Any]:
    n = _names()
    if run_id:
        q = sql.SQL("SELECT run_id::text, simulated, started_at FROM {pm}.import_runs WHERE run_id = %s::uuid").format(
            **n
        )
        row = cur.execute(q, (str(run_id),)).fetchone()
    else:
        q = sql.SQL(
            """SELECT r.run_id::text, r.simulated, r.started_at FROM {pm}.import_runs r
               WHERE NOT r.dry_run AND EXISTS (SELECT 1 FROM {pm}.diff_keys d WHERE d.run_id = r.run_id)
               ORDER BY r.started_at DESC LIMIT 1"""
        ).format(**n)
        row = cur.execute(q).fetchone()
    if not row:
        raise HTTPException(404, "Kein Lauf mit Änderungsdaten gefunden")
    return row


@app.get("/api/changes")
def changes(run_id: uuid.UUID | None = None, source: str | None = Query(None, max_length=100)) -> JSONResponse:
    """Änderungskarte: neu/geändert/entfallen/unverändert je Objekt eines Laufs."""
    n = _names()
    with cursor() as cur:
        run = _run_id(cur, run_id)
        q = sql.SQL(
            """SELECT d.source_id AS source, d.record_key AS key, d.change,
                      ST_AsGeoJSON(ST_SimplifyPreserveTopology(d.geom, 0.0002), 6)::json AS geometry
               FROM {pm}.diff_keys d WHERE d.run_id = %(run)s::uuid AND d.geom IS NOT NULL
                 AND (%(src)s::text IS NULL OR d.source_id = %(src)s::text) LIMIT %(lim)s"""
        ).format(**n)
        rows = cur.execute(q, {"run": run["run_id"], "src": source, "lim": MAX_FEATURES}).fetchall()
        stats = cur.execute(
            sql.SQL("SELECT change, count(*) AS n FROM {pm}.diff_keys WHERE run_id = %s::uuid GROUP BY 1").format(**n),
            (run["run_id"],),
        ).fetchall()
    out = _fc(rows)
    out["run"] = {**run, "counts": {r["change"]: r["n"] for r in stats}}
    return _json(out)


@app.get("/api/errors")
def errors(run_id: uuid.UUID | None = None) -> JSONResponse:
    """Fehler-Marker eines Laufs (mit Geometrie) und Liste ohne Geometrie."""
    n = _names()
    with cursor() as cur:
        if run_id:
            run = _run_id(cur, run_id)
        else:
            row = cur.execute(
                sql.SQL(
                    "SELECT run_id::text, simulated, started_at FROM {pm}.import_runs WHERE NOT dry_run "
                    "AND report IS NOT NULL ORDER BY started_at DESC LIMIT 1"
                ).format(**n)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Noch kein Lauf")
            run = row
        q = sql.SQL(
            """SELECT source_id AS source, record_key AS key, reason_code, reason,
                      ST_AsGeoJSON(ST_PointOnSurface(geom), 6)::json AS geometry
               FROM {pm}.import_errors WHERE run_id = %s::uuid ORDER BY id LIMIT 2000"""
        ).format(**n)
        rows = cur.execute(q, (run["run_id"],)).fetchall()
    with_geom = [r for r in rows if r["geometry"]]
    without = [{k: v for k, v in r.items() if k != "geometry"} for r in rows if not r["geometry"]]
    out = _fc(with_geom)
    out["run"] = run
    out["ohne_geometrie"] = without
    return _json(out)


@app.get("/api/radius")
def radius(
    dataset: str = Query(..., max_length=100),
    lat: float = Query(CAMPUS["lat"], ge=-90, le=90),
    lon: float = Query(CAMPUS["lon"], ge=-180, le=180),
    radius_m: float = Query(2000, ge=100, le=20000),
) -> JSONResponse:
    """Objekte im Umkreis (Meter, über ``geography``) mit Entfernung und Fläche im Umkreis."""
    if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(radius_m)):
        raise HTTPException(400, "Koordinaten ungültig")
    n = _names()
    q = sql.SQL(
        """WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography AS pt)
           SELECT g.{g_key}::text AS key, {g_name} AS name,
                  round(ST_Distance(g.{g_geom}::geography, p.pt)::numeric, 1)::float8 AS entfernung_m,
                  round(ST_Area(ST_Intersection(g.{g_geom}::geography, ST_Buffer(p.pt, %(r)s)))::numeric, 0)::float8 AS flaeche_im_umkreis_m2,
                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(g.{g_geom}, 0.0002), 6)::json AS geometry
           FROM {geo} g JOIN {meta} m ON m.{m_pk} = g.{g_fk}, p
           WHERE m.{m_ds} = %(ds)s AND ST_DWithin(g.{g_geom}::geography, p.pt, %(r)s)
           ORDER BY entfernung_m LIMIT 500"""
    ).format(**n)
    with cursor() as cur:
        rows = cur.execute(q, {"lon": lon, "lat": lat, "r": radius_m, "ds": dataset}).fetchall()
    out = _fc(rows)
    out["summe"] = {
        "anzahl": len(rows),
        "flaeche_im_umkreis_m2": float(sum(r["properties"]["flaeche_im_umkreis_m2"] or 0 for r in out["features"])),
    }
    out["zentrum"] = {"lat": lat, "lon": lon, "radius_m": radius_m}
    return _json(out, headers={"Cache-Control": "no-store"})


def _indicator_lock(dataset: str) -> threading.Lock:
    with _indicator_locks_guard:
        return _indicator_locks.setdefault(dataset, threading.Lock())


@app.get("/api/indicators")
def indicators(dataset: str = Query(..., max_length=100)) -> list[dict[str, Any]]:
    """Kennzahlen eines CSV-Datensatzes mit verfügbaren Jahren."""
    cfg = config()
    a = cfg.target.tables.attributes
    if a.layout != "key_value" or not a.name_column or not a.value_column:
        raise HTTPException(501, "Kennzahlenkarte braucht layout key_value")
    if dataset not in {s.target.dataset for s in cfg.enabled_sources()}:
        return []  # nur konfigurierte Datensätze: beliebige Parameter belegen weder Datenbank noch Sperren
    n = _names()
    period = sql.Identifier(a.period_column) if a.period_column else sql.SQL("NULL")
    q = sql.SQL(
        """SELECT a.{name} AS indikator, array_agg(DISTINCT a.{period} ORDER BY a.{period}) AS jahre, count(*) AS werte
           FROM {attr} a JOIN {meta} m ON m.{m_pk} = a.{fk} WHERE m.{m_ds} = %s GROUP BY 1 ORDER BY 1 LIMIT 1000"""
    ).format(name=sql.Identifier(a.name_column), period=period, fk=sql.Identifier(a.fk_metadata or "metadaten_id"), **n)
    with cursor() as cur:
        stamp = cur.execute(sql.SQL("SELECT max(finished_at) AS s FROM {pm}.import_runs").format(**n)).fetchone()
    key = stamp["s"] if stamp else None
    with _indicator_lock(dataset):
        cached = _indicator_cache.get(dataset)
        if cached and cached[0] == key:
            return cached[1]
        with cursor(INDICATORS_TIMEOUT_MS) as cur:
            rows = list(cur.execute(q, (dataset,)).fetchall())
        if rows:  # nur vorhandene Datensätze merken, beliebige Parameter füllen den Speicher nicht
            _indicator_cache[dataset] = (key, rows)
    return rows


@app.get("/api/choropleth")
def choropleth(
    dataset: str = Query(..., max_length=100),
    geometry_dataset: str = Query(..., max_length=100),
    indicator: str = Query(..., max_length=300),
    period: str = Query(..., max_length=20),
) -> JSONResponse:
    """Gemeinden eingefärbt nach einer CSV-Kennzahl – Verknüpfung WFS + CSV über den Objektschlüssel (AGS)."""
    cfg = config()
    a = cfg.target.tables.attributes
    if not (a.object_key_column and a.name_column and a.value_column and a.period_column):
        raise HTTPException(501, "Kennzahlenkarte braucht object_key_column, name/value/period_column")
    n = _names()
    q = sql.SQL(
        """SELECT g.{g_key}::text AS key, {g_name} AS name, a.{val} AS wert,
                  ST_AsGeoJSON(ST_SimplifyPreserveTopology(g.{g_geom}, 0.0005), 6)::json AS geometry
           FROM {geo} g JOIN {meta} mg ON mg.{m_pk} = g.{g_fk} AND mg.{m_ds} = %(gds)s
           LEFT JOIN ({attr} a JOIN {meta} ma ON ma.{m_pk} = a.{fk} AND ma.{m_ds} = %(ds)s)
             ON a.{okey} = g.{g_key}::text AND a.{name} = %(ind)s AND a.{period} = %(per)s
           LIMIT %(lim)s"""
    ).format(
        val=sql.Identifier(a.value_column),
        okey=sql.Identifier(a.object_key_column),
        name=sql.Identifier(a.name_column),
        period=sql.Identifier(a.period_column),
        fk=sql.Identifier(a.fk_metadata or "metadaten_id"),
        **n,
    )
    with cursor() as cur:
        rows = cur.execute(
            q, {"gds": geometry_dataset, "ds": dataset, "ind": indicator, "per": period, "lim": MAX_FEATURES}
        ).fetchall()
    for r in rows:
        with contextlib.suppress(ValueError):
            r["wert"] = None if r["wert"] is None else float(r["wert"])
    return _json(_fc(rows))
