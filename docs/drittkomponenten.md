# Drittkomponenten und Lizenzen

Automatisch erzeugt mit `python scripts/gen_third_party.py`: Paketliste und Versionen aus
`requirements.lock`, Lizenzen aus den Paket-Metadaten (pip-licenses). Nicht von Hand bearbeiten.
`--check` (CI) schlägt fehl, wenn die Datei veraltet ist oder eine Lizenz nicht als Open Source erkannt wird.

Alle Komponenten sind Open Source (LB 7, Bieterantwort 70). Eigener Code: **Apache-2.0**, siehe `LICENSE`
und `NOTICE`. Keine Abhängigkeit steht unter GPL oder AGPL. Unter LGPL stehen psycopg (LGPL-3.0) und GEOS
(LGPL-2.1, im Wheel von shapely): beide werden unverändert als Bibliothek dynamisch genutzt; das ist mit
Apache-2.0 für den eigenen Code vereinbar. PostGIS (GPL-2.0-or-later) ist ein eigenständiger Datenbankdienst,
der weder eingebunden noch mit der Software ausgeliefert wird (siehe unten).

## Python-Pakete (Laufzeit)

| Paket | Version | Lizenz | Projektseite |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.1 | MIT | https://anyio.readthedocs.io/en/stable/versionhistory.html |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click/ |
| colorama | 0.4.6 | BSD License | https://github.com/tartley/colorama |
| fastapi | 0.162.0 | MIT | https://github.com/fastapi/fastapi |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| httpx | 0.28.1 | BSD License | https://github.com/encode/httpx |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| Jinja2 | 3.1.6 | BSD License | https://github.com/pallets/jinja/ |
| markdown-it-py | 4.2.0 | MIT License | https://github.com/executablebooks/markdown-it-py |
| MarkupSafe | 3.0.4 | BSD-3-Clause | https://github.com/pallets/markupsafe/ |
| mdurl | 0.1.2 | MIT License | https://github.com/executablebooks/mdurl |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://numpy.org |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pandas | 2.3.3 | BSD License | https://pandas.pydata.org |
| psycopg | 3.3.6 | LGPL-3.0-only | https://psycopg.org/ |
| psycopg-binary | 3.3.6 | LGPL-3.0-only | https://psycopg.org/ |
| pydantic | 2.14.1 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.50.1 | MIT | https://github.com/pydantic |
| Pygments | 2.21.0 | BSD-2-Clause | https://pygments.org |
| pyogrio | 0.13.0 | MIT License | https://github.com/geopandas/pyogrio |
| pyproj | 3.8.0 | MIT | https://pyproj4.github.io/pyproj/ |
| python-dateutil | 3.0.0 | Apache Software License; BSD License | https://github.com/dateutil/dateutil |
| pytz | 2026.6 | MIT License | http://pythonhosted.org/pytz |
| PyYAML | 6.0.3 | MIT License | https://pyyaml.org/ |
| rich | 15.0.0 | MIT License | https://github.com/Textualize/rich |
| shapely | 2.2.0 | BSD-3-Clause | https://github.com/shapely/shapely |
| shellingham | 1.5.4 | ISC License (ISCL) | https://github.com/sarugaku/shellingham |
| six | 1.17.0 | MIT License | https://github.com/benjaminp/six |
| starlette | 1.10.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| tenacity | 9.2.1 | Apache-2.0 | https://github.com/jd/tenacity |
| typer | 0.27.3 | MIT | https://github.com/fastapi/typer |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tzdata | 2026.6 | Apache-2.0 | https://github.com/python/tzdata |
| uvicorn | 0.54.0 | BSD-3-Clause | https://uvicorn.dev/ |
| zipfile-deflate64 | 0.2.0 | Apache Software License | https://github.com/brianhelba/zipfile-deflate64 |

## Container-Image und eingebettete Bibliotheken

| Komponente | Version | Verwendung | Lizenz |
|---|---|---|---|
| Python (Image python:3.12-slim) | 3.12 | Basis-Image der Pipeline | PSF-2.0; Debian-Pakete des Images unter ihren jeweiligen freien Lizenzen |
| supercronic | v0.2.51 | Zeitsteuerung im Container (Prüfsumme im Dockerfile) | MIT |
| GDAL/OGR | 3.12.4 | im Wheel von pyogrio 0.13.0 enthalten | MIT |
| PROJ | 9.8.1 | im Wheel von pyproj 3.8.0 enthalten | MIT |
| GEOS | 3.14.1 | im Wheel von shapely 2.2.0 enthalten, dynamisch gebunden | LGPL-2.1-or-later |

## Eigenständige Dienste (nicht eingebunden, nicht ausgeliefert)

| Komponente | Version | Verwendung | Lizenz |
|---|---|---|---|
| PostgreSQL | 16 (Image postgis/postgis:16-3.5) | Datenbank; die Pipeline verbindet sich nur über das Netzwerkprotokoll (psycopg) | PostgreSQL License |
| PostGIS | 3.5 (Image postgis/postgis:16-3.5) | Erweiterung im Datenbankdienst der TH bzw. im Compose-Setup; eigenständiger Dienst, nicht eingebunden | GPL-2.0-or-later |

## JavaScript und Schriften (nur Demo-Karte)

| Bibliothek | Version | Verwendung | Lizenz |
|---|---|---|---|
| Leaflet | 1.9.4 | Demo-Karte, lokal unter webmap/static/vendor/leaflet (mit LICENSE) | BSD-2-Clause |
| Open Sans (Fontsource) | 5.3.0 | Schrift der Demo-Karte, lokal unter webmap/static/fonts (mit OFL.txt) | OFL-1.1 |

## Daten (getrennt von der Software)

| Daten | Lizenz / Quellenvermerk |
|---|---|
| Verwaltungsgrenzen Brandenburg (LGB, WFS vg_wfs) | dl-de/by-2-0, „© GeoBasis-DE/LGB, dl-de/by-2-0“ |
| INKAR (BBSR) | Nutzungsbedingungen des BBSR für die Daten sind beim BBSR anzufragen (offener Punkt der Daten, `license` in `config/sources/inkar_gemeinden.yaml`; betrifft nicht die Softwarelizenz) |
| Regionalstatistik, Tabelle 12411-01-01-5-B (Quelle `afs_bb`) | dl-de/by-2-0, „© Statistische Ämter des Bundes und der Länder, Deutschland, 2026“ |
| OpenStreetMap (Kartenhintergrund, Campus-Koordinate) | ODbL, „© OpenStreetMap-Mitwirkende“ |
