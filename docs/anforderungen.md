# Anforderungen: Rückverfolgung (Traceability-Matrix)

Quellen: **LB** Leistungsbeschreibung, **A1–A11** Beantwortung der Bieterfragen vom 29.10.2026, **V1–V14** verbindliche
Vorgaben aus der Beantwortung der Bieterfragen 1–58 vom 02.11.2026, **F59–F77** Antworten 59–77 vom 04.11.2026,
**WK** Wertungskriterien.
Status: ✅ umgesetzt und nachgewiesen · 🟡 umgesetzt, Abstimmung/Angabe der TH offen · ⏳ nach Zuschlag.
Stand: 05.11.2026 (Abgleich mit den Antworten vom 02.11. und 04.11.2026).

| ID | Anforderung | Quelle | Umsetzung | Nachweis | Antwort TH 02.11. | Status |
|---|---|---|---|---|---|---|
| R01 | WFS anbinden: Abfrage, Paginierung, unterschiedliche Schemata, Mapping | LB 4.1, AP 2 | `geoimport/sources/wfs.py`, Paging `none`/`startindex`/`bbox_tiles`/`auto`, `mapping:` | `tests/unit/test_wfs.py` (Paging, ExceptionReport, Schema), Live-Lauf `vg_wfs` | – | ✅ |
| R02 | 3 WFS-Quellen, eine Geobasis BB | A4 | `geobasis_bb_gemeinden.yaml` (geprüft), `wfs_quelle_2/3.TODO.yaml` | `validate-config`; Platzhalter `enabled: false` | V10: 1 WFS (Geobasis) fest, weitere in AP1 | 🟡 2 Quellen offen |
| R03 | CSV lokal und/oder per URL, flexible Spaltenzuordnung | LB 4.1 | `geoimport/sources/csv_source.py`, `location`, `columns`, ZIP/GZ | `tests/unit/test_csv.py` | – | ✅ |
| R04 | CSV inkar.de und Statistik Berlin-Brandenburg | A5 | `inkar_gemeinden.yaml` (geprüft, Live-Lauf 1.285.774 Zeilen), `afs_bb.yaml` (CSV per URL, `fallback_path` für AfS-Datei) | E2E mit Fixture, Abnahmeprotokoll | V10: INKAR + AfS per URL, AfS auch als Datei | 🟡 AfS-Datensatz in AP1 bestätigen (`afs_bb`, TODO_TH) |
| R05 | Anpassung per Config-Datei, ohne Codeänderung am Kern | A5, LB 4.1, LB 8.3 | YAML + Pydantic (`geoimport/config/models.py`), `docs/config.schema.json` | `test_config_validation.py`, Beispiel `config/examples/geobasis_bb_kreise.yaml` | – | ✅ |
| R06 | Vorarbeiten sichten, bewerten, integrieren/generalisieren | LB 5, 4.1, AP 1/3, A5 | Logik aus `inkar_import` generalisiert | [`vorarbeiten_bewertung.md`](vorarbeiten_bewertung.md), `test_latest_year_search` | V11: `inkar_import` berücksichtigen | ✅ (Lizenz der Vorarbeiten offen) |
| R07 | Zieldatenmodell vorhanden (Metadaten, Geometrien, Attribute) | LB 3, 4.2 | Adapter `table_mapping` aus `config/target.yaml`, keine DDL | `test_generic_adapter_other_model` (zweites Modell nur per YAML) | – | 🟡 DDL der TH ausstehend |
| R08 | Bestehende Strukturen nicht verändern | A6 | DDL-Sperre `target/guard.py`, Rollenvorschlag ohne `CREATE`/`ALTER`, `check-target` | `test_guard.py`, Schema-Schnappschuss vor/nach in Integrationstests und Abnahme | V1: keine DDL an den drei Tabellen | ✅ |
| R09 | Neue Daten importieren, alte Daten derselben Quelle löschen; keine Versionierung | A6, F59, F74 | `TableMappingAdapter.replace`: Transaktion je Quelle, Advisory-Lock; gelöscht werden nur Attribute (R52) | `test_other_sources_untouched_and_fault_isolation`, `test_rollback_keeps_old_data` | V2: Löschen und Ersetzen je Quelle | ✅ |
| R10 | Historisierung von Änderungen | LB 4.2, AP 4 | Lauf-Historie `pipeline_meta` (`import_runs`, `import_source_results`, `import_errors`, `diff_keys`) | `test_full_run_wfs_and_csv`, `test_changed_source_counts` | V3: Änderungsprotokoll je Lauf, `pipeline_meta` erlaubt | ✅ geklärt (V3) |
| R11 | EPSG:4326 | A7 | `transform/crs.py` (pyproj `always_xy`), Achsentausch-Erkennung | `test_transform.py`, SRID-Prüfung in Tests und Abnahme | V9: EPSG:4326 | ✅ |
| R12 | Python oder Node-RED, begründet | LB 4.3, 9, WK | Python 3.12 | [`architektur.md`](architektur.md) | – | ✅ |
| R13 | Modular, weitere Quellen und Quelltypen | LB 2, 4.3 | Plugin-Registry `@register_source`, `sources/_template.py`, `GEOIMPORT_PLUGINS` | `test_registry` | V10: Architektur muss skalieren | ✅ |
| R14 | Automatisierte, wiederholbare Ausführung | LB 4.3, 3, AP 5 | Service `scheduler` (supercronic), Crontab aus `schedule` je Quelle (`pipeline crontab`) | `make schedule-test`, Betriebshandbuch | V7: konfigurierbar, höchstens monatlich | ✅ (Auslegung „mindestens monatlich“, siehe unten) |
| R15 | Manuelles Auslösen | LB 4.3 | `docker compose run --rm pipeline run [--source]` | E2E über CLI | – | ✅ |
| R16 | Ausfall einer Quelle blockiert nicht; Fehlerbehandlung je Quelle | LB 4.3, AP 5 | Isolation je Quelle, Retries, Timeouts, ExceptionReport, Bestand bleibt | `test_other_sources_untouched_and_fault_isolation`, `test_exit_code_partial` | V5: Löschen erst nach vollständigem Abruf | ✅ |
| R17 | Docker-Image | LB 4.4, 8.2, A8 | `Dockerfile` (mehrstufig, nicht root, Healthcheck) | Docker-Build (CI) | – | ✅ |
| R18 | Compose inkl. PostgreSQL/PostGIS, bestehend oder neu | LB 4.4 | `compose.yaml` (eigenständig), `compose.mosidi.yaml` (Service `brandenburg_db`, Datenbank `brandenburg_db`, Werte aus Env) | Live-Lauf im Compose-Setup | V8: Container im MOSIDI-Compose, `brandenburg_db`, `postgis/postgis` | ✅ |
| R19 | Konfiguration über Env/Dateien, keine hartkodierten Zugangsdaten | LB 4.4, 8.2 | `.env.example`, `POSTGRES_PASSWORD_FILE`, `${VAR}` in YAML, Log-Maskierung | `test_no_password_defaults_in_repo`, `test_mask_secrets`, gitleaks (CI) | – | ✅ |
| R20 | Import-Bericht nach jedem Lauf | LB 2, 4.5, AP 7, 8.4 | `geoimport/report/` | alle Integrations-/E2E-Tests | – | ✅ |
| R21 | Quellen mit Zeitstempel | LB 4.5 | `sources[].started_at/finished_at` (ISO 8601, Offset) | `test_report_matches_schema_and_renders` | – | ✅ |
| R22 | Fehlgeschlagene Quellen inkl. Ursache | LB 4.5 | `status=failed`, `error.kind/message/http_status/url` | `test_other_sources_untouched_and_fault_isolation` | – | ✅ |
| R23 | Anzahl neu/geändert/gelöscht | LB 4.5, F62 | Diff über Schlüssel + Hash gegen den Zielbestand (`geoimport/diff.py`, `TableMappingAdapter.diff_basis`) | `test_diff.py`, `test_changed_source_counts`, `test_diff_against_target.py`, E2E 3. Lauf | V4: fachlicher Schlüssel je Quelle | ✅ |
| R24 | Laufzeit und Warnungen (Schemaabweichungen) | LB 4.5 | `duration_s`, `warnings[]` (`schema_drift`, `unmatched_keys`, …) | `test_schema_drift_warning_against_state` | – | ✅ |
| R25 | JSON, optional menschenlesbar | LB 4.5 | JSON + Markdown + `import_runs.report` | `docs/report.schema.json` | V6: JSON | ✅ |
| R26 | Berichtsformat abstimmen | LB 4.5 | Vorschlag Version 1.0 | [`import_bericht.md`](import_bericht.md) | V6: JSON | ✅ geklärt (V6) |
| R27 | Tests | LB 4.6, AP 8 | pytest Unit/Integration/E2E, Abdeckung ≥ 80 % | CI | – | ✅ |
| R28 | E2E mit ≥ 1 WFS + 1 CSV inkl. Bericht | LB 4.6, 11 | `tests/e2e/test_e2e_wfs_csv.py`, `scripts/acceptance.py` (eigene Umgebung, echte Quellen) | `docs/abnahme/e2e_<datum>.md` | V12: E2E in eigener Umgebung (Geobasis + INKAR) | ✅ |
| R29 | Abnahme | LB 11, A8 | [`abnahme/abnahme_checkliste.md`](abnahme/abnahme_checkliste.md) | Checkliste | V12: kein Zugriff auf TH-Systeme | ⏳ nach Übergabe |
| R30 | Quellcode-Dokumentation | LB 4.7 | Docstrings (deutsch), Modulköpfe | ruff `D1` | – | ✅ |
| R31 | Installations- und Betriebsdoku | LB 4.7 | [`betriebshandbuch.md`](betriebshandbuch.md) | Review | – | ✅ |
| R32 | Entwicklerdoku inkl. neue Quellen | LB 4.7 | [`neue_quelle_anbinden.md`](neue_quelle_anbinden.md), [`entwicklung.md`](entwicklung.md) | Übung in der Übergabesitzung | – | ✅ |
| R33 | Dokumentation Datenmodell | LB 4.7, 4.2 | [`datenmodell.md`](datenmodell.md) inkl. ER-Diagramm | Review | – | 🟡 TH-Tabellen nachtragen |
| R34 | Nur Open-Source-Komponenten | LB 7, 2 | Abhängigkeiten, Prüfung in CI | [`drittkomponenten.md`](drittkomponenten.md) | – | ✅ |
| R35 | Eigener Code unter OSS-Lizenz, abstimmen | LB 7 | `LICENSE` Apache-2.0, `NOTICE` | – | V13: Apache-2.0 | ✅ Apache-2.0 (V13) |
| R36 | Lizenzübersicht Drittkomponenten, jede Komponente mit Version und Lizenz; PostGIS als eigenständiger Dienst | LB 4.7, 7, 8.6, F70 | `scripts/gen_third_party.py` (Pakete, eingebettete Bibliotheken GDAL/PROJ/GEOS, eigenständige Dienste PostgreSQL/PostGIS, Demo-Karte) | CI `--check` | F70 | ✅ (Datenlizenz INKAR beim BBSR offen) |
| R37 | Versionierte Entwicklung (Git) | LB 8.1, 9 | Git, Conventional Commits, Tags | Historie | – | ✅ |
| R38 | Veröffentlichung bei MOSIDI-ORG | A8 | Entwicklung unter `Rit-Services`, Transfer/Spiegelung | Übergabe-Checkliste | V13: Übergabe an MOSIDI-ORG | ⏳ |
| R39 | Doku im Repo + Übergabesitzung | A8, LB 9 | `docs/`, [`uebergabe/agenda.md`](uebergabe/agenda.md) | – | – | ⏳ Sitzung |
| R40 | Betrieb in Docker, kein Support nach Übergabe | A8 | vollständiges Betriebshandbuch mit Störungstabelle | – | V13: kein Support, kein Betrieb | ✅ |
| R41 | Demonstrator als Referenz: Beschreibung + Link | A1, A2/A9 | README „Zweck“ (Kurzbeschreibung für Dritte) | Link erst nach Freigabe | – | 🟡 Veröffentlichung |
| R42 | Konzept zeigt Umsetzung | A11, WK | [`architektur.md`](architektur.md) | – | – | ✅ |
| R43 | Projektsteuerung und Abstimmung | AP 10, LB 9 | [`projektplan.md`](projektplan.md) | – | – | ✅ |
| R44 | Lieferung bis 30.11.2026 | LB 9 | Meilensteinplan | `projektplan.md` | V14: 30.11.2026 fest | ⏳ |
| R45 | Pauschalpreis | LV | – | – | – | – |
| R46 | Aktualität, Verlässlichkeit, Nachvollziehbarkeit | LB 1, 2 | Scheduler, Schutzregeln, Lauf-Historie | `test_guard_min_features_and_force`, `test_guard_drop_ratio` | V5 | ✅ |
| R47 | Nachnutzbarkeit | LB 2, 7 | generische Adapter, englische Bezeichner, deutsche Doku, keine MOSIDI-Spezifika im Kern | Review | – | ✅ |
| R48 | Demo-Karte, Änderungskarte, Fehler-Marker, Umkreissuche, Demo-Modus | Prototyp 6a | `webmap/`, `geoimport/demo.py` | `test_webmap_api`, `test_demo_mode_locks_and_simulation` | – | ✅ (Demo) |
| R49 | Fachlicher Schlüssel je Quelle in der Konfiguration, Verknüpfung über Schlüssel statt Fremdschlüssel | V4 | Pflichtfeld `business_key` (+ `key_normalize`), Prüfung gegen die Felder des Datensatzes; Diff nur über diesen Schlüssel; doppelte Schlüssel → `duplicate_key` | `test_missing_required_field`, `test_business_key_muss_im_datensatz_vorkommen`, `test_zusammengesetzter_business_key`, `test_doppelter_schluessel_ist_fehler` | V4 | ✅ |
| R50 | Löschen erst nach vollständigem, fehlerfreiem Abruf; Teilabbruch → Rollback, Status `aborted_incomplete` | V5 | Abruf und Prüfung vor der Transaktion; Schutzregeln (Fehlerquote, Mindestzahl, Rückgang, nie leer ersetzen); Erkennung abgeschnittener ZIP/CSV | `test_abbruch_auf_seite_2_ist_unvollstaendig`, `test_abgeschnittene_zip_ist_unvollstaendig`, `test_abgeschnittene_csv_ist_unvollstaendig`, `test_other_sources_untouched_and_fault_isolation` | V5 | ✅ |
| R51 | Update-Intervall je Quelle konfigurierbar, höchstens monatlich | V7 | Feld `schedule` (`daily`/`weekly`/`monthly`/Cron), Crontab per `pipeline crontab` beim Start des Schedulers | `tests/unit/test_schedule.py` | V7 | ✅ Auslegung |
| R52 | Löschen nur der Attribute, Raumbezüge bleiben und werden über den fachlichen Schlüssel aktualisiert; fehlende Gebiete nicht löschen, nur melden | F74 | `TableMappingAdapter._delete` (nur Attribute), `_upsert_geometries` (UPDATE nur bei Änderung, INSERT neuer Schlüssel), Warnung `geometry_retained`, Abbruch bei doppelten Schlüsseln im Bestand; `delete_scope.sql` darf keine Geometrien löschen | `tests/integration/test_geometries_kept.py` | F74 | ✅ |
| R53 | Warnungscodes `geometry_repaired`, `geometry_skipped`, `missing_required_field` mit Zählern je Quelle | F63 | `geoimport/quality.py`, `sources[].quality` im Bericht, Warnungen mit Anzahl und Beispielen, Abschnitt „Datenqualität“ im Markdown | `tests/unit/test_quality_counts.py`, `test_demo_mode_locks_and_simulation` | F63 | ✅ |
| R54 | Geometrien reparieren statt abbrechen; nicht reparierbar oder leer → überspringen; nur 2D inkl. Multi | F63 | `transform/geometry.check_geometry` (`make_valid`, `force_2d`), `TableMappingAdapter._repair_geometries` (`ST_Force2D`, `ST_MakeValid` + `ST_CollectionExtract`), `Orchestrator._reconcile_db_geometries` | `tests/integration/test_geometry_repair_db.py`, `test_z_und_m_werden_auf_2d_reduziert` | F63 | ✅ |
| R55 | Pflichtfelder Schlüssel, Geometrie bzw. Raumbezug, Zeitbezug, Wert; fehlend → überspringen | F63 | `csv_source.required_value_fields` (Raumbezug, Wert, Zeitbezug immer Pflicht), INKAR `guards.required` ergänzt, fehlender Schlüssel zählt als `missing_required_field`, fehlende Geometrie als `geometry_skipped` | `test_pflichtfelder_wert_und_zeitbezug`, `test_inkar_konfiguration_nennt_alle_pflichtfelder` | F63 | ✅ |
| R56 | Fachlicher Diff gegen den vorhandenen Datenbestand; erster Lauf gegen befüllten Bestand korrekt; INKAR-Schlüssel Kennziffer + Bereich + Kürzel + Indikator + Zeitbezug | F62 | `diff_basis` liest den Zielbestand und bildet die Lieferung auf dieselbe Form ab, Schlüssel aus den Zielspalten; `diff_keys` nur Protokoll bzw. Rückfall (`diff_from_baseline`) | `tests/integration/test_diff_against_target.py` | F62 | ✅ |
| R57 | Eigenständiger Betrieb ohne MOSIDI-Funktionen: Metadaten selbst in `table_metadata`, INKAR über `dct_catalog_publisher` gekennzeichnet, danach `REFRESH` auf `source_granularities` | F74, F75 | `target.yaml` (`table_metadata`, `dct_catalog_publisher: "{meta.publisher}"`, `after_import`), `TableMappingAdapter.refresh_views`, DDL-Sperre gibt nur `REFRESH MATERIALIZED VIEW <konfigurierte Sicht>` frei | `tests/integration/test_after_import.py`, `tests/unit/test_guard.py` | F74, F75 | 🟡 übrige Namen nach Schema-Dump (F61) |
| R58 | WFS 2.0.0 und 1.1.0, Abruf moderat und sequentiell | F67, F69 | `wfs.version: 1.1.0` (`TYPENAME`, `MAXFEATURES`, `numberOfFeatures`, GML 3.1.1, `gml:featureMember`), Paging `startindex`/`bbox_tiles`, `throttle_ms` | `tests/unit/test_wfs_110.py` | F67, F69 | ✅ |

