from __future__ import annotations

import pandas as pd
import shapely
from shapely.geometry import MultiPolygon, Point, box

from geoimport.diff import compute_diff, feature_hash, geometry_fingerprint, norm_text, projection_hash, value_hashes


def df(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["key", "hash"])


def test_counts() -> None:
    old = df([("a", "1"), ("b", "2"), ("c", "3")])
    new = df([("a", "1"), ("b", "X"), ("d", "4")])
    d = compute_diff(new, old, True)
    assert d.counts() == {"added": 1, "changed": 1, "deleted": 1, "unchanged": 1}
    changes = dict(zip(d.keys["key"], d.keys["change"], strict=True))
    assert changes == {"a": "unchanged", "b": "changed", "c": "deleted", "d": "added"}
    assert d.keys.set_index("key").loc["c", "hash"] == "3"  # gelöschte behalten den alten Hash


def test_first_run_all_added() -> None:
    d = compute_diff(df([("a", "1")]), None, True)
    assert d.counts() == {"added": 1, "changed": 0, "deleted": 0, "unchanged": 0}


def test_without_hashes_marks_replaced() -> None:
    d = compute_diff(df([("a", "1"), ("b", "2")]), pd.DataFrame({"key": ["a", "z"]}), False)
    assert d.added == 1 and d.deleted == 1 and d.changed is None and not d.compared
    assert set(d.keys["change"]) == {"replaced", "added", "deleted"}


def test_hash_ignores_rounding_noise_and_key_order() -> None:
    g1 = box(12.5, 52.4, 12.6, 52.5)
    g2 = box(12.5 + 1e-9, 52.4, 12.6, 52.5 - 1e-9)
    assert feature_hash("X", {"a": 1, "b": "2"}, g1) == feature_hash("X", {"b": "2", "a": 1}, g2)
    assert feature_hash("X", {"a": 1}, g1) != feature_hash("X", {"a": 2}, g1)
    assert feature_hash("X", {"a": 1}, Point(12.5, 52.4)) != feature_hash("X", {"a": 1}, Point(12.5001, 52.4))


def test_value_hashes() -> None:
    v = pd.DataFrame({"name": ["q", "q"], "value": [1.5, 1.5], "period": ["2023", "2024"], "extra": [None, None]})
    h = value_hashes(v)
    assert h[0] != h[1] and len(h[0]) == 32


def test_norm_text_numbers_json_and_text() -> None:
    assert norm_text("320.070") == norm_text(320.07) == "320.07"
    assert norm_text("12") == norm_text("12.0") == norm_text(12.0) == "12"
    assert norm_text('{"b": 1, "a": "x"}') == norm_text('{"a":"x","b":1}')
    assert norm_text("  Potsdam ") == "Potsdam" and norm_text(None) is None
    assert norm_text("nan") == "nan" and norm_text("1.234,5") == "1.234,5"  # kein Raten bei Nicht-Zahlen


def test_geometry_fingerprint_single_equals_multi_and_rounds() -> None:
    poly = box(13.0, 52.0, 13.1, 52.1)
    as_hex = shapely.to_wkb(poly, hex=True, include_srid=False)
    multi_hex = shapely.to_wkb(MultiPolygon([poly]), hex=True)
    shifted = shapely.to_wkb(box(13.0, 52.0, 13.1 + 1e-9, 52.1), hex=True)
    assert geometry_fingerprint(as_hex) == geometry_fingerprint(multi_hex) == geometry_fingerprint(shifted)
    assert geometry_fingerprint(None) == b""


def test_projection_hash_ignores_row_order() -> None:
    a = projection_hash(b"g", ["Name"], [["x", "1"], ["y", "2"]])
    assert a == projection_hash(b"g", ["Name"], [["y", "2.0"], ["x", "1"]])
    assert a != projection_hash(b"g", ["Name"], [["x", "1"]])
    assert projection_hash(b"", [None]) != projection_hash(b"", [""])
