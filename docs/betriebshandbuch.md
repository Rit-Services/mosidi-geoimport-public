# Betriebshandbuch (Administration)

Zielgruppe: Betreiber*innen der Docker-Umgebung bei der TH Brandenburg. Nach der Übergabe gibt es keine
Betriebsbegleitung durch RIT (Bieterantwort A8); dieses Handbuch soll den Betrieb ohne Rückfragen ermöglichen.

## 1. Voraussetzungen

- Docker Engine ≥ 24 mit Compose v2 (Linux empfohlen; Windows/macOS mit Docker Desktop möglich)
- Zugriff auf PostgreSQL ≥ 14 mit PostGIS ≥ 3.3 (bestehende Datenbank oder Compose-Service `db`)
- Ausgehender HTTPS-Zugriff auf die Quellen (z. B. `isk.geobasis-bb.de`, `www.bbr-server.de`)
- Plattenplatz: ca. 2 GB für Rohdaten (INKAR-ZIP 434 MB je Version, 30 Tage Aufbewahrung) und Berichte

## 2. Installation

```bash
git clone <Repository-URL> mosidi-geoimport && cd mosidi-geoimport
cp .env.example .env            # Werte setzen, siehe Abschnitt 3
docker compose build            # Image mosidi-geoimport:<Version>
```

**Variante A – eigenständig** (eigener PostGIS-Container, z. B. für Tests):

```bash
docker compose up -d --wait db
docker compose run --rm pipeline init-meta
docker compose up -d scheduler
```

**Variante B – Einbindung in das MOSIDI-Compose** (Bieterantwort V8: zusätzlicher Linux-Container, Datenbank
`brandenburg_db`, Image `postgis/postgis`). Die Pipeline bringt keine eigene Datenbank mit, sie nutzt den
bestehenden Service `brandenburg_db`:

1. Dieses Repository neben das MOSIDI-Repository legen, z. B. `…/MOSIDI` und `…/mosidi-geoimport`
   (anderer Ort: Variable `GEOIMPORT_DIR` setzen).
2. In `mosidi-geoimport` die Datei `.env.example` nach `.env` kopieren. Dort stehen die Verbindungswerte:
   `POSTGRES_HOST=brandenburg_db`, `POSTGRES_DB=brandenburg_db`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
   (dieselben Namen wie in der `.env` von MOSIDI, nichts davon steht im Code).
3. In `config/target.yaml` die Tabellen und Spalten des Zielmodells der TH eintragen (Platzhalter `TODO_TH_…`,
   siehe [Datenmodell](datenmodell.md)). Solange Platzhalter enthalten sind, bricht jeder Lauf mit Exit-Code 3 ab.
4. Im MOSIDI-Verzeichnis das Image bauen:
   `docker compose -f docker-compose.yml -f ../mosidi-geoimport/compose.mosidi.yaml build geoimport`
5. Konfiguration und Zielschema prüfen (liest nur):
   `docker compose -f docker-compose.yml -f ../mosidi-geoimport/compose.mosidi.yaml run --rm geoimport validate-config`
   und `… run --rm geoimport check-target`.
6. Lauf-Historie anlegen (eigenes Schema `pipeline_meta`, die Tabellen der TH bleiben unverändert):
   `… run --rm geoimport init-meta`
7. Ersten Lauf als Probe ohne Schreiben: `… run --rm geoimport run --dry-run`, danach `… run --rm geoimport run`.
   Bericht: `/data/reports/latest.json` bzw. `latest.md` im Volume `geoimport_data`.
8. Zeitsteuerung starten: `… up -d geoimport-scheduler`. Der Zeitplan kommt aus dem Feld `schedule` der Quellen;
   Kontrolle mit `… logs -f geoimport-scheduler`.
9. Optional: `compose.mosidi.yaml` dauerhaft in das MOSIDI-Projekt aufnehmen (`COMPOSE_FILE` in dessen `.env`),
   damit die Container mit `docker compose up -d` gemeinsam starten.

Das MOSIDI-Compose verwendet `postgis/postgis` ohne feste Version; für reproduzierbare Builds wird empfohlen,
eine Version festzulegen.

Hinweis: Das MOSIDI-Compose veröffentlicht Port 5432 nach außen. Die Pipeline braucht das nicht; sie verbindet sich
über das interne Docker-Netz. Wir empfehlen, den Port nicht öffentlich zu binden (Änderung liegt beim Betreiber).

## 3. Konfiguration

### Umgebungsvariablen (`.env`)