## Auslegung V7 „höchstens monatlich“

Die Antwort lautet „Update-Intervalle konfigurierbar, höchstens monatlich“. Wir legen das als **höchster Abstand
zwischen zwei Läufen = ein Monat** aus: Jede Quelle läuft mindestens einmal in jedem Monat; häufigere Läufe
(täglich, wöchentlich) sind erlaubt. Seltenere Zeitpläne lehnt `validate-config` ab. Diese Auslegung ist in AP1 zu
bestätigen.

## Widersprüche zwischen Leistungsbeschreibung und Bieterantworten

| LB | Antwort | Auflösung |
|---|---|---|
| 4.2 / AP 4: „Historisierung von Änderungen“ | A6: keine Versionierung, Import löscht alte Daten derselben Quelle | Lauf-Historie und Diff in `pipeline_meta`, keine Versionen in Zieltabellen; Bestätigung erbeten |
| 4.2 / AP 4: „Design und Migration des Zieldatenmodells“ | A6: Strukturen nicht verändern; „Migration“ = Daten importieren/ersetzen | keine DDL auf Zieltabellen; „Migration“ = Löschen + Ersetzen |
| 4.5: neu, geändert, gelöscht | A6: Löschen und Neuschreiben | Diff über Schlüssel + Hash vor dem Ersetzen |
| 3 / 4.2: Modell aus 3 Tabellen | öffentliches MOSIDI-Schema zeigt andere Tabellen; DROP/CREATE der Vorarbeit widerspricht A6 | Adapter-Schicht, DDL bei der TH erfragt |
| 9: Lieferung bis 30.11.2026 | A3/A10: Zuschlag „zeitnah“ | Demonstrator vorab, Restarbeiten nach Zuschlag |
| 4.1: kleine Menge vorgegebener WFS | A4: 3 Quellen, nur Geobasis BB benannt | zwei Platzhalter-Konfigurationen |
