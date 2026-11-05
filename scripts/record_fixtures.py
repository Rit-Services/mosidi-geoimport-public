"""Zeichnet kleine, echte Antworten der LGB-Dienste als Test-Fixtures auf.

Aufruf: python scripts/record_fixtures.py
Nur kleine Ausschnitte (Raum Brandenburg an der Havel), gedrosselt; Abrufdatum, URL und
Lizenzvermerk landen in tests/fixtures/README.md.

Die INKAR-Fixture ist synthetisch (gleicher Aufbau wie inkar_2025.csv), solange die
Nutzungsbedingungen für eine Weitergabe im öffentlichen Repository nicht geklärt sind.
"""

from __future__ import annotations

import csv
import io
import random
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
WFS = "https://isk.geobasis-bb.de/ows/vg_wfs"
UA = {"User-Agent": "mosidi-geoimport-fixtures (RIT Services GmbH)"}
CAMPUS = (12.5371959, 52.4107103)  # TH Brandenburg, OSM/Nominatim way 86953877 (29.10.2026)


def bbox_around(lon: float, lat: float, half_m: float) -> tuple[float, float, float, float]:
    x, y = Transformer.from_crs(4326, 25833, always_xy=True).transform(lon, lat)
    return (x - half_m, y - half_m, x + half_m, y + half_m)


def get(client: httpx.Client, **params: str) -> bytes:
    r = client.get(WFS, params={"SERVICE": "WFS", "VERSION": "2.0.0", **params})
    r.raise_for_status()
    time.sleep(0.5)
    return r.content


def main() -> None:
    (FIX / "wfs").mkdir(parents=True, exist_ok=True)
    (FIX / "csv").mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    bbox = bbox_around(*CAMPUS, 6000)
    bbox_param = ",".join(f"{v:.1f}" for v in bbox) + ",urn:ogc:def:crs:EPSG::25833"
    with httpx.Client(timeout=60, headers=UA) as c:
        (FIX / "wfs" / "vg_wfs_capabilities.xml").write_bytes(get(c, REQUEST="GetCapabilities"))
        (FIX / "wfs" / "vg_wfs_describe.xsd").write_bytes(get(c, REQUEST="DescribeFeatureType", TYPENAMES="app:gem"))
        gml = get(c, REQUEST="GetFeature", TYPENAMES="app:gem", SRSNAME="urn:ogc:def:crs:EPSG::25833", BBOX=bbox_param)
        (FIX / "wfs" / "vg_wfs_gem_brandenburg.gml").write_bytes(gml)
        hits = get(c, REQUEST="GetFeature", TYPENAMES="app:gem", RESULTTYPE="hits", BBOX=bbox_param)
        (FIX / "wfs" / "vg_wfs_gem_brandenburg_hits.xml").write_bytes(hits)
    ags = sorted(set(re.findall(rb"<app:ags>(\d+)</app:ags>", gml)))
    names = dict(re.findall(rb"<app:ags>(\d+)</app:ags>.*?<app:gem_name>([^<]+)</app:gem_name>", gml, re.S))
    print(f"{len(ags)} Gemeinden im Ausschnitt: {[a.decode() for a in ags]}")

    # Synthetische INKAR-Datei: gleicher Kopf, gleiche Konventionen (Dezimalkomma, 8-stellige Kennziffer)
    rng = random.Random(42)  # noqa: S311
    indicators = [
        ("LRB", "1001", "bev_dichte", "Einwohnerdichte"),
        ("LRB", "1002", "q_alo", "Arbeitslosenquote"),
        ("ZOM", "2001", "a_bb_4G", "Bevölkerung"),
        ("ZOM", "2001", "a_bb_4G", "Bildung"),
    ]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Bereich", "ID", "Kuerzel", "Indikator", "Raumbezug", "Kennziffer", "Name", "Zeitbezug", "Wert"])
    w.writerow(["EU", "18668", "q_alo", "Arbeitslosenquote", "NUTS0", "DE", "Deutschland", "2021", "3,60"])
    w.writerow(
        ["LRB", "1002", "q_alo", "Arbeitslosenquote", "Kreise", "12051", "Brandenburg an der Havel", "2023", "7,10"]
    )
    w.writerow(
        ["LRB", "1002", "q_alo", "Arbeitslosenquote", "Gemeinden", "01001000", "Flensburg, Stadt", "2023", "8,20"]
    )
    for a in ags:
        for bereich, iid, kurz, name in indicators:
            for year in (2022, 2023):
                value = f"{rng.uniform(1, 500):.2f}".replace(".", ",")
                w.writerow(
                    [bereich, iid, kurz, name, "Gemeinden", a.decode(), names.get(a, b"?").decode(), year, value]
                )
    w.writerow(
        ["LRB", "1001", "bev_dichte", "Einwohnerdichte", "Gemeinden", "12999999", "Unbekannt (Test)", "2023", "1,00"]
    )
    w.writerow(
        [
            "LRB",
            "1001",
            "bev_dichte",
            "Einwohnerdichte",
            "Gemeinden",
            "12051000",
            "Brandenburg an der Havel",
            "2024",
            "n/a",
        ]
    )
    text = buf.getvalue()
    (FIX / "csv" / "inkar_synthetisch.csv").write_text(text, encoding="utf-8")
    with zipfile.ZipFile(FIX / "csv" / "inkar_synthetisch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("inkar_synthetisch.csv", text)
        z.writestr("Readme.txt", "SYNTHETISCHE Testdaten im Aufbau von INKAR 2025 – keine echten Werte.\n")
    (FIX / "csv" / "statistik_cp1252_wide.csv").write_bytes(
        "Gemeindeschlüssel;Gemeinde;2021;2022\n12051000;Brandenburg an der Havel;1.234,5;1.300,0\n"
        "12069017;Beelitz, Stadt;-;98,7\nQuelle: synthetisch\n".encode("cp1252")
    )

    readme = f"""# Test-Fixtures

| Datei | Herkunft | Abgerufen | Lizenz / Vermerk |
|---|---|---|---|
| `wfs/vg_wfs_capabilities.xml` | {WFS} GetCapabilities | {now} | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `wfs/vg_wfs_describe.xsd` | {WFS} DescribeFeatureType app:gem | {now} | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `wfs/vg_wfs_gem_brandenburg.gml` | {WFS} GetFeature app:gem, BBOX {bbox_param} (±6 km um den Campus der TH Brandenburg) | {now} | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `wfs/vg_wfs_gem_brandenburg_hits.xml` | wie oben, RESULTTYPE=hits | {now} | © GeoBasis-DE/LGB, dl-de/by-2-0 |
| `csv/inkar_synthetisch.csv`, `.zip` | **synthetisch**, Aufbau wie INKAR 2025 (`inkar_2025.csv`) | {now} | keine echten Werte |
| `csv/statistik_cp1252_wide.csv` | **synthetisch**, typisches Statistik-Layout (cp1252, Jahre als Spalten, Fußzeile) | {now} | keine echten Werte |

Neu aufzeichnen: `python scripts/record_fixtures.py` (kleine Ausschnitte, gedrosselt).
Die echte INKAR-Datei wird nicht abgelegt (Nutzungsbedingungen für die Weitergabe offen, ca. 434 MB).
"""
    (FIX / "README.md").write_text(readme, encoding="utf-8")


if __name__ == "__main__":
    main()
