"""Normalisierung amtlicher Schlüssel und Bildung fachlicher Objektschlüssel.

Schlüssel werden immer als Text behandelt. Eine Umwandlung in Zahlen (wie in der
Vorarbeit für die INKAR-``Kennziffer``) würde führende Nullen entfernen
(``01001000`` → ``1001000``) und Schlüssel anderer Bundesländer verfälschen.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from geoimport.config.models import NormalizeSpec

KEY_SEP = "|"


def to_text(value: Any) -> str | None:
    """Wert als Text; ``None``/NaN/leer → ``None``. Ganzzahlige Floats ohne ``.0``."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value)
    return text if text != "" else None


def normalize(value: Any, spec: NormalizeSpec | None) -> str | None:
    """Einzelwert normalisieren (Leerzeichen, Großschreibung, führende Nullen)."""
    text = to_text(value)
    if text is None:
        return None
    spec = spec or NormalizeSpec()
    if spec.strip:
        text = "".join(text.split())
    if spec.upper:
        text = text.upper()
    if spec.zfill:
        text = text.zfill(spec.zfill)
    return text or None


def normalize_series(series: pd.Series, spec: NormalizeSpec | None) -> pd.Series:
    """Vektorisierte Variante von ``normalize`` für CSV-Spalten."""
    spec = spec or NormalizeSpec()
    s = series.astype("string")
    if spec.strip:
        s = s.str.replace(r"\s+", "", regex=True)
    if spec.upper:
        s = s.str.upper()
    s = s.mask(s == "")
    if spec.zfill:
        s = s.str.zfill(spec.zfill)
    return s


def build_key(parts: list[str | None]) -> str | None:
    """Zusammengesetzter Schlüssel ``a|b|c``; ``None``, wenn ein Teil fehlt."""
    if any(p is None for p in parts):
        return None
    return KEY_SEP.join(p for p in parts if p is not None)
