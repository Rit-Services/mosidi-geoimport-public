# Test-Fixtures

| Datei | Herkunft | Abgerufen | Lizenz / Vermerk |
|---|---|---|---|
| `wfs/vg_wfs_capabilities.xml` | https://isk.geobasis-bb.de/ows/vg_wfs GetCapabilities | 2026-10-29T15:27:21+01:00 | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `wfs/vg_wfs_describe.xsd` | https://isk.geobasis-bb.de/ows/vg_wfs DescribeFeatureType app:gem | 2026-10-29T15:27:21+01:00 | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `wfs/vg_wfs_gem_brandenburg.gml` | https://isk.geobasis-bb.de/ows/vg_wfs GetFeature app:gem, BBOX 326492.0,5803573.7,338492.0,5815573.7,urn:ogc:def:crs:EPSG::25833 (±6 km um den Campus der TH Brandenburg) | 2026-10-29T15:27:21+01:00 | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `wfs/vg_wfs_gem_brandenburg_hits.xml` | wie oben, RESULTTYPE=hits | 2026-10-29T15:27:21+01:00 | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `csv/inkar_synthetisch.csv`, `.zip` | **synthetisch**, Aufbau wie INKAR 2025 (`inkar_2025.csv`) | 2026-10-29T15:27:21+01:00 | keine echten Werte |
| `csv/afs_synthetisch.csv` | **synthetisch**, Aufbau wie Regionalstatistik-Tabelle 12411-01-01-5-B (ISO-8859-1, 7 Kopfzeilen, Fußnoten ab `__________`) | 2026-11-02 | keine echten Werte |
| `csv/statistik_cp1252_wide.csv` | **synthetisch**, typisches Statistik-Layout (cp1252, Jahre als Spalten, Fußzeile) | 2026-10-29T15:27:21+01:00 | keine echten Werte |
| `csv/statistik_cp1252_sonderzeichen.csv` | **synthetisch**, cp1252 mit Bytes 0x80–0x9F (–, €, „“) und Umlauten, für die Kodierungserkennung | 2026-11-03 | keine echten Werte |
| `wfs/geojson_int_null.json` | **synthetisch**, GeoJSON-FeatureCollection mit Ganzzahl-Schlüssel und `null` | 2026-11-03 | keine echten Werte |

Neu aufzeichnen: `python scripts/record_fixtures.py` (kleine Ausschnitte, gedrosselt).
Die echte INKAR-Datei wird nicht abgelegt (Nutzungsbedingungen für die Weitergabe offen, ca. 434 MB).
