# Übergabe-Checkliste

- [ ] Repository nach `github.com/MOSIDI-ORG/` übertragen oder gespiegelt (Weg mit der TH abgestimmt; ein Transfer braucht Admin-Rechte auf beiden Seiten) – A8
- [ ] Release-Tag `v1.0.0`, `CHANGELOG.md`, Docker-Image aus dem Repository baubar
- [ ] `LICENSE` (abgestimmte Lizenz), `NOTICE`, `docs/drittkomponenten.md` aktuell
- [ ] `config/target.yaml` für das echte Zielmodell der TH, `check-target` grün gegen die TH-Datenbank (oder eine Kopie)
- [ ] Alle 3 WFS- und 2 CSV-Quellen konfiguriert, `validate-config` grün, keine `TODO_`-Platzhalter in aktiven Quellen
- [ ] E2E-Protokoll WFS + CSV mit Bericht (`docs/abnahme/e2e_<datum>.md`)
- [ ] Abnahme-Checkliste A-01 … A-12 mit Ergebnis
- [ ] Betriebshandbuch, Entwicklerdoku, „Neue Quelle anbinden“, Datenmodell, Berichtsformat
- [ ] Rollen-SQL an die TH übergeben (wird von der TH ausgeführt, nicht von RIT)
- [ ] Keine Zugangsdaten im Repository oder in der Historie (gitleaks über die gesamte Historie)
- [ ] Zugänge von RIT zu Systemen der TH nach Übergabe entfernt
- [ ] Übergabesitzung durchgeführt, Protokoll im Repository (`docs/uebergabe/protokoll_<datum>.md`)
