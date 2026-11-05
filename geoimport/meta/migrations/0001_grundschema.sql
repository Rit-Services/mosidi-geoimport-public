-- Migration 1: Grundschema der Lauf-Historie (eigenes Schema, nie Spalten an Tabellen der TH).
-- Idempotent, damit Installationen aus Versionen ohne Versionstabelle (bis 0.10) per init-meta
-- übernommen werden. {schema} wird beim Ausführen durch den konfigurierten Schemanamen ersetzt;
-- Schema und Erweiterung postgis legt init-meta vorher an, falls sie fehlen.
-- Das ist die „Historisierung von Änderungen“ im Sinne von LB 4.2: je Lauf ist nachvollziehbar,
-- welche Objekte einer Quelle neu, geändert oder entfallen sind. Die Zieltabellen bleiben ohne
-- Versionen (Bieterantwort A6).
CREATE TABLE IF NOT EXISTS {schema}.import_runs (
  run_id        uuid PRIMARY KEY,
  started_at    timestamptz NOT NULL,
  finished_at   timestamptz,
  trigger       text NOT NULL,                 -- schedule | manual | test | demo
  status        text NOT NULL,                 -- running | ok | partial | aborted_incomplete | failed | aborted
  simulated     boolean NOT NULL DEFAULT false,
  dry_run       boolean NOT NULL DEFAULT false,
  app_version   text,
  config_hash   text,
  report        jsonb
);
-- nachträglich ergänzte Spalte (ältere Installationen)
ALTER TABLE {schema}.import_runs ADD COLUMN IF NOT EXISTS dry_run boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS import_runs_started_idx ON {schema}.import_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.import_source_results (
  run_id        uuid REFERENCES {schema}.import_runs ON DELETE CASCADE,
  source_id     text,
  source_type   text,
  dataset       text,
  status        text,                          -- ok | failed | aborted_incomplete | skipped_guard | unchanged | dry_run
  started_at    timestamptz,
  finished_at   timestamptz,
  fetched       int, valid int, invalid int, repaired int,
  added int, changed int, deleted int, unchanged int,
  rows_deleted  bigint, rows_inserted bigint,
  error_kind    text, error_message text,
  warnings      jsonb,
  raw_file      text, raw_sha256 text,
  PRIMARY KEY (run_id, source_id)
);

CREATE TABLE IF NOT EXISTS {schema}.import_errors (
  id            bigserial PRIMARY KEY,
  run_id        uuid REFERENCES {schema}.import_runs ON DELETE CASCADE,
  source_id     text,
  record_key    text,
  reason_code   text,
  reason        text,
  geom          geometry(Geometry, 4326),      -- nur wenn verwertbar (Fehler-Marker)
  raw           jsonb
);
CREATE INDEX IF NOT EXISTS import_errors_run_idx ON {schema}.import_errors (run_id, source_id);

CREATE TABLE IF NOT EXISTS {schema}.diff_keys (
  run_id        uuid REFERENCES {schema}.import_runs ON DELETE CASCADE,
  source_id     text,
  record_key    text,
  hash          text,
  change        text,                          -- added | changed | deleted | unchanged | replaced | invalid
  geom          geometry(Geometry, 4326),      -- nur wenn meta.diff_geoms = true (Änderungskarte)
  PRIMARY KEY (run_id, source_id, record_key)
);
CREATE INDEX IF NOT EXISTS diff_keys_source_idx ON {schema}.diff_keys (source_id, run_id);
