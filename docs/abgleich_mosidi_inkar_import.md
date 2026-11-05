# Abgleich mit den Vorarbeiten `inkar_import` (Bieterantwort V11)

Die TH verlangt mit Antwort V11, dass die vorhandenen Skripte unter `services/backend/src/inkar_import` im öffentlichen
Repository `MOSIDI-ORG/MOSIDI` berücksichtigt werden. Dieses Dokument gleicht sie Funktion für Funktion mit der Pipeline ab.
Es baut auf der Bewertung vom 29.10.2026 auf ([`vorarbeiten_bewertung.md`](vorarbeiten_bewertung.md)). Dort stehen auch die
Prüfergebnisse an der echten INKAR-Datei 2025. Wo dieses Dokument die Bewertung ergänzt oder berichtigt, ist das mit Beleg
aus dem Quelltext vermerkt.

## 1. Stand der Sichtung

| Angabe | Wert |
|---|---|
| Datum der Sichtung | 02.11.2026 |
| Repository | `MOSIDI-ORG/MOSIDI` (öffentlich, Standardzweig `master`, keine Lizenzdatei) |
| Gesichteter Commit (Repository-Stand) | `26cadbba3d6223c84872987104fc737359bf8bfc` (02.11.2026, „merge dev into master“) |
| Letzte Änderung im Ordner `inkar_import` | `fb1283431ee2355bda3f3bab0afbd74d2e0fd147` (02.09.2025, „Initial commit“). Seitdem hat sich am Ordner nichts geändert. Die Sichtung vom 29.10.2026 bezog sich damit auf denselben Inhalt. |
| Zugriff | nur lesend über die GitHub-API (`contents`, `commits`) |

| Datei | Zweck |
|---|---|
| `main_inkar_import.py` | Einstiegsskript: setzt URL und Archivordner, richtet das Logging in eine Datei ein und ruft nacheinander Download, Versionsvergleich und Datenbankimport auf. |
| `process_class.py` | Klasse `InkarProcess`: hält den Prozesszustand (URL, Archivordner, Schlüsseltabelle `key.csv`, Statusflags) und leitet an die drei Fachmodule weiter. |
| `instance.py` | Fast gleiche, ältere Fassung von `InkarProcess`. `main_inkar_import.py` importiert sie nicht. `comparison()` und `db_import()` rufen hier beide eine Funktion `comparison` in `inkar_download` auf, die es nicht gibt. |
| `inkar_download.py` | Sucht die neueste Jahresdatei per HEAD, lädt das ZIP herunter, entpackt es mit `patoolib`, findet die drei Nutzdateien per Dateimuster und trägt sie in die Schlüsseltabelle ein. |
| `inkar_version_comparison.py` | Vergleicht die beiden jüngsten heruntergeladenen Jahresstände (Spalten, Zeilenzahl, Indikatoren, Gemeindekennziffern der Projektregion). Schreibt einen Bericht als TXT, JSON und CSV und setzt das Flag `correctformat`. |
| `inkar_db_import.py` | Importiert die Indikatorwerte in die Tabelle `dashboard_data_de` und die Indikator-Metadaten (Excel, Blatt `ZOM`) in `dashboard_data_metadata`. Beide Tabellen werden dabei neu angelegt. Spalten und Typen kommen aus `config.xml`. |
| `config.xml` | Spaltenmapping CSV → SQL (Name und SQL-Typ) für die Abschnitte `indicator` (Werte) und `metadata` (Indikatorbeschreibung). |

## 2. Abgleich Funktion für Funktion

Legende: **übernommen** = gleiche Idee, neu implementiert; **generalisiert** = gleiche Idee, als konfigurierbare Funktion
für alle Quellen; **bewusst anders** = abweichendes Verhalten, mit Begründung.

