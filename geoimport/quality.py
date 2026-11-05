"""Datenqualität je Quelle: Warnungscodes und Zähler aus Bieterantwort 63.

- ``geometry_repaired``: ungültige Geometrie repariert (``shapely.make_valid`` bzw. ``ST_MakeValid``), übernommen.
- ``geometry_skipped``: Geometrie fehlt, ist leer, nicht reparierbar oder vom falschen Typ – Datensatz übersprungen.
- ``missing_required_field``: Pflichtfeld (Schlüssel, Raumbezug, Zeitbezug, Wert) leer – Datensatz
  übersprungen. Eine fehlende Geometrie zählt als ``geometry_skipped``.

Die Zähler stehen im Bericht unter ``sources[].quality``; ist ein Zähler größer als 0, erscheint
zusätzlich eine Warnung mit diesem Code, der Anzahl und bis zu 20 Schlüsseln.
"""

from __future__ import annotations

from typing import Any

from geoimport.sources.base import ParseResult, PipelineWarning

GEOMETRY_REPAIRED = "geometry_repaired"
GEOMETRY_SKIPPED = "geometry_skipped"
MISSING_REQUIRED_FIELD = "missing_required_field"
QUALITY_CODES = (GEOMETRY_REPAIRED, GEOMETRY_SKIPPED, MISSING_REQUIRED_FIELD)

# Fehlergründe (``import_errors.reason_code``), bei denen ein Datensatz wegen seiner Geometrie entfällt
GEOMETRY_SKIP_REASONS = frozenset(
    {"missing_geometry", "empty_geometry", "invalid_geometry", "unexpected_geometry_type"}
)
# Pflichtfeld leer; ein fehlender Schlüssel ist ebenfalls ein fehlendes Pflichtfeld (Bieterantwort 63)
MISSING_REQUIRED_REASONS = frozenset({MISSING_REQUIRED_FIELD, "missing_key"})
MAX_EXAMPLES = 20

_TEXT = {
    GEOMETRY_REPAIRED: "{n} ungültige Geometrien repariert und übernommen",
    GEOMETRY_SKIPPED: "{n} Datensätze wegen fehlender, leerer oder nicht reparierbarer Geometrie übersprungen",
    MISSING_REQUIRED_FIELD: "{n} Datensätze wegen leerer Pflichtfelder übersprungen",
}


def quality_counts(parsed: ParseResult, db_repaired: int = 0, db_skipped: int = 0) -> dict[str, int]:
    """Zähler je Code; ``db_*``: zusätzlich in der Datenbank reparierte bzw. verworfene Geometrien."""
    reasons = parsed.reason_counts()
    return {
        GEOMETRY_REPAIRED: parsed.repaired + db_repaired,
        GEOMETRY_SKIPPED: sum(n for r, n in reasons.items() if r in GEOMETRY_SKIP_REASONS) + db_skipped,
        MISSING_REQUIRED_FIELD: sum(n for r, n in reasons.items() if r in MISSING_REQUIRED_REASONS),
    }


def quality_examples(parsed: ParseResult) -> dict[str, list[str]]:
    """Bis zu 20 Schlüssel je Code (aus den gespeicherten Fehlern bzw. reparierten Objekten)."""
    out: dict[str, list[str]] = {
        GEOMETRY_REPAIRED: [f.key for f in parsed.features if f.repaired][:MAX_EXAMPLES],
        GEOMETRY_SKIPPED: [],
        MISSING_REQUIRED_FIELD: [],
    }
    for e in parsed.errors:
        code = (
            GEOMETRY_SKIPPED
            if e.reason_code in GEOMETRY_SKIP_REASONS
            else MISSING_REQUIRED_FIELD
            if e.reason_code in MISSING_REQUIRED_REASONS
            else None
        )
        if code and e.record_key is not None and len(out[code]) < MAX_EXAMPLES:
            out[code].append(e.record_key)
    return out


def quality_warnings(counts: dict[str, int], examples: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Warnungen für alle Codes mit Zähler > 0."""
    return [
        PipelineWarning(
            code, _TEXT[code].format(n=counts[code]), {"count": counts[code], "examples": examples.get(code, [])}
        ).as_dict()
        for code in QUALITY_CODES
        if counts.get(code)
    ]
