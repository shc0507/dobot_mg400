"""Console workspace plug-ins.

Each module in this package defines ``WORKSPACE``. The console finds the
modules itself, so a workspace never edits a shared list. A workspace owns:

- ``workspaces/<id>.py``: this declaration and an optional ``register`` hook;
- ``static/console/<id>.js`` and ``static/console/<id>.css``.

``register(blueprint, service)`` adds routes to a blueprint mounted at
``<prefix>/api/console/ws/<id>``.
"""

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Callable

_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class Workspace:
    id: str
    order: int
    title: dict = field(default_factory=dict)
    register: Callable | None = None
    script: bool = True
    style: bool = True

    def __post_init__(self):
        if not _ID.fullmatch(self.id):
            raise ValueError("Workspace ids use 1 to 32 lowercase letters, digits or -.")
        if not all(isinstance(self.title.get(lang), str) for lang in ("en", "zh")):
            raise ValueError("A workspace needs an English and a Chinese title.")


def discover():
    found = []
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_"):
            continue
        value = getattr(importlib.import_module(f"{__name__}.{module.name}"), "WORKSPACE", None)
        if isinstance(value, Workspace):
            found.append(value)
    ids = [item.id for item in found]
    if len(ids) != len(set(ids)) or "overview" not in ids:
        raise ValueError("Workspace ids must be unique and include overview.")
    return sorted(found, key=lambda item: (item.order, item.id))
