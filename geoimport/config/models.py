"""Pydantic-Modelle für alle Konfigurationsdateien.

Die YAML-Dateien in ``config/`` werden beim Laden gegen diese Modelle geprüft.
Fehlermeldungen nennen Datei und Feld. Aus den Modellen wird zusätzlich ein
JSON-Schema für Editoren erzeugt (``pipeline validate-config --schema``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from geoimport.schedule import cron_expression

PLACEHOLDER_RE = re.compile(r"\bTODO(_TH|_RIT)?\b|^TODO")
SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
PLACEHOLDER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,62}$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def contains_placeholder(value: Any) -> bool:
    """Prüft rekursiv, ob irgendwo ein Platzhalter ``TODO…`` steht."""
    if isinstance(value, str):
        return bool(PLACEHOLDER_RE.search(value))
    if isinstance(value, dict):
        return any(contains_placeholder(k) or contains_placeholder(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_placeholder(v) for v in value)
    return False


class StrictModel(BaseModel):
    """Basis: unbekannte Felder sind ein Fehler (Tippfehler fallen sofort auf)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- pipeline.yaml
class HttpSettings(StrictModel):
    timeout_s: float = 60
    retries: int = Field(4, ge=0, le=10)
    retry_on_status: list[int] = [429, 500, 502, 503, 504]
    backoff_base_s: float = Field(2.0, ge=0, description="Wartezeit vor dem 1. Wiederholversuch, danach verdoppelt")
    user_agent: str = "mosidi-geoimport"
    deadline_s: float = Field(
        1800, gt=0, description="Gesamtfrist je Anfrage bzw. Download samt Antwort (timeout_s gilt je Lesevorgang)"
    )
    max_retry_after_s: float = Field(
        300, ge=0, description="Obergrenze für die vom Dienst verlangte Wartezeit (Retry-After)"
    )
    max_response_mb: int = Field(
        512, ge=1, description="Höchstgröße einer Antwort im Speicher (z. B. WFS-Seite), entpackt"
    )
    max_download_mb: int = Field(2048, ge=1, description="Höchstgröße eines Downloads (z. B. CSV/ZIP), entpackt")
    max_decompression_ratio: float | None = Field(
        200, gt=1, description="Höchstes Verhältnis entpackt/übertragen bei Content-Encoding (null: keine Prüfung)"
    )


class StorageSettings(StrictModel):
    raw_dir: Path = Path("/data/raw")
    report_dir: Path = Path("/data/reports")
    state_dir: Path = Path("/data/state")
    retention_days: int = Field(30, ge=1)


class CrsSettings(StrictModel):
    target: Literal["EPSG:4326"] = "EPSG:4326"
    plausibility_bbox_4326: tuple[float, float, float, float] = (5.5, 47.0, 15.5, 55.2)


