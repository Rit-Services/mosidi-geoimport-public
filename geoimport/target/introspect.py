"""Nur lesende Abfragen auf den Systemkatalog: Spalten, Typen, Schema-Schnappschuss."""

from __future__ import annotations

from typing import Any

import psycopg


def table_columns(conn: psycopg.Connection[Any], schema: str, table: str) -> dict[str, str]:
    """Spalten einer Tabelle → Datentyp (``format_type``, z. B. ``geometry(MultiPolygon,4326)``)."""
    rows = conn.execute(
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (schema, table),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def table_exists(conn: psycopg.Connection[Any], schema: str, table: str) -> bool:
    """True, wenn die Tabelle (oder View) existiert."""
    row = conn.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
        (schema, table),
    ).fetchone()
    return row is not None


def not_null_columns(conn: psycopg.Connection[Any], schema: str, table: str) -> set[str]:
    """Spalten mit NOT NULL ohne Default (müssen beim INSERT befüllt werden)."""
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s AND is_nullable='NO' AND column_default IS NULL
          AND is_identity='NO' AND is_generated='NEVER'
        """,
        (schema, table),
    ).fetchall()
    return {r[0] for r in rows}


def schema_snapshot(conn: psycopg.Connection[Any], schema: str) -> dict[str, Any]:
    """Struktur eines Schemas (Spalten, Indizes, Constraints, Trigger) für den Vorher/Nachher-Vergleich."""
    q = {
        "columns": """SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default
                      FROM information_schema.columns WHERE table_schema=%s
                      ORDER BY table_name, ordinal_position""",
        "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY 1, 2",
        "constraints": """SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
                          FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid
                          JOIN pg_namespace n ON n.oid = rel.relnamespace WHERE n.nspname=%s ORDER BY 1, 2""",
        "triggers": """SELECT event_object_table, trigger_name, action_statement
                       FROM information_schema.triggers WHERE trigger_schema=%s ORDER BY 1, 2""",
        "tables": """SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                     WHERE n.nspname=%s AND c.relkind IN ('r','v','m','p') ORDER BY 1""",
    }
    return {k: [list(r) for r in conn.execute(v, (schema,)).fetchall()] for k, v in q.items()}
