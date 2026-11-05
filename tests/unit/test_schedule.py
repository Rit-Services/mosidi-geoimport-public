"""Zeitplan je Quelle (Bieterantwort V7: konfigurierbar, höchstens monatlich)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from geoimport.config.loader import ConfigError, load_config
from geoimport.schedule import ScheduleError, cron_expression, render_crontab
from tests.conftest import CONFIG, load_yaml, write_yaml


@pytest.mark.parametrize(
    ("schedule", "cron"),
    [
        ("daily", "0 2 * * *"),
        ("weekly", "0 3 * * 1"),
        ("Monthly", "0 4 1 * *"),
        ("30 5 */2 * *", "30 5 */2 * *"),
        ("0 6 1,15 * *", "0 6 1,15 * *"),
        ("0 3 31 * 1", "0 3 31 * 1"),  # Tag ODER Montag: läuft jede Woche
    ],
)
def test_gueltige_zeitplaene(schedule: str, cron: str) -> None:
    assert cron_expression(schedule) == cron


@pytest.mark.parametrize(
    ("schedule", "msg"),
    [
        ("yearly", "5 Feldern"),
        ("0 4 1 1 *", "nicht in jedem Monat"),  # jährlich
        ("0 4 1 */3 *", "nicht in jedem Monat"),  # vierteljährlich
        ("0 4 31 * *", "nicht in jedem Monat"),
        ("0 25 * * *", "außerhalb"),
        ("0 3 * * mo", "ungültig"),
    ],
)
def test_seltener_als_monatlich_oder_ungueltig(schedule: str, msg: str) -> None:
    with pytest.raises(ScheduleError, match=msg):
        cron_expression(schedule)


def test_crontab_aus_konfiguration() -> None:
    text = render_crontab(load_config(CONFIG).sources)
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert lines == [
        "0 4 1 * *  pipeline run --trigger schedule --source afs_bb --source inkar_gemeinden",
        "0 3 * * 1  pipeline run --trigger schedule --source geobasis_bb_gemeinden",
    ]


def test_deaktivierte_quellen_fehlen_in_der_crontab() -> None:
    src = [
        SimpleNamespace(id="a", enabled=False, schedule="daily"),
        SimpleNamespace(id="b", enabled=True, schedule="daily"),
    ]
    assert "--source a" not in render_crontab(src) and "--source b" in render_crontab(src)


def test_konfiguration_lehnt_jaehrlich_ab(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    write_yaml(cfg / "target.yaml", load_yaml(CONFIG / "target.demo.yaml"))
    src = load_yaml(CONFIG / "sources" / "geobasis_bb_gemeinden.yaml")
    src["schedule"] = "0 4 1 1 *"
    write_yaml(cfg / "sources" / "s.yaml", src)
    with pytest.raises(ConfigError, match="schedule.*nicht in jedem Monat"):
        load_config(cfg)
