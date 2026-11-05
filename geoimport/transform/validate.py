"""Vom Quellobjekt zum prüften ``FeatureRecord``: Mapping, Schlüssel, CRS, Geometrie, Pflichtfelder, Hash."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from shapely.geometry.base import BaseGeometry

from geoimport.config.models import KeyConfig, Mapping, PipelineConfig
from geoimport.diff import feature_hash
from geoimport.sources.base import FeatureRecord, RecordError
from geoimport.transform.crs import CrsError, check_plausible, to_target
from geoimport.transform.geometry import GeometryError, check_geometry
from geoimport.transform.keys import build_key, normalize
from geoimport.transform.mapping import MappingError, apply_mapping


@dataclass(slots=True)
class RawFeature:
    """Ein gelesenes Quellobjekt (Eigenschaften als Text, Geometrie im Quell-CRS)."""

    props: dict[str, Any]
    geometry: BaseGeometry | None
    fid: str | None = None


def _safe_raw(props: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v)) for k, v in props.items()}


def record_key(key: KeyConfig, props: dict[str, Any]) -> str | None:
    """Fachlicher Schlüssel aus den Quell- bzw. Zielfeldern."""
    return build_key([normalize(props.get(f), key.normalize.get(f)) for f in key.fields])


def prepare_features(
    raw: Iterable[RawFeature],
    *,
    key: KeyConfig,
    mapping: Mapping,
    required: list[str],
    geometry_types: list[str] | None,
    src_crs: str,
    pipeline: PipelineConfig,
    dedupe_identical: bool = False,
) -> tuple[list[FeatureRecord], list[RecordError]]:
    """Wandelt Quellobjekte in Zieldatensätze; fehlerhafte Objekte werden mit Grund gesammelt.

    ``required`` bezieht sich auf Quellfelder. Doppelte Schlüssel sind Fehler – außer bei
    ``dedupe_identical`` (Kachelabruf), wo identische Objekte an Kachelgrenzen mehrfach kommen.
    """
    bbox = pipeline.crs.plausibility_bbox_4326
    records: dict[str, FeatureRecord] = {}
    errors: list[RecordError] = []
    for item in raw:
        props = item.props
        k = record_key(key, props)
        geom4326: BaseGeometry | None = None
        try:
            if k is None:
                raise GeometryError("missing_key", f"Schlüsselfeld(er) {key.fields} leer")
            missing = [f for f in required if props.get(f) in (None, "")]
            if missing:
                raise GeometryError("missing_required_field", f"Pflichtfeld(er) leer: {', '.join(missing)}")
            if item.geometry is None:
                raise GeometryError("missing_geometry", "Geometrie fehlt")
            geom4326 = to_target(item.geometry, src_crs)
            check_plausible(geom4326, bbox)
            geom4326, repaired = check_geometry(geom4326, geometry_types)
            name, attrs = apply_mapping(mapping, props)
        except GeometryError as exc:
            errors.append(RecordError(k, exc.code, str(exc), _usable(geom4326), _safe_raw(props)))
            continue
        except CrsError as exc:
            errors.append(RecordError(k, "crs_implausible", str(exc), None, _safe_raw(props)))
            continue
        except MappingError as exc:
            errors.append(RecordError(k, "mapping_error", str(exc), _usable(geom4326), _safe_raw(props)))
            continue
        rec = FeatureRecord(k, name, geom4326, attrs, feature_hash(name, attrs, geom4326), repaired)
        prev = records.get(k)
        if prev is not None:
            if dedupe_identical and prev.hash == rec.hash:
                continue
            errors.append(
                RecordError(k, "duplicate_key", f"Schlüssel {k!r} mehrfach geliefert", geom4326, _safe_raw(props))
            )
            continue
        records[k] = rec
    return list(records.values()), errors


def _usable(geom: BaseGeometry | None) -> BaseGeometry | None:
    """Geometrie für Fehler-Marker, nur wenn darstellbar."""
    if geom is None or geom.is_empty:
        return None
    try:
        return geom if geom.is_valid else geom.representative_point()
    except Exception:  # GEOS-Fehler bei kaputten Geometrien
        return None
