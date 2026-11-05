# Änderungen

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach SemVer.

## [Unveröffentlicht]

Anpassungen nach den Antworten 59–77 vom 04.11.2026. **Geändertes Verhalten: Geometrien werden nie mehr
gelöscht.** Eigene `delete_scope.sql`-Konfigurationen mit einem `DELETE` auf die Geometrietabelle werden abgelehnt;
ohne `fk_metadata`/`source_column` an den Geometrien ist `delete_scope.geometry_scope` nötig.

### Geändert
- Lizenzübersicht `docs/drittkomponenten.md` (Antwort 70, LB 7): jede Komponente mit Version und Lizenz, auch
  die in den Wheels eingebetteten GDAL 3.12.4, PROJ 9.8.1 und GEOS 3.14.1; PostgreSQL und PostGIS
  (GPL-2.0-or-later) als eigenständige, nicht eingebundene Dienste ausgewiesen; „Vorschlag“ und
  „prüfen“-Markierungen entfernt (Apache-2.0 gilt).
- Beim Ersetzen einer Quelle werden nur ihre Attribute gelöscht und neu geschrieben (Antwort 74). Geometrien
  werden über den fachlichen Schlüssel aktualisiert (nur bei Änderung) bzw. neu angelegt; ihre Primärschlüssel
  bleiben stabil, Verweise anderer Daten bleiben gültig. Gebiete, die in der Lieferung fehlen, bleiben erhalten
  und erscheinen als Hinweis `geometry_retained`. Bericht: neues Feld `rows_updated`, `info.rows_updated_by_table`.
- Doppelte Schlüssel in den Geometrien eines Datensatzes führen zum Abbruch der Quelle statt zu einer geratenen
  Zuordnung.
- `demo --reset` entfernt die simulierten Zusatzobjekte gezielt (nur in der Demo-Datenbank).
- Fehlergrund `missing_required` heißt jetzt `missing_required_field` (Antwort 63).
- Kennzahl-Quellen: Raumbezug, Wert und (falls vorhanden) Zeitbezug sind immer Pflicht, auch ohne Eintrag in
  `guards.required` (Antwort 63). Zeilen ohne Wert (z. B. „-“ bei aufgelösten Gemeinden) werden übersprungen und
  als `missing_required_field` gezählt, statt mit leerem Wert gespeichert zu werden. INKAR: `period` und `value` in
  `guards.required` ergänzt. Ein leerer Schlüssel zählt ebenfalls als `missing_required_field`.

### Neu
- Test für WFS 1.1.0 (Antwort 67): GetFeature mit `MAXFEATURES`/`STARTINDEX`, `numberOfFeatures`, GML 3.1.1,
  Obergrenze des Dienstes ohne Paging wird als unvollständig erkannt; Abruf sequentiell (Antwort 69).
- Eigenständiger Betrieb ohne MOSIDI-Funktionen (Antworten 74, 75): `target.yaml: after_import` aktualisiert nach
  einem Lauf mit geschriebenen Daten materialisierte Sichten (`source_granularities`); die DDL-Sperre gibt dafür nur
  `REFRESH MATERIALIZED VIEW <konfigurierte Sicht>` frei. Fehler → Warnung `refresh_failed`, Bericht
  `run.refreshed_views`. `target.yaml` bereitet `table_metadata` und `dct_catalog_publisher` (aus
  `target.metadata.publisher`) vor; die übrigen Namen bleiben bis zum Schema-Dump Platzhalter.
  **Rechte:** Die Rolle braucht `MAINTAIN` auf der Sicht (PostgreSQL 17) bzw. muss Eigentümerin sein.
- Diff gegen den vorhandenen Datenbestand (Antwort 62): Neu, geändert und entfallen werden gegen die aktuellen
  Zieldaten ermittelt, auch beim ersten Lauf gegen einen befüllten Bestand. Bei Kennzahlen wird der fachliche
  Schlüssel aus den Zielspalten zusammengesetzt. `pipeline_meta.diff_keys` ist nur noch Protokoll bzw. Rückfall
  (Warnung `diff_from_baseline`). Neue Warnungen `diff_keys_mismatch`, `duplicate_existing_keys`.
