# Projektplan und Abstimmungspunkte

**Der Liefertermin 30.11.2026 ist fest** (Bieterantwort V14). Zuschlag voraussichtlich in KW 46 (09.–15.11.2026).
Die Abnahme findet in der eigenen Umgebung des Auftragnehmers statt; es gibt keinen Zugriff auf die Systeme der
TH (V12). Nach der Übergabe gibt es weder Support noch Betrieb durch den Auftragnehmer (V13).

## Bereits erledigt (Demonstrator, Stand 02.11.2026)

| MS | Inhalt | Status |
|---|---|---|
| M0–M5 | Grundgerüst, WFS- und CSV-Plugin, Ziel-Adapter mit DDL-Sperre, Löschen und Ersetzen, Diff, Schutzregeln, Orchestrator, CLI, Bericht, Scheduler, Docker, MOSIDI-Einbindung | erledigt |
| M6 | Live-Lauf gegen die echten Quellen (Geobasis-WFS, INKAR), Demo-Karte | erledigt |
| M7 | Dokumentation, Lizenzübersicht, Anforderungsmatrix | erledigt |
| M8 | Abgleich mit den Antworten vom 02.11.2026: fachlicher Schlüssel je Quelle, `aborted_incomplete`, Zeitplan je Quelle, AfS-Quelle, `compose.mosidi.yaml` mit `brandenburg_db` | erledigt |

## Meilensteine ab Zuschlag

| MS | Zeitraum (Vorschlag) | Inhalt | Ergebnis |
|---|---|---|---|
| AP1 | KW 46–47 | **Abstimmung von Quellen und Zielmodell:** DDL der drei Tabellen, weitere WFS-Dienste und FeatureTypes, AfS-Datensatz (`afs_bb`, TODO_TH), INKAR-Region (ganz Brandenburg oder Projektregion), Zeitpläne, Berichtsablage | Protokoll, `config/target.yaml` ohne Platzhalter |
| AP2 | KW 47 | **Anbindung an das Zielmodell der TH:** `target.yaml` füllen, `check-target` gegen ein Schema-Abbild in der eigenen Umgebung, Integrationstests mit dem Modell der TH | `check-target` ohne Fehler, Tests grün |
| AP3 | KW 47–48 | **Weitere WFS-Quellen** nach AP1 anbinden (je Quelle eine YAML-Datei), Fixtures aufzeichnen | `validate-config` grün, Tests je Quelle |
| AP4 | KW 48 | **Abnahmetest in der eigenen Umgebung:** E2E-Lauf mit Geobasis-WFS und INKAR (und AfS), Protokoll `docs/abnahme/e2e_<Datum>.md`, Abnahme-Checkliste | Abnahmeprotokoll |
| AP5 | bis 30.11.2026 | **Übergabe an MOSIDI-ORG:** Repository übertragen bzw. bereitstellen, Übergabetermin remote (Vorführung, Fragen, Übergabe der Dokumentation) | Übergabeprotokoll, Lieferung |

Bis die TH ihr Schema übergibt, bleibt `config/target.yaml` ein Platzhalter; Läufe dagegen brechen kontrolliert mit
Exit-Code 3 ab (keine Schreibzugriffe).

## Abstimmungspunkte mit dem Auftraggeber (AP1)

Durch die Antworten vom 02.11.2026 geklärt: Strukturen nicht ändern (V1), Löschen und Ersetzen je Quelle (V2),
Historisierung als Änderungsprotokoll mit `pipeline_meta` (V3), fachlicher Schlüssel je Quelle (V4), Löschen erst
nach vollständigem Abruf (V5), JSON-Bericht (V6), Intervalle konfigurierbar und höchstens monatlich (V7),
Einbindung ins MOSIDI-Compose (V8), EPSG:4326 (V9), Lizenz Apache-2.0 (V13), Liefertermin (V14).

Noch offen:

1. **Zielmodell:** DDL der drei Tabellen (Metadaten, Geometrien, Attribute) inkl. Schema, Schlüssel, Geometriespalte
   und der Spalte, an der die Quelle erkannt wird.
2. **WFS-Quellen:** weitere Dienste und FeatureTypes (V10); ganzes Land oder Ausschnitt?
3. **AfS-Datensatz:** Bestätigung oder Benennung (vorgeschlagen: Bevölkerung der Gemeinden, Tabelle 12411-01-01-5-B,
   siehe `config/sources/afs_bb.yaml`); Abruf per URL oder Ablage als Datei.
4. **INKAR:** ganz Brandenburg (derzeit) oder nur die Projektregion aus den Vorarbeiten (10 Kreise, siehe
   [Abgleich](abgleich_mosidi_inkar_import.md)); alle Indikatoren oder Auswahl.
5. **Auslegung V7:** „höchstens monatlich“ = mindestens ein Lauf je Monat (siehe [Anforderungen](anforderungen.md)).
6. **Zeitplan:** gewünschte Häufigkeit je Quelle.
7. **Berichtsablage:** Datei, Tabelle `pipeline_meta.import_runs`, zusätzlich Benachrichtigung?
8. **Übergabe:** Termin der Übergabesitzung (remote); Übertragung oder neues Repository bei MOSIDI-ORG.
