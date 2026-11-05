"""Bieterantwort 63: Warnungscodes ``geometry_repaired``, ``geometry_skipped``, ``missing_required_field``.

Die Zähler gelten je Quelle und müssen vollständig sein, auch wenn mehr Fehler auftreten, als einzeln
gespeichert werden (``MAX_STORED_ERRORS``).
"""

from __future__ import annotations

import jsonschema
import pandas as pd

from geoimport.quality import GEOMETRY_REPAIRED, GEOMETRY_SKIPPED, MISSING_REQUIRED_FIELD, quality_counts
from geoimport.report.builder import SourceReport, load_schema
from geoimport.sources.base import FeatureRecord, ParseResult, RecordError


def _feature(key: str, repaired: bool = False) -> FeatureRecord:
    return FeatureRecord(key, None, None, {}, "h", repaired)


def test_counts_from_errors_and_overflow() -> None:
    parsed = ParseResult(
        features=[_feature("a", repaired=True), _feature("b"), _feature("c", repaired=True)],
        errors=[
            RecordError("x", "invalid_geometry", "kaputt"),
            RecordError("y", "empty_geometry", "leer"),
            RecordError("z", "missing_geometry", "fehlt"),
            RecordError("w", "unexpected_geometry_type", "Punkt"),
            RecordError("v", "missing_required_field", "Pflichtfeld leer: wert"),
            RecordError("u", "duplicate_key", "doppelt"),
        ],
    )
    parsed.overflow_reasons = {"missing_required_field": 5, "invalid_value": 7}
    parsed.invalid_overflow = 12
    assert quality_counts(parsed) == {GEOMETRY_REPAIRED: 2, GEOMETRY_SKIPPED: 4, MISSING_REQUIRED_FIELD: 6}
    assert parsed.reason_counts() == {
        "invalid_geometry": 1,
        "empty_geometry": 1,
        "missing_geometry": 1,
        "unexpected_geometry_type": 1,
        "missing_required_field": 6,
        "duplicate_key": 1,
        "invalid_value": 7,
    }


def test_db_counts_are_added() -> None:
    parsed = ParseResult(features=[_feature("a", repaired=True)])
    assert quality_counts(parsed, db_repaired=3, db_skipped=2) == {
        GEOMETRY_REPAIRED: 4,
        GEOMETRY_SKIPPED: 2,
        MISSING_REQUIRED_FIELD: 0,
    }


def test_report_contains_quality_and_validates() -> None:
    sr = SourceReport("q", "wfs", "Quelle", "q", status="ok")
    sr.quality = {GEOMETRY_REPAIRED: 1, GEOMETRY_SKIPPED: 0, MISSING_REQUIRED_FIELD: 2}
    out = sr.as_dict()
    assert out["quality"] == {"geometry_repaired": 1, "geometry_skipped": 0, "missing_required_field": 2}
    item_schema = load_schema()["properties"]["sources"]["items"]
    jsonschema.validate(out, {**item_schema, "$defs": load_schema()["$defs"]})


def test_value_rows_reason_counts_complete() -> None:
    """Mehr fehlende Pflichtfelder als gespeichert werden: Zähler bleibt vollständig."""
    from geoimport.sources import csv_source

    values = pd.DataFrame({"reason": ["missing_required_field"] * 3 + ["duplicate_key"] * 2})
    stored = [RecordError("k", "missing_required_field", "x")]
    assert csv_source.overflow_by_reason(values["reason"], stored) == {"missing_required_field": 2, "duplicate_key": 2}