- Ungültige Geometrien in der Ladetabelle brechen die Quelle nicht mehr ab: `ST_MakeValid` mit Erhalt der
  Geometriefamilie (`geometry_repaired`); leer oder nicht reparierbar → Datensatz samt Attributen übersprungen,
  in `import_errors` und als `geometry_skipped` gemeldet, nicht als „neu“ gezählt (Antwort 63).
- Z- und M-Werte werden immer entfernt, gespeichert wird nur 2D (Antwort 63).
- Warnungscodes `geometry_repaired`, `geometry_skipped`, `missing_required_field` mit Zählern je Quelle
  (`sources[].quality`, Antwort 63), im Markdown-Bericht als Abschnitt „Datenqualität“.

### Behoben
- CSV: Ungültige Zeilen über die 10.000 einzeln gespeicherten hinaus (fehlender Schlüssel, Pflichtfeld,
  Dublette) wurden nicht mitgezählt; `invalid` war dann zu niedrig.

Behebungen aus Code- und Sicherheitsprüfung vom 03.11.2026 (Schritte 1–6). **Nach dem Update ist
`pipeline init-meta` Pflicht** (versioniertes Schema von `pipeline_meta`, sonst Exit 6).

### Sicherheit
- GDAL-Treiber `OGR_VRT`, `GPSBabel`, `LIBKML`, `KML`, `WFS`, `OAPIF` gesperrt (`GDAL_SKIP`, im Paket und im
  Dockerfile); jede WFS-Seite muss eine `FeatureCollection` sein. Verhindert, dass ein manipulierter Dienst über
  eine getarnte VRT-Datei lokale Dateien liest oder interne Adressen abruft.
- Umleitungen von https auf http werden abgelehnt (`error.kind` `insecure_redirect`).
- Text aus Quellen wird in Berichten, Meldungen und `pipeline_meta` von Steuerzeichen (inkl. NUL) bereinigt und
  gekürzt; der Markdown-Bericht maskiert Markdown/HTML.
- CI/Deploy: Demonstrator-Workflow nur nach Push auf `main` dieses Repositorys (nicht für Fork-PRs), Token nicht
  im Checkout gespeichert, `concurrency`; GitHub Actions per Commit-SHA, gitleaks per Digest; Dependabot.
- Zugangsdaten werden in Logs (auch in strukturierten Daten), Berichten und `pipeline_meta` zuverlässiger maskiert:
  Anmeldedaten in URLs jedes Schemas (auch `postgresql://`, `@` im Kennwort), geheime Parameter wie
  `client_secret`, `X-Amz-Signature`, `sig`, `authkey`, `Authorization`-Kopfzeilen sowie die Werte von DB-Kennwort,
  geheimen `${VAR}` und geheimen `params`. Gespeicherte CSV-URLs enthalten keine Token mehr.
- CSV: `csv.location.params` für Token (wie `wfs.params`), werden nicht mit der URL gespeichert.
- Datenbank-TLS einstellbar: `POSTGRES_SSLMODE` (z. B. `verify-full`), `POSTGRES_SSLROOTCERT`.
- Quell-IDs von Vorlagen (`TODO…`) unterliegen derselben Zeichenregel (kein `/` oder `..`).
- Demo-Datenbank: Kennwort der Karten-Rolle steht nicht mehr in der Prozessliste (`\getenv` statt `psql -v`).
- ArgoCD: eigenes AppProject `geoimport` (nur dieses Repository, nur Namespace `geoimport`).
- Kubernetes: NetworkPolicies (Standard: alles gesperrt), `automountServiceAccountToken: false`,
  `seccompProfile: RuntimeDefault`, Datenbankpasswort als Datei (`POSTGRES_PASSWORD_FILE`).

- Ressourcengrenzen beim Abruf (`pipeline.yaml`, `http`): `deadline_s`, `max_response_mb`, `max_download_mb`,
  `max_decompression_ratio` (entpackte Bytes, Schutz vor Kompressionsbomben), `max_retry_after_s`;
  `error.kind` `too_large`; verschachtelte `Content-Encoding` (z. B. `gzip, gzip`) wird abgelehnt. TLS-Fehler und
  unbrauchbare URLs werden nicht wiederholt (`tls`, `config`).
