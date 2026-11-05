import pytest

from geoimport.target.guard import GuardViolation, check_sql

ALLOWED = [
    'SELECT "id" FROM "ref"."metadaten" WHERE "datensatz" = %s FOR UPDATE',
    'INSERT INTO "ref"."geometrien" ("a") SELECT t.a FROM tmp_geoimport_geo t',
    'UPDATE "ref"."metadaten" SET "titel" = %(m0)s WHERE "id" = %(pk)s',
    'DELETE FROM "ref"."attribute" WHERE "metadaten_id" = %(meta_id)s',
    "CREATE TEMP TABLE tmp_geoimport_geo (object_key text, geom text) ON COMMIT DROP",
    'COPY tmp_geoimport_attr ("object_key", "name") FROM STDIN',
    "SELECT pg_advisory_xact_lock(hashtext(%s))",
    'SELECT "comment", "drop_date" FROM "s"."t"',  # zitierte Bezeichner sind keine Schlüsselwörter
    "SELECT 'DROP TABLE x' AS text",  # Zeichenketten ebenso
]
FORBIDDEN = [
    "DROP TABLE ref.geometrien",
    "TRUNCATE ref.attribute",
    "ALTER TABLE ref.metadaten ADD COLUMN x int",
    "CREATE INDEX x ON ref.geometrien (id)",
    "CREATE TABLE ref.neu (id int)",
    "COMMENT ON TABLE ref.metadaten IS 'x'",
    "GRANT SELECT ON ref.metadaten TO public",
    "REINDEX TABLE ref.geometrien",
    "DELETE FROM ref.attribute; DROP TABLE ref.attribute",
    "WITH x AS (SELECT 1) SELECT * FROM x; ALTER TABLE t RENAME TO u",
    "COPY ref.geometrien FROM STDIN",
    "DO $$ BEGIN DROP TABLE x; END $$",
    "SELECT * INTO ref.kopie FROM ref.geometrien",  # legt eine Tabelle an
    "WITH x AS (SELECT 1) SELECT * INTO neu FROM x",
    "COPY tmp_geoimport_x FROM PROGRAM 'rm -rf /'",
    "COPY tmp_geoimport_x TO '/tmp/out'",
    "SELECT '--' AS a; DROP TABLE ref.geometrien",  # „--“ in einer Zeichenkette ist kein Kommentar
]


@pytest.mark.parametrize("stmt", ALLOWED)
def test_allowed(stmt: str) -> None:
    check_sql(stmt)


@pytest.mark.parametrize("stmt", FORBIDDEN)
def test_forbidden(stmt: str) -> None:
    with pytest.raises(GuardViolation):
        check_sql(stmt)


# ---------------------------------------------------------------------- REFRESH (Bieterantwort 75)
VIEWS = frozenset({"th.source_granularities"})


@pytest.mark.parametrize(
    "stmt",
    [
        "REFRESH MATERIALIZED VIEW th.source_granularities",
        'REFRESH MATERIALIZED VIEW "th"."source_granularities"',
        "refresh materialized view concurrently th.source_granularities;",
        "-- nach dem Import\nREFRESH MATERIALIZED VIEW th.source_granularities",
    ],
)
def test_refresh_of_configured_view_allowed(stmt: str) -> None:
    check_sql(stmt, allow_refresh=VIEWS)


@pytest.mark.parametrize(
    "stmt",
    [
        "REFRESH MATERIALIZED VIEW th.andere_sicht",  # nicht konfiguriert
        "REFRESH MATERIALIZED VIEW source_granularities",  # ohne Schema nicht dieselbe Sicht
        "REFRESH MATERIALIZED VIEW th.source_granularities WITH NO DATA",  # leert die Sicht
        "REFRESH MATERIALIZED VIEW th.source_granularities; DROP TABLE th.x",
        "REFRESH MATERIALIZED VIEW th.source_granularities, th.x",
        "DROP MATERIALIZED VIEW th.source_granularities",
        "ALTER MATERIALIZED VIEW th.source_granularities RENAME TO x",
        "SELECT 1 FROM th.t WHERE x IN (SELECT 1); REFRESH MATERIALIZED VIEW th.source_granularities",
    ],
)
def test_other_refresh_and_ddl_still_blocked(stmt: str) -> None:
    with pytest.raises(GuardViolation):
        check_sql(stmt, allow_refresh=VIEWS)


def test_refresh_blocked_without_configuration() -> None:
    with pytest.raises(GuardViolation, match="REFRESH"):
        check_sql("REFRESH MATERIALIZED VIEW th.source_granularities")
