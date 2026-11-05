"""Demo-Modus für Präsentationen: gezielt veränderte Daten, sichtbar als SIMULATION gekennzeichnet.

``pipeline demo --changes`` liest die zuletzt gespeicherten **echten** Rohdaten jeder aktiven
Quelle erneut ein und verändert die gelesenen Datensätze im Speicher: einige Attribute ändern,
einige Objekte entfernen, einige mit verschobener Geometrie ergänzen, wenige absichtlich
ungültig machen. Danach läuft die Pipeline normal durch. Jeder solche Lauf trägt
``simulated=true`` in Lauf-Historie, Bericht („SIMULATION“ im Titel) und Karte (Banner).

Sperren – alle drei müssen erfüllt sein, sonst Abbruch:

1. ``PIPELINE_ENV=demo``
2. Ziel-Adapter ``demo_reference``
3. Name der Zieldatenbank endet auf ``_demo``

``pipeline demo --reset`` liest dieselben echten Rohdaten unverändert ein und stellt den Stand wieder her.
Weil die Pipeline Geometrien nie löscht (Bieterantwort 74), entfernt der Reset vorher gezielt die
simulierten Zusatzobjekte (Schlüssel ``…_sim<n>``) – nur in der Demo-Datenbank.
"""

from __future__ import annotations

import os
import random
from dataclasses import replace
from typing import Any

from psycopg import sql
from shapely import affinity
from shapely.geometry import Polygon

from geoimport.config.loader import ConfigError, LoadedConfig
from geoimport.config.models import SourceBase
from geoimport.db import connect
from geoimport.diff import feature_hash
from geoimport.orchestrator import RunOptions, latest_raw_dir
from geoimport.sources.base import ParseResult, PipelineWarning, RecordError
from geoimport.target.demo_reference import require_demo_database

DEMO_MARK = "SIMULATION"
SIM_KEY_RE = "_sim[0-9]+$"  # Schlüssel der simulierten Zusatzobjekte (siehe mutate)


def require_demo(cfg: LoadedConfig) -> str:
    """Prüft alle drei Sperren; liefert den Namen der Demo-Datenbank."""
    if os.environ.get("PIPELINE_ENV") != "demo":
        raise ConfigError("Demo-Modus nur mit PIPELINE_ENV=demo")
    if cfg.target.adapter != "demo_reference":
        raise ConfigError("Demo-Modus nur mit dem Ziel-Adapter 'demo_reference' (TARGET_CONFIG=target.demo.yaml)")
    conn = connect(autocommit=True)
    try:
        return require_demo_database(conn)
    except RuntimeError as exc:
        raise ConfigError(str(exc)) from exc
    finally:
        conn.close()