| Vorarbeit (Modul.Funktion) | Unser Modul / unsere Konfiguration | Umgang | Begründung |
|---|---|---|---|
| `main_inkar_import` (Modulebene): Ablauf Download → Vergleich → Import, Logging in `./inkar_import.log` | `geoimport/orchestrator.py` (`Orchestrator.run`, `_process`), CLI, JSON-Logs auf stdout | bewusst anders | Gleiche Reihenfolge (Abruf → Prüfung → Ersetzen), aber für jede Quelle getrennt und gegeneinander abgeschottet. Ein Datei-Log im Arbeitsverzeichnis taugt nicht für Container (siehe Bewertung). |
| `process_class.InkarProcess` (Zustand, Flags `new_data_available`, `correctformat`) | `orchestrator.Orchestrator`, Zustandsdatei je Quelle (`StateStore`), `pipeline_meta` | bewusst anders | Den Zustand halten die Zustandsdatei und die Lauf-Historie in der Datenbank, nicht Attribute eines Prozessobjekts. Die Flags werden zu Statuswerten je Quelle (`unchanged`, `ok`, `skipped_guard`, `failed`). |
| `process_class.InkarProcess.key_save` / Schlüsseltabelle `key.csv` (Jahr, Ordner, Dateinamen, Erfolgsflags) | Zustandsdatei je Quelle (URL, ETag, Last-Modified, SHA-256, Spaltenliste), Rohdatenordner je Lauf, `pipeline_meta` | bewusst anders | Die CSV-Schlüsseltabelle wird nicht transaktionssicher geschrieben. Zusätzlicher Beleg vom 02.11.: In `inkar_db_import.db_import` wird das Erfolgsflag unter dem Spaltennamen `import.successful` gesetzt. Die Tabelle legt aber `import_successful` an, das Flag landet also in einer falschen Spalte. |
| `instance.InkarProcess` | – | nicht übernommen | Wird von `main_inkar_import.py` nicht verwendet und verweist auf eine Funktion, die es nicht gibt (siehe Abschnitt 1). |
| `inkar_download.download`, Teil „neueste Version suchen“ (ab nächstem Jahr rückwärts per HEAD) | `csv_source.CsvSourcePlugin.resolve_url`; YAML `csv.location.url_template`, `year: latest`, `search_years_back` | generalisiert | Gleiche Suchstrategie. Bei uns ist die Suche auf `search_years_back` Jahre begrenzt (die Vorarbeit sucht ohne Grenze weiter, bis ein HEAD mit 200 antwortet) und es gibt einen festen Jahreswert (in der Vorarbeit ist `year` ohne „latest“ undefiniert, dort im Code vermerkt). Test: `test_latest_year_search`. |
| `inkar_download.download`, Teil „bereits geladen?“ (Abgleich mit `key.csv`, Ordner vorhanden, `force_download`) | `CsvSourcePlugin.fetch`: bedingter Abruf mit ETag/Last-Modified, Vergleich SHA-256; `skip_if_unchanged`; CLI-Option für vollständigen Lauf | generalisiert | Statt „Jahr schon eingetragen“ zählt, ob sich die Datei geändert hat. Damit fällt auch eine korrigierte Datei desselben Jahres auf. Tests: `test_download_conditional_and_unchanged`, `test_not_modified_304`. |
| `inkar_download.download`, Teil „Download und Entpacken“ (ganze Antwort im Speicher, `patoolib`, Ordner mit Zähler-Suffix) | `geoimport/http` (gestreamter Download in den Rohdatenordner des Laufs), `csv_source.open_table` (Lesen direkt aus dem ZIP, Deflate64 über `zipfile-deflate64`), `orchestrator.cleanup_raw` | bewusst anders | Die 434-MB-Datei wird nicht komplett in den Speicher geladen und nicht auf die Platte entpackt (entpackt 6,8 GB). Es braucht keine externen Packprogramme. Aufbewahrung über `retention_days` statt unbegrenzt wachsender Ordner. |
| `inkar_download.filenames_in_folder` (Muster `inkar*.csv`, `*ndikatoren*.xlsx`, `*eferenz*.xlsx`, je genau ein Treffer) | `csv_source._match_members`, `open_table`; YAML `csv.archive.member_glob: "inkar*.csv"` | übernommen | Gleiches Muster für die CSV, gleiche Regel „genau ein Treffer“ (Test `test_archive_member_must_be_unique`). Die Vorarbeit trennt Pfade mit `\\`, läuft also nur unter Windows; bei uns ist das plattformneutral. Die beiden XLSX-Muster werden derzeit nicht gebraucht (Metadaten siehe unten). |
| `inkar_version_comparison.comparison`, Teil „Spalten“ (Pflichtspalten `Indikator`, `Kennziffer`, `Name`, `Zeitbezug`, `Wert`; Spaltenunterschiede alt/neu) | `CsvSourcePlugin.parse`: gemappte Spalten fehlen → Abbruch `schema_mismatch`; `guards.expected_columns` fehlen → Warnung `schema_drift`; Spaltenliste gegen den letzten Lauf → Warnung `schema_drift` | generalisiert | Gleicher Zweck, für alle CSV-Quellen. Zusätzlicher Beleg vom 02.11.: In der Vorarbeit bricht der Vergleich nur ab, wenn dem alten Stand Pflichtspalten fehlen und der neue sie hat. Fehlen sie im neuen Stand, läuft er weiter. Am Ende wird `correctformat` immer auf wahr gesetzt, auch wenn Unterschiede gefunden wurden. Bei uns führen fehlende Pflichtspalten immer zum Abbruch der Quelle, der Bestand bleibt dabei erhalten. |
| `inkar_version_comparison.comparison`, Teil „Zeilen, Indikatoren, Kennziffern“ (Mengenvergleich über die ganze Datei) | `geoimport/diff.py` (`compute_diff`: Schlüssel + Hash je Datensatz → neu/geändert/gelöscht/unverändert), Import-Bericht; Schutzregeln `guards.min_rows`, `guards.max_drop_ratio` | generalisiert | Die Vorarbeit vergleicht Mengen von Indikatornamen und Kennziffern. Wir vergleichen jeden Datensatz über den fünfteiligen Schlüssel (siehe Bewertung). Ein eigener Bericht über die Indikatormengen (Indikatoren nur in alt bzw. nur in neu) ist bei uns nicht umgesetzt. Diese Änderungen zeigen sich nur als neue bzw. gelöschte Datensätze. |
| `inkar_version_comparison.InProjectRegionOnly` (fest eingetragene Liste von 10 Kreisschlüsseln, siehe Abschnitt 3.4) | YAML `csv.region_filter` (`column: Kennziffer`, `prefix: "12"`; `prefix` darf eine Liste sein) | generalisiert, Umfang abweichend | In der Vorarbeit gilt die Liste nur für den Kennziffern-Vergleich im Bericht, nicht für den Import. Importiert werden dort alle Gemeinden Deutschlands. Wir filtern beim Import auf Brandenburg (`12`). Ob die TH ganz Brandenburg oder nur die 10 Kreise der Projektregion will, ist offen (Abschnitt 3.4). |
| `InkarAnalysisReport.export_as_txt` / `export_as_json` / `export_as_csv` (Berichtsdateien mit Zeitstempel im Arbeitsverzeichnis) | Import-Bericht (`docs/import_bericht.md`, Schema `docs/report.schema.json`), `pipeline_meta.import_source_results` | generalisiert | Ein Bericht für alle Quellen, maschinenlesbar und in der Datenbank abgelegt statt verstreuter Dateien. |
| `inkar_version_comparison.testSingleDataset` (erster Lauf: nur Pflichtspalten prüfen) | Erster Lauf ohne Vergleichsbasis: `compute_diff` ohne alten Stand bzw. Warnung `no_baseline` | übernommen | Gleicher Grundgedanke: Ohne Vorgängerstand wird nur die Struktur geprüft und der Diff ab dem nächsten Lauf gezählt. |
| `inkar_db_import.importIndicators`, Teil Tabellenanlage (`DROP TABLE IF EXISTS dashboard_data_de`, `CREATE TABLE` aus `config.xml`, danach mehrere `ALTER TABLE`: `id` durch `serial primary key` ersetzen, `wert` umwandeln, Spalten `raumbezug`, `bereich`, `name` löschen) | `target/table_mapping.TableMappingAdapter.replace` (Löschen der Zeilen des Datensatzes, dann `COPY` in temporäre Tabellen und `INSERT … SELECT`), `orchestrator._replace` (eine Transaktion je Quelle), `target/guard.py` (DDL-Sperre) | bewusst anders | Die Vorarbeit legt die Zieltabelle bei jedem Lauf neu an. Bei uns ersetzt eine Transaktion je Quelle die Daten (Löschen + Ersetzen), ohne die Tabellenstruktur der TH anzufassen (Bieterantwort A6: keine DDL). `CREATE`, `ALTER`, `DROP` und `TRUNCATE` auf Zieltabellen blockiert `guard.check_sql`. Andere Quellen in denselben Tabellen bleiben unberührt. **Berichtigung zur Bewertung vom 29.10.:** Dort steht, `DROP`/`CREATE`/`ALTER` liefen nicht in einer gemeinsamen Transaktion. Im Quelltext öffnet `importIndicators` aber eine einzige psycopg2-Verbindung ohne Autocommit und ruft `commit()` erst am Ende auf. Für die Indikatortabelle laufen DROP, CREATE, alle COPY und ALTER also in einer Transaktion. Bei einem Fehler rollt PostgreSQL alles zurück. Getrennt laufen dagegen Indikatoren und Metadaten (`insertMetaData` hat eine eigene Verbindung und einen eigenen Commit): Neue Werte können mit alten oder fehlenden Metadaten stehen bleiben. Außerdem hält das `DROP` für die gesamte Importdauer eine exklusive Sperre auf der Tabelle. Am Ergebnis der Bewertung („ersetzt“, Widerspruch zu A6) ändert das nichts. |
| `inkar_db_import.importIndicators`, Teil Lesen (`read_csv` in Blöcken zu 100.000 Zeilen, `on_bad_lines='skip'`, Spaltenauswahl nach `config.xml`, Zeilenfilter `Raumbezug == 'Gemeinden'` im Code) | `CsvSourcePlugin.parse` (`chunksize`, `usecols` aus `columns`), `_filter` (YAML `csv.row_filter`, `csv.region_filter`), `_convert` (Fehler je Datensatz) | generalisiert | Spaltenauswahl und Zeilenfilter stehen in der Konfiguration statt im Code. Fehlerhafte Zeilen werden nicht stillschweigend übersprungen, sondern in `import_errors` abgelegt und über die Fehlerquote (`max_error_ratio`) bewertet (siehe Bewertung). |
| `inkar_db_import.importIndicators`, Teil Übergabe an die Datenbank (jeder Block als temporäre CSV ohne Kopfzeile, `COPY … WITH HEADER CSV`) | `TableMappingAdapter._insert_attributes` (`COPY` aus dem Speicher in eine temporäre Tabelle) | bewusst anders | Zusätzlicher Beleg vom 02.11.: Die Vorarbeit schreibt jeden Block ohne Kopfzeile, liest ihn aber mit der Option `HEADER` ein. PostgreSQL verwirft dabei die erste Datenzeile jedes Blocks mit Gemeindewerten. Bei uns wird nichts übersprungen; Zeilenzahlen gelesen/gefiltert/geschrieben stehen im Bericht. |
| `inkar_db_import.convert_or_na` (`Kennziffer` numerisch) | `columns.Kennziffer: {type: str, normalize: {zfill: 8}}`, `transform/keys.normalize_series` | bewusst anders | Führende Nullen bleiben erhalten (siehe Bewertung). Die Vorarbeit macht `kennziffer` am Ende ohnehin wieder zu `text`, dann aber ohne die Nullen. Eine weitere Schwäche: Ist ein einziger Wert eines Blocks nicht numerisch, setzt die Funktion die ganze Spalte des Blocks auf NA, nicht nur diesen Wert. |
| `inkar_db_import.importIndicators`, Teil `wert` (zuerst `VARCHAR`, dann Komma → Punkt per `UPDATE`, dann `numeric`) | `csv.decimal: ","`, `columns.Wert: {type: float}`, `convert_column` | übernommen (Logik), bewusst anders (Ort) | Die Dezimalkomma-Umwandlung passiert beim Lesen, nicht nachträglich per `UPDATE` in der Zieltabelle. Nicht umwandelbare Werte werden Fehlerdatensätze. Den Typ in der Datenbank bestimmt die Zielspalte der TH (Cast in `TableMappingAdapter._cast`). |
| `inkar_db_import.importIndicators`, Teil `id` (INKAR-Spalte `ID` laden, löschen, durch `serial` ersetzen) | Schlüssel `key.fields: [region_key, domain, indicator_code, indicator, period]`; Doppelte Schlüssel werden Fehlerdatensätze (Test `test_duplicate_keys_become_errors`) | bewusst anders | Ein künstlicher Zählerwert ändert sich bei jedem Lauf und taugt nicht für neu/geändert/gelöscht (LB 4.5). INKAR-`ID` wird nicht gemappt. Ob sie als Indikator-ID gebraucht wird, hängt vom Zielmodell der TH ab. |
| `inkar_db_import.importIndicators`, Teil Spaltenlöschung (`bereich`, `raumbezug`, `name` nach dem Laden entfernt) | `columns.Bereich → domain` (Teil des Schlüssels), `Raumbezug` nur als Filter, `columns.Name: {keep: false}` | bewusst anders | `Bereich` muss erhalten bleiben: Ohne `Bereich` und `Kuerzel` ist `(Kennziffer, Indikator, Zeitbezug)` nicht eindeutig (36.232 Dubletten in Brandenburg, siehe Bewertung). Die Vorarbeit verliert diese Unterscheidung. `Name` (Gemeindename) und `Raumbezug` werden wie in der Vorarbeit nicht gespeichert. |
| `inkar_db_import.importMetaData` / `preprocessMetaData` / `insertMetaData` (Excel-Blatt `ZOM`, Zeilen 1 und 2 verwerfen, erste Zeile als Kopf, Spalten nach `config.xml`, Zeilen ohne `MERK_ID` verwerfen, `DROP`/`CREATE` von `dashboard_data_metadata`) | – (vorgesehen als optionaler Schritt) | noch nicht übernommen | Wie in der Bewertung: Das hängt vom Zielmodell der TH für Indikator-Metadaten ab. Eine Übernahme würde ebenfalls ohne DDL laufen. Hinweis vom 02.11.: Die Metadatenfunktionen lesen `config.xml` relativ zum Arbeitsverzeichnis, die Indikatorfunktion über den festen Pfad `/app/src/inkar_import/config.xml`. |
| `inkar_db_import.importInkar` (Dateinamen aus der letzten Zeile von `key.csv`, Aufräumen der temporären Dateien) | `orchestrator._process` (Rohdatei des aktuellen Laufs), `load_fetch_info` / Replay eines früheren Rohdatenordners | generalisiert | Ein früherer Stand lässt sich über den Rohdatenordner gezielt erneut einspielen, nicht nur „letzte Zeile der Schlüsseltabelle“. Temporäre Daten liegen nur in temporären Tabellen der Transaktion (`ON COMMIT DROP`). |
| `inkar_db_import.__connect`, `dbConfig` (Standardpasswort `1234`, Variablen `POSTGRES_*`) | Verbindungsaufbau mit Umgebungsvariablen ohne Standardpasswort | übernommen (Variablennamen), bewusst anders (Standardwerte) | Siehe Bewertung: Die Variablennamen `POSTGRES_*` sind übernommen. Ohne gesetztes Passwort startet die Pipeline nicht (LB 4.4). |
| `inkar_db_import.stderr_handler` (`sys.excepthook` beim Import des Moduls umgebogen) | `orchestrator._process` (Fehler je Quelle abgefangen, im Bericht und in `pipeline_meta`) | bewusst anders | **Ergänzung zur Bewertung:** Der Excepthook wird in `inkar_db_import.py` umgebogen, nicht in `main_inkar_import.py`. Weil `process_class` das Modul importiert, wirkt das für den ganzen Prozess. Bei uns bricht ein Fehler nur die betroffene Quelle ab; der Exit-Code zeigt ihn an. |
| `inkar_db_import.__parse_xml_config` (liest `csv` bzw. `sql/name`+`sql/type` je Abschnitt) | `scripts/xml_mapping_to_yaml.py` (einmalige Überführung), `geoimport/config/models.py` (Pydantic-Prüfung), `docs/config.schema.json` | generalisiert | Die Pipeline wertet `config.xml` zur Laufzeit nicht aus. Das Skript überführt ein vorhandenes XML-Mapping einmalig in YAML. |

