"""DDL-Sperre: Der Ziel-Adapter darf die Bestandstabellen nur lesen und Daten schreiben.

Bieterantwort A6: „Die bestehenden Tabellenstrukturen dürfen nicht verändert werden.“
Jede Anweisung, die der Adapter ausführt, läuft durch ``check_sql``. Erlaubt sind nur
``SELECT``, ``INSERT``, ``UPDATE``, ``DELETE``, ``WITH``, ``COPY … FROM STDIN`` in temporäre Tabellen,
``CREATE TEMP TABLE`` und ``SET LOCAL``; dazu ``REFRESH MATERIALIZED VIEW`` nur für die in ``target.yaml``
freigegebenen Sichten (Bieterantwort 75). Alles andere (``CREATE``, ``ALTER``, ``DROP``,
``TRUNCATE``, ``COMMENT``, ``GRANT``, ``REINDEX`` …) führt zu ``GuardViolation``.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

import psycopg
from psycopg import sql

ALLOWED_START = re.compile(
    r"^(SELECT|INSERT|UPDATE|DELETE|WITH|SET\s+LOCAL|COPY\s+tmp_geoimport_\w+|CREATE\s+TEMP(ORARY)?\s+TABLE\s+tmp_geoimport_\w+)\b",
    re.IGNORECASE,
)
FORBIDDEN = re.compile(
    r"\b(CREATE|ALTER|DROP|TRUNCATE|COMMENT|GRANT|REVOKE|REINDEX|VACUUM|CLUSTER|REFRESH|SECURITY|OWNER|RENAME|"
    r"ATTACH|DETACH|IMPORT|LOCK\s+TABLE|DO|CALL|EXECUTE|PREPARE)\b",
    re.IGNORECASE,
)
_COPY_IN = re.compile(r"^COPY\s+tmp_geoimport_\w+\s*(\([^)]*\))?\s*FROM\s+STDIN\b", re.IGNORECASE)
# SELECT … INTO legt eine Tabelle an; erlaubt ist INTO nur in INSERT INTO
_INSERT_INTO = re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE)
_INTO = re.compile(r"\bINTO\b", re.IGNORECASE)
_TEMP_CREATE = re.compile(r"^CREATE\s+TEMP(ORARY)?\s+TABLE\s+tmp_geoimport_\w+", re.IGNORECASE)
_ON_COMMIT_DROP = re.compile(r"\bON\s+COMMIT\s+DROP\s*$", re.IGNORECASE)
# Ein Durchgang, damit z. B. „--“ in einer Zeichenkette nicht als Kommentar den Rest der Zeile verdeckt
_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.S)


# Bieterantwort 75: Nach dem Import wird eine konfigurierte materialisierte Sicht aktualisiert. Erlaubt ist
# genau diese Form, mit Schema und ohne Zusätze (WITH NO DATA würde die Sicht leeren).
_REFRESH = re.compile(
    r"^REFRESH\s+MATERIALIZED\s+VIEW\s+(?:CONCURRENTLY\s+)?"
    r'("?)([A-Za-z_][A-Za-z0-9_]*)\1\s*\.\s*("?)([A-Za-z_][A-Za-z0-9_]*)\3$',
    re.IGNORECASE,
)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


class GuardViolation(RuntimeError):
    """Eine Anweisung würde die Struktur der Zieldatenbank verändern."""


def _is_allowed_refresh(statement: str, allow_refresh: Collection[str]) -> bool:
    """True für ``REFRESH MATERIALIZED VIEW [CONCURRENTLY] <schema>.<sicht>`` mit einer freigegebenen Sicht."""
    if not allow_refresh:
        return False
    text = " ".join(_COMMENTS.sub(" ", statement).split()).rstrip("; ")
    m = _REFRESH.match(text)
    return m is not None and f"{m.group(2)}.{m.group(4)}" in allow_refresh


def _replace_token(m: re.Match[str]) -> str:
    tok = m.group(0)
    if tok.startswith("'"):
        return "''"  # Zeichenkette
    if tok.startswith('"'):
        return '""'  # zitierter Bezeichner (z. B. eine Spalte "comment")
    return " "  # Kommentar


def _strip(text: str) -> str:
    return " ".join(_TOKENS.sub(_replace_token, text).split())


def check_sql(statement: str, allow_refresh: Collection[str] = ()) -> None:
    """Wirft ``GuardViolation``, wenn ``statement`` keine reine Datenoperation ist.

    ``allow_refresh``: Sichten (``schema.name``), für die genau ``REFRESH MATERIALIZED VIEW`` erlaubt ist
    (``target.yaml: after_import.refresh_materialized_views``). Alles andere bleibt gesperrt.
    """
    if _is_allowed_refresh(statement, allow_refresh):
        return
    text = _strip(statement).rstrip("; ")
    if ";" in text:
        raise GuardViolation("Mehrere Anweisungen in einem Aufruf sind nicht erlaubt")
    if not ALLOWED_START.match(text):
        raise GuardViolation(f"Nicht erlaubte Anweisung für Zieltabellen: {text[:80]!r}")
    if text.upper().startswith("COPY") and not _COPY_IN.match(text):
        raise GuardViolation(f"COPY nur FROM STDIN in temporäre Tabellen: {text[:80]!r}")
    if _INTO.search(_INSERT_INTO.sub("", text)):
        raise GuardViolation(f"INTO ist nur in INSERT INTO erlaubt (SELECT … INTO legt Tabellen an): {text[:80]!r}")
    body, temp_create = _TEMP_CREATE.subn("", text, count=1)
    if temp_create:  # "ON COMMIT DROP" gehört zur erlaubten temporären Tabelle
        body = _ON_COMMIT_DROP.sub("", body)
    m = FORBIDDEN.search(body)
    if m:
        raise GuardViolation(f"Nicht erlaubtes Schlüsselwort '{m.group(1)}' in: {text[:80]!r}")


_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_PARAM = re.compile(r"%\((\w+)\)s|%s")
SCOPE_PARAMS = ("dataset", "meta_id")


def check_scope_sql(statement: str, verb: str) -> None:
    """Prüft eine eigene Anweisung aus ``delete_scope`` (``verb``: ``DELETE`` bzw. ``SELECT``).

    Sie muss ``check_sql`` bestehen, mit ``verb`` beginnen und nach ``WHERE`` auf
    ``%(dataset)s`` oder ``%(meta_id)s`` eingeschränkt sein – sonst träfe sie alle Quellen (H6).
    Kommentare und Zeichenketten zählen nicht. Wie ``check_sql`` eine Plausibilitätsprüfung,
    kein Beweis, dass nur der eigene Datensatz betroffen ist.
    """
    check_sql(statement)
    text = _strip(statement).rstrip("; ")
    if not re.match(rf"{verb}\b", text, re.IGNORECASE):
        raise GuardViolation(f"Anweisung muss mit {verb} beginnen: {text[:80]!r}")
    unknown = sorted({m.group(1) or "%s" for m in _PARAM.finditer(text)} - set(SCOPE_PARAMS))
    if unknown:
        raise GuardViolation(f"Unbekannte Parameter {', '.join(unknown)} (erlaubt: %(dataset)s, %(meta_id)s)")
    where = _WHERE.search(text)
    if where is None:
        raise GuardViolation(f"{verb} ohne WHERE träfe alle Quellen: {text[:80]!r}")
    if not _PARAM.search(text, where.end()):
        raise GuardViolation(f"WHERE muss %(dataset)s oder %(meta_id)s verwenden: {text[:80]!r}")


def as_text(conn: psycopg.Connection[Any], query: str | sql.Composable) -> str:
    """SQL-Text einer Anweisung (für Prüfung und Vorschau)."""
    return query if isinstance(query, str) else query.as_string(conn)


def guarded_execute(
    cur: psycopg.Cursor[Any], query: str | sql.Composable, params: Any = None, allow_refresh: Collection[str] = ()
) -> psycopg.Cursor[Any]:
    """``cur.execute`` mit vorheriger DDL-Prüfung."""
    check_sql(as_text(cur.connection, query), allow_refresh)
    return cur.execute(query, params)  # type: ignore[arg-type]
