"""Ziel-Adapter ohne Datenbank: keine ``assert``-Prüfungen zur Laufzeit, klare Fehler."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from geoimport.config.models import TargetConfig
from geoimport.target.table_mapping import TableMappingAdapter, TargetError
from tests.conftest import CONFIG, load_yaml

TARGET_DIR = Path(__file__).resolve().parents[2] / "geoimport" / "target"


@pytest.mark.parametrize("path", sorted(TARGET_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_runtime_asserts(path: Path) -> None:
    # assert entfällt unter ``python -O`` und liefert sonst nur „AssertionError“ ohne Text
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    assert lines == [], f"{path.name}: assert in Zeile {lines}"


def test_scope_geo_without_owner_column_is_target_error() -> None:
    t = load_yaml(CONFIG / "target.demo.yaml")
    del t["tables"]["geometries"]["fk_metadata"]
    del t["tables"]["attributes"]["fk_geometry"]
    t["delete_scope"] = {
        "strategy": "sql",
        "sql": ["DELETE FROM ref.attribute WHERE metadaten_id = %(meta_id)s"],
        "scope_sql": {
            "features": "SELECT objekt_schluessel FROM ref.geometrien WHERE name = %(dataset)s",
            "values": "SELECT 1 FROM ref.attribute WHERE metadaten_id = %(meta_id)s",
        },
        "geometry_scope": "name = %(dataset)s",
    }
    cfg = TargetConfig.model_validate(t)
    adapter = TableMappingAdapter(cfg)
    assert "name = %(dataset)s" in adapter._scope_geo().as_string(None)
    # eine (an der Validierung vorbei) fehlende Zuordnung ist ein klarer Fehler, kein AssertionError
    cfg.delete_scope.geometry_scope = None
    with pytest.raises(TargetError, match="fk_metadata, source_column oder delete_scope.geometry_scope"):
        adapter._scope_geo()
