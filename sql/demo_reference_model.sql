-- =====================================================================================
-- NUR DEMO: Referenz-Drei-Tabellen-Modell (Metadaten, Geometrien, Attribute)
--
-- ANNAHME der RIT Services GmbH zur Demonstration – NICHT das Datenmodell der
-- TH Brandenburg. Die TH hat die Struktur ihrer drei Tabellen (LB 3, LB 4.2) noch nicht
-- bereitgestellt. Sobald die DDL vorliegt, wird sie als tests/fixtures/th_schema.sql
-- eingespielt und config/target.yaml darauf abgebildet; dieses Modell wird dann nur
-- noch für die Demo-Karte verwendet.
--
-- Wird ausschließlich in Datenbanken angelegt, deren Name auf "_demo" endet
-- (pipeline init-meta prüft das). Indizes hier sind erlaubt, weil es eine eigene
-- Demo-Datenbank ist – auf den Tabellen der TH legt die Pipeline nie Indizes an.
-- =====================================================================================
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS ref;
COMMENT ON SCHEMA ref IS 'NUR DEMO: Referenzmodell von RIT (Annahme), nicht das Datenmodell der TH Brandenburg';

CREATE TABLE IF NOT EXISTS ref.metadaten (
  id             serial PRIMARY KEY,
  datensatz      text NOT NULL UNIQUE,      -- Kennung der Quelle (= target.dataset)
  titel          text,
  quelle_url     text,
  lizenz         text,
  quellenvermerk text,
  quelltyp       text,
  importiert_am  timestamptz
);

CREATE TABLE IF NOT EXISTS ref.geometrien (
  id                bigserial PRIMARY KEY,
  metadaten_id      integer NOT NULL REFERENCES ref.metadaten (id),
  objekt_schluessel text NOT NULL,          -- fachlicher Schlüssel, z. B. AGS
  name              text,
  geom              geometry(Geometry, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS ref.attribute (
  id                bigserial PRIMARY KEY,
  metadaten_id      integer NOT NULL REFERENCES ref.metadaten (id),
  geometrie_id      bigint REFERENCES ref.geometrien (id),   -- nur bei Attributen derselben Quelle
  objekt_schluessel text NOT NULL,          -- Raumbezug, z. B. AGS (verknüpft CSV-Kennzahlen mit Geometrien)
  name              text NOT NULL,          -- Attribut- bzw. Indikatorname
  wert              text,
  zeitbezug         text,
  zusatz            jsonb                   -- weitere Felder (z. B. Bereich, Indikatorkürzel)
);

CREATE INDEX IF NOT EXISTS geometrien_meta_idx ON ref.geometrien (metadaten_id);
CREATE INDEX IF NOT EXISTS geometrien_key_idx ON ref.geometrien (objekt_schluessel);
CREATE INDEX IF NOT EXISTS geometrien_geog_idx ON ref.geometrien USING gist ((geom::geography));
CREATE INDEX IF NOT EXISTS attribute_meta_idx ON ref.attribute (metadaten_id);
CREATE INDEX IF NOT EXISTS attribute_geom_idx ON ref.attribute (geometrie_id);
CREATE INDEX IF NOT EXISTS attribute_lookup_idx ON ref.attribute (name, zeitbezug, objekt_schluessel);
