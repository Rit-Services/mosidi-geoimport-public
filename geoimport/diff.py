"""Hash je Datensatz und Diff gegen den vorhandenen Datenbestand (neu/geändert/entfallen).

Damit der Import-Bericht „neu, geändert, gelöscht“ ausweisen kann (LB 4.5), wird vor dem
Ersetzen verglichen (Bieterantwort 62):

- Vergleichsbasis sind die **aktuell vorhandenen Zieldaten** des Datensatzes, nicht ein früherer
  Lauf. Der Ziel-Adapter liest sie und bildet die neue Lieferung auf dieselbe Form ab
  (``TableMappingAdapter.diff_basis``): genau die Spalten, die er schreibt, als normalisierter Text.
  So liefert auch der erste Lauf gegen einen befüllten Bestand korrekte Zahlen.
- Schlüssel: ``business_key`` der Quelle; bei Kennzahlen wird er aus den Zielspalten
  zusammengesetzt (INKAR: Kennziffer, Bereich, Kürzel, Indikator, Zeitbezug).
- Hash: SHA-256 (gekürzt auf 128 Bit) über die normalisierten Werte; Geometrien mit Koordinaten auf
  7 Nachkommastellen gerundet (≈ 1 cm), Einzel- und Multi-Geometrie gleich behandelt.
- ``pipeline_meta.diff_keys`` bleibt das Protokoll je Lauf. Nur wenn sich der Bestand nicht auf die
  Lieferung abbilden lässt, dient es als Rückfall (Warnung ``diff_from_baseline``).
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import shapely
from shapely.geometry.base import BaseGeometry

HASH_HEX_LEN = 32
COORD_DECIMALS = 7

CHANGE_ADDED = "added"
CHANGE_CHANGED = "changed"
CHANGE_DELETED = "deleted"
CHANGE_UNCHANGED = "unchanged"
CHANGE_REPLACED = "replaced"


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return int(value) if value.is_integer() else round(value, 10)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Stabile JSON-Darstellung (sortierte Schlüssel, normalisierte Zahlen)."""
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_FIELD_SEP, _ROW_SEP, _NONE = "\x1f", "\x1e", "\x01"
_MULTI = {"Point": shapely.MultiPoint, "LineString": shapely.MultiLineString, "Polygon": shapely.MultiPolygon}


def norm_text(value: Any) -> str | None:
    """Wert für den Vergleich mit dem Zielbestand: Zahlen und JSON kanonisch, sonst getrimmter Text.

    Beide Seiten (Lieferung als geschriebener Text, Bestand als ``::text`` gelesen) laufen hier durch,
    damit z. B. ``1.50`` und ``1.5`` oder ``{"a": 1}`` und ``{"a":1}`` gleich sind.
    """
    if value is None:
        return None
    return _norm_str(str(value))


@functools.lru_cache(maxsize=1 << 16)
def _norm_str(raw: str) -> str:
    """``norm_text`` für Text; zwischengespeichert, weil sich Namen, Zeitbezüge und Zusatzfelder stark wiederholen."""
    text = raw.strip()
    if _NUMBER_RE.match(text):
        number = float(text)
        if math.isfinite(number):
            return str(int(number)) if number.is_integer() and abs(number) < 1e15 else repr(number)
    if text[:1] in ("{", "["):
        try:
            return canonical_json(json.loads(text))
        except ValueError:
            pass
    return text


def geometry_fingerprint(ewkb_hex: str | None) -> bytes:
    """Vergleichsform einer Geometrie: gerundet, normalisiert, Einzelgeometrie als Multi."""
    if not ewkb_hex:
        return b""
    geom = shapely.from_wkb(ewkb_hex)
    multi = _MULTI.get(geom.geom_type)
    if multi is not None:
        geom = multi([geom])
    return rounded_wkb(geom)


