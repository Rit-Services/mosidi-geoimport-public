#!/bin/sh
# Wird vom postgis-Image beim ersten Start der Demo-/Test-Datenbank ausgeführt.
# Legt die Schemas für Demo und Lauf-Historie an und eine nur lesende Rolle für die Demo-Karte.
# Für den Betrieb bei der TH: Rollen-SQL in docs/betriebshandbuch.md (wird von der TH ausgeführt).
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE EXTENSION IF NOT EXISTS postgis;
  CREATE SCHEMA IF NOT EXISTS pipeline_meta;
  CREATE SCHEMA IF NOT EXISTS ref;
EOSQL

if [ -n "${MAP_DB_PASSWORD:-}" ]; then
  # Kennwort per \getenv (psql ≥ 15) statt -v: so steht es nicht in der Prozessliste (ps)
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
       -v map_user="${MAP_DB_USER:-geoimport_map}" <<-'EOSQL'
    \getenv map_pw MAP_DB_PASSWORD
    SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'map_user', :'map_pw')
      WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'map_user') \gexec
    SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'map_user') \gexec
    SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'map_user') \gexec
    SELECT format('GRANT USAGE ON SCHEMA ref, pipeline_meta TO %I', :'map_user') \gexec
    SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA ref, pipeline_meta GRANT SELECT ON TABLES TO %I', :'map_user') \gexec
EOSQL
fi