def mutate(seed: int = 20261106) -> Any:
    """Erzeugt die Veränderungsfunktion für den Orchestrator (deterministisch über ``seed``)."""
    rng = random.Random(seed)  # noqa: S311 – keine Kryptografie, nur reproduzierbare Demo

    def _mutate(src: SourceBase, parsed: ParseResult) -> list[PipelineWarning]:
        if parsed.values is not None:
            df = parsed.values
            if df.empty:
                return []
            n = min(len(df), 25)
            idx = rng.sample(range(len(df)), n)
            changed = idx[: n // 2]
            dropped = idx[n // 2 :]
            df.loc[changed, "value"] = df.loc[changed, "value"].map(lambda v: _bump(v))
            from geoimport.diff import value_hashes

            df.loc[changed, "hash"] = value_hashes(df.loc[changed])
            parsed.values = df.drop(index=dropped).reset_index(drop=True)
            return [
                PipelineWarning(
                    "simulation", f"{DEMO_MARK}: {len(changed)} Werte geändert, {len(dropped)} Zeilen entfernt"
                )
            ]

        feats = parsed.features
        if len(feats) < 4:
            return [PipelineWarning("simulation", f"{DEMO_MARK}: zu wenige Objekte für eine Veränderung")]
        order = list(range(len(feats)))
        rng.shuffle(order)
        k = max(1, min(5, len(feats) // 10))
        to_change, to_drop, to_copy = order[:k], set(order[k : 2 * k]), order[2 * k : 3 * k]
        for i in to_change:
            f = feats[i]
            attrs = dict(f.attributes)
            name = next(iter(attrs), None)
            if name is not None:
                attrs[name] = f"{attrs[name]} ({DEMO_MARK})"
            feats[i] = replace(f, attributes=attrs, hash=feature_hash(f.name, attrs, f.geometry))
        added = []
        for n, i in enumerate(to_copy, start=1):
            f = feats[i]
            geom = affinity.translate(f.geometry, xoff=0.02, yoff=0.01)
            key = f"{f.key}_sim{n}"
            name = f"{f.name or f.key} ({DEMO_MARK} neu)"
            added.append(replace(f, key=key, name=name, geometry=geom, hash=feature_hash(name, f.attributes, geom)))
        kept = [f for i, f in enumerate(feats) if i not in to_drop] + added
        parsed.features = kept
        # absichtlich ungültige Datensätze (erscheinen als Fehler-Marker)
        first_geom = feats[order[0]].geometry
        assert first_geom is not None
        ref = first_geom.centroid
        bowtie = Polygon([(ref.x, ref.y), (ref.x + 0.01, ref.y + 0.01), (ref.x + 0.01, ref.y), (ref.x, ref.y + 0.01)])
        parsed.errors += [
            RecordError(
                f"{DEMO_MARK}_1",
                "invalid_geometry",
                f"{DEMO_MARK}: ungültige Geometrie (Selbstüberschneidung)",
                bowtie.representative_point(),
                {"hinweis": DEMO_MARK},
            ),
            RecordError(
                f"{DEMO_MARK}_2",
                "missing_required_field",
                f"{DEMO_MARK}: Pflichtfeld leer",
                affinity.translate(ref, 0.03, -0.02),
                {"hinweis": DEMO_MARK},
            ),
            RecordError(None, "missing_key", f"{DEMO_MARK}: Schlüsselfeld leer", None, {"hinweis": DEMO_MARK}),
        ]
        return [
            PipelineWarning(
                "simulation",
                f"{DEMO_MARK}: {len(to_change)} geändert, {len(to_drop)} entfernt, "
                f"{len(added)} ergänzt, 3 ungültige Datensätze",
            )
        ]

    return _mutate


def _bump(v: Any) -> Any:
    try:
        return round(float(v) * 1.1 + 1, 2)
    except (TypeError, ValueError):
        return v


def demo_options(cfg: LoadedConfig, changes: bool, source_ids: list[str] | None = None) -> RunOptions:
    """Lauf-Optionen für ``demo --changes`` bzw. ``demo --reset`` (erneutes Einlesen der echten Rohdaten)."""
    sources = [cfg.source(s) for s in source_ids] if source_ids else cfg.enabled_sources()
    replay = {}
    for s in sources:
        d = latest_raw_dir(cfg.pipeline.storage.raw_dir, s.id)
        if d is None:
            raise ConfigError(f"Keine Rohdaten für '{s.id}' – zuerst einen echten Lauf ausführen (pipeline run)")
        replay[s.id] = d
    return RunOptions(
        source_ids=[s.id for s in sources],
        trigger="demo",
        simulated=changes,
        replay=replay,
        mutate=mutate() if changes else None,
        full=True,
        force_replace={s.id for s in sources},
    )


def purge_simulated(cfg: LoadedConfig, source_ids: list[str] | None = None) -> int:
    """Simulierte Zusatzobjekte (Geometrien und ihre Attribute) entfernen; nur in der Demo-Datenbank.

    Die Pipeline selbst löscht Geometrien nie. Ohne diesen Schritt blieben die bei ``--changes``
    ergänzten Objekte nach ``--reset`` stehen.
    """
    t = cfg.target
    g, a = t.tables.geometries, t.tables.attributes
    sources = [cfg.source(s) for s in source_ids] if source_ids else cfg.enabled_sources()
    datasets = [s.target.dataset for s in sources]
    geo = sql.SQL("{}.{}").format(sql.Identifier(t.schema_name), sql.Identifier(g.name))
    attr = sql.SQL("{}.{}").format(sql.Identifier(t.schema_name), sql.Identifier(a.name))
    meta = sql.SQL("{}.{}").format(sql.Identifier(t.schema_name), sql.Identifier(t.tables.metadata.name))
    if not (g.fk_metadata and a.fk_geometry):
        return 0  # Referenzmodell der Demo hat beides; andere Modelle werden hier nicht angefasst
    sim = sql.SQL(
        "SELECT g.{gpk} FROM {geo} g JOIN {meta} m ON m.{mpk} = g.{gfk} "
        "WHERE m.{mds} = ANY(%(ds)s) AND g.{gkey} ~ %(re)s"
    ).format(
        gpk=sql.Identifier(g.pk),
        geo=geo,
        meta=meta,
        mpk=sql.Identifier(t.tables.metadata.pk),
        gfk=sql.Identifier(g.fk_metadata),
        mds=sql.Identifier(t.tables.metadata.dataset_column),
        gkey=sql.Identifier(g.object_key_column),
    )
    conn = connect(autocommit=True)
    try:
        require_demo_database(conn)
        params = {"ds": datasets, "re": SIM_KEY_RE}
        with conn.transaction():
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE {} IN (").format(attr, sql.Identifier(a.fk_geometry))
                + sim
                + sql.SQL(")"),
                params,
            )
            cur = conn.execute(
                sql.SQL("DELETE FROM {} WHERE {} IN (").format(geo, sql.Identifier(g.pk)) + sim + sql.SQL(")"), params
            )
            return cur.rowcount
    finally:
        conn.close()
