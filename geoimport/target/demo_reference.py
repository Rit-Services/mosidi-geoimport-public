"""Adapter ``demo_reference``: wie ``table_mapping``, legt aber in einer Demo-Datenbank das
Referenzmodell aus ``sql/demo_reference_model.sql`` an.

Das Referenzmodell ist eine Annahme von RIT zur Demonstration, nicht das Datenmodell der
TH Brandenburg. Angelegt wird es nur, wenn der Datenbankname auf ``_demo`` endet.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg

from geoimport.target.table_mapping import TableMappingAdapter

DEMO_SUFFIX = "_demo"


class DemoDatabaseError(RuntimeError):
    """Demo-Funktion gegen eine Nicht-Demo-Datenbank verweigert."""


def demo_sql_path() -> Path:
    """Pfad der Referenzmodell-DDL (``GEOIMPORT_SQL_DIR`` oder ``sql/`` im Repository)."""
    base = os.environ.get("GEOIMPORT_SQL_DIR")
    root = Path(base) if base else Path(__file__).resolve().parents[2] / "sql"
    return root / "demo_reference_model.sql"


def require_demo_database(conn: psycopg.Connection[Any]) -> str:
    """Name der aktuellen Datenbank; ``DemoDatabaseError``, wenn er nicht auf ``_demo`` endet."""
    row = conn.execute("SELECT current_database()").fetchone()
    name = str(row[0]) if row else ""
    if not name.endswith(DEMO_SUFFIX):
        raise DemoDatabaseError(
            f"Datenbank '{name}' ist keine Demo-Datenbank (Name muss auf '{DEMO_SUFFIX}' enden) – Abbruch"
        )
    return name


class DemoReferenceAdapter(TableMappingAdapter):
    """Ziel-Adapter für die Demo-Datenbank."""

    def ensure_model(self, conn: psycopg.Connection[Any]) -> None:
        """Referenzmodell anlegen (idempotent), nur in einer Demo-Datenbank."""
        require_demo_database(conn)
        with conn.transaction():
            conn.execute(demo_sql_path().read_text(encoding="utf-8"))