- CSV: `csv.limits` (`max_line_bytes`, `max_uncompressed_bytes`, `max_compression_ratio`); `footer_rows` wird
  beim Lesen übersprungen, ohne die Datei vollständig in den Speicher zu laden; `footer_rows` und
  `footer_marker` erkennen auch Zeilenende nur CR.
- WFS `bbox_tiles`: Kachelzahl wird vor der ersten Anfrage gegen `paging.max_pages` geprüft, der Rahmen auf die
  Plausibilitäts-BBOX zugeschnitten.
- Demo-Karte: CSRF-Schutz für Anmelden und Abmelden (`POST /logout` mit `X-CSRF-Token`), globale Sperre nach
  `MAP_LOGIN_GLOBAL_MAX` Fehlversuchen je Minute (auch bei vielen gleichzeitigen Anfragen), Cookies standardmäßig `Secure` (`MAP_COOKIE_SECURE`),
  API-Dokumentation abgeschaltet, `/api/health` ohne Versionsangabe, Untergrenze für `simplify`.

### Behoben
- WFS: Blättern endet nicht mehr bei einer kurzen Seite (stilles Abschneiden); Abbruch erst bei leerer Seite bzw.
  erreichter Trefferzahl eindeutiger Objekte; `SORTBY` nach dem fachlichen Schlüssel, wenn der Dienst sortieren
  kann (`wfs.sort_by`); eine Objekt-ID, die auf einer späteren Seite erneut kommt, gilt als unvollständiger
  Abruf (doppelte fachliche Schlüssel mit verschiedenen IDs bleiben Datensatzfehler); GeoJSON wird auf 2D
  reduziert; Kachelmodus prüft
  die Gesamtzahl. Ohne prüfbare Trefferzahl bricht der Abruf ab (`guards.strict_count: false` → Warnung
  `count_unverified`).
- WFS: `auto` bevorzugt GML; GeoJSON wird ohne Typraten gelesen (keine `"nan"`- oder `".0"`-Schlüssel mehr).
  Abfrageparameter in `wfs.url` bleiben erhalten.
- CSV: Kodierungserkennung nur noch UTF-8 → cp1252 (→ latin-1) mit Warnung `encoding`; bei `decimal: ","` ist ein
  Punkt nur als Tausendertrenner erlaubt; `to_int` rundet nicht mehr ab; `type: date` verlangt `date_format`;
  `__MACOSX`-Einträge in ZIP-Dateien werden ignoriert.
- `delete_scope.strategy: sql` wird geprüft (nur `DELETE … WHERE` mit `%(dataset)s`/`%(meta_id)s`) und verlangt
  `scope_sql` für Zählung und Vergleich. **Bestehende Konfigurationen mit `strategy: sql` müssen ergänzt werden.**
- Die Pipeline ersetzt nur Metadatenzeilen, die ihr gehören (`pipeline_meta.owned_datasets`, Migration 0002);
  fremde Zeilen → `error.kind` `ownership`; Übernahme mit `run --adopt <id>`; Altbestand wird automatisch
  übernommen (`ownership_backfilled`).
- Läufe führen kein DDL mehr aus; Migrationen nur mit `init-meta` (`pipeline_meta.schema_version`).
- Abbrüche (Datenbankausfall, interner Fehler, SIGTERM) schließen den Lauf immer ab, schreiben einen Bericht und
  lösen den Webhook aus; hängengebliebene `running`-Läufe werden `aborted`.
- Ergebnis und Diff-Schlüssel werden in derselben Transaktion wie der Bestand geschrieben (Meta im selben
  Schema); nach dem Commit wird eine Quelle nie mehr als `failed` gemeldet.
- Zählwerte im Bericht nur noch für tatsächlich geschriebene Quellen; ungültige bisherige Datensätze zählen
  nicht als gelöscht (`invalid_existing`).
- Sperrschlüssel: Lauf- und Datensatzsperre in getrennten Schlüsselräumen (ein Datensatz `run` blockierte sich
  selbst).
- Konfigurationsänderungen an einer Quelle heben „unverändert überspringen“ einmalig auf.
- Zeilen für das Einfügen werden vor dem Löschen aufgebaut (kürzere Sperren).

