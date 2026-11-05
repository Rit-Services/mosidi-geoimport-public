"""Zeitplan je Quelle (Bieterantwort V7: Intervalle konfigurierbar, höchstens monatlich).

``schedule`` ist ein Kürzel (``daily``, ``weekly``, ``monthly``) oder ein Cron-Ausdruck mit fünf Feldern
(Minute Stunde Tag Monat Wochentag, Zeitzone ``TZ``). Die Crontab für supercronic wird beim Start des
Schedulers aus der Konfiguration erzeugt (``pipeline crontab``), nicht fest ins Image geschrieben.

Auslegung von „höchstens monatlich“: Der Abstand zwischen zwei Läufen ist höchstens ein Monat, jede Quelle
läuft also mindestens einmal in jedem Monat. Seltenere Zeitpläne (z. B. jährlich oder nur am 31.) werden
bei der Prüfung der Konfiguration abgelehnt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

KEYWORDS = {
    "daily": "0 2 * * *",
    "weekly": "0 3 * * 1",
    "monthly": "0 4 1 * *",
}
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["Minute", "Stunde", "Tag", "Monat", "Wochentag"]
_ITEM = re.compile(r"^(\*|\d+(-\d+)?)(/\d+)?$")
# Jeder Monat hat mindestens 28 Tage: Ein Lauf an einem späteren Tag fiele im Februar aus
_SAFE_DAY = 28


class ScheduleError(ValueError):
    """Zeitplan ungültig oder seltener als monatlich."""


def cron_expression(schedule: str) -> str:
    """Kürzel in einen Cron-Ausdruck übersetzen und den Ausdruck prüfen."""
    expr = KEYWORDS.get(schedule.strip().lower(), schedule.strip())
    fields = expr.split()
    if len(fields) != 5:
        raise ScheduleError(f"Zeitplan '{schedule}': daily, weekly, monthly oder Cron mit 5 Feldern erwartet")
    for value, (lo, hi), name in zip(fields, _RANGES, _NAMES, strict=True):
        _values(value, lo, hi, name, schedule)
    _check_at_least_monthly(fields, schedule)
    return " ".join(fields)


def _values(field: str, lo: int, hi: int, name: str, schedule: str) -> set[int]:
    out: set[int] = set()
    for item in field.split(","):
        if not _ITEM.match(item):
            raise ScheduleError(f"Zeitplan '{schedule}': Feld {name} '{field}' ungültig")
        base, _, step = item.partition("/")
        if base == "*":
            start, end = lo, hi
        else:
            start_s, _, end_s = base.partition("-")
            start, end = int(start_s), int(end_s or start_s)
        if not (lo <= start <= end <= hi):
            raise ScheduleError(f"Zeitplan '{schedule}': Feld {name} '{field}' außerhalb {lo}–{hi}")
        out.update(range(start, end + 1, int(step or 1)))
    return out


def _check_at_least_monthly(fields: list[str], schedule: str) -> None:
    _, _, dom, month, dow = fields
    if _values(month, 1, 12, "Monat", schedule) != set(range(1, 13)):
        raise ScheduleError(f"Zeitplan '{schedule}' läuft nicht in jedem Monat (höchstens monatlich gefordert)")
    if dow != "*":
        return  # Cron: Tag ODER Wochentag – ein Wochentag kommt in jedem Monat vor
    if dom != "*" and min(_values(dom, 1, 31, "Tag", schedule)) > _SAFE_DAY:
        raise ScheduleError(f"Zeitplan '{schedule}': Tag {dom} gibt es nicht in jedem Monat – Tag 1–{_SAFE_DAY} wählen")


def render_crontab(sources: Iterable[Any]) -> str:
    """Crontab für supercronic: eine Zeile je Zeitplan, Quellen mit gleichem Zeitplan in einem Lauf.

    Ein gemeinsamer Lauf statt mehrerer gleichzeitiger, weil die Lauf-Sperre parallele Läufe abweist.
    """
    groups: dict[str, list[str]] = {}
    for src in sources:
        if src.enabled:
            groups.setdefault(cron_expression(src.schedule), []).append(src.id)
    lines = [
        "# Erzeugt von 'pipeline crontab' aus config/sources/*.yaml (Feld schedule) – nicht von Hand bearbeiten.",
        "# Zeitzone über TZ im Container (Standard Europe/Berlin).",
    ]
    for expr, ids in sorted(groups.items(), key=lambda kv: kv[1]):
        args = " ".join(f"--source {i}" for i in ids)
        lines.append(f"{expr}  pipeline run --trigger schedule {args}")
    return "\n".join(lines) + "\n"
