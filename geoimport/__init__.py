"""geoimport – konfigurierbare Import-Pipeline für Geodaten (WFS/CSV → PostgreSQL/PostGIS).

Entwickelt von der RIT Services GmbH, Köln.
"""

import os

__version__ = "0.10.0"

# GDAL wählt den Treiber nach dem Inhalt, nicht nach der Dateiendung. Eine als WFS-Seite getarnte OGR-VRT
# könnte sonst lokale Dateien lesen oder interne URLs abrufen (SECURITY_REVIEW S1). GDAL_SKIP wirkt nur,
# wenn es vor dem ersten Import von pyogrio gesetzt ist – deshalb hier im Paketkopf (pyogrio wird nur
# innerhalb von geoimport importiert). Das Dockerfile setzt den Wert zusätzlich.
GDAL_SKIP_DRIVERS = ("OGR_VRT", "GPSBabel", "LIBKML", "KML", "WFS", "OAPIF")


def _merge_gdal_skip(current: str | None) -> str:
    """Vorhandenen ``GDAL_SKIP``-Wert um die gesperrten Treiber ergänzen (kommagetrennt).

    GDAL trennt mit Komma, ohne Komma im Wert mit Leerzeichen (ältere Schreibweise).
    """
    current = (current or "").strip()
    sep = "," if "," in current else None
    names = [n.strip() for n in current.split(sep) if n.strip()]
    return ",".join(names + [d for d in GDAL_SKIP_DRIVERS if d not in names])


os.environ["GDAL_SKIP"] = _merge_gdal_skip(os.environ.get("GDAL_SKIP"))
