"""Auswertung der Mapping-Ausdrücke aus der YAML-Konfiguration.

Erlaubt sind nur deklarative Ausdrücke – keine Ausführung von Python-Code aus YAML:

- ``"feldname"``                       Wert des Quellfelds
- ``{field: ags, zfill: 8}``           Feld mit Normalisierung (``zfill``, ``to_int``, ``to_float``, ``to_date``;
                                       Zahlen mit ``decimal``/``thousands``)
- ``{concat: [krss, "-", {field: x}], sep: ""}``  Verkettung (Text in Anführungszeichen ist ein Feldname!)
- ``{const: "Brandenburg"}``           fester Wert
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from geoimport.config.models import ConcatExpr, ConstExpr, FieldExpr, Mapping
from geoimport.transform.keys import to_text

# Technische Felder, die GDAL aus GML erzeugt und die kein Fachattribut sind
TECHNICAL_FIELDS = frozenset({"gml_id", "lowerCorner", "upperCorner", "boundedBy"})


class MappingError(ValueError):
    """Ein Ausdruck lässt sich für einen Datensatz nicht auswerten."""


def parse_number(value: Any, decimal: str | None = None, thousands: str | None = None) -> Decimal:
    """Zahl aus einem Feldwert. Ohne ``decimal`` ist genau ein Punkt oder Komma Dezimaltrennzeichen.

    Mit ``decimal: ","`` ist ein Punkt nur als ``thousands: "."`` erlaubt – sonst würde „1.234“
    (deutscher Tausenderpunkt) still zu 1,234.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        text = str(value).strip()
        if thousands:
            text = text.replace(thousands, "")
        if decimal is None:
            if "," in text and "." in text:
                raise ValueError("Punkt und Komma zugleich – decimal/thousands angeben")
            text = text.replace(",", ".")
        elif decimal != ".":
            if "." in text:
                raise ValueError(f"Punkt bei Dezimaltrennzeichen {decimal!r} – Tausenderpunkt? thousands: '.' angeben")
            text = text.replace(decimal, ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("keine Zahl") from exc
    if not number.is_finite():
        raise ValueError("keine endliche Zahl")
    return number


def evaluate(expr: Any, props: dict[str, Any]) -> Any:
    """Wertet einen Ausdruck gegen die Eigenschaften eines Quellobjekts aus."""
    if expr is None:
        return None
    if isinstance(expr, str):
        return props.get(expr)
    if isinstance(expr, ConstExpr):
        return expr.const
    if isinstance(expr, FieldExpr):
        value = props.get(expr.field)
        if value is None or value == "":
            return None
        try:
            if expr.to_int or expr.to_float:
                number = parse_number(value, expr.decimal, expr.thousands)
                if expr.to_float:
                    return float(number)
                if number != number.to_integral_value():
                    raise ValueError("keine ganze Zahl")  # nicht still abschneiden (3,9 ≠ 3)
                return int(number)
            if expr.to_date:
                return datetime.strptime(str(value), expr.to_date).date().isoformat()
        except ValueError as exc:
            raise MappingError(f"Feld '{expr.field}': Wert {value!r} nicht umwandelbar ({exc})") from exc
        text = to_text(value)
        if expr.zfill and text is not None:
            text = text.strip().zfill(expr.zfill)
        return text
    if isinstance(expr, ConcatExpr):
        parts = [evaluate(p, props) for p in expr.concat]
        if any(p is None for p in parts):
            return None
        return expr.sep.join(str(p) for p in parts)
    raise MappingError(f"Unbekannter Ausdruck: {expr!r}")


def _with_numbers(expr: Any, decimal: str, thousands: str | None) -> Any:
    if isinstance(expr, FieldExpr) and (expr.to_int or expr.to_float) and expr.decimal is None:
        return expr.model_copy(update={"decimal": decimal, "thousands": expr.thousands or thousands})
    if isinstance(expr, ConcatExpr):
        return expr.model_copy(update={"concat": [_with_numbers(p, decimal, thousands) for p in expr.concat]})
    return expr


def with_number_format(mapping: Mapping, decimal: str, thousands: str | None) -> Mapping:
    """Mapping, dessen to_int/to_float ohne eigenes ``decimal`` das Zahlenformat der Quelle (CSV) verwenden."""
    return mapping.model_copy(
        update={
            "name": _with_numbers(mapping.name, decimal, thousands),
            "attributes": {k: _with_numbers(v, decimal, thousands) for k, v in mapping.attributes.items()},
        }
    )


def referenced_fields(expr: Any) -> set[str]:
    """Quellfelder, die ein Ausdruck verwendet (für die Schemaprüfung)."""
    if expr is None or isinstance(expr, ConstExpr):
        return set()
    if isinstance(expr, str):
        return {expr}
    if isinstance(expr, FieldExpr):
        return {expr.field}
    if isinstance(expr, ConcatExpr):
        out: set[str] = set()
        for p in expr.concat:
            out |= referenced_fields(p)
        return out
    return set()


def mapping_fields(mapping: Mapping) -> set[str]:
    """Alle Quellfelder eines Mappings."""
    out = referenced_fields(mapping.name)
    for expr in mapping.attributes.values():
        out |= referenced_fields(expr)
    return out


def apply_mapping(mapping: Mapping, props: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Name und Zielattribute eines Objekts. Ohne ``attributes`` werden alle Felder übernommen."""
    name = evaluate(mapping.name, props)
    if mapping.attributes:
        attrs = {target: evaluate(expr, props) for target, expr in mapping.attributes.items()}
    else:
        attrs = {k: v for k, v in props.items() if k not in TECHNICAL_FIELDS}
    return (None if name is None else str(name)), attrs
