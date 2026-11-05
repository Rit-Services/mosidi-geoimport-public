# Neue Quelle anbinden (ohne Codeänderung)

Eine Quelle = eine YAML-Datei in `config/sources/`. Das JSON-Schema für Editor-Unterstützung (Autovervollständigung,
Prüfung beim Tippen) liegt in [`config.schema.json`](config.schema.json).

## Ablauf

1. Quelle untersuchen (Capabilities bzw. Datei ansehen).
2. YAML-Datei anlegen, zunächst mit `enabled: false`.
3. `pipeline validate-config` → Fehler nennen Datei und Feld.
4. `pipeline probe --source <id>` → Erreichbarkeit, FeatureType, CRS, Paging, Trefferzahl, Felder.
5. `enabled: true`, dann `pipeline run --source <id> --dry-run` → Bericht mit Zahlen und SQL-Vorschau, nichts geschrieben.
6. `pipeline run --source <id>` → echter Import, Bericht prüfen.
7. Zeitplan: `schedule` setzen (`daily`, `weekly`, `monthly` oder Cron; mindestens monatlich).

## Gemeinsame Felder

| Feld | Bedeutung |
|---|---|
| `id` | eindeutig, Kleinbuchstaben/Ziffern/`_`; Kennung in Bericht und Lauf-Historie |
| `type` | `wfs` oder `csv` (weitere Typen per Plugin) |
| `enabled` | Quelle mit Platzhaltern (`TODO…`) muss `false` sein (wird geprüft) |
| `title`, `license`, `attribution` | erscheinen in Metadaten, Bericht und Karte |
| `schedule` | Zeitplan: `daily`, `weekly`, `monthly` oder Cron mit 5 Feldern; mindestens ein Lauf je Monat (Bieterantwort V7) |
| `business_key` | **Pflicht.** Fachlicher Schlüssel als Liste von Feldern, z. B. `[ags]` oder zusammengesetzt `[region_key, indicator, period]`. Einzige Grundlage für Verknüpfung und neu/geändert/entfernt; doppelte Schlüssel in einem Lauf werden als fehlerhafte Datensätze protokolliert |
| `key_normalize` | Normalisierung je Schlüsselfeld, z. B. `{ags: {zfill: 8}}` |
| `target.dataset` | Kennung des Datensatzes in der Metadaten-Tabelle; je Quelle eindeutig |
| `guards` | `min_features`/`min_rows`, `max_drop_ratio`, `max_error_ratio`, `required` (bei Kennzahl-Quellen sind Raumbezug, Wert und Zeitbezug aus `record` immer Pflicht, Bieterantwort 63), `geometry_types`, `expected_columns`, `strict_count` (Standard `true`: ohne prüfbare Trefferzahl des WFS bricht der Abruf ab; `false` importiert mit Warnung `count_unverified`) |
| `skip_if_unchanged` | unveränderte Quelle (ETag/Hash) nicht neu schreiben (Standard `true`) |

Zugangsdaten nie in YAML: `${NAME}` setzt eine Umgebungsvariable ein, z. B. `params: {apikey: "${LGB_KEY}"}`.
Token gehören in `params` (WFS: `wfs.params`, CSV: `csv.location.params`), nicht in die URL: Die URL steht in
Bericht, Zustand und Metadaten. Werte von Variablen, deren Name auf `_TOKEN`, `_PASSWORD`, `_SECRET`, `_KEY` o. Ä.
endet, und geheime Parameter (`apikey`, `token`, `signature` …) werden in Logs und Berichten durch `***` ersetzt.

## Beispiel 1: zweiter FeatureType aus dem LGB-WFS (Landkreise)

Vollständig in `config/examples/geobasis_bb_kreise.yaml` (Abnahmeprüfung A-10). Schritte:

```bash
curl "https://isk.geobasis-bb.de/ows/vg_wfs?SERVICE=WFS&REQUEST=GetCapabilities&VERSION=2.0.0"   # FeatureTypes, CRS
docker compose run --rm pipeline probe --source geobasis_bb_kreise                                # nach Anlegen der Datei
```