| Variable | Pflicht | Bedeutung |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` | ja | Zieldatenbank (gleiche Namen wie im MOSIDI-Compose) |
| `POSTGRES_PASSWORD` oder `POSTGRES_PASSWORD_FILE` | ja | kein Standardwert; Start bricht ohne ab |
| `POSTGRES_SSLMODE`, `POSTGRES_SSLROOTCERT` | nein | TLS zur Datenbank. Liegt die Datenbank auf einem anderen Rechner: `verify-full` und die CA-Datei der TH. Ohne Angabe gilt `prefer` (verschlüsselt, wenn möglich, ohne Zertifikatsprüfung) |
| `TARGET_CONFIG` | nein | `target.yaml` (Zielmodell der TH, Standard) oder `target.demo.yaml` |
| `PIPELINE_ENV` | nein | `prod`, `demo`, `test`; Demo-Modus nur mit `demo` |
| `GEOIMPORT_DIFF_GEOMS` | nein | `true` speichert Geometrien geänderter Objekte für die Änderungskarte (nur Demo) |
| `LOG_LEVEL`, `LOG_FORMAT` | nein | `INFO`/`DEBUG`; `json` (Standard) oder `text` |
| `TZ` | nein | Zeitzone für Zeitplan und Berichte (Standard `Europe/Berlin`) |
| `MAP_DB_USER`, `MAP_DB_PASSWORD`, `MAP_PORT` | nur Demo-Karte | nur lesende Rolle |
| `MAP_AUTH_USER`, `MAP_AUTH_PASSWORD`, `MAP_SESSION_SECRET`, `MAP_AUTH_REQUIRED` | nur Demo-Karte | Anmeldung (Kennwort ≥ 12, Secret ≥ 32 Zeichen) |
| `MAP_LOGIN_MAX_ATTEMPTS`, `MAP_LOGIN_LOCK_MINUTES`, `MAP_LOGIN_GLOBAL_MAX` | nein | Sperre je Adresse (5 Fehlversuche → 15 min) und über alle Adressen (30 Fehlversuche je Minute, `0` = aus; solange so viele eintreffen, kann sich niemand neu anmelden, bestehende Sitzungen bleiben) |
| `MAP_COOKIE_SECURE` | nein | Standard `true` (Cookies nur über https); `false` nur für lokales http |

### Dateien (`config/`, als Volume schreibgeschützt eingebunden)

| Datei | Inhalt |
|---|---|
| `pipeline.yaml` | Timeouts, Wiederholversuche, Pfade, Aufbewahrung, Ort der Lauf-Historie, Fehlerquote |
| `target.yaml` | Abbildung auf die drei Zieltabellen ([Datenmodell](datenmodell.md)) |
| `sources/*.yaml` | eine Datei je Quelle ([Neue Quelle anbinden](neue_quelle_anbinden.md)) |

Nach jeder Änderung: `docker compose run --rm pipeline validate-config` und `… check-target`.

## 4. Datenbankrollen und Rechte (von der TH auszuführen)

Die Pipeline braucht **keine** Rechte zum Ändern der Tabellenstruktur. Vorschlag (Platzhalter `<…>` ersetzen):

```sql
CREATE ROLE geoimport_writer LOGIN PASSWORD '<sicheres Passwort>';
GRANT CONNECT ON DATABASE <db> TO geoimport_writer;
GRANT USAGE ON SCHEMA <zielschema> TO geoimport_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON <zielschema>.<metadaten>, <zielschema>.<geometrien>, <zielschema>.<attribute>
  TO geoimport_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA <zielschema> TO geoimport_writer;   -- für serial/identity-Spalten
GRANT TEMPORARY ON DATABASE <db> TO geoimport_writer;                     -- Ladetabellen (ON COMMIT DROP)
-- REFRESH nach dem Import (Antwort 75, target.yaml: after_import): ab PostgreSQL 17 genügt MAINTAIN,
-- davor muss die Rolle Eigentümerin der Sicht sein (bzw. Mitglied der Eigentümerrolle)
GRANT MAINTAIN ON <zielschema>.source_granularities TO geoimport_writer;
-- Lauf-Historie: eigenes Schema, das der Rolle gehört
CREATE SCHEMA pipeline_meta AUTHORIZATION geoimport_writer;

-- optional: nur lesende Rolle für Auswertungen/Demo-Karte
CREATE ROLE geoimport_reader LOGIN PASSWORD '<…>';
ALTER ROLE geoimport_reader SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA <zielschema>, pipeline_meta TO geoimport_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA <zielschema>, pipeline_meta TO geoimport_reader;
```

Soll kein zusätzliches Schema in der TH-Datenbank entstehen: `meta.location: database` in `pipeline.yaml`
und eine eigene Datenbank (Standard `geoimport_meta`) auf demselben Server anlegen, in der `geoimport_writer`
`CREATE` hat.

## 5. Betrieb

| Aufgabe | Befehl |
|---|---|
| Alle aktiven Quellen importieren | `docker compose run --rm pipeline run` |
| Eine Quelle | `docker compose run --rm pipeline run --source geobasis_bb_gemeinden` |
| Probelauf ohne Schreiben | `… run --dry-run` (Bericht mit SQL-Vorschau) |
| Unveränderte Quellen trotzdem neu einlesen | `… run --full` |
| Schutzregel übersteuern | `… run --source <id> --force-replace <id>` (wird protokolliert) |
| Vorhandenen Datensatz bewusst übernehmen | `… run --source <id> --adopt <id>` (einmalig, wird in `pipeline_meta.owned_datasets` vermerkt) |
| Quelle prüfen ohne Import | `… probe --source <id>` |
| Quellen und letzter Status | `… sources` |
| Letzter Bericht | `… report` bzw. `… report --format json` |
| Scheduler starten/stoppen | `docker compose up -d scheduler` / `docker compose stop scheduler` |
| Logs | `docker compose logs -f scheduler` (JSON-Zeilen mit `run_id`, `source_id`, `event`) |

**Exit-Codes** (`geoimport/exitcodes.py`), geeignet für Monitoring (z. B. Cron-Mail, Node-RED):

| Code | Bedeutung |
|---|---|
| 0 | alles ok |
| 1 | mindestens eine Quelle fehlgeschlagen/nicht ersetzt |
| 2 | alle Quellen fehlgeschlagen |
| 3 | Konfiguration oder Zielschema |
| 4 | anderer Lauf aktiv (Lauf-Sperre) |
| 5 | Datenbank nicht erreichbar oder Verbindung im Lauf verloren |
| 6 | `pipeline_meta` fehlt oder hat eine andere Version → `pipeline init-meta` ausführen |
| 7 | interner Fehler |
| 130 / 143 | abgebrochen (Strg-C / SIGTERM) |

Bei jedem Abbruch nach Laufbeginn – auch Exit 5–7 und SIGTERM – wird der Lauf in `pipeline_meta` als `failed`
abgeschlossen, ein Bericht geschrieben und der Webhook ausgelöst. Läufe, die beim Start noch auf `running`
stehen (z. B. nach einem Absturz), setzt der nächste Lauf auf `aborted` (Warnung `stale_runs`).

**Zeitlimits** (`pipeline.yaml`, Abschnitt `db`): `lock_timeout_s` (Standard 60), `statement_timeout_s`
(Standard 1800) und TCP-Keepalives. Ein von einer anderen Anwendung gesperrter Datensatz lässt den Lauf damit
nicht mehr unbegrenzt hängen.

**Grenzen beim Abruf** (`pipeline.yaml`, Abschnitt `http`; Standardwerte gelten auch ohne Eintrag):

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `deadline_s` | 1800 | Gesamtfrist je Anfrage bzw. Download (`timeout_s` gilt je Lesevorgang) |
| `max_retry_after_s` | 300 | längste vom Dienst per `Retry-After` verlangte Wartezeit |
| `max_response_mb` | 512 | größte Antwort im Speicher (z. B. eine WFS-Seite), entpackt |
| `max_download_mb` | 2048 | größter Download (CSV/ZIP), entpackt |
| `max_decompression_ratio` | 200 | Verhältnis entpackt/übertragen bei `Content-Encoding`; `null` = aus |

TLS-Fehler und unbrauchbare URLs werden nicht wiederholt (`error.kind` `tls` bzw. `config`). Für CSV-Dateien
gelten zusätzlich `csv.limits` je Quelle ([Neue Quelle anbinden](neue_quelle_anbinden.md)). Eine überschrittene
Grenze bricht den Abruf der Quelle ab (`aborted_incomplete`); der Bestand bleibt unverändert.

**Demo-Karte:** `/livez` (Prozess lebt) und `/readyz` (Datenbank erreichbar) ohne Anmeldung, für Liveness- und
Readiness-Probe; `/api/health` verhält sich wie `/readyz` und nennt keine Version mehr. Abmelden nur per
`POST /logout` mit Kopfzeile `X-CSRF-Token` (Wert `csrf` aus `/api/info`); die API-Dokumentation (`/docs`,
`/openapi.json`) ist abgeschaltet. Höchstens 5000 Objekte je Antwort, `simplify` mindestens 0,0001.

### Zeitplan ändern

Der Zeitplan steht je Quelle im Feld `schedule` (`config/sources/*.yaml`): `daily`, `weekly`, `monthly` oder ein
Cron-Ausdruck mit fünf Feldern (Zeitzone `TZ`). Der Scheduler erzeugt beim Start daraus die Crontab
(`pipeline crontab`); Quellen mit gleichem Zeitplan laufen gemeinsam. Nach einer Änderung:
`docker compose run --rm pipeline validate-config`, dann `docker compose restart scheduler`.
Vorschau: `docker compose run --rm pipeline crontab`.

Bieterantwort V7 verlangt Intervalle „konfigurierbar, höchstens monatlich“. **Auslegung:** Zwischen zwei Läufen
liegt höchstens ein Monat, jede Quelle läuft also mindestens einmal im Monat. Die Prüfung lehnt seltenere
Zeitpläne ab (z. B. `0 4 1 1 *` = jährlich oder `0 4 31 * *` = nicht in jedem Monat).
Parallele Läufe verhindert eine Datenbanksperre (Exit 4). Ein zeitgesteuerter Lauf (`--trigger schedule`)
wartet bis zu `run.lock_wait_s` (Standard 1800 s) auf die Sperre; gibt er auf, löst er den Webhook mit Status
`locked` aus, damit kein Monatslauf unbemerkt ausfällt.
Alternative ohne Scheduler-Container: Host-Cron oder systemd-Timer mit `docker compose run --rm pipeline run`.

### Berichte und Aufbewahrung

Berichte liegen im Volume `data` unter `/data/reports/<Jahr>/` und in `pipeline_meta.import_runs.report`.
Rohdaten: `/data/raw/<quelle>/<lauf>/`, gelöscht nach `storage.retention_days` (Standard 30), der jüngste Stand je
Quelle bleibt. Lauf-Details (Fehler, Diff-Schlüssel) älter als `meta.keep_runs` Läufe werden gelöscht, Lauf-Köpfe
bleiben; die Vergleichsbasis jeder Quelle bleibt immer erhalten.

### Datensicherung

Die Zieltabellen sichert der bestehende Datenbank-Backup der TH. Zusätzlich sinnvoll: `pipeline_meta` (im selben
Backup) und das Volume `data` (Berichte; Rohdaten sind wiederbeschaffbar).

### Aktualisierung auf eine neue Version

```bash
git pull && docker compose build && docker compose run --rm pipeline validate-config
docker compose run --rm pipeline init-meta     # spielt fehlende Migrationen in pipeline_meta ein (idempotent)
docker compose up -d scheduler
```
CHANGELOG.md beachten. **`init-meta` ist nach jeder Aktualisierung Pflicht:** Das Schema von `pipeline_meta` ist
versioniert (`pipeline_meta.schema_version`, Migrationen unter `geoimport/meta/migrations/`). Ein normaler Lauf
führt kein DDL mehr aus, sondern prüft nur die Version und bricht bei Abweichung mit Exit 6 ab. Damit genügt
der Schreibrolle aus Abschnitt 4 DML; `init-meta` braucht `CREATE` auf dem Schema (bzw. ist bei aktuellem
Stand ein No-op). Eine Installation der Version 0.10 wird mit einem einzigen `init-meta` übernommen.

## 6. Störungen

| Symptom | Ursache | Maßnahme |
|---|---|---|
| Exit 3, „target.yaml enthält Platzhalter“ | Zielmodell noch nicht eingetragen | `config/target.yaml` ausfüllen, `check-target` |
| Exit 3, „Spalte … fehlt“ | Zielmodell geändert oder Tippfehler | `check-target` zeigt alle Abweichungen; `target.yaml` anpassen |
| Exit 4, „Lauf-Sperre belegt“ | anderer Lauf läuft noch | warten; `SELECT * FROM pg_locks WHERE locktype='advisory'` |
| Quelle `failed`, `http`/`timeout` | Dienst nicht erreichbar | später erneut; Bestand bleibt unverändert; `probe --source <id>` |
| Quelle `failed`, `ogc_exception` | Dienst meldet Fehler (z. B. FeatureType umbenannt) | Meldung im Bericht; `probe`, Konfiguration anpassen |
| Quelle `failed`, `schema_mismatch` | gemapptes Feld/Spalte fehlt in der Quelle | Mapping anpassen (`neue_quelle_anbinden.md`) |
| Quelle `failed`, `incomplete`/`possibly_truncated` | Dienst liefert nicht alle Objekte | `paging.strategy: bbox_tiles` bzw. `startindex` |
| Quelle `failed`, `too_many_errors` | > 5 % fehlerhafte Datensätze | `pipeline_meta.import_errors` ansehen; Quelle oder Mapping prüfen |
| Quelle `skipped_guard` | weniger Daten als erwartet / starker Rückgang | Quelle prüfen; bewusst ersetzen mit `--force-replace <id>` |
| Warnung `schema_drift` | Quelle hat Felder/Spalten geändert | prüfen, ob Mapping angepasst werden muss |
| Warnung `unmatched_keys` | Kennzahlen ohne passende Gemeinde (z. B. Gebietsreform) | Gebietsstand von CSV und WFS abgleichen |
| Exit 5, `Datenbank nicht erreichbar` | Host/Port/Passwort, Netzwerk, Datenbank-Neustart | `.env`, Netzwerk, `check-target --quick` |
| Exit 6, „pipeline_meta … Version“ | neue Version ohne `init-meta` | `docker compose run --rm pipeline init-meta` |
| Exit 7, interner Fehler | Programmfehler | Log (`event=run_aborted`) und Bericht an den Hersteller |
| Quelle `failed`, `ownership` | Metadatenzeile mit gleichem `target.dataset` gehört nicht der Pipeline (z. B. von MOSIDI gepflegt) | Zeile prüfen; anderen `target.dataset` wählen oder bewusst `run --adopt <id>` |
| Quelle `failed`, `insecure_redirect` | Dienst leitet von https auf http um | Dienstbetreiber informieren; URL prüfen |
| Quelle `failed`, `possibly_truncated` „Anzahl nicht prüfbar“ | Dienst beantwortet `RESULTTYPE=hits` nicht | wenn bekannt und akzeptiert: `guards.strict_count: false` (Warnung `count_unverified`) |
| Warnung `unsorted_paging` | Dienst kann nicht sortieren; Blättern ohne stabile Reihenfolge | Vollständigkeit prüfen; ggf. `wfs.sort_by` setzen |
| Warnung `invalid_existing` | bisherige Datensätze sind in der neuen Lieferung ungültig | `pipeline_meta.import_errors`; sie zählen nicht als „gelöscht“ |
| Quelle `aborted_incomplete`, `too_large` | Antwort/Download größer als `http.max_response_mb` bzw. `max_download_mb` oder auffällig stark komprimiert | Quelle prüfen; Grenze in `pipeline.yaml` bewusst erhöhen |
| Quelle `aborted_incomplete`, „mehr als … Bytes entpackt“ | CSV-Archiv größer als `csv.limits` erlaubt (Archivbombe oder Grenze zu niedrig) | Datei prüfen; `csv.limits.max_uncompressed_bytes` bzw. `max_compression_ratio` der Quelle erhöhen |
| Quelle `aborted_incomplete`, `parse` „Zeile länger als …“ | Datei ohne passende Zeilenumbrüche oder keine CSV | Datei prüfen; ggf. `csv.limits.max_line_bytes` |
| Quelle `failed`, `tls` | Zertifikat des Dienstes ungültig | Dienstbetreiber informieren; wird nicht wiederholt |
| Quelle `aborted_incomplete`, `paging` „… Kacheln > paging.max_pages“ | `bbox_tiles` würde zu viele Kacheln anfragen | `tile_size_m` vergrößern, `wfs.bbox` einschränken oder `max_pages` erhöhen |
| Warnung `encoding` | Kodierung automatisch als cp1252/latin-1 erkannt | Kodierung prüfen und `encoding:` fest eintragen |

### Quelle nach Fehlimport wiederherstellen

Die Rohdaten jedes Laufs liegen unter `/data/raw/<quelle>/`. Den letzten guten Stand erneut einspielen:
`docker compose run --rm pipeline run --source <id> --full` (lädt neu) oder für einen bestimmten Stand die Rohdatei
als `csv.location.path` bzw. per Demo-Reset in einer Testumgebung einspielen. Datenbank-Backup bleibt der
übergeordnete Weg.

## 7. FAQ

**Verändert die Pipeline unsere Tabellen?** Nein. Erlaubt sind nur Datenoperationen; jede andere Anweisung blockiert
die eingebaute DDL-Sperre. Der Test vergleicht den Schema-Schnappschuss vor und nach einem Lauf.

**Was passiert, wenn ein Dienst während des Imports ausfällt?** Die Transaktion der Quelle wird zurückgerollt, ihre
bisherigen Daten bleiben; andere Quellen laufen weiter. Der Bericht nennt Ursache und URL.

**Werden alte Stände aufbewahrt?** In den Zieltabellen nicht (A6). Welche Objekte je Lauf neu, geändert oder
entfallen sind, steht in `pipeline_meta`.