## 3. Abbildung `config.xml` → YAML

### 3.1 Abschnitt `indicator` (Werte)

| CSV-Spalte (`config.xml`) | SQL-Name / SQL-Typ (`config.xml`) | Verbleib in der Vorarbeit nach dem Import | YAML `config/sources/inkar_gemeinden.yaml` | Anmerkung |
|---|---|---|---|---|
| `Bereich` | `bereich` / `VARCHAR(200)` | gelöscht | `Bereich: {to: domain, type: str}`, Schlüsselteil | wird für die Eindeutigkeit gebraucht |
| `ID` | `id` / `int` | gelöscht, ersetzt durch `serial` | nicht gemappt | siehe Abschnitt 2 |
| `Indikator` | `indikator` / `VARCHAR(200)` → später `text` | bleibt | `Indikator: {to: indicator, type: str}`, `record.name` | |
| `Raumbezug` | `raumbezug` / `VARCHAR(200)` | gelöscht (vorher Filter `Gemeinden` im Code) | `csv.row_filter: {Raumbezug: "Gemeinden"}` | Filter in die Konfiguration verlagert |
| `Kennziffer` | `kennziffer` / `integer` → später `text` | bleibt (ohne führende Nullen) | `Kennziffer: {to: region_key, type: str, normalize: {zfill: 8}}`, `record.object_key`, `csv.region_filter` | `str` statt `integer`, im Konverterskript fest so umgesetzt |
| `Name` | `name` / `VARCHAR(200)` | gelöscht | `Name: {to: region_name, type: str, keep: false}` | nur gelesen, nicht gespeichert |
| `Zeitbezug` | `zeitbezug` / `int` | bleibt | `Zeitbezug: {to: period, type: str}`, `record.period` | `str` statt `int`. **Hinweis:** Das Konverterskript würde hier `int` erzeugen; die YAML-Datei weicht davon bewusst ab. Ein Grund steht weder in der YAML-Datei noch im Skript. Zu dokumentieren oder anzugleichen. |
| `Wert` | `wert` / `VARCHAR` → später `numeric` | bleibt | `Wert: {to: value, type: float}`, `csv.decimal: ","` | Umwandlung beim Lesen |
| – (fehlt in `config.xml`) | – | – | `Kuerzel: {to: indicator_code, type: str}`, Schlüsselteil | siehe Abschnitt 4 |

