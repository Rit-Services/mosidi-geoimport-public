# Abnahme-Checkliste (LB 11, Bieterantwort A8)

Spiegelt den Test der TH nach Übergabe. Jede Zeile: Befehl und erwartetes Ergebnis.
`dcr` = `docker compose run --rm`. Live-Protokolle: `docs/abnahme/e2e_<datum>.md` (`make acceptance`).

| Nr. | Prüfung | Befehl / Nachweis | Erwartung | Ergebnis |
|---|---|---|---|---|
| A-01 | Repository vollständig (Code, Docker, Konfiguration, Doku, Lizenzübersicht) | [Übergabe-Checkliste](../uebergabe/checkliste.md) | alle Punkte LB 8 Nr. 1–6 vorhanden | |
| A-02 | Frisches Setup startet | `cp .env.example .env` (Werte setzen); `docker compose up -d --wait db`; `dcr pipeline init-meta`; `docker compose up -d scheduler` | Container `healthy` | |
| A-03 | Konfiguration gültig | `dcr pipeline validate-config` | Exit 0 | |
| A-04 | Zielschema passt und wird nicht verändert | `dcr pipeline check-target --snapshot /data/vorher.json`; nach A-05/A-07 erneut mit `nachher.json`; Dateien vergleichen | Exit 0; Schnappschüsse identisch | |
| A-05 | E2E WFS + CSV | `dcr pipeline run --source geobasis_bb_gemeinden --source inkar_gemeinden` | Exit 0; Daten in den Zieltabellen; alle Geometrien SRID 4326 | |
| A-06 | Bericht korrekt | `reports/latest.json` gegen `docs/report.schema.json`; Zahlen = SQL-Gegenprobe (`sql/beispiele.sql`) | valide, Zahlen stimmen | |
| A-07 | Ersetzen je Quelle | zweiter Lauf wie A-05 (bzw. mit `--full`) | keine Dubletten, andere Quellen unberührt, Bericht 0/0/0 bzw. `unchanged` | |
| A-08 | Fehlertoleranz | in einer Quelle `url` auf eine ungültige Adresse setzen, Lauf | Exit 1; andere Quelle importiert; Fehler mit Ursache im Bericht; Bestand der fehlerhaften Quelle unverändert | |
| A-09 | Zeitsteuerung | `make schedule-test` (Testzeitplan jede Minute) | Lauf erscheint in `pipeline_meta.import_runs` mit `trigger = schedule` | |
| A-10 | Neue Quelle ohne Codeänderung | `config/examples/geobasis_bb_kreise.yaml` nach `config/sources/` kopieren; `validate-config`, `probe`, `run --source geobasis_bb_kreise` | Import erfolgreich (18 Objekte), `git diff -- geoimport/` leer | |
| A-11 | Keine Geheimnisse im Repository | `docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.21.2 detect --source /repo` | keine Funde | |
| A-12 | Lizenzen | `python scripts/gen_third_party.py --check` | Exit 0, nur Open Source | |

Abnahme erteilt am: ______________ durch: ______________