```yaml
id: geobasis_bb_kreise
type: wfs
title: "Landkreise und kreisfreie Städte Brandenburg (LGB)"
license: "dl-de/by-2-0"
attribution: "© GeoBasis-DE/LGB, dl-de/by-2-0"
wfs:
  url: "https://isk.geobasis-bb.de/ows/vg_wfs"
  type_name: "app:krs"
  request_crs: "EPSG:25833"     # Quell-CRS; die Pipeline transformiert nach EPSG:4326
business_key: ["krss"]              # fachlicher Schlüssel (Pflicht): verknüpft und vergleicht die Läufe
key_normalize: {krss: {zfill: 5}}
mapping:
  geometry: geom
  name: krs_name
  attributes: {krss: krss, kfz: krs_kfz, art: krs_art, aktualitaet: krs_aktualitaet}
target: {dataset: "geobasis_bb_kreise"}
guards: {min_features: 18, geometry_types: [MultiPolygon, Polygon], required: [krss, krs_name]}
```

**WFS-Optionen**

| Feld | Hinweis |
|---|---|
| `version` | `2.0.0` (Standard) oder `1.1.0` (Bieterantwort 67). Bei 1.1.0 nutzt die Pipeline `TYPENAME`, `MAXFEATURES`, `SRSNAME=EPSG:<code>` und die Trefferzahl aus `numberOfFeatures` (`RESULTTYPE=hits`). `STARTINDEX` ist in 1.1.0 kein Standard, sondern eine verbreitete Erweiterung (z. B. GeoServer): Für Paging `paging.strategy: startindex` setzen. Ignoriert ein Dienst `STARTINDEX`, bricht der Abruf ab („Seite wiederholt eine frühere Seite“). Sonst `bbox_tiles` nutzen. Abgerufen wird immer sequentiell mit `throttle_ms` Pause (Antwort 69). |
| `request_crs` | leer = Default-CRS aus GetCapabilities. Möglichst ein projiziertes CRS anfordern (Achsenreihenfolge eindeutig) |
| `output_format` | `auto` wählt GML 3.2 (alle Felder als Text, führende Nullen bleiben); GeoJSON nur, wenn kein GML angeboten wird |
| `paging.strategy` | `auto` wertet `ImplementsResultPaging`/`CountDefault` aus; `startindex` (COUNT/STARTINDEX), `bbox_tiles` (Kacheln, `tile_size_m`), `none` (ein Request). Geblättert wird bis zu einer leeren Seite bzw. bis die Zahl **eindeutiger** Objekte die Trefferzahl (`RESULTTYPE=hits`) erreicht; kommt eine Objekt-ID (`gml:id`) auf einer späteren Seite erneut, gilt der Abruf als unvollständig; doppelte fachliche Schlüssel mit verschiedenen IDs sind Datensatzfehler (`duplicate_key`) |
| `sort_by` | Sortierfeld für das Blättern (`SORTBY`). Ohne Angabe sortiert die Pipeline nach dem ersten Feld von `business_key`, sofern der Dienst Sortierung unterstützt; sonst Warnung `unsorted_paging` |
| `bbox` | Ausschnitt `[minx, miny, maxx, maxy]` in `request_crs` |
| `paging.max_pages` | Obergrenze der Seiten **und** der Kacheln bei `bbox_tiles`; die Kachelzahl wird vor der ersten Anfrage geprüft (Rahmen auf die Plausibilitäts-BBOX zugeschnitten) |
| `filter` | einfache Gleichheitsfilter `{feld: wert}` oder OGC-Filter-XML |
| `mapping.attributes` | Zielname ← Quellfeld; Ausdrücke `{field: x, zfill: 8}`, `{field: x, to_int: true}` (keine Abrundung: „3,9“ ist ein Fehler; optional `decimal`/`thousands`, bei CSV-Quellen von `csv.decimal`/`csv.thousands` übernommen), `{field: d, to_date: "%d.%m.%Y"}`, `{concat: [a, {const: "-"}, b]}`, `{const: wert}`. Ohne `attributes` werden alle Felder übernommen |

## Beispiel 2: CSV mit Jahren als Spalten (Statistik-Layout)