Dateiformat: In der Vorarbeit stehen Trennzeichen `;` und Dezimalkomma im Code (`read_csv`), nicht in `config.xml`. Bei uns
stehen sie in `csv.delimiter` und `csv.decimal`. Die Kodierung legt die Vorarbeit nicht fest (pandas-Standard UTF-8); bei uns
gilt `csv.encoding: auto`.

### 3.2 Abschnitt `metadata` (Indikatorbeschreibung, Excel-Blatt `ZOM`)

`config.xml` nennt 9 Spalten, alle außer `MERK_ID` vom SQL-Typ `text`: `MERK_ID` → `id` (`int`), `Kurzname` → `kurzname`,
`Name` → `name`, `Algorithmus` → `algorithmus`, `Anmerkungen` → `anmerkungen`, `Statistische Grundlagen` →
`statistische_grundlagen`, `Gemeinden` → `gemeinde`, `Zeitreihe / Jahr / Entwicklung` → `zeitreihe_jahr_entwicklung`,
`Kartierbar` → `kartierbar`. Ein YAML-Gegenstück gibt es noch nicht (Metadaten noch nicht übernommen, siehe Abschnitt 2).
`scripts/xml_mapping_to_yaml.py config.xml metadata` erzeugt die Spaltenliste, sobald das Zielmodell feststeht.