### Neu
- Exit-Codes 5 (Datenbank nicht erreichbar), 6 (`pipeline_meta` veraltet), 7 (interner Fehler), 130/143.
- `pipeline.yaml`: `db.lock_timeout_s`, `db.statement_timeout_s`, `db.keepalives_idle_s`, `run.lock_wait_s`
  (zeitgesteuerte Läufe warten auf die Sperre und melden `locked` per Webhook).
- Demo-Karte: `/livez` und `/readyz` (Kubernetes-Probes zeigen darauf).

### Geändert
- `geobasis_bb_gemeinden` und die WFS-Vorlagen legen `paging.strategy` fest statt `auto`.

## [0.10.0] – 2026-11-02

Abgleich mit der Beantwortung der Bieterfragen vom 02.11.2026.

### Neu
- Fachlicher Schlüssel je Quelle als Pflichtfeld `business_key` (+ `key_normalize`); Prüfung, dass die Felder im
  Datensatz vorkommen; Vergleich der Läufe ausschließlich über diesen Schlüssel.
- Status `aborted_incomplete` (Lauf und Quelle): Abruf unvollständig oder fehlerhaft, nichts gelöscht.
  Erkennung abgeschnittener ZIP- und CSV-Dateien; eine leere Lieferung ersetzt nie einen Bestand.
- Zeitplan je Quelle (`schedule`: `daily`, `weekly`, `monthly`, Cron), mindestens monatlich; Crontab wird beim
  Start des Schedulers erzeugt (`pipeline crontab`), `config/crontab` entfällt.
- Quelle `afs_bb` (Bevölkerung der Gemeinden, CSV per URL mit Ersatzdatei); CSV-Optionen `column_names`,
  `footer_marker`, `location.fallback_path`.
- `compose.mosidi.yaml`: Datenbank `brandenburg_db`, Verbindungswerte aus Umgebungsvariablen.
- Abgleich mit den Vorarbeiten `inkar_import` (`docs/abgleich_mosidi_inkar_import.md`).

### Geändert
- `schedule_group` und `run --group` entfallen (ersetzt durch `schedule`); `key: {fields, normalize}` heißt jetzt
  `business_key` / `key_normalize`.
- Lizenz Apache-2.0 bestätigt.
- Robustheit: Ergebnis und Diff-Schlüssel atomar, DDL-Sperre strenger, Kodierungs-Rückfall bei großen CSV-Dateien,
  WFS `featureMembers` und BBOX-Achsenreihenfolge, Härtung der Demo-Karte; mypy im strikten Modus.

## [0.9.0-demonstrator] – 2026-10-29

Demonstrator zur Angebotsabgabe.

### Neu
- WFS-Quellen (2.0.0/1.1.0): Capabilities, DescribeFeatureType mit Schema-Vergleich, Paging-Strategien
  `none`, `startindex`, `bbox_tiles`, `auto`; Erkennung von OGC-ExceptionReports auch bei HTTP 200;
  Vollständigkeitsprüfung gegen `RESULTTYPE=hits`.
- CSV-Quellen: lokale Datei oder URL, Jahressuche `year: latest`, ZIP (auch Deflate64) und GZ,
  Kodierungserkennung, Dezimal-/Tausendertrennzeichen, Kopf-/Fußzeilen, NA-Werte, Zeilen- und Regionsfilter,
  wide→long, Punktgeometrien aus Koordinatenspalten, Verknüpfung mit Geometrien über amtliche Schlüssel.
- Ziel-Adapter `table_mapping` (vollständig aus `target.yaml`) und `demo_reference`; DDL-Sperre;
  Löschen und Ersetzen je Quelle in einer Transaktion; Schutzregeln gegen Leer- und Teilimporte.
- Lauf-Historie in `pipeline_meta` (Läufe, Ergebnisse je Quelle, fehlerhafte Datensätze, Diff-Schlüssel).
- Import-Bericht JSON (Schema 1.0) und Markdown nach jedem Lauf.
- CLI, Zeitsteuerung (supercronic), Docker-Image, Compose-Setups (eigenständig und MOSIDI-Einbindung).
- Demo-Karte (Änderungskarte, Fehler-Marker, Umkreissuche, Kennzahlenkarte) und Demo-Modus mit Sperren.
