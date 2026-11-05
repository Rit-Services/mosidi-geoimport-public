"""Generischer Ziel-Adapter für das Drei-Tabellen-Modell (Metadaten, Geometrien, Attribute).

Tabellen- und Spaltennamen stehen vollständig in ``target.yaml``; der Code kennt keine
Namen des Zielmodells. Ablauf von ``replace`` (in der Transaktion des Aufrufers):

0. Ladezeilen im COPY-Format in einer temporären Datei aufbauen (noch ohne Sperren), dann
   Datensatz-Sperre ``pg_advisory_xact_lock(hashtext('geoimport:dataset'), hashtext(<dataset>))``.
1. Metadatenzeile des Datensatzes suchen (``dataset_column``) und aktualisieren bzw. anlegen.
2. Alte Attribute des Datensatzes löschen. Geometrien werden nie gelöscht (Bieterantwort 74).
3. Neue Geometrien per ``COPY`` in eine temporäre Tabelle laden und über den fachlichen Schlüssel
   (``object_key_column``) übernehmen: vorhandene aktualisieren (``UPDATE``, nur bei Änderung),
   fehlende anlegen (``INSERT``). Geometrien, die in der Lieferung fehlen, bleiben stehen und werden
   im Bericht gemeldet (``geometry_retained``). Ihre Primärschlüssel bleiben stabil, damit Verweise
   anderer Daten (z. B. der MOSIDI-Anwendung) gültig bleiben.
4. Neue Attribute per ``COPY`` laden und mit ``INSERT … SELECT`` übernehmen; Typen werden auf die
   Spaltentypen des Zielmodells gecastet.

Spaltenvorlagen in ``columns`` (z. B. ``"{source.license}"``) werden je Datensatz bzw. je Zeile
ausgewertet. Verfügbar: ``dataset``, ``run.run_id``, ``run.started_at``, ``source.id|title|license|
attribution|type|url``, ``meta.<feld>`` (aus ``target.metadata`` der Quelle) sowie je Zeile
``record.key|name|object_key|value|period``, ``record.attr.<name>`` und ``record.extra.<name>``.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import IO, Any

import pandas as pd
import psycopg
import shapely
from psycopg import sql
from psycopg.copy import Copy, FileWriter

from geoimport.config.models import IDENT_RE, TargetConfig
from geoimport.diff import canonical_json, geometry_fingerprint, projection_hash
from geoimport.sources.base import FeatureRecord
from geoimport.target.adapter import (
    DatasetContext,
    DiffBasis,
    OwnershipError,
    ProjectionUnavailable,
    ReplaceStats,
    SchemaIssue,
)
from geoimport.target.guard import as_text, check_sql, guarded_execute
from geoimport.target.introspect import not_null_columns, table_columns
from geoimport.transform.keys import KEY_SEP, to_text

TEMPLATE_RE = re.compile(r"\{([a-z_]+(?:\.[A-Za-z0-9_]+)*)\}")
CONST_ROOTS = {"dataset", "run", "source", "meta"}
ROW_ROOTS = {"record"}
_TMP_GEO = "tmp_geoimport_geo"
_TMP_ATTR = "tmp_geoimport_attr"
# Datensatz-Sperre mit zwei Schlüsseln: eigener Schlüsselraum, getrennt von Sperren mit einem
# bigint-Schlüssel; ein Datensatz namens „run“ trifft so nie die Lauf-Sperre.
_DATASET_LOCK = "SELECT pg_advisory_xact_lock(hashtext('geoimport:dataset'), hashtext(%s))"
_SPOOL_MEMORY = 32 * 1024 * 1024  # darüber lagern die Ladezeilen in einer Datei unter TMPDIR
_COPY_BLOCK = 1024 * 1024
MAX_RETAINED_EXAMPLES = 20  # Beispiele erhaltener Geometrien im Bericht


@dataclass
class _Spool:
    """Vorbereitete Ladezeilen im COPY-Textformat."""

    file: IO[bytes]
    rows: int = 0

    def __enter__(self) -> _Spool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.file.close()


def _row_cols(columns: dict[str, str]) -> list[str]:
    """Spalten, deren Vorlage Felder je Zeile (``record.…``) nutzt."""
    return [c for c, tpl in columns.items() if is_row_template(tpl)]


def _frame(keys: list[str], hashes: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"key": pd.Series(keys, dtype="string"), "hash": pd.Series(hashes, dtype="string")})


class TargetError(RuntimeError):
    """Zielmodell passt nicht zur Konfiguration oder zu den Daten."""


def template_fields(template: str) -> list[str]:
    """Platzhalter einer Vorlage, z. B. ``['source.license']``."""
    return TEMPLATE_RE.findall(template)


def is_row_template(template: str) -> bool:
    """True, wenn die Vorlage Felder je Zeile (``record.…``) nutzt."""
    return any(f.split(".")[0] in ROW_ROOTS for f in template_fields(template))


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def render(template: str, ctx: dict[str, Any]) -> Any:
    """Vorlage auswerten. Besteht sie aus genau einem Platzhalter, bleibt der Typ erhalten."""
    fields = template_fields(template)
    if not fields:
        return template
    if template == "{" + fields[0] + "}":
        return _lookup(ctx, fields[0])
    return TEMPLATE_RE.sub(lambda m: "" if (v := _lookup(ctx, m.group(1))) is None else str(v), template)


def _text(value: Any) -> str | None:
    """Wert als Text für COPY (Dicts/Listen als JSON)."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return to_text(value)