### 3.3 Filter

| Filter | Vorarbeit | Ort in der Vorarbeit | Bei uns |
|---|---|---|---|
| Raumbezug | nur `Gemeinden` | Code (`importIndicators`), nicht `config.xml` | `csv.row_filter.Raumbezug` |
| Region beim Import | keiner (alle Gemeinden Deutschlands) | – | `csv.region_filter` mit Präfix `12` (Brandenburg) |
| Region im Versionsvergleich | 10 Kreisschlüssel (Abschnitt 3.4), nur 8-stellige Kennziffern | Code (`InProjectRegionOnly`), nicht `config.xml` | – (Diff gilt für den ganzen importierten Bestand) |
| Indikatoren | keiner | – | keiner (alle 333 Indikatoren für Brandenburg) |
| Zeitbezug | keiner | – | keiner (1995–2024) |

### 3.4 Legt `config.xml` eine Auswahl von Indikatoren oder Spalten fest?

- **Indikatoren: nein.** `config.xml` enthält keinen Indikatornamen, kein Kürzel und keine Indikator-ID. Auch im Code der
  Vorarbeit gibt es keinen Indikatorfilter: Importiert werden alle Indikatoren aller Zeilen mit Raumbezug `Gemeinden`.
- **Spalten: ja.** `config.xml` legt fest, welche CSV-Spalten gelesen werden (Abschnitt `indicator`), und zwar diese 8:
  `Bereich`, `ID`, `Indikator`, `Raumbezug`, `Kennziffer`, `Name`, `Zeitbezug`, `Wert`.
  Von diesen löscht `importIndicators` nach dem Laden `Bereich`, `Raumbezug`, `Name` und `ID` wieder. In
  `dashboard_data_de` bleiben `indikator`, `kennziffer`, `zeitbezug`, `wert` und ein neuer Zählerwert `id`.
  Für die Metadaten legt `config.xml` die 9 Spalten aus Abschnitt 3.2 fest.
