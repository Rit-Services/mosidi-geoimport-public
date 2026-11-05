"""Datenbankverbindungen aus Umgebungsvariablen.

Gleiche Variablennamen wie im MOSIDI-Compose (``POSTGRES_HOST``, ``POSTGRES_PORT``, ``POSTGRES_DB``,
``POSTGRES_USER``, ``POSTGRES_PASSWORD``), damit sich die Pipeline ohne Umbenennung einbinden lässt.
Das Passwort kann auch aus einer Datei kommen (``POSTGRES_PASSWORD_FILE``, Docker-Secrets).
Es gibt bewusst keine Standardpasswörter: fehlt das Passwort, bricht der Start ab.

Jede Verbindung der Pipeline setzt ``lock_timeout`` und ``statement_timeout`` (``pipeline.yaml``,
Abschnitt ``db``) und TCP-Keepalives: Eine von der MOSIDI-Anwendung gehaltene Sperre oder eine
abgerissene Verbindung lässt einen Lauf nicht endlos hängen (und mit ihm die Lauf-Sperre).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from geoimport.config.loader import ConfigError
from geoimport.config.models import DatabaseSettings
from geoimport.logging import register_secret

_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


class DatabaseUnavailable(ConfigError):
    """Datenbank nicht erreichbar (Unterklasse von ``ConfigError`` für bestehende Aufrufer)."""


def _password() -> str:
    file = os.environ.get("POSTGRES_PASSWORD_FILE")
    if file:
        try:
            pw = Path(file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"POSTGRES_PASSWORD_FILE nicht lesbar: {file}") from exc
        register_secret(pw)
        return pw
    env_pw = os.environ.get("POSTGRES_PASSWORD")
    if not env_pw:
        raise ConfigError("POSTGRES_PASSWORD (oder POSTGRES_PASSWORD_FILE) ist nicht gesetzt")
    register_secret(env_pw)
    return env_pw


def conninfo(dbname: str | None = None) -> str:
    """Verbindungsangaben für die Ziel- bzw. eine andere Datenbank auf demselben Server."""
    db = dbname or os.environ.get("POSTGRES_DB")
    user = os.environ.get("POSTGRES_USER")
    if not db or not user:
        raise ConfigError("POSTGRES_DB und POSTGRES_USER müssen gesetzt sein")
    return make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=db,
        user=user,
        password=_password(),
        application_name="geoimport",
        connect_timeout=int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "15")),
        **_tls(),
    )


def _tls() -> dict[str, str]:
    """TLS zur Datenbank: ``POSTGRES_SSLMODE`` (z. B. ``verify-full`` bei entfernter DB) und ``POSTGRES_SSLROOTCERT``.

    Ohne Angabe gilt der libpq-Standard (``PGSSLMODE`` bzw. ``prefer``: TLS wenn möglich, ohne Zertifikatsprüfung).
    """
    opts = {}
    mode = os.environ.get("POSTGRES_SSLMODE", "").strip()
    if mode:
        if mode not in _SSLMODES:
            raise ConfigError(f"POSTGRES_SSLMODE ungültig: {mode!r} (erlaubt: {', '.join(_SSLMODES)})")
        opts["sslmode"] = mode
    root = os.environ.get("POSTGRES_SSLROOTCERT", "").strip()
    if root:
        opts["sslrootcert"] = root
    return opts


def session_conninfo(base: str, settings: DatabaseSettings | None = None) -> str:
    """Zeitlimits (``options=-c …``) und TCP-Keepalives ergänzen."""
    s = settings or DatabaseSettings()
    return make_conninfo(
        base,
        options=f"-c lock_timeout={int(s.lock_timeout_s * 1000)} -c statement_timeout={int(s.statement_timeout_s * 1000)}",
        keepalives=1,
        keepalives_idle=s.keepalives_idle_s,
        keepalives_interval=10,
        keepalives_count=6,
    )


def connect(
    dbname: str | None = None, autocommit: bool = False, settings: DatabaseSettings | None = None
) -> psycopg.Connection[Any]:
    """Neue Verbindung mit Zeitlimits; nicht erreichbar → ``DatabaseUnavailable`` mit verständlicher Meldung."""
    info = session_conninfo(conninfo(dbname), settings)
    try:
        return psycopg.connect(info, autocommit=autocommit)
    except psycopg.OperationalError as exc:
        host = os.environ.get("POSTGRES_HOST", "localhost")
        raise DatabaseUnavailable(
            f"Datenbank nicht erreichbar ({host}/{dbname or os.environ.get('POSTGRES_DB')}): {exc}"
        ) from exc


def current_database(conn: psycopg.Connection[Any]) -> str:
    """Name der verbundenen Datenbank."""
    row = conn.execute("SELECT current_database()").fetchone()
    return str(row[0]) if row else ""
