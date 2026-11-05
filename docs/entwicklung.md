# Entwicklung

## Umgebung

```bash
python3.12 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,webmap]"
docker compose -f compose.test.yaml up -d --wait       # PostGIS für Tests (Port 55434, Daten im RAM)
export TEST_DATABASE_URL=postgresql://geoimport:test@127.0.0.1:55434/geoimport_test_demo
pytest                                                 # Unit + Integration + E2E (ohne Internet)
```

Ohne `TEST_DATABASE_URL` laufen nur die Unit-Tests; Datenbanktests werden übersprungen.

| Befehl | Zweck |
|---|---|
| `make test` | alle Tests mit Abdeckung (Ziel ≥ 80 % in `geoimport/`) |
| `make lint` | ruff (inkl. Docstring-Regeln) und mypy |
| `make licenses` | `docs/drittkomponenten.md` neu erzeugen |
| `make schema` | `docs/config.schema.json` und `docs/report.schema.json` exportieren |
| `make acceptance` | Live-Abnahme gegen echte Quellen (Compose-Setup), Protokoll nach `docs/abnahme/` |

## Code-Stil

- Bezeichner englisch; Kommentare, Docstrings, Log- und Fehlermeldungen für Betreiber deutsch.
- Öffentliche Funktionen/Klassen mit Docstring (ruff `D1`); Modulkopf beschreibt den Zweck.
- Keine Zugangsdaten oder Passwort-Standardwerte im Code; Konfiguration nur über YAML und Umgebungsvariablen.
- Zieltabellen nur über `guarded_execute` ansprechen (DDL-Sperre).
- Commits nach Conventional Commits (`feat:`, `fix:`, `docs:`, `test:` …).

## Tests und Fixtures

| Ebene | Ordner | Inhalt |
|---|---|---|
| Unit | `tests/unit/` | Konfiguration, CRS/Achsentausch, Geometrie, Schlüssel, Mapping, Diff, DDL-Sperre, WFS (Capabilities, Paging, ExceptionReport), CSV (Kodierung, ZIP, wide→long), HTTP-Wiederholversuche, Bericht/Schema |
| Integration | `tests/integration/` | Löschen + Ersetzen, andere Quellen unberührt, Rollback, Schutzregeln, Probelauf, Lauf-Sperre, Zielschema-Prüfung, zweites Zielmodell (jsonb, Quellspalte), Lauf-Historie in eigener DB |
| E2E | `tests/e2e/` | drei Läufe über die CLI (neu, 0/0/0, geändert), Exit-Codes, Demo-Modus inkl. Sperren, Karten-API |

Der WFS wird in Tests durch einen lokalen HTTP-Server mit **aufgezeichneten LGB-Antworten** simuliert
(`tests/fixtures/wfs/`). Neu aufzeichnen: `python scripts/record_fixtures.py` (kleiner Ausschnitt, gedrosselt; Datum
und Lizenz landen in `tests/fixtures/README.md`). Die INKAR-Fixture ist synthetisch mit gleichem Aufbau.

## Neuen Quelltyp ergänzen

1. `geoimport/sources/_template.py` kopieren (Beispiel: GeoJSON per HTTP).
2. Konfigurationsmodell von `SourceBase` ableiten, Feld `type: Literal["<typ>"]`.
3. Plugin-Klasse von `Source` ableiten und mit `@register_source("<typ>", Modell)` anmelden:
   - `fetch()` speichert Rohdaten unter `self.ctx.raw_dir` und liefert `FetchResult` (mit `state_updates`, z. B. Hash,
     für die Erkennung unveränderter Quellen),
   - `parse()` liefert `ParseResult`: Objekte über `prepare_features(...)` (Mapping, Schlüssel, EPSG:4326, Prüfung, Hash)
     oder Kennzahlen als DataFrame mit `VALUE_COLUMNS`,
   - `probe()` für `pipeline probe`.
4. Modul in `BUILTIN_MODULES` (`geoimport/sources/__init__.py`) eintragen – oder ohne Codeänderung am Kern über
   `GEOIMPORT_PLUGINS=meinpaket.meinmodul` laden.
5. Unit-Tests mit Fixtures, ein Integrationstest mit `run_pipeline`.

Orchestrator, Ziel-Adapter, Diff und Bericht bleiben unverändert.

## Neuen Ziel-Adapter ergänzen

Für die meisten Modelle genügt `target.yaml` (Adapter `table_mapping`). Für grundlegend andere Modelle:
Klasse mit den Methoden aus `TargetAdapter` (`geoimport/target/adapter.py`): `check`, `count_existing`,
`existing_feature_keys`, `replace`, `preview_sql`; in `make_adapter` eintragen; alle SQL-Aufrufe über
`guarded_execute`. Der Integrationstest `test_generic_adapter_other_model` zeigt, wie ein zweites Modell getestet wird
(Schema-Schnappschuss vor/nach, zweiter Lauf 0/0/0).

## Release

1. `CHANGELOG.md` ergänzen, Version in `pyproject.toml` und `geoimport/__init__.py` erhöhen.
2. `make lint test licenses schema`, Docker-Build.
3. Tag `vX.Y.Z` setzen; das Image wird aus dem Repository gebaut (`docker compose build`).
