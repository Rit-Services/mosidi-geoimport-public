"""Vorlage für einen neuen Quelltyp (z. B. OGC API Features, Shapefile, GeoPackage).

Schritte (ausführlich in ``docs/entwicklung.md``):

1. Datei kopieren, z. B. nach ``geoimport/sources/ogcapi.py``.
2. Konfigurationsmodell mit ``type: Literal["ogcapi"]`` definieren (erbt von ``SourceBase``).
3. ``fetch()`` lädt Rohdaten nach ``self.ctx.raw_dir`` (``self.ctx.http`` nutzen: Timeouts, Wiederholversuche).
4. ``parse()`` liest sie und ruft ``prepare_features`` (Objekte mit Geometrie) auf bzw. liefert
   Kennzahlen als DataFrame (``VALUE_COLUMNS``).
5. Modul in ``BUILTIN_MODULES`` (``geoimport/sources/__init__.py``) eintragen oder per
   Umgebungsvariable ``GEOIMPORT_PLUGINS=paket.modul`` laden.
6. Tests mit aufgezeichneten Antworten unter ``tests/fixtures/`` ergänzen.

Der Kern (Orchestrator, Ziel-Adapter, Bericht) bleibt unverändert.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from geoimport.config.models import Mapping, SourceBase
from geoimport.sources import register_source
from geoimport.sources.base import FetchResult, ParseResult, RawArtifact, Source, SourceFailure
from geoimport.transform.validate import RawFeature, prepare_features


class TemplateSettings(SourceBase):
    """Konfiguration des Beispieltyps (Datei ``config/sources/<id>.yaml``)."""

    type: Literal["template_example"]
    url: str
    crs: str = "EPSG:4326"
    mapping: Mapping = Mapping()


# Zum Aktivieren: Kommentarzeichen entfernen und Modul registrieren (siehe Schritt 5).
# @register_source("template_example", TemplateSettings)
class TemplateSource(Source):
    """Beispiel: GeoJSON-Datei per HTTP."""

    cfg: TemplateSettings

    def location_url(self) -> str | None:  # noqa: D102
        return self.cfg.url

    def probe(self) -> dict[str, Any]:  # noqa: D102
        return {"url": self.cfg.url, "erreichbar": self.ctx.http.head_ok(self.cfg.url)}

    def fetch(self) -> FetchResult:  # noqa: D102
        response = self.ctx.http.get(self.cfg.url)
        self.ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.ctx.raw_dir / "data.geojson"
        path.write_bytes(response.content)
        sha = hashlib.sha256(response.content).hexdigest()
        return FetchResult([RawArtifact(path, sha, self.cfg.url)], state_updates={"raw_sha256": sha})

    def parse(self, fetched: FetchResult) -> ParseResult:  # noqa: D102
        import json

        import shapely.geometry

        try:
            data = json.loads(fetched.artifacts[0].path.read_bytes())
        except ValueError as exc:
            raise SourceFailure("parse", f"GeoJSON nicht lesbar: {exc}") from exc
        raw = [
            RawFeature(
                {k: (None if v is None else str(v)) for k, v in (f.get("properties") or {}).items()},
                shapely.geometry.shape(f["geometry"]) if f.get("geometry") else None,
            )
            for f in data.get("features", [])
        ]
        features, errors = prepare_features(
            raw,
            key=self.cfg.key,
            mapping=self.cfg.mapping,
            required=self.cfg.guards.required,
            geometry_types=self.cfg.guards.geometry_types,
            src_crs=self.cfg.crs,
            pipeline=self.ctx.pipeline,
        )
        return ParseResult(features=features, errors=errors, fetched=len(raw))


__all__ = ["TemplateSettings", "TemplateSource", "register_source"]
