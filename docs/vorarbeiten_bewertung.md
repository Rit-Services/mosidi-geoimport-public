# Bewertung der Vorarbeiten (LB 5, LB 6 AP 1/3)

Gesichtet: Repository `MOSIDI-ORG/MOSIDI` (öffentlich, letzter Commit 18.09.2026), Ordner
`services/backend/src/inkar_import/` und `services/backend/src/geobasis_sources/`, am 29.10.2026.
Dass dies die in der Leistungsbeschreibung genannten Vorarbeiten sind, ist eine Annahme und bei der TH zu bestätigen.
Laut Bieterantwort A5 ist der bisherige Code Open Source und darf weitergenutzt werden; das Repository enthält keine
Lizenzdatei. Deshalb wurde **kein Code kopiert**, sondern die Logik neu und generisch umgesetzt (Herkunft in `NOTICE`).

## Prüfung an den echten Daten (29.10.2026)

| Befund | Wert |
|---|---|
| Datei | `https://www.bbr-server.de/imagemap/inkar/download/inkar_2025.zip`, 433.790.695 Bytes, Last-Modified 13.08.2025; `inkar_2026.zip` → 404 |
| Kompression | **Deflate64** (ZIP-Methode 9) – Pythons `zipfile` kann das nicht; die Vorarbeit nutzt dafür `patoolib` (externe Packprogramme) |
| Inhalt | `inkar_2025.csv` (6.762.930.064 Bytes, 63.426.864 Zeilen), `Indikatorenübersicht (INKAR 2025).xlsx`, `BBSR_Raumgliederungen_Referenz_2023.xlsx`, `Readme.txt` |
| Kopfzeile | `Bereich;ID;Kuerzel;Indikator;Raumbezug;Kennziffer;Name;Zeitbezug;Wert` (UTF-8, Dezimalkomma). `Kuerzel` fehlt im `config.xml` der Vorarbeit |
| Gemeinde-Kennziffern | 8-stellig mit führender Null (`01001000` Flensburg) |
| Brandenburg, Raumbezug „Gemeinden“ | 1.285.774 Zeilen, 413 Gemeinden (= 413 im LGB-WFS), 333 Indikatoren, Zeitbezug 1995–2024 |
| Eindeutigkeit | `(Kennziffer, Indikator, Zeitbezug)` hat 36.232 Dubletten; eindeutig ist `(Kennziffer, Bereich, Kuerzel, Indikator, Zeitbezug)` |

## Je Datei

| Vorarbeit | Zweck | Umgang | Begründung |
|---|---|---|---|
| `inkar_download.download` | neueste Jahresdatei per HEAD ab nächstem Jahr rückwärts suchen, Download, Entpacken | **generalisiert** als `csv.location.url_template` + `year: latest` (+ `search_years_back`) | gute Idee; im Original ist `year` undefiniert, wenn nicht „latest“ gewählt wird (im Code vermerkt), der Download liegt komplett im Speicher (`response.content`, 434 MB), Pfade mit `\\` sind Windows-spezifisch. Neu: gestreamter Download, ETag/Last-Modified, plattformneutral, Test `test_latest_year_search` |
| `filenames_in_folder` | Dateien im Archiv per Muster finden (`inkar*.csv`, `*ndikatoren*.xlsx`, `*eferenz*.xlsx`) | **übernommen** als `csv.archive.member_glob` (genau ein Treffer erforderlich) | Konzept brauchbar; neu ohne Entpacken auf Platte, direkt aus dem ZIP (Deflate64 über `zipfile-deflate64`) |
| `config.xml` | Spaltenmapping CSV → SQL | **überführt** nach YAML `columns:`; Konverter `scripts/xml_mapping_to_yaml.py` | entspricht „Config-Files“ (A5); YAML wird mit Schema geprüft und ist editorfreundlich |
| `inkar_db_import.importIndicators` | `DROP TABLE dashboard_data_de; CREATE TABLE …`, COPY, danach `ALTER TABLE` | **ersetzt** durch Löschen + Ersetzen je Quelle in einer Transaktion | widerspricht A6 (Strukturen nicht verändern); `DROP`/`CREATE`/`ALTER` laufen nicht in einer gemeinsamen Transaktion – ein Fehler hinterlässt eine leere oder halbfertige Tabelle |
| ebenda: `Kennziffer` numerisch (`convert_or_na`) | Typumwandlung | **verworfen** – Schlüssel bleiben Text mit `zfill` | führende Nullen gehen verloren; für Brandenburg (12…) unkritisch, für andere Länder falsch |
| ebenda: `on_bad_lines='skip'` | fehlerhafte Zeilen überspringen | **ersetzt** durch Fehler je Datensatz in `import_errors` + Fehlerquote | stilles Überspringen verfälscht den Bestand ohne Hinweis |
| `importMetaData` / `preprocessMetaData` | Indikator-Metadaten aus XLSX (Blatt `ZOM`) | **noch nicht übernommen**; vorgesehen als optionaler Schritt, falls das Zielmodell der TH Indikator-Metadaten vorsieht | Struktur des Zielmodells offen; Bereich und Kürzel werden bereits je Wert gespeichert |
| `inkar_version_comparison` | Formatvergleich neue vs. alte Version (Spalten, Zeilen, Indikatoren, Kennziffern), Bericht als TXT/JSON/CSV | **generalisiert** als Schema-Vergleich (`schema_drift`), Diff neu/geändert/gelöscht und Import-Bericht | gleicher Zweck für alle Quellen statt nur INKAR |
| `instance.py` / `process_class.py` | Ablaufsteuerung, Schlüsseltabelle `key.csv` | **ersetzt** durch Orchestrator + `pipeline_meta` + Zustandsdatei je Quelle | in `instance.py` ruft `db_import()` versehentlich `comparison()` auf; die Schlüsseltabelle ist nicht transaktionssicher |
| `main_inkar_import.py` | Einstieg, Logging in `./inkar_import.log`, `sys.excepthook` umgebogen | **ersetzt** durch CLI und JSON-Logs auf stdout | Datei-Log und umgebogener Excepthook sind nicht containertauglich |
| Zugangsdaten-Standardwerte (`'password': '1234'`) | DB-Verbindung | **verworfen** | widerspricht LB 4.4; die Pipeline startet ohne gesetztes Passwort nicht |
| `geobasis_sources/get_available_data.py` | WMS-Capabilities der LGB lesen → `external_wms_sources`/`table_metadata` | **nicht Teil der Pipeline** | betrifft den WMS-Katalog der Anwendung, nicht den WFS-Import; die Variablennamen (`POSTGRES_*`) wurden übernommen |

## Konsequenzen für das Angebot und die Umsetzung

- Die Vorarbeiten bestätigen Python als Technologie (LB 4.3, 9).
- Für INKAR gilt der fünfteilige Schlüssel; sonst wären neu/geändert/gelöscht im Bericht falsch.
- Zu klären mit der TH: Lizenz der Vorarbeiten, Zielmodell für Indikator-Metadaten, Auswahl der INKAR-Indikatoren
  (alle 333 oder eine Teilmenge).
