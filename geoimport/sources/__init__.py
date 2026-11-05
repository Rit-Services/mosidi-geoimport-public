"""Registry der Quelltypen.

Ein neuer Quelltyp besteht aus einem Pydantic-Konfigurationsmodell (Feld ``type`` als Literal)
und einer Plugin-Klasse, die mit ``@register_source("typ", Modell)`` angemeldet wird.
Vorlage: ``geoimport/sources/_template.py``; Anleitung: ``docs/entwicklung.md``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from geoimport.sources.base import Source

_REGISTRY: dict[str, tuple[type[BaseModel], type[Source]]] = {}

S = TypeVar("S", bound=type[Source])

# Module mit eingebauten Quelltypen; weitere per Umgebungsvariable GEOIMPORT_PLUGINS (kommagetrennt)
BUILTIN_MODULES = ["geoimport.sources.wfs", "geoimport.sources.csv_source"]


def register_source(kind: str, config_model: type[BaseModel]) -> Callable[[S], S]:
    """Dekorator: meldet eine Plugin-Klasse für ``type: <kind>`` an."""

    def deco(cls: S) -> S:
        if kind in _REGISTRY and _REGISTRY[kind][1] is not cls:
            raise ValueError(f"Quelltyp '{kind}' ist bereits registriert")
        cls.kind = kind
        _REGISTRY[kind] = (config_model, cls)
        return cls

    return deco


def load_plugins() -> None:
    """Importiert eingebaute und zusätzlich konfigurierte Plugin-Module."""
    import os

    extra = [m.strip() for m in os.environ.get("GEOIMPORT_PLUGINS", "").split(",") if m.strip()]
    for module in BUILTIN_MODULES + extra:
        importlib.import_module(module)


def source_class(kind: str) -> type[Source]:
    """Plugin-Klasse zum Quelltyp."""
    load_plugins()
    try:
        return _REGISTRY[kind][1]
    except KeyError:
        raise KeyError(f"Unbekannter Quelltyp '{kind}' (bekannt: {', '.join(sorted(_REGISTRY))})") from None


def config_models() -> list[type[BaseModel]]:
    """Konfigurationsmodelle aller registrierten Quelltypen."""
    load_plugins()
    return [model for model, _ in _REGISTRY.values()]


def registered_kinds() -> list[str]:
    """Namen aller registrierten Quelltypen."""
    load_plugins()
    return sorted(_REGISTRY)
