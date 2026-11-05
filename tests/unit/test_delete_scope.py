"""delete_scope ``sql``: Validierung der eigenen Lösch- und Bestandsabfragen (H6)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from geoimport.config.models import TargetConfig
from tests.conftest import CONFIG, load_yaml

DEL_ATTR = "DELETE FROM ref.attribute WHERE metadaten_id = %(meta_id)s"
DEL_GEO = "DELETE FROM ref.geometrien WHERE metadaten_id = %(meta_id)s"
SCOPE = {
    "features": "SELECT objekt_schluessel FROM ref.geometrien WHERE metadaten_id = %(meta_id)s",
    "values": "SELECT 1 FROM ref.attribute WHERE metadaten_id = %(meta_id)s",
}


def _target(**scope: Any) -> dict[str, Any]:
    t = load_yaml(CONFIG / "target.demo.yaml")
    t["delete_scope"] = scope
    return t


def _ok(**scope: Any) -> TargetConfig:
    return TargetConfig.model_validate(_target(**scope))


def _err(match: str, **scope: Any) -> None:
    with pytest.raises(ValidationError, match=match):
        TargetConfig.model_validate(_target(**scope))


def test_valid_sql_scope() -> None:
    cfg = _ok(strategy="sql", sql=[DEL_ATTR], scope_sql=SCOPE)
    assert cfg.delete_scope.sql == [DEL_ATTR]
    assert cfg.delete_scope.scope_sql is not None
    # Kommentare, Semikolon, USING und %(dataset)s sind erlaubt
    _ok(
        strategy="sql",
        sql=[
            "-- Attribute zuerst\nDELETE FROM ref.attribute a USING ref.metadaten m "
            "WHERE m.id = a.metadaten_id AND m.datensatz = %(dataset)s;"
        ],
        scope_sql=SCOPE,
    )


@pytest.mark.parametrize(
    ("stmt", "match"),
    [
        ("DELETE FROM ref.attribute", "WHERE"),  # H6: löscht alle Quellen
        ("DELETE FROM ref.attribute WHERE true", "%\\(dataset\\)s"),
        ("DELETE FROM ref.attribute WHERE x = 1 -- %(dataset)s", "%\\(dataset\\)s"),  # nur im Kommentar
        ("DELETE FROM ref.attribute WHERE x = '%(dataset)s'", "%\\(dataset\\)s"),  # nur in einer Zeichenkette
        ("DELETE FROM ref.attribute RETURNING %(dataset)s", "WHERE"),
        ("UPDATE ref.attribute SET wert = NULL WHERE metadaten_id = %(meta_id)s", "DELETE"),
        ("WITH d AS (DELETE FROM ref.attribute RETURNING 1) SELECT %(dataset)s WHERE true", "DELETE"),
        ("SELECT 1 WHERE %(dataset)s IS NULL", "DELETE"),
        ("TRUNCATE ref.attribute", "TRUNCATE|DELETE"),
        ("DELETE FROM ref.attribute WHERE id = %(meta_id)s; DROP TABLE ref.attribute", "Mehrere"),
        ("DELETE FROM ref.attribute WHERE quelle = %(quelle)s", "quelle"),  # unbekannter Parameter
    ],
)
def test_bad_delete_statement_rejected(stmt: str, match: str) -> None:
    _err(match, strategy="sql", sql=[DEL_ATTR, stmt], scope_sql=SCOPE)


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT objekt_schluessel FROM ref.geometrien",
        "DELETE FROM ref.geometrien WHERE metadaten_id = %(meta_id)s",
        "WITH d AS (DELETE FROM ref.geometrien RETURNING 1) SELECT 1 WHERE %(dataset)s IS NULL",
        "SELECT * INTO ref.kopie FROM ref.geometrien WHERE metadaten_id = %(meta_id)s",
    ],
)
def test_bad_scope_statement_rejected(stmt: str) -> None:
    _err("scope_sql.features", strategy="sql", sql=[DEL_ATTR], scope_sql={**SCOPE, "features": stmt})


def test_sql_strategy_needs_statements_and_scope() -> None:
    _err("DELETE-Anweisungen", strategy="sql")
    _err("DELETE-Anweisungen", strategy="sql", sql=[], scope_sql=SCOPE)
    _err("scope_sql", strategy="sql", sql=[DEL_ATTR])  # sonst zählt die Schutzregel einen anderen Bereich


@pytest.mark.parametrize("strategy", ["by_metadata_source", "by_source_column"])
def test_sql_only_with_strategy_sql(strategy: str) -> None:
    _err("strategy: sql", strategy=strategy, sql=[DEL_ATTR])
    _err("strategy: sql", strategy=strategy, scope_sql=SCOPE)
    _err("strategy: sql", strategy=strategy, geometry_scope="metadaten_id = %(meta_id)s")


@pytest.mark.parametrize(
    "stmt",
    [
        DEL_GEO,
        'DELETE FROM "ref"."geometrien" WHERE metadaten_id = %(meta_id)s',
        "-- Geometrien\nDELETE FROM ONLY ref . geometrien WHERE metadaten_id = %(meta_id)s",
        "DELETE FROM geometrien WHERE metadaten_id = %(meta_id)s",
    ],
)
def test_sql_scope_must_not_delete_geometries(stmt: str) -> None:
    """Bieterantwort 74: Geometrien werden über den Schlüssel aktualisiert, nie gelöscht."""
    _err("Geometrien werden nicht gelöscht", strategy="sql", sql=[DEL_ATTR, stmt], scope_sql=SCOPE)


@pytest.mark.parametrize(
    ("cond", "match"),
    [
        ("true", r"%\(dataset\)s"),
        ("herkunft = 'x'", r"%\(dataset\)s"),
        ("herkunft = %(dataset)s; DROP TABLE ref.geometrien", "Mehrere"),
        ("herkunft = %(quelle)s", "quelle"),
    ],
)
def test_bad_geometry_scope_rejected(cond: str, match: str) -> None:
    _err("geometry_scope", strategy="sql", sql=[DEL_ATTR], scope_sql=SCOPE, geometry_scope=cond)


def test_sql_scope_needs_way_to_find_geometries() -> None:
    """Geometrien werden über den Schlüssel abgeglichen – dafür muss ihr Datensatz erkennbar sein."""
    t = _target(strategy="sql", sql=[DEL_ATTR], scope_sql=SCOPE)
    del t["tables"]["geometries"]["fk_metadata"]
    t["tables"]["attributes"].pop("fk_geometry")
    with pytest.raises(ValidationError, match="geometry_scope"):
        TargetConfig.model_validate(t)
    t["delete_scope"]["geometry_scope"] = "name = %(dataset)s"
    TargetConfig.model_validate(t)
