"""Schnittstelle der Ziel-Adapter und Auswahl über ``target.yaml: adapter``.

Ein Adapter bildet geprüfte Datensätze auf ein bestehendes Zielmodell ab. Er darf nur
lesen und Daten schreiben (``SELECT``, ``INSERT``, ``UPDATE``, ``DELETE``), nie die Struktur
ändern – das erzwingt ``geoimport.target.guard``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import pandas as pd
import psycopg

from geoimport.config.models import SourceBase, TargetConfig
from geoimport.sources.base import FeatureRecord


@dataclass
class SchemaIssue:
    """Abweichung zwischen ``target.yaml`` und der Datenbank."""

    level: Literal["error", "warning"]
    message: str


@dataclass
class DatasetContext:
    """Angaben zu Quelle und Lauf, die in Spaltenvorlagen (``{source.title}`` …) verfügbar sind."""

    dataset: str
    source: SourceBase
    source_url: str | None
    run_id: str
    run_started_at: datetime
    extra: dict[str, Any] = field(default_factory=dict)
    # Ergebnis der Besitzprüfung (H7): ``replace`` bricht ab, wenn die Metadatenzeile beim Schreiben
    # nicht mehr die geprüfte ist (z. B. zwischenzeitlich von der MOSIDI-Anwendung angelegt).
    expected_metadata_id: Any = None
    check_metadata_id: bool = False


class ProjectionUnavailable(RuntimeError):
    """Der Bestand lässt sich nicht auf die Lieferung abbilden (Vergleich mit dem Bestand nicht möglich)."""


@dataclass
class DiffBasis:
    """Vergleichsgrundlage aus dem Zielbestand (Bieterantwort 62).

    ``new``/``existing``: Spalten ``key`` und ``hash`` in derselben Form; ``old_geoms``: EWKB (hex) der
    vorhandenen Geometrien je Schlüssel (für die Änderungskarte); ``duplicates``: im Bestand mehrfach
    vorkommende Schlüssel (nur einmal verglichen).
    """

    new: pd.DataFrame
    existing: pd.DataFrame
    old_geoms: dict[str, str | None] = field(default_factory=dict)
    duplicates: int = 0


class OwnershipError(RuntimeError):
    """Die Metadatenzeile des Datensatzes gehört nicht (mehr) der Pipeline."""


@dataclass
class ReplaceStats:
    """Physisch gelöschte, eingefügte und aktualisierte Zeilen je Tabelle.

    ``retained``: Geometrien des Datensatzes, die in der Lieferung fehlen und stehen bleiben
    (Bieterantwort 74); ``retained_examples`` nennt bis zu 20 ihrer Schlüssel.
    """

    deleted: dict[str, int] = field(default_factory=dict)
    inserted: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    metadata_id: Any = None
    retained: int = 0
    retained_examples: list[str] = field(default_factory=list)
    # Schutznetz in der Datenbank (Bieterantwort 63): mit ST_MakeValid repariert bzw. verworfen
    db_repaired: int = 0
    db_repaired_keys: list[str] = field(default_factory=list)
    db_skipped: int = 0
    db_skipped_keys: list[str] = field(default_factory=list)

    @property
    def rows_deleted(self) -> int:
        """Summe aller gelöschten Zeilen."""
        return sum(self.deleted.values())

    @property
    def rows_inserted(self) -> int:
        """Summe aller eingefügten Zeilen."""
        return sum(self.inserted.values())

    @property
    def rows_updated(self) -> int:
        """Summe aller aktualisierten Zeilen (Geometrien)."""
        return sum(self.updated.values())


class TargetAdapter(Protocol):
    """Methoden, die jeder Ziel-Adapter bereitstellt."""

    cfg: TargetConfig

    def check(self, conn: psycopg.Connection[Any]) -> list[SchemaIssue]:
        """Zielmodell prüfen (nur lesend)."""
        ...

    def metadata_id(self, conn: psycopg.Connection[Any], dataset: str, lock: bool = False) -> Any:
        """PK der Metadatenzeile des Datensatzes (``None``, wenn es keine gibt); ``lock``: ``FOR UPDATE``."""
        ...

    def count_existing(self, conn: psycopg.Connection[Any], dataset: str, kind: str) -> int:
        """Anzahl Objekte (``features``) bzw. Kennzahlzeilen (``values``) des Datensatzes im Bestand."""
        ...

    def existing_feature_keys(self, conn: psycopg.Connection[Any], dataset: str) -> pd.DataFrame:
        """Objektschlüssel des Datensatzes im Bestand (Spalte ``key``)."""
        ...

    def diff_basis(
        self,
        conn: psycopg.Connection[Any],
        ctx: DatasetContext,
        features: list[FeatureRecord],
        values: pd.DataFrame | None,
    ) -> DiffBasis:
        """Schlüssel/Hashes von Zielbestand und Lieferung in derselben Form; sonst ``ProjectionUnavailable``."""
        ...

    def replace(
        self,
        conn: psycopg.Connection[Any],
        ctx: DatasetContext,
        features: list[FeatureRecord],
        values: pd.DataFrame | None,
    ) -> ReplaceStats:
        """Attribute des Datensatzes ersetzen, Geometrien über den Schlüssel abgleichen (Transaktion des Aufrufers)."""
        ...

    def refresh_views(self, conn: psycopg.Connection[Any]) -> list[str]:
        """Materialisierte Sichten aus ``after_import`` aktualisieren; liefert ihre Namen."""
        ...

    def preview_sql(self, conn: psycopg.Connection[Any], ctx: DatasetContext, kind: str) -> list[str]:
        """Die Anweisungen eines Ersetzens als Text (``--dry-run``, Doku)."""
        ...


def make_adapter(cfg: TargetConfig) -> TargetAdapter:
    """Adapter zur Konfiguration erzeugen."""
    if cfg.adapter == "demo_reference":
        from geoimport.target.demo_reference import DemoReferenceAdapter

        return DemoReferenceAdapter(cfg)
    from geoimport.target.table_mapping import TableMappingAdapter

    return TableMappingAdapter(cfg)
