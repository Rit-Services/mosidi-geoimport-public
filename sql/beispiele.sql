-- Beispielabfragen auf dem Demo-Referenzmodell (Schema ref) und der Lauf-Historie (pipeline_meta).
-- Für das Zielmodell der TH die Tabellen-/Spaltennamen aus config/target.yaml einsetzen.
-- Geometrien liegen in EPSG:4326; metrische Werte über geography (Meter, m²).

-- 1) Gemeinde als Polygon mit Fläche in km²
SELECT g.objekt_schluessel AS ags, g.name,
       round((ST_Area(g.geom::geography) / 1e6)::numeric, 2) AS flaeche_km2
FROM ref.geometrien g JOIN ref.metadaten m ON m.id = g.metadaten_id
WHERE m.datensatz = 'geobasis_bb_gemeinden'
ORDER BY flaeche_km2 DESC LIMIT 10;

-- 2) Umkreissuche: Gemeinden im Umkreis von 2 km um den Campus der TH Brandenburg
--    (Koordinate: OSM/Nominatim, abgefragt 29.10.2026)
WITH p AS (SELECT ST_SetSRID(ST_MakePoint(12.5371959, 52.4107103), 4326)::geography AS pt)
SELECT g.objekt_schluessel, g.name, round(ST_Distance(g.geom::geography, p.pt)) AS entfernung_m
FROM ref.geometrien g, p
WHERE ST_DWithin(g.geom::geography, p.pt, 2000)
ORDER BY entfernung_m;

-- 3) Kennzahl je Gemeinde (WFS-Geometrie + CSV-Kennzahl über den AGS)
SELECT g.objekt_schluessel AS ags, g.name, a.wert::numeric AS arbeitslosenquote
FROM ref.geometrien g
JOIN ref.metadaten mg ON mg.id = g.metadaten_id AND mg.datensatz = 'geobasis_bb_gemeinden'
JOIN ref.attribute a ON a.objekt_schluessel = g.objekt_schluessel AND a.name = 'Arbeitslosenquote' AND a.zeitbezug = '2023'
JOIN ref.metadaten ma ON ma.id = a.metadaten_id AND ma.datensatz = 'inkar_gemeinden'
ORDER BY arbeitslosenquote DESC NULLS LAST LIMIT 10;

-- 4) Letzte Läufe mit Kennzahlen
SELECT started_at, trigger, status, simulated, report->'summary' AS summary
FROM pipeline_meta.import_runs ORDER BY started_at DESC LIMIT 10;

-- 5) Was hat sich im letzten erfolgreichen Lauf einer Quelle geändert?
SELECT d.change, count(*)
FROM pipeline_meta.diff_keys d
WHERE d.run_id = (SELECT r.run_id FROM pipeline_meta.import_source_results r JOIN pipeline_meta.import_runs u USING (run_id)
                  WHERE r.source_id = 'geobasis_bb_gemeinden' AND r.status = 'ok' ORDER BY u.started_at DESC LIMIT 1)
GROUP BY 1;

-- 6) Fehlerhafte Datensätze des letzten Laufs
SELECT source_id, record_key, reason_code, reason
FROM pipeline_meta.import_errors
WHERE run_id = (SELECT run_id FROM pipeline_meta.import_runs ORDER BY started_at DESC LIMIT 1);
