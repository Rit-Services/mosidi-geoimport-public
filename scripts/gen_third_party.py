"""Erzeugt docs/drittkomponenten.md aus den Paket-Metadaten (keine Lizenzangaben von Hand).

Aufruf: python scripts/gen_third_party.py [--check]
--check: Exit 1, wenn die Datei veraltet ist oder eine Lizenz nicht als Open Source erkannt wird.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "drittkomponenten.md"

LOCK = ROOT / "requirements.lock"
# Name → (Lizenz, Projektseite) für Pakete, die requirements.lock nur für eine Plattform pinnt
PLATFORM_ONLY = {"colorama": ("BSD License", "https://github.com/tartley/colorama")}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_packages() -> dict[str, str]:
    """Laufzeit-Abhängigkeiten laut requirements.lock (Name → gepinnte Version)."""
    out = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


OSS = (
    "MIT",
    "BSD",
    "Apache",
    "PSF",
    "Python Software Foundation",
    "ISC",
    "MPL",
    "Mozilla",
    "LGPL",
    "Unlicense",
    "Zope",
    "HPND",
    "zlib",
    "CC0",
    "Public Domain",
    "0BSD",
)

# (Komponente, Version, Verwendung, Lizenz)
IMAGES = [
    (
        "Python (Image python:3.12-slim)",
        "3.12",
        "Basis-Image der Pipeline",
        "PSF-2.0; Debian-Pakete des Images unter ihren jeweiligen freien Lizenzen",
    ),
    ("supercronic", "v0.2.51", "Zeitsteuerung im Container (Prüfsumme im Dockerfile)", "MIT"),
    ("GDAL/OGR", "3.12.4", "im Wheel von pyogrio 0.13.0 enthalten", "MIT"),
    ("PROJ", "9.8.1", "im Wheel von pyproj 3.8.0 enthalten", "MIT"),
    ("GEOS", "3.14.1", "im Wheel von shapely 2.2.0 enthalten, dynamisch gebunden", "LGPL-2.1-or-later"),
]
# Eigenständige Dienste: werden nicht eingebunden, verlinkt oder mit der Software ausgeliefert
SERVICES = [
    (
        "PostgreSQL",
        "16 (Image postgis/postgis:16-3.5)",
        "Datenbank; die Pipeline verbindet sich nur über das Netzwerkprotokoll (psycopg)",
        "PostgreSQL License",
    ),
    (
        "PostGIS",
        "3.5 (Image postgis/postgis:16-3.5)",
        "Erweiterung im Datenbankdienst der TH bzw. im Compose-Setup; eigenständiger Dienst, nicht eingebunden",
        "GPL-2.0-or-later",
    ),
]
JS = [
    ("Leaflet", "1.9.4", "Demo-Karte, lokal unter webmap/static/vendor/leaflet (mit LICENSE)", "BSD-2-Clause"),
    (
        "Open Sans (Fontsource)",
        "5.3.0",
        "Schrift der Demo-Karte, lokal unter webmap/static/fonts (mit OFL.txt)",
        "OFL-1.1",
    ),
]


def packages() -> list[dict[str, str]]:
    raw = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-urls", "--from=mixed"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    wanted = runtime_packages()
    rows = [r for r in json.loads(raw) if _norm(r["Name"]) in wanted]
    missing = sorted(set(wanted) - {_norm(r["Name"]) for r in rows})
    # Pakete mit Plattform-Marker (z. B. nur Windows) fehlen auf anderen Systemen; feste Angaben aus den
    # Paket-Metadaten halten die Datei auf allen Plattformen gleich.
    for n in [n for n in missing if n in PLATFORM_ONLY]:
        lic, url = PLATFORM_ONLY[n]
        rows.append({"Name": n, "Version": wanted[n], "License": lic, "URL": url})
        missing.remove(n)
    if missing:
        print("Nicht installiert (Lizenz unbekannt): " + ", ".join(missing))
        rows += [
            {"Name": n, "Version": wanted[n], "License": "UNKNOWN (nicht installiert)", "URL": ""} for n in missing
        ]
    for r in rows:
        pinned = wanted.get(_norm(r["Name"]))
        if pinned and pinned != r["Version"]:
            print(
                f"Hinweis: {r['Name']} installiert {r['Version']}, gepinnt {pinned} – Tabelle nutzt die gepinnte Version"
            )
            r["Version"] = pinned
    return sorted(rows, key=lambda r: r["Name"].lower())


def render(rows: list[dict[str, str]]) -> str:
    lines = [
        "# Drittkomponenten und Lizenzen",
        "",
        "Automatisch erzeugt mit `python scripts/gen_third_party.py`: Paketliste und Versionen aus",
        "`requirements.lock`, Lizenzen aus den Paket-Metadaten (pip-licenses). Nicht von Hand bearbeiten.",
        "`--check` (CI) schlägt fehl, wenn die Datei veraltet ist oder eine Lizenz nicht als Open Source erkannt wird.",
        "",
        "Alle Komponenten sind Open Source (LB 7, Bieterantwort 70). Eigener Code: **Apache-2.0**, siehe `LICENSE`",
        "und `NOTICE`. Keine Abhängigkeit steht unter GPL oder AGPL. Unter LGPL stehen psycopg (LGPL-3.0) und GEOS",
        "(LGPL-2.1, im Wheel von shapely): beide werden unverändert als Bibliothek dynamisch genutzt; das ist mit",
        "Apache-2.0 für den eigenen Code vereinbar. PostGIS (GPL-2.0-or-later) ist ein eigenständiger Datenbankdienst,",
        "der weder eingebunden noch mit der Software ausgeliefert wird (siehe unten).",
        "",
        "## Python-Pakete (Laufzeit)",
        "",
        "| Paket | Version | Lizenz | Projektseite |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lic = r["License"]
        flag = "" if any(o.lower() in lic.lower() for o in OSS) else " **nicht als Open Source erkannt**"
        lines.append(f"| {r['Name']} | {r['Version']} | {lic}{flag} | {r.get('URL', '')} |")
    lines += [
        "",
        "## Container-Image und eingebettete Bibliotheken",
        "",
        "| Komponente | Version | Verwendung | Lizenz |",
        "|---|---|---|---|",
    ]
    lines += [f"| {a} | {v} | {b} | {c} |" for a, v, b, c in IMAGES]
    lines += [
        "",
        "## Eigenständige Dienste (nicht eingebunden, nicht ausgeliefert)",
        "",
        "| Komponente | Version | Verwendung | Lizenz |",
        "|---|---|---|---|",
    ]
    lines += [f"| {a} | {v} | {b} | {c} |" for a, v, b, c in SERVICES]
    lines += [
        "",
        "## JavaScript und Schriften (nur Demo-Karte)",
        "",
        "| Bibliothek | Version | Verwendung | Lizenz |",
        "|---|---|---|---|",
    ]
    lines += [f"| {a} | {v} | {b} | {c} |" for a, v, b, c in JS]
    lines += [
        "",
        "## Daten (getrennt von der Software)",
        "",
        "| Daten | Lizenz / Quellenvermerk |",
        "|---|---|",
        "| Verwaltungsgrenzen Brandenburg (LGB, WFS vg_wfs) | dl-de/by-2-0, „© GeoBasis-DE/LGB, dl-de/by-2-0“ |",
        "| INKAR (BBSR) | Nutzungsbedingungen des BBSR für die Daten sind beim BBSR anzufragen (offener Punkt der Daten, "
        "`license` in `config/sources/inkar_gemeinden.yaml`; betrifft nicht die Softwarelizenz) |",
        "| Regionalstatistik, Tabelle 12411-01-01-5-B (Quelle `afs_bb`) | dl-de/by-2-0, „© Statistische Ämter des "
        "Bundes und der Länder, Deutschland, 2026“ |",
        "| OpenStreetMap (Kartenhintergrund, Campus-Koordinate) | ODbL, „© OpenStreetMap-Mitwirkende“ |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    rows = packages()
    text = render(rows)
    unclear = [r["Name"] for r in rows if not any(o.lower() in r["License"].lower() for o in OSS)]
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("docs/drittkomponenten.md ist veraltet – python scripts/gen_third_party.py ausführen")
            return 1
        if unclear:
            print("Lizenz nicht als Open Source erkannt: " + ", ".join(unclear))
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"{OUT} geschrieben ({len(rows)} Pakete; prüfen: {', '.join(unclear) or 'keine'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