- **Region: nicht in `config.xml`.** Im Code (`inkar_version_comparison.InProjectRegionOnly`) steht als Standardwert
  eine Projektregion aus 10 Kreisschlüsseln: `12051`, `12054`, `12060`, `12063`, `12065`, `12068`, `12069`, `12070`,
  `12072`, `12073`. Nach amtlichem Kreisschlüssel (unsere Zuordnung, nicht im Code): Brandenburg an der Havel, Potsdam,
  Barnim, Havelland, Oberhavel, Ostprignitz-Ruppin, Potsdam-Mittelmark, Prignitz, Teltow-Fläming, Uckermark.
  Die Liste gilt nur für den Kennziffern-Vergleich im Bericht, nicht für den Import.
  Zu klären mit der TH: Gilt für den Import ganz Brandenburg (`prefix: "12"`, derzeitige Konfiguration) oder diese
  Projektregion (`prefix` als Liste der 10 Kreisschlüssel)?

## 4. Abweichungen der echten INKAR-Datei 2025 von `config.xml`

Grundlage ist die Prüfung vom 29.10.2026 in [`vorarbeiten_bewertung.md`](vorarbeiten_bewertung.md) (Abschnitt „Prüfung an
den echten Daten“). Dort belegt sind:

| Punkt | `config.xml` / Vorarbeit | INKAR 2025 | Folge |
|---|---|---|---|
| Spalte `Kuerzel` | fehlt | vorhanden (3. Spalte: `Bereich;ID;Kuerzel;Indikator;Raumbezug;Kennziffer;Name;Zeitbezug;Wert`) | Die Vorarbeit liest sie nicht. Bei uns ist sie gemappt (`indicator_code`) und Teil des Schlüssels. |
| Eindeutigkeit | Vorarbeit behält nur `indikator`, `kennziffer`, `zeitbezug` | `(Kennziffer, Indikator, Zeitbezug)` hat 36.232 Dubletten in Brandenburg; eindeutig erst mit `Bereich` und `Kuerzel` | In `dashboard_data_de` sind die Dubletten nicht zu unterscheiden. Bei uns gilt der fünfteilige Schlüssel. |
| `Kennziffer` | `integer` | 8-stellig mit führender Null (z. B. `01001000`) | Für Brandenburg (`12…`) folgenlos, für Länder mit `0` vorn falsch. Bei uns Text mit `zfill: 8`. |
| Kompression | `patoolib` (externe Packprogramme) | Deflate64 (ZIP-Methode 9) | Pythons `zipfile` reicht nicht. Bei uns `zipfile-deflate64`. |
| Dateigröße | Download komplett im Speicher, dann Entpacken auf die Platte | ZIP 433.790.695 Bytes, CSV entpackt 6.762.930.064 Bytes, 63.426.864 Zeilen | Bei uns gestreamt, kein Entpacken auf die Platte. |
| Kodierung | nicht angegeben (pandas-Standard UTF-8) | UTF-8, Dezimalkomma | passt. Bei uns zusätzlich automatische Erkennung. |

