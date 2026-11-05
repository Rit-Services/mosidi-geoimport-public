"""Fixtures für Integrations- und E2E-Tests."""

from __future__ import annotations

import pytest

from geoimport.meta.repository import MetaRepository


@pytest.fixture(autouse=True)
def _meta_schema_auto_init(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein Lauf führt kein DDL mehr aus (``pipeline init-meta`` vorab). Die meisten Tests starten mit
    leerer Datenbank und rufen ``run`` direkt auf – für sie legt die Versionsprüfung das Schema an.

    Tests, die genau das Verhalten ohne ``init-meta`` prüfen, setzen im Modul ``AUTO_INIT_META = False``.
    """
    if not getattr(request.module, "AUTO_INIT_META", True):
        return
    check = MetaRepository.check_schema

    def migrate_then_check(self: MetaRepository) -> None:
        self.migrate()
        check(self)

    monkeypatch.setattr(MetaRepository, "check_schema", migrate_then_check)
