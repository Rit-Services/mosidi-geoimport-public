# Import-Bericht

Nach **jedem** Lauf (auch bei Teil- oder Totalausfall) erzeugt die Pipeline automatisch:

| Datei / Ort | Inhalt |
|---|---|
| `reports/<JJJJ>/<JJJJ-MM-TT>_<run_id>.json` | maschinenlesbar, Schema [`report.schema.json`](report.schema.json) |
| `reports/<JJJJ>/<JJJJ-MM-TT>_<run_id>.md` | menschenlesbar (deutsch) |
| `reports/latest.json`, `reports/latest.md` | Kopie des letzten Laufs |
| `pipeline_meta.import_runs.report` | Kopie des JSON in der Datenbank |

`pipeline report [--run ID] [--format md|json]` zeigt einen Bericht an.
Das Format (Version `schema_version: "1.0"`) ist ein **Vorschlag zur Abstimmung mit dem Auftraggeber** (LB 4.5).

## Inhalt (LB 4.5)

| Anforderung | Feld |
|---|---|
| verarbeitete Quellen mit Zeitstempel | `sources[].started_at`, `finished_at`; `run.started_at` (ISO 8601 mit Offset, Europe/Berlin) |
| nicht erreichbare/fehlgeschlagene Quellen inkl. Ursache | `sources[].status = "failed"`, `error.kind`, `error.message`, `error.http_status`, `error.url` |
| Anzahl neu/geändert/gelöscht je Quelle | `sources[].added`, `changed`, `deleted`, zusätzlich `unchanged` |
| Laufzeit | `run.duration_s`, `sources[].duration_s` |
| Warnungen (z. B. Schemaabweichungen) | `warnings[]` (Lauf) und `sources[].warnings[]` mit `code`, `message`, `data` |
| Datenqualität je Quelle (Bieterantwort 63) | `sources[].quality`: `geometry_repaired`, `geometry_skipped`, `missing_required_field` |

## Datenqualität (Bieterantwort 63)

Jede Quelle hat drei Zähler in `sources[].quality`. Ist ein Zähler größer als 0, steht zusätzlich eine Warnung
mit demselben Code in `sources[].warnings[]`, mit `data.count` und bis zu 20 Schlüsseln in `data.examples`.

| Code | Bedeutung | Datensatz |
|---|---|---|
| `geometry_repaired` | ungültige Geometrie repariert (`make_valid` bzw. `ST_MakeValid`), Z/M auf 2D reduziert | übernommen |
| `geometry_skipped` | Geometrie fehlt, ist leer, nicht reparierbar oder hat einen nicht erlaubten Typ | übersprungen, in `pipeline_meta.import_errors` |
| `missing_required_field` | Pflichtfeld leer: Schlüssel, Geometrie bzw. Raumbezug, Zeitbezug, Wert | übersprungen, in `pipeline_meta.import_errors` |

Die Zähler sind vollständig, auch wenn mehr Fehler auftreten, als einzeln gespeichert werden (10.000 je Quelle).
Der Markdown-Bericht zeigt sie im Abschnitt „Datenqualität“.

## Statuswerte

| Lauf (`run.status`) | Bedeutung | Exit-Code |
|---|---|---|
| `ok` | alle Quellen erfolgreich oder unverändert | 0 |
| `partial` | mindestens eine Quelle fehlgeschlagen oder wegen Schutzregel nicht ersetzt | 1 |
| `aborted_incomplete` | Teilabbruch: mindestens eine Quelle wurde nicht vollständig oder nicht fehlerfrei abgerufen; ihre Transaktion wurde nicht begonnen bzw. zurückgerollt, der Bestand ist unverändert (Bieterantwort V5) | 1 (2, wenn alle Quellen betroffen) |
| `failed` | alle Quellen fehlgeschlagen | 2 |
| `failed` | Konfigurations- oder Zielschemafehler, vor dem ersten Schreiben abgebrochen | 3 |
| – | anderer Lauf aktiv, kein Bericht (zeitgesteuerte Läufe: Webhook mit Status `locked`) | 4 |
| `failed` | Datenbank nicht erreichbar oder Verbindung verloren | 5 |
| `failed` | `pipeline_meta` fehlt oder falsche Version (`init-meta` ausführen) | 6 |
| `failed` | interner Fehler bzw. Abbruch (SIGTERM 143, Strg-C 130); nicht bearbeitete Quellen mit `error.kind` `run_aborted` | 7 |

