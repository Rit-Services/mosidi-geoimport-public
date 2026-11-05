"""Exit-Codes der Kommandozeile – an einer Stelle, für Orchestrator, CLI und Monitoring."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit-Codes von ``pipeline run``/``demo`` (0–4 unverändert seit 0.1)."""

    OK = 0  # alle Quellen ok bzw. unverändert
    PARTIAL = 1  # mindestens eine Quelle fehlgeschlagen oder nicht ersetzt
    ALL_FAILED = 2  # alle Quellen fehlgeschlagen
    CONFIG = 3  # Konfiguration oder Zielschema passt nicht
    LOCKED = 4  # anderer Lauf aktiv (Lauf-Sperre belegt)
    DB_UNAVAILABLE = 5  # Datenbank nicht erreichbar oder Verbindung abgerissen
    META_SCHEMA = 6  # pipeline_meta fehlt oder hat eine andere Version → pipeline init-meta
    INTERNAL = 7  # unerwarteter Fehler außerhalb einer Quelle
    INTERRUPTED = 130  # Strg-C (SIGINT)
    TERMINATED = 143  # SIGTERM (z. B. Pod/Container gestoppt)
