-- Migration 2: Besitz der Datensätze (CODE_REVIEW H7).
-- Je Datensatz die Metadatenzeile in den Zieltabellen, die die Pipeline angelegt (oder bewusst mit
-- „run --adopt“ übernommen) hat. Eine bestehende Zeile ohne Eintrag hier gehört der MOSIDI-Anwendung;
-- ein Lauf überschreibt sie nicht. Bestand aus der Zeit vor dieser Migration wird beim ersten Lauf
-- anhand der Lauf-Historie nachgetragen (backfilled, Warnung ownership_backfilled) – hier nicht
-- möglich, weil der Primärschlüssel nur in der Zieldatenbank steht.
CREATE TABLE IF NOT EXISTS {schema}.owned_datasets (
  dataset         text PRIMARY KEY,              -- target.dataset der Quelle (= Wert in dataset_column)
  source_id       text NOT NULL,
  target_schema   text NOT NULL,
  metadata_table  text NOT NULL,
  metadata_pk     text NOT NULL,                 -- Primärschlüssel der Metadatenzeile als Text
  created_run_id  uuid REFERENCES {schema}.import_runs ON DELETE SET NULL,  -- Lauf, der die Zeile angelegt hat
  created_at      timestamptz NOT NULL DEFAULT now(),
  adopted         boolean NOT NULL DEFAULT false, -- fremde Zeile bewusst übernommen (run --adopt)
  adopted_run_id  uuid REFERENCES {schema}.import_runs ON DELETE SET NULL,
  backfilled      boolean NOT NULL DEFAULT false, -- aus der Historie nachgetragen (Bestand vor Migration 2)
  UNIQUE (target_schema, metadata_table, metadata_pk)
);