class TableMappingAdapter:
    """Adapter ``table_mapping`` (Produktivpfad)."""

    def __init__(self, cfg: TargetConfig) -> None:
        self.cfg = cfg
        self.types: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------ Bezeichner
    def _t(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.cfg.schema_name), sql.Identifier(name))

    @staticmethod
    def _c(name: str) -> sql.Identifier:
        return sql.Identifier(name)

    def _type(self, table: str, column: str) -> sql.SQL:
        typ = self.types.get(table, {}).get(column)
        if typ is None:
            raise TargetError(f"Spaltentyp {table}.{column} unbekannt – erst check() ausführen")
        return sql.SQL(typ)  # stammt aus format_type() des Systemkatalogs

    def _cast(self, expr: sql.Composable, table: str, column: str) -> sql.Composed:
        return sql.SQL("CAST({} AS {})").format(expr, self._type(table, column))

    # ------------------------------------------------------------------ Prüfung
    def load_types(self, conn: psycopg.Connection[Any]) -> None:
        """Spaltentypen der drei Tabellen laden."""
        t = self.cfg.tables
        for table in (t.metadata.name, t.geometries.name, t.attributes.name):
            self.types[table] = table_columns(conn, self.cfg.schema_name, table)

    def check(self, conn: psycopg.Connection[Any]) -> list[SchemaIssue]:
        """Vergleicht ``target.yaml`` mit dem Systemkatalog. Ändert nichts."""
        issues: list[SchemaIssue] = []
        if self.cfg.has_placeholders():
            return [
                SchemaIssue("error", "target.yaml enthält Platzhalter (TODO_TH…) – Zielmodell der TH noch eintragen")
            ]
        bad = [n for n in self.cfg.identifiers() if not IDENT_RE.fullmatch(n)]
        if bad:
            return [SchemaIssue("error", f"Ungültige Bezeichner in target.yaml: {', '.join(bad)}")]
        for name, tpl in self._all_templates():
            for f in template_fields(tpl):
                if f.split(".")[0] not in CONST_ROOTS | ROW_ROOTS:
                    issues.append(SchemaIssue("error", f"Spalte {name}: unbekannter Platzhalter {{{f}}}"))
        self.load_types(conn)
        t = self.cfg.tables
        wanted: dict[str, list[str]] = {
            t.metadata.name: [t.metadata.pk, t.metadata.dataset_column, *t.metadata.columns],
            t.geometries.name: [
                t.geometries.pk,
                t.geometries.object_key_column,
                t.geometries.geometry_column,
                *[x for x in (t.geometries.fk_metadata, t.geometries.source_column) if x],
                *t.geometries.columns,
            ],
            t.attributes.name: [
                *[
                    x
                    for x in (
                        t.attributes.fk_metadata,
                        t.attributes.fk_geometry,
                        t.attributes.source_column,
                        t.attributes.object_key_column,
                        t.attributes.name_column,
                        t.attributes.value_column,
                        t.attributes.period_column,
                        t.attributes.jsonb_column,
                    )
                    if x
                ],
                *t.attributes.wide_columns.values(),
                *t.attributes.columns,
            ],
        }
        for table, cols in wanted.items():
            have = self.types.get(table) or {}
            if not have:
                issues.append(
                    SchemaIssue("error", f"Tabelle {self.cfg.schema_name}.{table} fehlt oder ist nicht lesbar")
                )
                continue
            for col in dict.fromkeys(cols):
                if col not in have:
                    issues.append(SchemaIssue("error", f"Spalte {table}.{col} fehlt"))
            written = set(cols) - {t.metadata.pk if table == t.metadata.name else "", t.geometries.pk}
            for col in sorted(not_null_columns(conn, self.cfg.schema_name, table) - written):
                issues.append(
                    SchemaIssue("warning", f"Spalte {table}.{col} ist NOT NULL ohne Default, wird aber nicht befüllt")
                )
        gtype = (self.types.get(t.geometries.name) or {}).get(t.geometries.geometry_column, "")
        if gtype:
            if not gtype.startswith("geometry"):
                issues.append(
                    SchemaIssue(
                        "error", f"{t.geometries.name}.{t.geometries.geometry_column} ist {gtype}, nicht geometry"
                    )
                )
            else:
                m = re.search(r",\s*(\d+)\)", gtype)
                if m and m.group(1) != "4326":
                    issues.append(SchemaIssue("error", f"Geometriespalte hat SRID {m.group(1)} statt 4326 (A7)"))
        for schema, view in self.cfg.refresh_views():
            row = conn.execute(
                "SELECT 1 FROM pg_matviews WHERE schemaname = %s AND matviewname = %s", (schema, view)
            ).fetchone()
            if row is None:
                issues.append(
                    SchemaIssue(
                        "error", f"after_import: materialisierte Sicht {schema}.{view} fehlt oder ist nicht lesbar"
                    )
                )
        if t.attributes.layout == "jsonb" and t.attributes.jsonb_column:
            jt = (self.types.get(t.attributes.name) or {}).get(t.attributes.jsonb_column, "")
            if jt and jt not in ("jsonb", "json"):
                issues.append(SchemaIssue("warning", f"jsonb_column hat Typ {jt}"))
        return issues

    def _all_templates(self) -> Iterator[tuple[str, str]]:
        t = self.cfg.tables
        for tab in (t.metadata, t.geometries, t.attributes):
            for col, tpl in tab.columns.items():
                yield f"{tab.name}.{col}", tpl

    # ------------------------------------------------------------------ Kontext
    @staticmethod
    def const_context(ctx: DatasetContext) -> dict[str, Any]:
        """Platzhalterwerte, die für den ganzen Datensatz gelten."""
        s = ctx.source
        return {
            "dataset": ctx.dataset,
            "run": {"run_id": ctx.run_id, "started_at": ctx.run_started_at},
            "source": {
                "id": s.id,
                "title": s.title,
                "license": s.license,
                "attribution": s.attribution,
                "type": getattr(s, "type", None),
                "url": ctx.source_url,
            },
            "meta": dict(s.target.metadata),
        }

    def _scope_geo(self) -> sql.Composable:
        """WHERE-Bedingung für die Geometrien des Datensatzes (Spalte, die ``replace`` beim Einfügen befüllt)."""
        g = self.cfg.tables.geometries
        if g.source_column and (self.cfg.delete_scope.strategy == "by_source_column" or not g.fk_metadata):
            return sql.SQL("{} = %(dataset)s").format(self._c(g.source_column))
        if g.fk_metadata:
            return sql.SQL("{} = %(meta_id)s").format(self._c(g.fk_metadata))
        if self.cfg.delete_scope.geometry_scope:  # geprüft in DeleteScope (Bedingung mit Scope-Parameter)
            # Zeilenumbruch: ein Kommentar am Ende der Bedingung darf die Klammer nicht verdecken
            return sql.SQL("({}\n)").format(sql.SQL(self.cfg.delete_scope.geometry_scope))
        raise TargetError(
            "geometries braucht fk_metadata, source_column oder delete_scope.geometry_scope, "
            "um Geometrien des Datensatzes zu finden"
        )

    def _in_scope_geo(self, alias: str) -> sql.Composed:
        """``<alias>.pk`` gehört zu den Geometrien des Datensatzes (unabhängig von Spaltennamen der Ladetabelle)."""
        g = self.cfg.tables.geometries
        return (
            sql.SQL("{}.{} IN (SELECT {} FROM {} WHERE ").format(
                sql.Identifier(alias), self._c(g.pk), self._c(g.pk), self._t(g.name)
            )
            + self._scope_geo()
            + sql.SQL(")")
        )

    def _scope_attr(self) -> sql.Composable:
        a, g = self.cfg.tables.attributes, self.cfg.tables.geometries
        if self.cfg.delete_scope.strategy == "by_source_column" and a.source_column:
            return sql.SQL("{} = %(dataset)s").format(self._c(a.source_column))
        if a.fk_metadata:
            return sql.SQL("{} = %(meta_id)s").format(self._c(a.fk_metadata))
        if not a.fk_geometry:
            raise TargetError("attributes braucht fk_metadata, fk_geometry oder source_column (Löschbereich)")
        return (
            sql.SQL("{} IN (SELECT {} FROM {} WHERE ").format(self._c(a.fk_geometry), self._c(g.pk), self._t(g.name))
            + self._scope_geo()
            + sql.SQL(")")
        )

    def _scope_query(self, kind: str) -> str:
        """Eigene Bestandsabfrage (``delete_scope.scope_sql``) ohne abschließendes Semikolon."""
        scope = self.cfg.delete_scope.scope_sql
        if scope is None:
            raise TargetError("delete_scope sql braucht scope_sql")
        return (scope.features if kind == "features" else scope.values).strip().rstrip(";")

    def metadata_id(self, conn: psycopg.Connection[Any], dataset: str, lock: bool = False) -> Any:
        """PK der Metadatenzeile des Datensatzes (``None``, wenn es noch keine gibt)."""
        m = self.cfg.tables.metadata
        q = sql.SQL("SELECT {} FROM {} WHERE {} = %s" + (" FOR UPDATE" if lock else "")).format(
            self._c(m.pk), self._t(m.name), self._c(m.dataset_column)
        )
        with conn.cursor() as cur:
            row = guarded_execute(cur, q, (dataset,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ Lesen
    def count_existing(self, conn: psycopg.Connection[Any], dataset: str, kind: str) -> int:  # noqa: D102
        strategy = self.cfg.delete_scope.strategy
        meta_id = self.metadata_id(conn, dataset)
        params = {"dataset": dataset, "meta_id": meta_id}
        if strategy == "sql":
            # derselbe Bereich, den die eigenen DELETE-Anweisungen löschen (H6)
            q: sql.Composable = sql.SQL("SELECT count(*) FROM (\n{}\n) AS s").format(sql.SQL(self._scope_query(kind)))
        elif meta_id is None and strategy != "by_source_column":
            return 0
        elif kind == "features":
            q = (
                sql.SQL("SELECT count(*) FROM {} WHERE ").format(self._t(self.cfg.tables.geometries.name))
                + self._scope_geo()
            )
        else:
            q = (
                sql.SQL("SELECT count(*) FROM {} WHERE ").format(self._t(self.cfg.tables.attributes.name))
                + self._scope_attr()
            )
        with conn.cursor() as cur:
            row = guarded_execute(cur, q, params).fetchone()
        return int(row[0]) if row else 0

    def existing_feature_keys(self, conn: psycopg.Connection[Any], dataset: str) -> pd.DataFrame:  # noqa: D102
        g = self.cfg.tables.geometries
        strategy = self.cfg.delete_scope.strategy
        meta_id = self.metadata_id(conn, dataset)
        if strategy == "sql":
            # Spaltenalias benennt nur die erste Spalte (Objektschlüssel) um
            q: sql.Composable = sql.SQL("SELECT DISTINCT s.k::text FROM (\n{}\n) AS s(k)").format(
                sql.SQL(self._scope_query("features"))
            )
        elif meta_id is None and strategy != "by_source_column":
            return pd.DataFrame({"key": pd.Series(dtype="string")})
        else:
            q = (
                sql.SQL("SELECT DISTINCT {}::text FROM {} WHERE ").format(self._c(g.object_key_column), self._t(g.name))
                + self._scope_geo()
            )
        with conn.cursor() as cur:
            rows = guarded_execute(cur, q, {"dataset": dataset, "meta_id": meta_id}).fetchall()
        return pd.DataFrame({"key": pd.Series([r[0] for r in rows], dtype="string")})

    # ------------------------------------------------------------------ Schreiben
    def _upsert_metadata(self, cur: psycopg.Cursor[Any], ctx: DatasetContext, const: dict[str, Any]) -> Any:
        m = self.cfg.tables.metadata
        meta_id = self.metadata_id(cur.connection, ctx.dataset, lock=True)
        if ctx.check_metadata_id and meta_id != ctx.expected_metadata_id:
            found = "keine" if meta_id is None else f"{m.pk}={meta_id}"
            raise OwnershipError(
                f"Metadatenzeile von '{ctx.dataset}' hat sich seit der Besitzprüfung geändert ({found}) – nicht übernommen"
            )
        cols = list(m.columns)
        vals = {f"m{i}": _text(render(m.columns[c], const)) for i, c in enumerate(cols)}
        if meta_id is not None:
            if cols:
                sets = sql.SQL(", ").join(
                    sql.SQL("{} = {}").format(self._c(c), self._cast(sql.Placeholder(f"m{i}"), m.name, c))
                    for i, c in enumerate(cols)
                )
                q = sql.SQL("UPDATE {} SET {} WHERE {} = %(pk)s").format(self._t(m.name), sets, self._c(m.pk))
                guarded_execute(cur, q, {**vals, "pk": meta_id})
            return meta_id
        names = [m.dataset_column, *cols]
        exprs = [self._cast(sql.Placeholder("dataset"), m.name, m.dataset_column)] + [
            self._cast(sql.Placeholder(f"m{i}"), m.name, c) for i, c in enumerate(cols)
        ]
        q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            self._t(m.name), sql.SQL(", ").join(map(self._c, names)), sql.SQL(", ").join(exprs), self._c(m.pk)
        )
        row = guarded_execute(cur, q, {**vals, "dataset": ctx.dataset}).fetchone()
        if row is None:
            raise TargetError(f"INSERT in {m.name} lieferte keinen Primärschlüssel")
        return row[0]

    def _delete(self, cur: psycopg.Cursor[Any], ctx: DatasetContext, meta_id: Any, stats: ReplaceStats) -> None:
        """Attribute des Datensatzes löschen. Geometrien bleiben (Bieterantwort 74), siehe ``_upsert_geometries``."""
        t = self.cfg.tables
        params = {"dataset": ctx.dataset, "meta_id": meta_id}
        if self.cfg.delete_scope.strategy == "sql":
            for i, stmt in enumerate(self.cfg.delete_scope.sql or []):
                guarded_execute(cur, stmt, params)
                stats.deleted[f"sql_{i + 1}"] = cur.rowcount
            return
        q = sql.SQL("DELETE FROM {} WHERE ").format(self._t(t.attributes.name)) + self._scope_attr()
        guarded_execute(cur, q, params)
        stats.deleted[t.attributes.name] = cur.rowcount

    def _geometry_rows(self, features: list[FeatureRecord], const: dict[str, Any]) -> Iterator[list[Any]]:
        """Zeilen für die temporäre Geometrie-Tabelle: object_key, geom (EWKB), c*."""
        g = self.cfg.tables.geometries
        row_cols = _row_cols(g.columns)
        for f in features:
            # nur 2D (Bieterantwort 63): Z- und M-Werte werden nicht gespeichert
            geom = shapely.to_wkb(shapely.set_srid(f.geometry, 4326), hex=True, include_srid=True, output_dimension=2)
            rctx = {**const, "record": {"key": f.key, "name": f.name, "object_key": f.key, "attr": f.attributes}}
            yield [f.key, geom, *[_text(render(g.columns[c], rctx)) for c in row_cols]]

    def _copy_into_temp(self, cur: psycopg.Cursor[Any], table: str, cols: list[str], spool: _Spool) -> None:
        """Temporäre Ladetabelle (nur ``text``-Spalten) anlegen und die vorbereiteten Zeilen per COPY laden."""
        guarded_execute(
            cur,
            sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                sql.SQL(table), sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in cols)
            ),
        )
        copy_q = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.SQL(table), sql.SQL(", ").join(map(sql.Identifier, cols))
        )
        check_sql(as_text(cur.connection, copy_q))
        spool.file.seek(0)
        with cur.copy(copy_q) as cp:
            while block := spool.file.read(_COPY_BLOCK):
                cp.write(block)

    def _upsert_geometries(
        self,
        cur: psycopg.Cursor[Any],
        spool: _Spool,
        const: dict[str, Any],
        params: dict[str, Any],
        stats: ReplaceStats,
    ) -> None:
        """Geometrien über den fachlichen Schlüssel aktualisieren bzw. anlegen; nie löschen (Bieterantwort 74).

        Das Zielmodell hat keinen bekannten Unique-Index auf dem Schlüssel; deshalb kein ``ON CONFLICT``,
        sondern ``UPDATE … FROM`` und ``INSERT … WHERE NOT EXISTS`` innerhalb des Datensatzes. Doppelte
        Schlüssel im Bestand führen zum Abbruch, statt eine der Geometrien zu raten.
        """
        g = self.cfg.tables.geometries
        row_cols = _row_cols(g.columns)
        const_cols = [c for c in g.columns if c not in row_cols]
        self._copy_into_temp(cur, _TMP_GEO, ["object_key", "geom", *[f"c{i}" for i in range(len(row_cols))]], spool)

        gtype = self.types[g.name][g.geometry_column].lower()
        geom_expr: sql.Composable = sql.SQL("ST_SetSRID(t.geom::geometry, 4326)")
        if "multi" in gtype:
            geom_expr = sql.SQL("ST_Multi({})").format(geom_expr)
        names = [g.object_key_column, g.geometry_column]
        exprs: list[sql.Composable] = [self._cast(sql.SQL("t.object_key"), g.name, g.object_key_column), geom_expr]
        if g.fk_metadata:
            names.append(g.fk_metadata)
            exprs.append(self._cast(sql.Placeholder("meta_id"), g.name, g.fk_metadata))
        if g.source_column:
            names.append(g.source_column)
            exprs.append(self._cast(sql.Placeholder("dataset"), g.name, g.source_column))
        for i, c in enumerate(row_cols):
            names.append(c)
            exprs.append(self._cast(sql.SQL("t.{}").format(sql.Identifier(f"c{i}")), g.name, c))
        for i, c in enumerate(const_cols):
            names.append(c)
            params[f"g{i}"] = _text(render(g.columns[c], const))
            exprs.append(self._cast(sql.Placeholder(f"g{i}"), g.name, c))
        self._repair_geometries(cur, stats)
        self._check_unique_keys(cur, params)
        key = sql.SQL("{}.{}::text = t.object_key").format(sql.Identifier("g"), self._c(g.object_key_column))
        # UPDATE nur, wenn sich etwas ändert: unveränderte Zeilen bleiben unberührt (keine neuen Zeilenversionen)
        set_names = [(n, e) for n, e in zip(names, exprs, strict=True) if n != g.object_key_column]
        upd = (
            sql.SQL("UPDATE {} AS g SET {} FROM {} t WHERE ").format(
                self._t(g.name),
                sql.SQL(", ").join(sql.SQL("{} = {}").format(self._c(n), e) for n, e in set_names),
                sql.SQL(_TMP_GEO),
            )
            + key
            + sql.SQL(" AND ")
            + self._in_scope_geo("g")
            + sql.SQL(" AND ROW({}) IS DISTINCT FROM ROW({})").format(
                sql.SQL(", ").join(sql.SQL("g.{}").format(self._c(n)) for n, _ in set_names),
                sql.SQL(", ").join(e for _, e in set_names),
            )
        )
        guarded_execute(cur, upd, params)
        stats.updated[g.name] = cur.rowcount
        ins = (
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} t WHERE NOT EXISTS (SELECT 1 FROM {} g WHERE ").format(
                self._t(g.name),
                sql.SQL(", ").join(map(self._c, names)),
                sql.SQL(", ").join(exprs),
                sql.SQL(_TMP_GEO),
                self._t(g.name),
            )
            + key
            + sql.SQL(" AND ")
            + self._in_scope_geo("g")
            + sql.SQL(")")
        )
        guarded_execute(cur, ins, params)
        stats.inserted[g.name] = cur.rowcount
        self._count_retained(cur, params, stats)

    def _repair_geometries(self, cur: psycopg.Cursor[Any], stats: ReplaceStats) -> None:
        """Schutznetz in der Datenbank (Bieterantwort 63): reparieren statt die Quelle abzubrechen.

        Python prüft und repariert schon vorher (``shapely.make_valid``); PostGIS kann mit einer anderen
        GEOS-Version anders urteilen. Dann: auf 2D reduzieren, ``ST_MakeValid`` und die ursprüngliche
        Geometriefamilie behalten (Fläche bleibt Fläche) → ``db_repaired``. Leer oder nicht reparierbar →
        Zeile aus der Ladetabelle entfernen → ``db_skipped`` (die Attribute dieser Objekte entfallen ebenfalls).
        """
        tmp = sql.SQL(_TMP_GEO)
        g = sql.SQL("geom::geometry")
        hexed = "encode(ST_AsEWKB({}), 'hex')"
        guarded_execute(
            cur,
            sql.SQL("UPDATE {} SET geom = " + hexed + " WHERE ST_NDims({}) > 2").format(
                tmp, sql.SQL("ST_Force2D({})").format(g), g
            ),
        )
        skipped = [
            r[0]
            for r in guarded_execute(
                cur,
                sql.SQL("DELETE FROM {} WHERE geom IS NULL OR ST_IsEmpty({}) RETURNING object_key").format(tmp, g),
            ).fetchall()
        ]
        repaired = {
            r[0]
            for r in guarded_execute(
                cur,
                sql.SQL("UPDATE {} SET geom = " + hexed + " WHERE NOT ST_IsValid({}) RETURNING object_key").format(
                    tmp, sql.SQL("ST_CollectionExtract(ST_MakeValid({}), ST_Dimension({}) + 1)").format(g, g), g
                ),
            ).fetchall()
        }
        if repaired:
            failed = [
                r[0]
                for r in guarded_execute(
                    cur,
                    sql.SQL(
                        "DELETE FROM {} WHERE object_key = ANY(%s) AND (ST_IsEmpty({}) OR NOT ST_IsValid({})) "
                        "RETURNING object_key"
                    ).format(tmp, g, g),
                    (sorted(repaired),),
                ).fetchall()
            ]
            repaired -= set(failed)
            skipped += failed
        stats.db_repaired = len(repaired)
        stats.db_repaired_keys = sorted(repaired)
        stats.db_skipped = len(skipped)
        stats.db_skipped_keys = sorted(skipped)

    def _check_unique_keys(self, cur: psycopg.Cursor[Any], params: dict[str, Any]) -> None:
        """Abbruch, wenn ein Schlüssel im Bestand des Datensatzes mehrfach vorkommt."""
        g = self.cfg.tables.geometries
        q = (
            sql.SQL("SELECT {}::text, count(*) FROM {} WHERE ").format(self._c(g.object_key_column), self._t(g.name))
            + self._scope_geo()
            + sql.SQL(" GROUP BY 1 HAVING count(*) > 1 ORDER BY 1 LIMIT 10")
        )
        dups = guarded_execute(cur, q, params).fetchall()
        if dups:
            shown = ", ".join(f"{k} ({n}×)" for k, n in dups)
            raise TargetError(
                f"Schlüssel mehrfach in {self.cfg.schema_name}.{g.name} im Bestand des Datensatzes: {shown} – "
                "Geometrie nicht eindeutig zuzuordnen, Bestand bereinigen"
            )

    def _count_retained(self, cur: psycopg.Cursor[Any], params: dict[str, Any], stats: ReplaceStats) -> None:
        """Geometrien des Datensatzes, die in der Lieferung fehlen (bleiben stehen, Hinweis im Bericht)."""
        g = self.cfg.tables.geometries
        okey = sql.SQL("g.{}::text").format(self._c(g.object_key_column))
        q = (
            sql.SQL("SELECT {}, count(*) OVER () FROM {} g WHERE ").format(okey, self._t(g.name))
            + self._in_scope_geo("g")
            + sql.SQL(" AND NOT EXISTS (SELECT 1 FROM {} t WHERE t.object_key = {}) ORDER BY 1 LIMIT {}").format(
                sql.SQL(_TMP_GEO), okey, sql.Literal(MAX_RETAINED_EXAMPLES)
            )
        )
        rows = guarded_execute(cur, q, params).fetchall()
        stats.retained = int(rows[0][1]) if rows else 0
        stats.retained_examples = [str(r[0]) for r in rows]

    def _attribute_rows(
        self, features: list[FeatureRecord], values: pd.DataFrame | None, const: dict[str, Any], row_cols: list[str]
    ) -> Iterator[list[Any]]:
        """Zeilen für die temporäre Attribut-Tabelle: object_key, name, value, period, doc, w*, c*."""
        a = self.cfg.tables.attributes
        wide = list(a.wide_columns)
        for f in features:
            base = {"key": f.key, "name": f.name, "object_key": f.key, "attr": f.attributes}
            if a.layout == "key_value":
                for name, value in f.attributes.items():
                    rctx = {**const, "record": {**base, "value": value, "attr_name": name}}
                    yield [
                        f.key,
                        name,
                        _text(value),
                        None,
                        None,
                        *([None] * len(wide)),
                        *[_text(render(a.columns[c], rctx)) for c in row_cols],
                    ]
            else:
                rctx = {**const, "record": base}
                doc = canonical_json(f.attributes) if a.layout == "jsonb" else None
                yield [
                    f.key,
                    None,
                    None,
                    None,
                    doc,
                    *[_text(f.attributes.get(w)) for w in wide],
                    *[_text(render(a.columns[c], rctx)) for c in row_cols],
                ]
        if values is None or values.empty:
            return
        if a.layout == "wide":
            raise TargetError("layout 'wide' unterstützt keine Kennzahl-Quellen (CSV ohne Geometrie)")
        cols = ["key", "object_key", "name", "value", "period", "extra"]
        needs_extra = bool(row_cols) or a.layout == "jsonb"  # sonst bei INKAR 1,3 Mio. unnötige json.loads
        for key, okey, name, value, period, extra in values[cols].itertuples(index=False, name=None):
            ex = json.loads(extra) if needs_extra and isinstance(extra, str) else {}
            value_t = _text(value)
            period_t = _text(period)
            if row_cols:
                rctx = {
                    **const,
                    "record": {
                        "key": key,
                        "object_key": okey,
                        "name": name,
                        "value": value,
                        "period": period,
                        "extra": ex,
                    },
                }
                extra_vals = [_text(render(a.columns[c], rctx)) for c in row_cols]
            else:
                extra_vals = []
            doc = None
            if a.layout == "jsonb":
                doc = canonical_json({"name": name, "value": value, "period": period, **ex})
            yield [_text(okey), _text(name), value_t, period_t, doc, *([None] * len(wide)), *extra_vals]

    def _insert_attributes(
        self,
        cur: psycopg.Cursor[Any],
        spool: _Spool,
        with_geometries: bool,
        const: dict[str, Any],
        params: dict[str, Any],
        skip_keys: list[str] | None = None,
    ) -> int:
        a, g = self.cfg.tables.attributes, self.cfg.tables.geometries
        if spool.rows == 0:
            return 0
        row_cols = _row_cols(a.columns)
        const_cols = [c for c in a.columns if c not in row_cols]
        wide = list(a.wide_columns)
        tmp_cols = [
            "object_key",
            "name",
            "value",
            "period",
            "doc",
            *[f"w{i}" for i in range(len(wide))],
            *[f"c{i}" for i in range(len(row_cols))],
        ]
        self._copy_into_temp(cur, _TMP_ATTR, tmp_cols, spool)
        if skip_keys:  # Objekte, deren Geometrie in der Datenbank verworfen wurde
            guarded_execute(
                cur, sql.SQL("DELETE FROM {} WHERE object_key = ANY(%s)").format(sql.SQL(_TMP_ATTR)), (skip_keys,)
            )
        names: list[str] = []
        exprs: list[sql.Composable] = []

        def add(col: str | None, expr: sql.Composable) -> None:
            if col:
                names.append(col)
                exprs.append(self._cast(expr, a.name, col))

        add(a.object_key_column, sql.SQL("t.object_key"))
        if a.layout == "key_value":
            add(a.name_column, sql.SQL("t.name"))
            add(a.value_column, sql.SQL("t.value"))
            add(a.period_column, sql.SQL("t.period"))
        elif a.layout == "jsonb":
            add(a.jsonb_column, sql.SQL("t.doc"))
        for i, w in enumerate(wide):
            add(a.wide_columns[w], sql.SQL("t.{}").format(sql.Identifier(f"w{i}")))
        add(a.fk_metadata, sql.Placeholder("meta_id"))
        add(a.source_column, sql.Placeholder("dataset"))
        join: sql.Composable = sql.SQL("")
        if a.fk_geometry and with_geometries:
            names.append(a.fk_geometry)
            exprs.append(sql.SQL("g.{}").format(self._c(g.pk)))
            join = (
                sql.SQL(" LEFT JOIN {} g ON g.{}::text = t.object_key AND g.").format(
                    self._t(g.name), self._c(g.object_key_column)
                )
                + self._scope_geo()
            )
        for i, c in enumerate(row_cols):
            add(c, sql.SQL("t.{}").format(sql.Identifier(f"c{i}")))
        for i, c in enumerate(const_cols):
            params[f"a{i}"] = _text(render(a.columns[c], const))
            names.append(c)
            exprs.append(self._cast(sql.Placeholder(f"a{i}"), a.name, c))
        q = (
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} t").format(
                self._t(a.name), sql.SQL(", ").join(map(self._c, names)), sql.SQL(", ").join(exprs), sql.SQL(_TMP_ATTR)
            )
            + join
        )
        guarded_execute(cur, q, params)
        return cur.rowcount

    def replace(
        self,
        conn: psycopg.Connection[Any],
        ctx: DatasetContext,
        features: list[FeatureRecord],
        values: pd.DataFrame | None,
    ) -> ReplaceStats:
        """Ersetzt die Attribute des Datensatzes und gleicht die Geometrien ab (Transaktion des Aufrufers).

        Die Ladezeilen (bei INKAR ~1,3 Mio.) entstehen vorher in einer temporären Datei: Sperre,
        DELETE und INSERT folgen danach ohne Python-Arbeit dazwischen, Zeilensperren bleiben kurz.
        """
        if not self.types:
            self.load_types(conn)
        stats = ReplaceStats()
        const = self.const_context(ctx)
        t = self.cfg.tables
        if not features and self.cfg.delete_scope.strategy == "by_metadata_source" and not t.attributes.fk_metadata:
            # Kennzahlen ohne eigene Geometrien: Der Löschbereich über fk_geometry erfasst sie nie,
            # jeder Lauf würde den Bestand verdoppeln.
            raise TargetError(
                "Kennzahl-Quelle ohne Geometrien braucht attributes.fk_metadata (oder delete_scope by_source_column)"
            )
        with ExitStack() as stack:
            geo = stack.enter_context(self._spool(conn, self._geometry_rows(features, const))) if features else None
            attr = stack.enter_context(
                self._spool(conn, self._attribute_rows(features, values, const, _row_cols(t.attributes.columns)))
            )
            with conn.cursor() as cur:
                guarded_execute(cur, _DATASET_LOCK, (ctx.dataset,))
                meta_id = self._upsert_metadata(cur, ctx, const)
                stats.metadata_id = meta_id
                self._delete(cur, ctx, meta_id, stats)
                params: dict[str, Any] = {"dataset": ctx.dataset, "meta_id": meta_id}
                if geo is not None:
                    self._upsert_geometries(cur, geo, const, dict(params), stats)
                stats.inserted[t.attributes.name] = self._insert_attributes(
                    cur, attr, geo is not None, const, dict(params), stats.db_skipped_keys
                )
        return stats

    @staticmethod
    def _spool(conn: psycopg.Connection[Any], rows: Iterable[Sequence[Any]]) -> _Spool:
        """Zeilen im COPY-Textformat in eine temporäre Datei schreiben (ohne Datenbankzugriff)."""
        spool = _Spool(tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY))
        try:
            with conn.cursor() as cur, Copy(cur, binary=False, writer=FileWriter(spool.file)) as cp:
                for row in rows:
                    cp.write_row(row)
                    spool.rows += 1
        except BaseException:
            spool.file.close()
            raise
        return spool

    # ------------------------------------------------------------------ Nach dem Import (Antwort 75)
    def refresh_views(self, conn: psycopg.Connection[Any]) -> list[str]:
        """Konfigurierte materialisierte Sichten aktualisieren (je Sicht eine Transaktion); liefert ihre Namen.

        Die DDL-Sperre gibt dafür genau ``REFRESH MATERIALIZED VIEW <schema>.<sicht>`` frei, nichts anderes.
        Ohne ``CONCURRENTLY`` sperrt PostgreSQL die Sicht für Leser, bis sie neu berechnet ist;
        ``lock_timeout``/``statement_timeout`` der Verbindung gelten auch hier.
        """
        allowed = {f"{s}.{v}" for s, v in self.cfg.refresh_views()}
        done = []
        for schema, view in self.cfg.refresh_views():
            q = sql.SQL("REFRESH MATERIALIZED VIEW {}{}.{}").format(
                sql.SQL("CONCURRENTLY ") if self.cfg.after_import.concurrently else sql.SQL(""),
                sql.Identifier(schema),
                sql.Identifier(view),
            )
            with conn.transaction(), conn.cursor() as cur:
                guarded_execute(cur, q, allow_refresh=allowed)
            done.append(f"{schema}.{view}")
        return done

    # ------------------------------------------------------------------ Vergleich mit dem Bestand (Antwort 62)
    def diff_basis(
        self,
        conn: psycopg.Connection[Any],
        ctx: DatasetContext,
        features: list[FeatureRecord],
        values: pd.DataFrame | None,
    ) -> DiffBasis:
        """Schlüssel und Hashes der vorhandenen Zieldaten und der neuen Lieferung in derselben Form.

        Verglichen wird, was im Zielmodell steht: die Spalten mit Vorlagen je Datensatz (``record.…``),
        die Geometrie und die Attributzeilen – genau so, wie ``replace`` sie schreiben würde. Spalten mit
        Vorlagen je Lauf (z. B. ``{run.started_at}``) zählen nicht. ``ProjectionUnavailable``, wenn sich der
        Bestand nicht auf die Lieferung abbilden lässt (dann vergleicht der Aufrufer mit dem letzten Lauf).
        """
        if not self.types:
            self.load_types(conn)
        const = self.const_context(ctx)
        params = {"dataset": ctx.dataset, "meta_id": self.metadata_id(conn, ctx.dataset)}
        if values is not None:
            return self._value_basis(conn, ctx, values, const, params)
        return self._feature_basis(conn, features, const, params)

    def _attr_projection(self) -> list[tuple[int, str]]:
        """(Position in der Attribut-Ladezeile, Zielspalte) aller geschriebenen Attributspalten mit Werten je Zeile."""
        a = self.cfg.tables.attributes
        wide = list(a.wide_columns)
        pos: list[tuple[int, str]] = []
        if a.layout == "key_value":
            pos += [(i, c) for i, c in ((1, a.name_column), (2, a.value_column), (3, a.period_column)) if c]
        elif a.layout == "jsonb" and a.jsonb_column:
            pos.append((4, a.jsonb_column))
        pos += [(5 + i, a.wide_columns[w]) for i, w in enumerate(wide)]
        pos += [(5 + len(wide) + i, c) for i, c in enumerate(_row_cols(a.columns))]
        return pos

    def _existing_attr_rows(
        self, conn: psycopg.Connection[Any], cols: Sequence[sql.Composable], params: dict[str, Any]
    ) -> list[tuple[Any, ...]]:
        """Attributzeilen des Datensatzes im Bestand: erst der Objektschlüssel, dann ``cols`` (als Text)."""
        a, g = self.cfg.tables.attributes, self.cfg.tables.geometries
        try:
            scope = self._scope_attr()
        except TargetError as exc:
            raise ProjectionUnavailable(str(exc)) from exc
        if a.object_key_column:
            okey: sql.Composable = sql.SQL("x.{}::text").format(self._c(a.object_key_column))
        elif a.fk_geometry:
            okey = sql.SQL("(SELECT g.{}::text FROM {} g WHERE g.{} = x.{})").format(
                self._c(g.object_key_column), self._t(g.name), self._c(g.pk), self._c(a.fk_geometry)
            )
        else:
            raise ProjectionUnavailable("attributes braucht object_key_column oder fk_geometry")
        q = (
            sql.SQL("SELECT {}{} FROM (SELECT * FROM {} WHERE ").format(
                okey, sql.SQL("").join(sql.SQL(", ") + c for c in cols), self._t(a.name)
            )
            + scope
            + sql.SQL(") AS x")
        )
        if params["meta_id"] is None and self.cfg.delete_scope.strategy == "by_metadata_source":
            return []
        with conn.cursor() as cur:
            return guarded_execute(cur, q, params).fetchall()

    def _feature_basis(
        self,
        conn: psycopg.Connection[Any],
        features: list[FeatureRecord],
        const: dict[str, Any],
        params: dict[str, Any],
    ) -> DiffBasis:
        g = self.cfg.tables.geometries
        geo_cols = _row_cols(g.columns)
        attr_pos = self._attr_projection()
        # neue Lieferung, aufbereitet wie beim Schreiben
        attr_new: dict[str, list[list[Any]]] = {}
        for row in self._attribute_rows(features, None, const, _row_cols(self.cfg.tables.attributes.columns)):
            attr_new.setdefault(row[0], []).append([row[i] for i, _ in attr_pos])
        new_keys, new_hashes = [], []
        for row in self._geometry_rows(features, const):
            new_keys.append(row[0])
            new_hashes.append(projection_hash(geometry_fingerprint(row[1]), row[2:], attr_new.get(row[0], [])))
        # Bestand
        old_attr: dict[str, list[tuple[Any, ...]]] = {}
        for r in self._existing_attr_rows(
            conn, [sql.SQL("x.{}::text").format(self._c(c)) for _, c in attr_pos], params
        ):
            old_attr.setdefault(r[0], []).append(r[1:])
        q = (
            sql.SQL("SELECT {}::text, encode(ST_AsEWKB({}), 'hex'){} FROM {} WHERE ").format(
                self._c(g.object_key_column),
                self._c(g.geometry_column),
                sql.SQL("").join(sql.SQL(", {}::text").format(self._c(c)) for c in geo_cols),
                self._t(g.name),
            )
            + self._scope_geo()
        )
        if params["meta_id"] is None and self.cfg.delete_scope.strategy == "by_metadata_source":
            geo_rows: list[tuple[Any, ...]] = []
        else:
            with conn.cursor() as cur:
                geo_rows = guarded_execute(cur, q, params).fetchall()
        new_set = set(new_keys)
        old_keys, old_hashes, old_geoms = [], [], {}
        for key, ewkb, *cols in geo_rows:
            rows = old_attr.get(key, [])
            # Geometrie ohne eigene Attribute, die die Lieferung nicht mehr enthält: schon in einem früheren
            # Lauf entfallen und nur erhalten (Antwort 74) – dieser Lauf ändert an ihr nichts
            if key not in new_set and not rows and attr_pos:
                continue
            old_keys.append(key)
            old_hashes.append(projection_hash(geometry_fingerprint(ewkb), cols, rows))
            old_geoms[key] = ewkb
        return DiffBasis(_frame(new_keys, new_hashes), _frame(old_keys, old_hashes), old_geoms)

    def _value_key_columns(self, ctx: DatasetContext) -> list[sql.Composable]:
        """SQL-Ausdrücke, die den fachlichen Schlüssel einer Kennzahl aus den Zielspalten zusammensetzen.

        Die Zuordnung folgt aus ``target.yaml``: Raumbezug → ``object_key_column``, Name/Zeitbezug →
        ``name_column``/``period_column`` (bzw. Felder im jsonb-Dokument), weitere Felder → eine Spalte mit
        der Vorlage ``{record.extra.<feld>}`` oder ein Feld der Spalte mit ``{record.extra}``.
        """
        src = ctx.source
        record = getattr(src, "record", None)
        if record is None:
            raise ProjectionUnavailable("Quelle hat keine record-Zuordnung")
        a = self.cfg.tables.attributes
        roles = {record.object_key: "object_key", record.name: "name", record.value: "value"}
        if record.period:
            roles[record.period] = "period"
        role_cols = {"object_key": a.object_key_column}
        if a.layout == "key_value":
            role_cols |= {"name": a.name_column, "value": a.value_column, "period": a.period_column}
        whole_extra = [c for c, tpl in a.columns.items() if tpl.strip() == "{record.extra}"]
        out: list[sql.Composable] = []
        for f in src.key.fields:
            role = roles.get(f)
            col = role_cols.get(role) if role else None
            if col:
                out.append(sql.SQL("x.{}::text").format(self._c(col)))
                continue
            json_field = role or f
            single = [c for c, tpl in a.columns.items() if tpl.strip() == "{record.extra." + f + "}"]
            if role is None and single:
                out.append(sql.SQL("x.{}::text").format(self._c(single[0])))
            elif a.layout == "jsonb" and a.jsonb_column and role != "object_key":
                out.append(sql.SQL("(x.{}::jsonb ->> {})").format(self._c(a.jsonb_column), sql.Literal(json_field)))
            elif role is None and whole_extra:
                out.append(sql.SQL("(x.{}::jsonb ->> {})").format(self._c(whole_extra[0]), sql.Literal(f)))
            else:
                raise ProjectionUnavailable(f"Schlüsselfeld '{f}' ist im Zielmodell keiner Spalte zugeordnet")
        return out

    def _value_basis(
        self,
        conn: psycopg.Connection[Any],
        ctx: DatasetContext,
        values: pd.DataFrame,
        const: dict[str, Any],
        params: dict[str, Any],
    ) -> DiffBasis:
        attr_pos = self._attr_projection()
        key_cols = self._value_key_columns(ctx)
        rows = self._attribute_rows([], values, const, _row_cols(self.cfg.tables.attributes.columns))
        new_hashes = [projection_hash(b"", [r[i] for i, _ in attr_pos]) for r in rows]
        new = _frame(values["key"].astype("string").tolist(), new_hashes)
        cols = [*key_cols, *(sql.SQL("x.{}::text").format(self._c(c)) for _, c in attr_pos)]
        old_keys, old_hashes = [], []
        n_key = len(key_cols)
        for r in self._existing_attr_rows(conn, cols, params):
            parts = r[1 : 1 + n_key]
            if any(p is None for p in parts):
                continue  # Zeile ohne vollständigen Schlüssel kann keiner Lieferung entsprechen
            old_keys.append(KEY_SEP.join(parts))
            old_hashes.append(projection_hash(b"", r[1 + n_key :]))
        return DiffBasis(new, _frame(old_keys, old_hashes), {})

    def preview_sql(self, conn: psycopg.Connection[Any], ctx: DatasetContext, kind: str) -> list[str]:
        """Anweisungsfolge des Ersetzens als Text (ohne Ausführung)."""
        t = self.cfg.tables
        out = [
            "BEGIN; "
            + as_text(conn, sql.SQL(_DATASET_LOCK.replace("%s", "{}")).format(sql.Literal(ctx.dataset)))
            + ";",
            as_text(
                conn,
                sql.SQL("SELECT {} FROM {} WHERE {} = {} FOR UPDATE; -- danach UPDATE bzw. INSERT").format(
                    self._c(t.metadata.pk),
                    self._t(t.metadata.name),
                    self._c(t.metadata.dataset_column),
                    sql.Literal(ctx.dataset),
                ),
            ),
        ]
        if self.cfg.delete_scope.strategy == "sql":
            out += list(self.cfg.delete_scope.sql or [])
        else:
            out.append(
                as_text(conn, sql.SQL("DELETE FROM {} WHERE ").format(self._t(t.attributes.name)) + self._scope_attr())
            )
        if kind == "features":
            geo = f"{self.cfg.schema_name}.{t.geometries.name}"
            out.append(
                f"COPY {_TMP_GEO} … ; UPDATE {geo} g … FROM {_TMP_GEO} t WHERE g.{t.geometries.object_key_column} = "
                f"t.object_key …; INSERT INTO {geo} … SELECT … FROM {_TMP_GEO} t WHERE NOT EXISTS (…); "
                "-- Geometrien werden nie gelöscht (Bieterantwort 74)"
            )
        out.append(
            f"COPY {_TMP_ATTR} … ; INSERT INTO {self.cfg.schema_name}.{t.attributes.name} … SELECT … FROM {_TMP_ATTR};"
        )
        out.append("COMMIT;")
        return out