```yaml
id: statistik_bevoelkerung
type: csv
title: "Bevölkerung je Gemeinde"
license: "<Lizenz der Quelle>"
csv:
  location: {url: "https://…/bevoelkerung.csv"}     # oder path: /data/inbox/bevoelkerung.csv
  encoding: auto                                    # UTF-8 (mit/ohne BOM), sonst cp1252 (Warnung `encoding`) – besser fest eintragen
  delimiter: ";"
  decimal: ","
  thousands: "."
  skip_rows: 3          # Kopfzeilen über der Spaltenzeile
  footer_rows: 2        # Fußnoten
  na_values: ["-", ".", "x", "…"]
reshape:
  wide_to_long:
    id_columns: ["Gemeindeschlüssel", "Gemeinde"]
    var_name: period
    value_name: value
    indicator: "Bevölkerung"
columns:
  "Gemeindeschlüssel": {to: region_key, normalize: {zfill: 8}}
  value: {to: value, type: float}
business_key: [region_key, indicator, period]
geometry_join: {source: geobasis_bb_gemeinden, key: ags, column: region_key, on_missing: warn}
target: {dataset: "statistik_bevoelkerung"}
guards: {min_rows: 400, required: [region_key]}
```

**CSV-Optionen**

| Feld | Hinweis |
|---|---|
| `location` | genau eines: `url`, `path` oder `url_template` mit `{year}` + `year: latest` (sucht rückwärts per HEAD) |
| `location.params` | zusätzliche Abfrageparameter, z. B. `{token: "${INKAR_TOKEN}"}`; werden angehängt, aber nicht mit der URL gespeichert |
| `archive.member_glob` | Datei im ZIP (muss genau eine treffen); GZ wird automatisch entpackt |
| `columns` | CSV-Spalte → `{to, type: str\|int\|float\|date, normalize, keep, date_format}`; `keep: false` = nicht speichern. Bei `type: date` ist `date_format` Pflicht. Zahlen: Bei `decimal: ","` ist ein Punkt nur als Tausendertrenner erlaubt (`thousands: "."`), sonst ist der Wert ungültig („1.234“ wird nicht still zu 1,234) |
| `row_filter` | `{spalte: wert}` oder `{spalte: [w1, w2]}` |
| `region_filter` | `{column, prefix}`, z. B. Brandenburg `prefix: "12"` |
| `record` | welche Zielfelder Raumbezug, Name, Wert, Zeitbezug sind (Standard `region_key`, `indicator`, `value`, `period`) |
| `coordinates` | `{x, y, crs}` → Punktobjekte statt Kennzahlen |
| `geometry_join` | prüft, ob jeder Raumbezug eine Geometrie hat (`warn`, `drop`, `error`) |
| `limits` | Schutz vor Archivbomben und Dateien ohne Zeilenumbruch: `max_line_bytes` (Standard 1 MiB), `max_uncompressed_bytes` (16 GiB, entpackt), `max_compression_ratio` (ZIP/GZ: höchstens das 100-fache der Archivgröße, mindestens 256 MiB; `null` = aus). INKAR 2025: 6,8 GB entpackt, Verhältnis 15,6. Überschreitung → `aborted_incomplete`, Bestand unverändert |

Amtliche Schlüssel immer als Text behandeln und mit `zfill` normalisieren (AGS 8, ARS 12, Kreis 5 Stellen) –
eine Umwandlung in Zahlen entfernt führende Nullen.

## Checkliste

- [ ] `validate-config` ohne Fehler, Hinweise gelesen
- [ ] `probe` zeigt den erwarteten FeatureType bzw. die Datei
- [ ] Schlüssel eindeutig (Dry-Run-Bericht: keine `duplicate_key`-Fehler)
- [ ] `min_features`/`min_rows` aus einem echten Abruf festgelegt (mit Datum im Kommentar)
- [ ] Lizenz und Quellenvermerk eingetragen
- [ ] Dry-Run-Bericht plausibel, danach echter Lauf

## Typische Fehler

| Meldung | Lösung |
|---|---|
| `unbekanntes Feld (Tippfehler?)` | Feldname prüfen (Schema `config.schema.json`) |
| `Quelle enthält Platzhalter … enabled: false` | Platzhalter ersetzen oder Quelle deaktiviert lassen |
| `FeatureType '…' nicht in GetCapabilities` | Namen inkl. Präfix aus den Capabilities übernehmen |
| `Gemappte Felder fehlen im Dienst` | Feldnamen aus `probe` (Abschnitt `felder`) übernehmen |
| `Koordinaten liegen nur mit vertauschten Achsen …` | `request_crs` setzen (projiziertes CRS) |
| `Spalten fehlen in …` | Spaltennamen/`skip_rows`/`delimiter` prüfen |
| `Schlüssel mehrfach geliefert` | Schlüssel um weitere Felder ergänzen (z. B. INKAR: Bereich und Kürzel) |
