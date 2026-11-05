"""Menschenlesbarer Import-Bericht (Markdown, deutsch).

Texte von außen (Fehlermeldungen der Server, Schlüssel, Gründe) werden entschärft: Steuerzeichen
(Terminal-Escapes, OSC-8-Links) entfernt und Markdown-/HTML-Zeichen maskiert, damit ein Server keine
Links, Bilder oder HTML in ``latest.md`` und die Ausgabe von ``pipeline report`` schleusen kann.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from jinja2 import Environment

from geoimport.sources.base import clean_text

STATUS_TEXT = {
    "ok": "erfolgreich",
    "partial": "teilweise erfolgreich",
    "failed": "fehlgeschlagen",
    "aborted_incomplete": "abgebrochen, Abruf unvollständig (Bestand unverändert)",
    "running": "läuft",
    "skipped_guard": "nicht ersetzt (Schutzregel)",
    "unchanged": "unverändert",
    "dry_run": "Probelauf (nichts geschrieben)",
}

TEMPLATE = """\
# {% if run.simulated %}SIMULATION – {% endif %}Import-Bericht {{ run.started_at | dt }}

{% if run.simulated -%}
> **SIMULATION – keine echten Änderungen der Quelle.** Die Daten dieses Laufs wurden im Demo-Modus
> gezielt verändert, um die Änderungserkennung vorzuführen.

{% endif -%}
{% if run.dry_run -%}
> **Probelauf (`--dry-run`):** Es wurde nichts in die Zieltabellen geschrieben.

{% endif -%}
| | |
|---|---|
| Lauf-ID | `{{ run.run_id }}` |
| Status | **{{ run.status | st }}** |
| Auslöser | {{ run.trigger }} |
| Beginn / Ende | {{ run.started_at | dt }} / {{ run.finished_at | dt }} |
| Laufzeit | {{ run.duration_s | num }} s |
| Version / Konfiguration | {{ run.app_version }} / `{{ run.config_hash[:19] }}` |

## Quellen

| Quelle | Typ | Status | geladen | gültig | Fehler | neu | geändert | gelöscht | unverändert | Dauer (s) |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
{% for s in sources -%}
| {{ s.title | md }} (`{{ s.id }}`) | {{ s.type }} | {{ s.status | st }} | {{ s.fetched | num }} | {{ s.valid | num }} | {{ s.invalid | num }} | {{ s.added | num }} | {{ s.changed | num }} | {{ s.deleted | num }} | {{ s.unchanged | num }} | {{ s.duration_s | num }} |
{% endfor %}
{%- if sources | selectattr("compared", "equalto", false) | list %}

„–“ bei geändert/unverändert: Der Zielbestand dieser Quelle ließ sich nicht mit der Lieferung vergleichen
(siehe Warnung `diff_from_baseline`), und es gab keinen früheren Lauf als Ersatz. Der Bestand wurde ersetzt.
{%- endif %}

## Datenqualität

| Quelle | Geometrie repariert | Geometrie übersprungen | Pflichtfeld fehlt |
|---|--:|--:|--:|
{% for s in sources if s.quality -%}
| `{{ s.id }}` | {{ s.quality.geometry_repaired }} | {{ s.quality.geometry_skipped }} | {{ s.quality.missing_required_field }} |
{% endfor %}
## Nicht erreichbare oder fehlgeschlagene Quellen
{% set failed = sources | selectattr("status", "in", ["failed", "aborted_incomplete"]) | list %}
{% if failed -%}
{% for s in failed -%}
- **{{ s.title | md }}** (`{{ s.id }}`): {{ s.error.message | md }}{% if s.error.http_status %} (HTTP {{ s.error.http_status }}){% endif %}{% if s.error.url %} – {{ s.error.url | md }}{% endif %}. {% if s.data_unchanged is false %}Bestand der Quelle wurde ersetzt.{% else %}Bestand der Quelle unverändert.{% endif %}
{% endfor %}
{%- else -%}
Keine.
{% endif %}
## Warnungen
{% set ns = namespace(n=0) -%}
{% for w in warnings -%}
{% set ns.n = ns.n + 1 -%}
- {{ w.message | md }} (`{{ w.code }}`)
{% endfor -%}
{% for s in sources -%}
{% for w in s.warnings -%}
{% set ns.n = ns.n + 1 -%}
- `{{ s.id }}`: {{ w.message | md }} (`{{ w.code }}`)
{% endfor -%}
{% endfor -%}
{% if ns.n == 0 %}Keine.
{% endif %}
{%- set with_errors = sources | selectattr("error_examples") | list %}
{% if with_errors %}
## Beispiele fehlerhafter Datensätze
{% for s in with_errors %}
**{{ s.id }}** ({{ s.invalid }} insgesamt, vollständige Liste in `pipeline_meta.import_errors`):

| Schlüssel | Grund |
|---|---|
{% for e in s.error_examples -%}
| {{ (e.record_key | md) or "–" }} | {{ e.reason | md }} |
{% endfor %}
{% endfor %}
{%- endif %}

---
Quellenvermerke: {% for s in sources if s.attribution %}{{ s.attribution | md }}{% if not loop.last %}; {% endif %}{% endfor %}

Erzeugt von mosidi-geoimport {{ run.app_version }} (RIT Services GmbH, Köln).
"""


def _dt(value: str | None) -> str:
    if not value:
        return "–"
    return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M:%S %z")


def _num(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if isinstance(value, int):
        return f"{value:,}".replace(",", ".")
    return str(value)


# Zeichen mit Bedeutung in Markdown/HTML (Links, Bilder, Hervorhebung, Code, HTML-Tags)
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>!~&#])")


def _md(value: Any) -> str:
    """Fremdtext für Markdown: Steuerzeichen raus, Sonderzeichen mit Backslash maskiert."""
    if value is None:
        return ""
    return _MD_SPECIAL_RE.sub(r"\\\1", clean_text(value))


_env = Environment(autoescape=False, trim_blocks=False, lstrip_blocks=False)  # noqa: S701 – Markdown, kein HTML
_env.filters["dt"] = _dt
_env.filters["num"] = _num
_env.filters["st"] = lambda s: STATUS_TEXT.get(s, s)
_env.filters["md"] = _md
_template = _env.from_string(TEMPLATE)


def _md_safe(value: Any) -> Any:
    """Steuerzeichen, Tabellen-Trennzeichen und Zeilenumbrüche in allen Texten entschärfen."""
    if isinstance(value, str):
        return clean_text(value, 100_000).replace("|", "/").replace("\n", " ").replace("\t", " ")
    if isinstance(value, dict):
        return {k: _md_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_md_safe(v) for v in value]
    return value


def render_markdown(report: dict[str, Any]) -> str:
    """Bericht als Markdown."""
    data = _md_safe(report)
    return _template.render(run=data["run"], sources=data["sources"], warnings=data.get("warnings", []))