Abweichungen bei den Metadaten (Blatt `ZOM` der `Indikatorenübersicht (INKAR 2025).xlsx`) sind in der Bewertung nicht
geprüft. Ob die 9 Spaltennamen aus Abschnitt 3.2 in der Fassung 2025 noch stimmen, ist offen.

## 5. Lizenz und Herkunft

- Das Repository `MOSIDI-ORG/MOSIDI` enthält keine Lizenzdatei (GitHub-API: `license` leer, Stand 02.11.2026). Laut
  Bieterantwort A5 ist der Code Open Source und darf weitergenutzt werden. Die Lizenz ist beim Auftraggeber erfragt.
- **Es wurde kein Code kopiert.** Die Vorarbeit wurde gelesen; ihre Funktionen sind in diesem Dokument beschrieben und
  wurden in der Pipeline neu und generisch umgesetzt. Aus der Vorarbeit stammen nur Bezeichner, Feld- und Spaltennamen
  (z. B. Dateimuster `inkar*.csv`, CSV-Spaltennamen, Variablennamen `POSTGRES_*`) und die URL der Datenquelle.
- `config.xml` ist ein Datenmapping, keine Programmlogik. Das Skript `scripts/xml_mapping_to_yaml.py` liest es zur
  Überführung ein, enthält aber selbst keinen Code der Vorarbeit.
- Den Herkunftsvermerk enthält [`NOTICE`](../NOTICE) im Abschnitt „Herkunft und Vorarbeiten“ (Stand 29.10.2026). Da der
  Ordner `inkar_import` seit dem 02.09.2025 unverändert ist (Abschnitt 1), stimmt der Vermerk inhaltlich auch für die
  Sichtung vom 02.11.2026. Bei der nächsten Änderung an `NOTICE` sollten der Sichtungsstand (Commit
  `26cadbba3d6223c84872987104fc737359bf8bfc`) und ein Verweis auf dieses Dokument nachgetragen werden.
