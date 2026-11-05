"""Überführt das Spaltenmapping der Vorarbeit (MOSIDI ``inkar_import/config.xml``) in YAML ``columns:``.

Aufruf: python scripts/xml_mapping_to_yaml.py pfad/zu/config.xml [abschnitt]
Standardabschnitt: indicator. Ausgabe auf stdout zum Einfügen in config/sources/<id>.yaml.

SQL-Typen werden auf die Pipeline-Typen abgebildet (int → int, VARCHAR/text → str, numeric → float).
Hinweis: In der Vorarbeit ist ``Kennziffer`` integer – das entfernt führende Nullen. Das Skript
macht daraus bewusst ``str`` mit ``zfill: 8``.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # noqa: S405 – lokale Datei aus dem Repository der TH

import yaml

TYPE_MAP = {"int": "int", "integer": "int", "numeric": "float", "double precision": "float", "real": "float"}


def convert(path: str, section: str = "indicator") -> dict[str, dict[str, object]]:
    root = ET.parse(path).getroot()  # noqa: S314
    out: dict[str, dict[str, object]] = {}
    for sec in root.iter(section):
        for m in sec.iter("mapping"):
            csv_name = (m.findtext("csv") or "").strip()
            sql_name = (m.findtext("sql/name") or "").strip()
            sql_type = (m.findtext("sql/type") or "").strip().lower()
            if not csv_name:
                continue
            spec: dict[str, object] = {"to": sql_name or csv_name.lower(), "type": TYPE_MAP.get(sql_type, "str")}
            if csv_name == "Kennziffer":
                spec = {"to": "region_key", "type": "str", "normalize": {"zfill": 8}}
            if csv_name == "Wert":
                spec["type"] = "float"
            out[csv_name] = spec
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    print(yaml.safe_dump({"columns": convert(*sys.argv[1:3])}, allow_unicode=True, sort_keys=False))