| Quelle (`sources[].status`) | Bedeutung | Bestand |
|---|---|---|
| `ok` | ersetzt | neu geschrieben |
| `unchanged` | Quelle seit dem letzten erfolgreichen Lauf unverändert (ETag/Last-Modified/SHA-256) | unverändert |
| `skipped_guard` | Schutzregel verletzt (zu wenige Datensätze, starker Rückgang) | unverändert |
| `aborted_incomplete` | Abruf unvollständig oder fehlerhaft: Netz-/HTTP-Fehler, Abbruch auf einer Folgeseite, abgeschnittene oder unlesbare Datei, Fehlerquote über `max_error_ratio` | unverändert |
| `failed` | Konfigurationsfehler, Schemaabweichung, Datei fehlt, Datenbankfehler (Transaktion zurückgerollt) | unverändert |
| `dry_run` | Probelauf: alles außer Schreiben, mit SQL-Vorschau (`preview_sql`) | unverändert |

Zählwerte (`added`/`changed`/`deleted`/`unchanged`) stehen nur bei Quellen, deren Bestand geschrieben bzw. im
Probelauf berechnet wurde (`ok`, `dry_run`, `unchanged`); sonst sind sie `null`, die berechneten Werte stehen
in `info.not_applied`. Nur diese Quellen gehen in `summary` ein. `invalid_existing`: Schlüssel, die bisher im
Bestand waren, deren neuer Datensatz aber ungültig ist – sie zählen nicht als „gelöscht“.

`deleted` heißt „in der Lieferung entfallen“. Bei Objekt-Quellen (WFS) werden dabei nur die Attribute entfernt;
die Geometrie bleibt erhalten (Bieterantwort 74, Hinweis `geometry_retained`). `rows_deleted`, `rows_inserted`
und `rows_updated` zählen die physisch geschriebenen Zeilen, je Tabelle in `info.rows_*_by_table`.

**Vergleichsbasis (Bieterantwort 62):** Neu, geändert und entfallen werden gegen die **aktuell vorhandenen
Zieldaten** des Datensatzes ermittelt, nicht gegen einen früheren Lauf. Der erste Lauf gegen einen befüllten
Bestand liefert deshalb korrekte Zahlen. Verglichen wird genau das, was die Pipeline schreibt: Geometrie
(auf 7 Nachkommastellen gerundet, Einzel- und Multi-Geometrie gleich), Spalten mit Vorlagen je Datensatz und die
Attributzeilen, jeweils normalisiert (`1.50` = `1.5`, JSON unabhängig von Reihenfolge und Leerzeichen). Bei
Kennzahlen wird der fachliche Schlüssel aus den Zielspalten zusammengesetzt (INKAR: Kennziffer, Bereich, Kürzel,
Indikator, Zeitbezug). `pipeline_meta.diff_keys` bleibt das Protokoll je Lauf (Änderungskarte).

Lässt sich der Bestand nicht auf die Lieferung abbilden (z. B. Attribute ohne Bezug auf den Datensatz), wird mit
dem letzten Lauf verglichen (Warnung `diff_from_baseline`). Nur wenn es auch den nicht gibt, steht
`compared: false`, `changed`/`unchanged` sind dann `null`, der Bestand wurde ersetzt.

Eine Geometrie, die schon in einem früheren Lauf entfallen ist und nur noch erhalten bleibt (ohne eigene
Attribute), zählt nicht erneut als entfallen. Liefert die Quelle das Gebiet wieder, zählt es als geändert.

Laufzeit: Bei INKAR (ca. 1,3 Mio. Zeilen) kostet der Vergleich mit dem Bestand etwa eine Minute zusätzlich.

## Fehlerarten (`error.kind`)

