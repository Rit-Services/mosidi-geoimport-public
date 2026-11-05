"""Live-Abnahme gegen die echten Quellen über das Docker-Compose-Setup (spiegelt den Test der TH, LB 11).

Aufruf (im Repository, .env gesetzt, ``docker compose up -d db`` gelaufen):

    python scripts/acceptance.py [--sources geobasis_bb_gemeinden,inkar_gemeinden]

Schritte: validate-config, init-meta, check-target mit Schema-Schnappschuss vorher/nachher, Lauf,
zweiter Lauf (Idempotenz), SQL-Gegenprobe der Berichtszahlen. Protokoll: docs/abnahme/e2e_<datum>.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose(*args: str, check: bool = False) -> tuple[int, str, float]:
    t0 = time.monotonic()
    p = subprocess.run(  # noqa: S603
        ["docker", "compose", "run", "--rm", "-e", "LOG_FORMAT=text", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # Statuszeilen von Docker Compose ("Container … Created") gehören nicht ins Protokoll
    lines = (p.stdout + p.stderr).splitlines()
    out = "\n".join(line for line in lines if not line.strip().startswith("Container ")).strip()
    if check and p.returncode:
        raise SystemExit(f"Fehlgeschlagen: {' '.join(args)}\n{out}")
    return p.returncode, out, time.monotonic() - t0


def psql(query: str) -> str:
    p = subprocess.run(  # noqa: S603
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$0"',
            query,
        ],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return p.stdout.strip()


def last_report() -> dict:
    p = subprocess.run(  # noqa: S603
        ["docker", "compose", "run", "--rm", "pipeline", "report", "--format", "json"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(p.stdout[p.stdout.index("{") :])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="geobasis_bb_gemeinden,inkar_gemeinden")
    args = ap.parse_args()
    sources = [s for s in args.sources.split(",") if s]
    src_args = [x for s in sources for x in ("--source", s)]
    lines: list[str] = []
    rows: list[tuple[str, str, str, str]] = []

    def check(nr: str, name: str, ok: bool, detail: str) -> None:
        rows.append((nr, name, "erfüllt" if ok else "NICHT erfüllt", detail))

    code, out, _ = compose("pipeline", "validate-config")
    check("A-03", "Konfiguration gültig", code == 0, out.splitlines()[0] if out else "")
    compose("pipeline", "init-meta", check=True)
    code, out, _ = compose("pipeline", "check-target")
    check("A-04a", "Zielschema passt", code == 0, out.splitlines()[-1] if out else "")
    snap_before = psql(
        "SELECT md5(string_agg(table_name||column_name||data_type, ',' ORDER BY 1)) FROM information_schema.columns WHERE table_schema='ref'"
    )

    code, out, secs = compose("pipeline", "run", *src_args, "--full")
    rep = last_report()
    lines += ["## Lauf 1", "", "```", out[-3000:], "```", ""]
    check("A-05", "E2E WFS + CSV", code == 0, f"Exit {code}, {secs:.0f} s, Status {rep['run']['status']}")
    srid = psql("SELECT count(*) FILTER (WHERE ST_SRID(geom) <> 4326) FROM ref.geometrien")
    check("A-05b", "SRID 4326", srid == "0", f"Geometrien mit anderem SRID: {srid}")
    ok_counts = True
    detail = []
    for s in rep["sources"]:
        n_geo = psql(
            f"SELECT count(*) FROM ref.geometrien g JOIN ref.metadaten m ON m.id=g.metadaten_id WHERE m.datensatz='{s['dataset']}'"
        )
        n_attr = psql(
            f"SELECT count(*) FROM ref.attribute a JOIN ref.metadaten m ON m.id=a.metadaten_id WHERE m.datensatz='{s['dataset']}'"
        )
        expected = s["valid"] if s["type"] == "csv" else s["valid"]
        actual = int(n_attr) if s["type"] == "csv" else int(n_geo)
        ok_counts &= actual == expected
        detail.append(f"{s['id']}: Bericht gültig {expected}, Datenbank {actual}")
    check("A-06", "Bericht korrekt (SQL-Gegenprobe)", ok_counts, "; ".join(detail))

    # Zweiter Lauf ohne --full: unveränderte Quellen werden per ETag/Hash erkannt (kein erneuter 434-MB-Download)
    code, out, secs = compose("pipeline", "run", *src_args)
    rep2 = last_report()
    zero = all((s["added"], s["changed"], s["deleted"]) == (0, 0, 0) for s in rep2["sources"])
    check(
        "A-07",
        "Ersetzen je Quelle, zweiter Lauf 0/0/0",
        code == 0 and zero,
        f"{secs:.0f} s; "
        + "; ".join(f"{s['id']}: {s['added']}/{s['changed']}/{s['deleted']}" for s in rep2["sources"]),
    )
    snap_after = psql(
        "SELECT md5(string_agg(table_name||column_name||data_type, ',' ORDER BY 1)) FROM information_schema.columns WHERE table_schema='ref'"
    )
    check(
        "A-04b",
        "Zielschema unverändert",
        snap_before == snap_after,
        f"md5 vorher {snap_before[:12]} / nachher {snap_after[:12]}",
    )

    date = datetime.now().strftime("%Y-%m-%d")
    out_path = ROOT / "docs" / "abnahme" / f"e2e_{date}.md"
    head = [
        f"# Live-Abnahmelauf {date}",
        "",
        f"Erzeugt mit `python scripts/acceptance.py` am {datetime.now().astimezone():%d.%m.%Y %H:%M %Z}, "
        f"Quellen: {', '.join(sources)}. Zielmodell: Demo-Referenzmodell (Annahme von RIT, nicht das Modell der TH).",
        "",
        "| Nr. | Prüfung | Ergebnis | Nachweis |",
        "|---|---|---|---|",
        *[f"| {a} | {b} | {c} | {d.replace('|', '/')} |" for a, b, c, d in rows],
        "",
        "## Kennzahlen Lauf 1",
        "",
        "| Quelle | Status | geladen | gültig | Fehler | neu | geändert | gelöscht | Dauer (s) |",
        "|---|---|--:|--:|--:|--:|--:|--:|--:|",
        *[
            f"| {s['id']} | {s['status']} | {s['fetched']} | {s['valid']} | {s['invalid']} | {s['added']} | {s['changed']} | {s['deleted']} | {s['duration_s']} |"
            for s in rep["sources"]
        ],
        "",
    ]
    out_path.write_text("\n".join(head + lines), encoding="utf-8")
    print(out_path)
    for r in rows:
        print(" | ".join(r))


if __name__ == "__main__":
    main()