def projection_hash(geometry: bytes, columns: Sequence[Any], rows: Iterable[Sequence[Any]] = ()) -> str:
    """Hash über Geometrie, Spaltenwerte und (sortierte) Unterzeilen, alle über ``norm_text``."""

    def joined(values: Sequence[Any]) -> str:
        return _FIELD_SEP.join(_NONE if (n := norm_text(v)) is None else n for v in values)

    h = hashlib.sha256(geometry)
    h.update(b"\x00" + joined(columns).encode())
    h.update(b"\x00" + _ROW_SEP.join(sorted(joined(r) for r in rows)).encode())
    return h.hexdigest()[:HASH_HEX_LEN]


def rounded_wkb(geom: BaseGeometry | None) -> bytes:
    """WKB mit gerundeten Koordinaten (Grundlage des Geometrie-Hashes)."""
    if geom is None:
        return b""
    rounded = shapely.transform(geom, lambda c: np.round(c, COORD_DECIMALS))
    return bytes(shapely.to_wkb(shapely.normalize(rounded), output_dimension=2))


def feature_hash(name: str | None, attributes: dict[str, Any], geom: BaseGeometry | None) -> str:
    """Hash eines Objekts aus Name, Attributen und Geometrie."""
    h = hashlib.sha256()
    h.update(canonical_json({"name": name, "attributes": attributes}).encode())
    h.update(b"\x00")
    h.update(rounded_wkb(geom))
    return h.hexdigest()[:HASH_HEX_LEN]


def value_hashes(df: pd.DataFrame) -> pd.Series:
    """Hash je Kennzahlzeile aus Name, Wert, Zeitbezug und Zusatzfeldern (vektorisiert über Zeilen)."""
    cols = [df[c].astype("string").fillna("\x01") for c in ("name", "value", "period", "extra")]
    joined = cols[0].str.cat(cols[1:], sep="\x00")
    return pd.Series(
        [hashlib.sha256(s.encode()).hexdigest()[:HASH_HEX_LEN] for s in joined.tolist()],
        index=df.index,
        dtype="string",
    )


@dataclass
class DiffResult:
    """Zählung und Schlüssel je Änderungsart."""

    added: int
    changed: int | None
    deleted: int
    unchanged: int | None
    compared: bool
    keys: pd.DataFrame  # Spalten key, hash, change

    def counts(self) -> dict[str, int | None]:
        """Für Bericht und ``import_source_results``."""
        return {"added": self.added, "changed": self.changed, "deleted": self.deleted, "unchanged": self.unchanged}


def compute_diff(new: pd.DataFrame, old: pd.DataFrame | None, old_has_hashes: bool) -> DiffResult:
    """Vergleicht neue Schlüssel/Hashes mit dem Bestand.

    ``new``: Spalten ``key``, ``hash``. ``old``: Spalten ``key`` und (falls ``old_has_hashes``) ``hash``.
    """
    new = new[["key", "hash"]]
    if old is None or old.empty:
        keys = new.assign(change=CHANGE_ADDED)
        return DiffResult(len(new), 0, 0, 0, True, keys)
    old = old[["key", "hash"]] if old_has_hashes else old[["key"]].assign(hash=pd.NA)
    merged = new.merge(old, on="key", how="outer", suffixes=("", "_old"), indicator=True)
    both = merged["_merge"] == "both"
    added = merged["_merge"] == "left_only"
    deleted = merged["_merge"] == "right_only"
    change = pd.Series(CHANGE_UNCHANGED, index=merged.index, dtype="object")
    change[added] = CHANGE_ADDED
    change[deleted] = CHANGE_DELETED
    if old_has_hashes:
        changed = both & (merged["hash"] != merged["hash_old"])
        change[changed] = CHANGE_CHANGED
        n_changed: int | None = int(changed.sum())
        n_unchanged: int | None = int((both & ~changed).sum())
    else:
        change[both] = CHANGE_REPLACED
        n_changed = n_unchanged = None
    merged["hash"] = merged["hash"].where(~deleted, merged["hash_old"])
    keys = pd.DataFrame({"key": merged["key"], "hash": merged["hash"], "change": change})
    return DiffResult(int(added.sum()), n_changed, int(deleted.sum()), n_unchanged, old_has_hashes, keys)