`http`, `timeout`, `network` (nach allen Wiederholversuchen) · `ogc_exception` (ExceptionReport, auch bei HTTP 200) ·
`featuretype_missing` · `schema_mismatch` (gemapptes Feld/Spalte fehlt) · `incomplete` (weniger Objekte als
`RESULTTYPE=hits`) · `possibly_truncated` (Antwort erreicht `CountDefault`) · `paging` · `parse` · `archive` ·
`not_found` · `file_missing` · `too_many_errors` · `database` (Transaktion zurückgerollt) · `target_schema` · `config` · `internal` ·
`too_large` (Antwort oder Download über der Grenze aus `http`) · `tls` (Zertifikatsprüfung fehlgeschlagen, nicht wiederholt) ·
`insecure_redirect` (Umleitung von https auf http abgelehnt) · `ownership` (Metadatenzeile gehört nicht der Pipeline) · `run_aborted` (Lauf abgebrochen, bevor die Quelle bearbeitet wurde)

## Warnungen (`code`)

`schema_drift` (Felder/Spalten neu, entfallen, Typ geändert) · `unmatched_keys` (Kennzahlen ohne passende Geometrie,
mit Anzahl und bis zu 20 Beispielen) · `no_baseline` (nur im Rückfall) · `guard` / `guard_forced` · `count_mismatch` ·
`possibly_truncated` · `schema_check_skipped` · `target_schema` · `simulation` · `config` (offene Angaben, z. B. Lizenz) ·
`count_unverified` (Trefferzahl des WFS nicht prüfbar, nur bei `strict_count: false`) · `unsorted_paging` · `encoding`
(Kodierung automatisch erkannt) · `geometry_retained` (Geometrien fehlen in der Lieferung und bleiben erhalten, mit
Anzahl und bis zu 20 Schlüsseln) · `diff_from_baseline` (Bestand nicht abbildbar, Vergleich mit dem letzten
Lauf) · `diff_keys_mismatch` (kein gemeinsamer Schlüssel von Lieferung und Bestand – Abbildung prüfen) ·
`duplicate_existing_keys` (Schlüssel im Bestand mehrfach) · `refresh_failed` (materialisierte Sicht nach dem
Import nicht aktualisiert, Lauf-Warnung; aktualisierte Sichten stehen in `run.refreshed_views`) · `geometry_repaired` · `geometry_skipped` · `missing_required_field` (siehe
„Datenqualität“) · `invalid_existing` · `after_commit` / `state_write_failed` (Problem nach dem
Schreiben; der Bestand **wurde** ersetzt) · `stale_runs` · `run_aborted` · `dataset_adopted` · `ownership_backfilled`

Text aus den Quellen (Fehlermeldungen, Feldnamen) wird in Berichten von Steuerzeichen bereinigt, gekürzt und im
Markdown-Bericht maskiert.

## Beispiel (illustrativ)

```json
{
  "schema_version": "1.0",
  "run": {"run_id": "…", "trigger": "schedule", "simulated": false, "dry_run": false,
          "started_at": "2026-11-20T03:00:00+01:00", "finished_at": "2026-11-20T03:04:12+01:00",
          "duration_s": 252.4, "status": "partial", "app_version": "1.0.0", "config_hash": "sha256:…"},
  "summary": {"sources_total": 2, "ok": 1, "failed": 1, "skipped": 0, "unchanged": 0,
              "added": 0, "changed": 2, "deleted": 0, "errors": 0, "warnings": 1},
  "warnings": [],
  "sources": [
    {"id": "geobasis_bb_gemeinden", "type": "wfs", "status": "ok", "fetched": 413, "valid": 413, "invalid": 0,
     "added": 0, "changed": 2, "deleted": 0, "unchanged": 411, "duration_s": 6.1,
     "warnings": [{"code": "schema_drift", "message": "Feld 'gem_aktualitaet' Typ geändert: string → date"}]},
    {"id": "inkar_gemeinden", "type": "csv", "status": "failed", "data_unchanged": true,
     "error": {"kind": "http", "http_status": 503, "message": "HTTP 503 nach 5 Versuchen",
               "url": "https://www.bbr-server.de/imagemap/inkar/download/inkar_2025.zip"}}
  ]
}
```
(Werte illustrativ, Felder gekürzt.)