class MetaSettings(StrictModel):
    location: Literal["schema", "database"] = "schema"
    schema_name: str = Field("pipeline_meta", alias="schema")
    database: str = "geoimport_meta"
    diff_geoms: bool = False
    keep_runs: int = Field(90, ge=1)

    @field_validator("schema_name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not IDENT_RE.fullmatch(v):
            raise ValueError("ungültiger Schemaname")
        return v


class RunSettings(StrictModel):
    fail_exit_code_partial: int = 1
    fail_exit_code_all: int = 2
    max_error_ratio: float = Field(0.05, ge=0, le=1)
    lock_wait_s: int = Field(
        1800, ge=0, description="geplante Läufe (--trigger schedule): so lange auf einen aktiven Lauf warten"
    )


class DatabaseSettings(StrictModel):
    lock_timeout_s: float = Field(60, ge=0, description="Warten auf Tabellensperren (0 = unbegrenzt)")
    statement_timeout_s: float = Field(1800, ge=0, description="Höchstdauer einer SQL-Anweisung (0 = unbegrenzt)")
    keepalives_idle_s: int = Field(60, ge=1, description="TCP-Keepalive: erste Prüfung nach so vielen Sekunden Ruhe")


class ReportSettings(StrictModel):
    notify_webhook_env: str | None = Field(
        None, description="Name einer Umgebungsvariablen mit Webhook-URL für Fehlermeldungen (Standard: aus)"
    )


class PipelineConfig(StrictModel):
    http: HttpSettings = HttpSettings()
    storage: StorageSettings = StorageSettings()
    crs: CrsSettings = CrsSettings()
    meta: MetaSettings = MetaSettings()
    run: RunSettings = RunSettings()
    report: ReportSettings = ReportSettings()
    db: DatabaseSettings = DatabaseSettings()


# --------------------------------------------------------------------------- Quellen (gemeinsam)
class NormalizeSpec(StrictModel):
    zfill: int | None = Field(None, ge=1, le=20, description="mit führenden Nullen auf diese Länge auffüllen")
    strip: bool = True
    upper: bool = False


class KeyConfig(StrictModel):
    fields: list[str] = Field(min_length=1)
    normalize: dict[str, NormalizeSpec] = {}


class Guards(StrictModel):
    min_features: int | None = Field(None, ge=0, description="WFS/Punkt-CSV: Mindestanzahl Objekte")
    min_rows: int | None = Field(None, ge=0, description="CSV: Mindestanzahl Zeilen nach Filter")
    max_drop_ratio: float | None = Field(0.2, ge=0, le=1, description="max. Rückgang ggü. Bestand, sonst kein Ersetzen")
    max_error_ratio: float | None = Field(None, ge=0, le=1)
    geometry_types: list[str] | None = None
    required: list[str] = []
    expected_columns: list[str] | None = None
    strict_count: bool = True


class TargetRef(StrictModel):
    dataset: str
    metadata: dict[str, Any] = {}


class SourceBase(StrictModel):
    id: str
    enabled: bool = True
    title: str
    license: str
    attribution: str | None = None
    schedule: str = Field(
        "monthly",
        description="Zeitplan (Bieterantwort V7): daily | weekly | monthly oder Cron mit 5 Feldern; "
        "mindestens ein Lauf je Monat",
    )
    skip_if_unchanged: bool = True
    business_key: list[str] = Field(
        min_length=1,
        description="Fachlicher Schlüssel (Bieterantwort V4): Feld(er), über die Datensätze verknüpft und "
        "Läufe verglichen werden (neu/geändert/entfernt), z. B. [ags]",
    )
    key_normalize: dict[str, NormalizeSpec] = Field({}, description="Normalisierung je Schlüsselfeld")
    target: TargetRef
    guards: Guards = Guards()

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if PLACEHOLDER_RE.search(v):  # Vorlage (TODO_TH_…): Großbuchstaben erlaubt, sonst dieselbe Regel
            if not PLACEHOLDER_ID_RE.fullmatch(v):
                raise ValueError("id: nur Buchstaben, Ziffern und _ (2–63 Zeichen, Beginn mit Buchstabe)")
            return v
        if not SOURCE_ID_RE.fullmatch(v):
            raise ValueError("id: nur Kleinbuchstaben, Ziffern und _ (2–63 Zeichen, Beginn mit Buchstabe)")
        return v

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, v: str) -> str:
        cron_expression(v)  # ScheduleError ist ein ValueError → verständliche Meldung der Prüfung
        return v

    @property
    def key(self) -> KeyConfig:
        """Schlüssel samt Normalisierung (aus ``business_key`` und ``key_normalize``)."""
        return KeyConfig(fields=self.business_key, normalize=self.key_normalize)

    @model_validator(mode="after")
    def _key_normalize_fields(self) -> SourceBase:
        unknown = sorted(set(self.key_normalize) - set(self.business_key))
        if unknown:
            raise ValueError(f"key_normalize nennt Felder außerhalb von business_key: {', '.join(unknown)}")
        if len(set(self.business_key)) != len(self.business_key):
            raise ValueError("business_key enthält ein Feld mehrfach")
        return self

    @model_validator(mode="after")
    def _placeholders_need_disabled(self) -> SourceBase:
        # Lizenz-/Quellenvermerke dürfen bei aktiven Quellen noch offen sein (nur Warnung),
        # alle technischen Angaben nicht.
        data = self.model_dump(exclude={"license", "attribution", "title"}, by_alias=True)
        if self.enabled and contains_placeholder(data):
            raise ValueError("Quelle enthält Platzhalter (TODO…) und muss deshalb 'enabled: false' haben")
        return self

    def placeholder_warnings(self) -> list[str]:
        """Hinweise auf offene Angaben, die einen Lauf nicht verhindern."""
        out = []
        for name in ("license", "attribution", "title"):
            if contains_placeholder(getattr(self, name)):
                out.append(f"{self.id}: Feld '{name}' ist noch offen ({getattr(self, name)!r})")
        return out


