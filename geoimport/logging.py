"""JSON-Logs auf stdout (Docker-Logs), mit Lauf- und Quellkennung in jeder Zeile.

Felder: ``ts``, ``level``, ``run_id``, ``source_id``, ``event``, ``msg``.
Zugangsdaten (Passwörter, Token, API-Schlüssel in URLs, Parametern und Kopfzeilen, auch in ``data``) werden
maskiert, ebenso registrierte Werte (DB-Kennwort, geheime ``${VAR}``-Werte, ``params``).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
source_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("source_id", default=None)

# Namen, deren Wert geheim ist: als ganzes Wort oder als Endung nach ``_``/``-``/``.`` (``client_secret``,
# ``X-Amz-Signature``, ``access_token``). ``key`` nur allein, damit ``business_key`` lesbar bleibt.
_HIDDEN_NAMES = (
    r"(?:[\w.-]*[_.-])?(?:pass|passwd|password|pwd|token|secret|apikey|api[_-]?key|access[_-]?key|authkey"
    r"|auth[_-]?token|auth|signature|sig|credentials?|session(?:id)?|cookie)|key"
)
SECRET_NAME_RE = re.compile(rf"(?i)^(?:{_HIDDEN_NAMES})$")
# name=wert in URLs, Formularen und libpq-Verbindungsangaben (``password=…``)
_SECRET_PARAM_RE = re.compile(rf"(?i)(?<![\w.-])({_HIDDEN_NAMES})=([^&\s\"'#;,]+)")
# Zugangsdaten in URLs jedes Schemas (https, postgresql, …); ein ``@`` im Kennwort zählt mit
_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s?#@]+(?:@[^/\s?#@]+)*)@(?=[^/\s?#@]+)")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key)(\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s,;\"']+"
)
MASK = "***"
_known: set[str] = set()


def register_secret(value: str | None) -> None:
    """Einen konkreten geheimen Wert (z. B. DB-Kennwort, API-Schlüssel aus ``${VAR}``) künftig überall maskieren."""
    if value and len(value) >= 6:  # kurze Werte würden beliebigen Text zerstückeln
        _known.add(value)


def _mask_userinfo(m: re.Match[str]) -> str:
    user, sep, _ = m.group(2).partition(":")
    return f"{m.group(1)}{user}:{MASK}@" if sep else f"{m.group(1)}{MASK}@"


def mask_secrets(text: str) -> str:
    """Maskiert Zugangsdaten: URL-Anmeldedaten, geheime Parameter, Authorization-Kopfzeilen, bekannte Werte."""
    for value in sorted(_known, key=len, reverse=True):
        if value in text:
            text = text.replace(value, MASK)
    text = _URL_CRED_RE.sub(_mask_userinfo, text)
    text = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) + ' ') if m.group(3) else ''}{MASK}", text
    )
    return _SECRET_PARAM_RE.sub(rf"\1={MASK}", text)


def mask_data(value: Any) -> Any:
    """``mask_secrets`` rekursiv; Werte unter geheimen Schlüsseln (``password``, ``token`` …) ganz ersetzen."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {
            k: MASK if isinstance(k, str) and SECRET_NAME_RE.match(k) and v else mask_data(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_data(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Eine JSON-Zeile je Log-Eintrag."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "run_id": run_id_var.get(),
            "source_id": source_id_var.get(),
            "event": getattr(record, "event", None),
            "msg": mask_secrets(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if extra:
            entry["data"] = mask_data(extra)
        if record.exc_info:
            entry["exc"] = mask_secrets(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Lesbare Ausgabe für die Konsole (``LOG_FORMAT=text``)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        src = source_id_var.get()
        prefix = f"[{src}] " if src else ""
        line = f"{datetime.now().strftime('%H:%M:%S')} {record.levelname:<7} {prefix}{record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return mask_secrets(line)


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Konfiguriert das Root-Logging einmalig (Level aus ``LOG_LEVEL``, Format aus ``LOG_FORMAT``)."""
    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    fmt = fmt or os.environ.get("LOG_FORMAT", "json")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "pyogrio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def event(logger: logging.Logger, name: str, msg: str, level: int = logging.INFO, **data: Any) -> None:
    """Strukturierter Log-Eintrag mit Ereignisname und Zusatzdaten."""
    logger.log(level, msg, extra={"event": name, "data": data or None})