# --------------------------------------------------------------------------- Ausdrücke im Mapping
class FieldExpr(StrictModel):
    """Feldwert, optional normalisiert: ``{field: ags, zfill: 8}``."""

    field: str
    zfill: int | None = None
    to_int: bool = False
    to_float: bool = False
    to_date: str | None = Field(None, description="strptime-Format, z. B. '%d.%m.%Y'")
    decimal: Literal[",", "."] | None = Field(
        None,
        description="Dezimaltrennzeichen für to_int/to_float; ohne Angabe genau ein Punkt oder Komma "
        "(bei CSV-Quellen gilt csv.decimal)",
    )
    thousands: str | None = Field(
        None, description="Tausendertrennzeichen für to_int/to_float (bei CSV: csv.thousands)"
    )


class ConcatExpr(StrictModel):
    concat: list[str | FieldExpr | ConstExpr]
    sep: str = ""


class ConstExpr(StrictModel):
    const: str | int | float | bool | None


Expr = Annotated[str | FieldExpr | ConcatExpr | ConstExpr, Field(union_mode="left_to_right")]
ConcatExpr.model_rebuild()


class Mapping(StrictModel):
    geometry: str | None = Field(None, description="Name der Geometrie-Eigenschaft (nur zur Prüfung des Schemas)")
    name: Expr | None = None
    attributes: dict[str, Expr] = {}


# --------------------------------------------------------------------------- WFS
class WfsPaging(StrictModel):
    strategy: Literal["auto", "startindex", "bbox_tiles", "none"] = "auto"
    page_size: int = Field(1000, ge=1, le=100_000)
    tile_size_m: float = Field(20_000, gt=0)
    max_pages: int = Field(10_000, ge=1)
    throttle_ms: int = Field(200, ge=0)


class WfsSettings(StrictModel):
    url: str
    version: Literal["2.0.0", "1.1.0"] = "2.0.0"
    type_name: str
    request_crs: str | None = Field(None, description="Standard: Default-CRS des FeatureTypes aus GetCapabilities")
    output_format: str = "auto"
    paging: WfsPaging = WfsPaging()
    bbox: tuple[float, float, float, float] | None = Field(None, description="[minx, miny, maxx, maxy] in request_crs")
    filter: str | dict[str, str] | None = Field(None, description="OGC-Filter (XML) oder einfache Gleichheitsfilter")
    sort_by: str | None = Field(
        None,
        description="SORTBY beim seitenweisen Abruf; Standard: erstes business_key-Feld, wenn der Dienst "
        "ImplementsSorting meldet",
    )
    params: dict[str, str] = Field({}, description="zusätzliche Request-Parameter (z. B. API-Schlüssel per ${VAR})")


class WfsSource(SourceBase):
    type: Literal["wfs"]
    wfs: WfsSettings
    mapping: Mapping = Mapping()


# --------------------------------------------------------------------------- CSV
class CsvLocation(StrictModel):
    url: str | None = None
    params: dict[str, str] = Field(
        {}, description="zusätzliche Abfrageparameter (z. B. Token per ${VAR}); werden nicht mit der URL gespeichert"
    )
    url_template: str | None = Field(None, description="URL mit {year}, z. B. …/inkar_{year}.zip")
    year: Literal["latest"] | int | None = None
    search_years_back: int = Field(5, ge=0, le=30)
    path: str | None = None
    fallback_path: str | None = Field(
        None, description="Ersatzdatei, wenn der Download scheitert (z. B. AfS-Export; Bieterantwort V10)"
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> CsvLocation:
        given = [x for x in (self.url, self.url_template, self.path) if x]
        if len(given) != 1:
            raise ValueError("location: genau eines von 'url', 'url_template' oder 'path' angeben")
        if self.url_template and "{year}" not in self.url_template:
            raise ValueError("location.url_template muss {year} enthalten")
        if self.url_template and self.year is None:
            raise ValueError("location.year angeben ('latest' oder Jahreszahl)")
        return self


class CsvArchive(StrictModel):
    member_glob: str


class ColumnSpec(StrictModel):
    to: str
    type: Literal["str", "int", "float", "date"] = "str"
    normalize: NormalizeSpec | None = None
    keep: bool = True
    date_format: str | None = Field(None, description="strptime-Format, Pflicht bei type: date (z. B. '%d.%m.%Y')")

    @model_validator(mode="after")
    def _date_needs_format(self) -> ColumnSpec:
        # Ohne Format rät pandas je Block: 01.02.2024 wäre mal der 1. Februar, mal der 2. Januar
        if self.type == "date" and not self.date_format:
            raise ValueError(
                f"Spalte → '{self.to}': bei type: date ist date_format Pflicht (z. B. '%d.%m.%Y'), "
                "sonst wird das Datumsformat je Block geraten"
            )
        return self


class RegionFilter(StrictModel):
    column: str
    prefix: str | list[str]


class WideToLong(StrictModel):
    id_columns: list[str]
    value_columns: list[str] | None = Field(None, description="Standard: alle übrigen Spalten")
    var_name: str = "period"
    value_name: str = "value"
    indicator: str | None = Field(None, description="fester Indikatorname, falls die Datei nur einen enthält")


class Reshape(StrictModel):
    wide_to_long: WideToLong | None = None


class Coordinates(StrictModel):
    x: str
    y: str
    crs: str


class GeometryJoin(StrictModel):
    source: str
    key: str | None = Field(None, description="nur zur Dokumentation: Schlüsselfeld der Geometrie-Quelle")
    column: str = Field("region_key", description="Zielfeld der CSV mit dem Schlüssel (= record.object_key)")
    on_missing: Literal["warn", "error", "drop"] = "warn"


class ValueRecordSpec(StrictModel):
    """Welche Zielfelder einer CSV-Zeile Bezug, Name, Wert und Zeitbezug sind."""

    object_key: str = "region_key"
    name: str = "indicator"
    value: str = "value"
    period: str | None = "period"


class CsvLimits(StrictModel):
    """Obergrenzen beim Lesen (Schutz vor Archivbomben und Dateien ohne Zeilenumbruch)."""

    max_line_bytes: int = Field(1024 * 1024, ge=1024, description="längste erlaubte Zeile in Bytes")
    max_uncompressed_bytes: int = Field(
        16 * 1024**3, ge=1024 * 1024, description="höchstens so viele Bytes (entpackt) lesen; INKAR 2025: 6,8 GB"
    )
    max_compression_ratio: float | None = Field(
        100,
        gt=1,
        description="ZIP/GZ: entpackt höchstens das N-fache der Archivgröße (mindestens 256 MiB); INKAR 2025: 15,6; "
        "null = aus",
    )


class CsvSettings(StrictModel):
    location: CsvLocation
    archive: CsvArchive | None = None
    encoding: str = "auto"
    delimiter: str = ";"
    decimal: str = ","
    thousands: str | None = None
    quotechar: str = '"'
    header_row: int = 0
    skip_rows: int = 0
    column_names: list[str] | None = Field(
        None, description="eigene Spaltennamen, wenn die Kopfzeilen nicht verwendbar sind (dann ohne Kopfzeile lesen)"
    )
    footer_rows: int = 0
    footer_marker: str | None = Field(
        None, description="Lesen endet vor der ersten Zeile, die so beginnt (z. B. '__________' vor Fußnoten)"
    )
    na_values: list[str] = []
    chunksize: int = Field(100_000, ge=100)
    row_filter: dict[str, str | list[str]] = {}
    region_filter: RegionFilter | None = None
    limits: CsvLimits = CsvLimits()


class CsvSource(SourceBase):
    type: Literal["csv"]
    csv: CsvSettings
    reshape: Reshape = Reshape()
    columns: dict[str, ColumnSpec]
    coordinates: Coordinates | None = Field(None, description="Punktgeometrie aus Koordinatenspalten")
    geometry_join: GeometryJoin | None = None
    record: ValueRecordSpec = ValueRecordSpec()
    mapping: Mapping = Field(Mapping(), description="nur bei coordinates: Name/Attribute der Punktobjekte")


SourceConfig = Annotated[WfsSource | CsvSource, Field(discriminator="type")]


# --------------------------------------------------------------------------- target.yaml
class MetadataTable(StrictModel):
    name: str
    pk: str
    dataset_column: str = Field(description="Spalte mit der Kennung des Datensatzes (= target.dataset der Quelle)")
    columns: dict[str, str] = {}


class GeometriesTable(StrictModel):
    name: str
    pk: str
    fk_metadata: str | None = None
    source_column: str | None = None
    object_key_column: str
    geometry_column: str
    srid: Literal[4326] = 4326
    columns: dict[str, str] = {}


class AttributesTable(StrictModel):
    name: str
    fk_metadata: str | None = None
    fk_geometry: str | None = None
    source_column: str | None = None
    object_key_column: str | None = None
    layout: Literal["key_value", "jsonb", "wide"] = "key_value"
    name_column: str | None = None
    value_column: str | None = None
    period_column: str | None = None
    jsonb_column: str | None = None
    wide_columns: dict[str, str] = Field({}, description="Attributname → Spalte (nur layout: wide)")
    columns: dict[str, str] = {}

    @model_validator(mode="after")
    def _layout_fields(self) -> AttributesTable:
        if self.layout == "key_value" and not (self.name_column and self.value_column):
            raise ValueError("layout key_value braucht name_column und value_column")
        if self.layout == "jsonb" and not self.jsonb_column:
            raise ValueError("layout jsonb braucht jsonb_column")
        if self.layout == "wide" and not self.wide_columns:
            raise ValueError("layout wide braucht wide_columns")
        return self


class TargetTables(StrictModel):
    metadata: MetadataTable
    geometries: GeometriesTable
    attributes: AttributesTable


_DELETE_TABLE_RE = re.compile(r'^\s*DELETE\s+FROM\s+(?:ONLY\s+)?((?:"[^"]+"|\w+)(?:\s*\.\s*(?:"[^"]+"|\w+))?)', re.I)
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def _delete_table(statement: str) -> str | None:
    """Tabelle einer ``DELETE FROM``-Anweisung (``schema.tabelle`` bzw. ``tabelle``, ohne Anführungszeichen)."""
    m = _DELETE_TABLE_RE.match(_SQL_COMMENT_RE.sub(" ", statement))
    if m is None:
        return None
    return "".join(m.group(1).split()).replace('"', "")


class ScopeSql(StrictModel):
    """Bestand eines Datensatzes bei ``strategy: sql`` – genau die Zeilen, die die DELETE-Anweisungen löschen.

    Zählt für die Schutzregeln (Rückgang, Minimum) und liefert die Objektschlüssel ohne Vergleichsbasis.
    """

    features: str = Field(
        description="SELECT mit einer Zeile je Geometrie des Datensatzes; erste Spalte = Objektschlüssel"
    )
    values: str = Field(description="SELECT mit einer Zeile je Attribut-/Kennzahlzeile des Datensatzes")


class DeleteScope(StrictModel):
    strategy: Literal["by_metadata_source", "by_source_column", "sql"] = "by_metadata_source"
    sql: list[str] | None = Field(
        None, description="nur bei strategy=sql: DELETE … WHERE … mit %(dataset)s bzw. %(meta_id)s"
    )
    scope_sql: ScopeSql | None = Field(None, description="nur bei strategy=sql (Pflicht): Bestand für die Schutzregeln")
    geometry_scope: str | None = Field(
        None,
        description=(
            "nur bei strategy=sql: Bedingung auf der Geometrietabelle, die die Geometrien des Datensatzes "
            'findet (z. B. "herkunft = %(dataset)s"); nötig, wenn geometries weder fk_metadata noch '
            "source_column hat. Geometrien werden darüber aktualisiert, nie gelöscht (Bieterantwort 74)."
        ),
    )

    @model_validator(mode="after")
    def _statements(self) -> DeleteScope:
        from geoimport.target.guard import GuardViolation, check_scope_sql

        if self.strategy != "sql":
            if self.sql is not None or self.scope_sql is not None or self.geometry_scope is not None:
                raise ValueError("delete_scope.sql, scope_sql und geometry_scope gibt es nur bei strategy: sql")
            return self
        if not self.sql:
            raise ValueError("delete_scope sql braucht eine Liste von DELETE-Anweisungen")
        if self.scope_sql is None:
            raise ValueError(
                "delete_scope sql braucht scope_sql (features, values): SELECT auf genau den Bereich, den die "
                "DELETE-Anweisungen löschen – sonst zählt die Schutzregel einen anderen Bestand"
            )
        checks = [(f"sql[{i}]", s, "DELETE") for i, s in enumerate(self.sql)]
        checks += [(f"scope_sql.{k}", getattr(self.scope_sql, k), "SELECT") for k in ("features", "values")]
        if self.geometry_scope is not None:
            # als vollständige Anweisung prüfen: eine Bedingung, Parameter %(dataset)s bzw. %(meta_id)s
            checks.append(("geometry_scope", f"SELECT 1 WHERE {self.geometry_scope}", "SELECT"))
        for where, stmt, verb in checks:
            try:
                check_scope_sql(stmt, verb)
            except GuardViolation as e:
                raise ValueError(f"delete_scope.{where}: {e}") from None
        return self


class AfterImport(StrictModel):
    """Schritte nach dem Import (Bieterantwort 75: eigenständiger Betrieb ohne MOSIDI-Funktionen)."""

    refresh_materialized_views: list[str] = Field(
        default_factory=list,
        description=(
            "materialisierte Sichten (name oder schema.name, ohne Schema: schema des Zielmodells), die nach einem "
            "Lauf mit geschriebenen Daten per REFRESH MATERIALIZED VIEW aktualisiert werden (z. B. source_granularities)"
        ),
    )
    concurrently: bool = Field(
        False, description="REFRESH … CONCURRENTLY (braucht einen eindeutigen Index auf der Sicht, sperrt keine Leser)"
    )


class TargetConfig(StrictModel):
    adapter: Literal["table_mapping", "demo_reference"] = "table_mapping"
    schema_name: str = Field(alias="schema")
    tables: TargetTables
    delete_scope: DeleteScope = DeleteScope()
    after_import: AfterImport = AfterImport()

    def refresh_views(self) -> list[tuple[str, str]]:
        """Zu aktualisierende Sichten als (Schema, Name)."""
        out = []
        for name in self.after_import.refresh_materialized_views:
            schema, _, view = name.rpartition(".")
            out.append((schema or self.schema_name, view))
        return out

    @model_validator(mode="after")
    def _scope(self) -> TargetConfig:
        t = self.tables
        if self.delete_scope.strategy == "by_metadata_source":
            if not t.geometries.fk_metadata or not (t.attributes.fk_metadata or t.attributes.fk_geometry):
                raise ValueError("delete_scope by_metadata_source braucht fk_metadata in geometries und attributes")
        if self.delete_scope.strategy == "by_source_column":
            if not (t.geometries.source_column and t.attributes.source_column):
                raise ValueError("delete_scope by_source_column braucht source_column in geometries und attributes")
        if self.delete_scope.strategy == "sql":
            ds = self.delete_scope
            if not (t.geometries.fk_metadata or t.geometries.source_column or ds.geometry_scope):
                raise ValueError(
                    "delete_scope sql braucht fk_metadata oder source_column in geometries oder "
                    "delete_scope.geometry_scope (Geometrien des Datensatzes werden über den Schlüssel aktualisiert)"
                )
            for i, stmt in enumerate(ds.sql or []):
                if _delete_table(stmt) in (t.geometries.name, f"{self.schema_name}.{t.geometries.name}"):
                    raise ValueError(
                        f"delete_scope.sql[{i}] löscht aus {t.geometries.name}: Geometrien werden nicht gelöscht, "
                        "sondern über ihren fachlichen Schlüssel aktualisiert (Bieterantwort 74) – nur Attribute löschen"
                    )
        return self

    def has_placeholders(self) -> bool:
        """True, solange die Abbildung auf das echte Zielmodell noch offen ist."""
        return contains_placeholder(self.model_dump(by_alias=True))

    def identifiers(self) -> list[str]:
        """Alle Tabellen-/Spaltennamen (für die Bezeichnerprüfung)."""
        t = self.tables
        names = [self.schema_name, t.metadata.name, t.metadata.pk, t.metadata.dataset_column, *t.metadata.columns]
        g = t.geometries
        names += [g.name, g.pk, g.object_key_column, g.geometry_column, *g.columns]
        names += [x for x in (g.fk_metadata, g.source_column) if x]
        a = t.attributes
        names += [a.name, *a.columns, *a.wide_columns.values()]
        names += [
            x
            for x in (
                a.fk_metadata,
                a.fk_geometry,
                a.source_column,
                a.object_key_column,
                a.name_column,
                a.value_column,
                a.period_column,
                a.jsonb_column,
            )
            if x
        ]
        names += [part for s, v in self.refresh_views() for part in (s, v)]
        return names
