"""Command registry: every module in this package that exposes the command contract is a command.

Purpose: later plans add a subcommand by adding one module here, so none of them edits cli.py
and parallel plans cannot collide there.
Logic: discover() walks this package with pkgutil, imports each public module by name, requires
NAME, HELP, add_arguments(parser) and run(args) -> Result, and returns the modules sorted by
module name. cli._parser and cli._HANDLERS merge the result with the built-in commands.
Guardrails: only names from pkgutil.iter_modules over this package's own __path__ are imported;
no user path or argv value is ever imported. Modules whose names start with an underscore are
helpers and are skipped. A duplicate NAME, or a NAME that collides with init or status,
raises RuntimeError at startup so the mistake shows up in the first test run.
QA checklist: a new command module sets NAME (lowercase words joined by hyphens), HELP,
add_arguments and run, and gets a test that calls cli.main with its NAME.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from types import ModuleType

RESERVED_NAMES = frozenset({"init", "status"})
_NAME = re.compile(r"[a-z]+(-[a-z]+)*")
_REQUIRED = ("NAME", "HELP", "add_arguments", "run")


def discover() -> list[ModuleType]:
    modules: list[ModuleType] = []
    seen: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        missing = [name for name in _REQUIRED if not hasattr(module, name)]
        if missing:
            raise RuntimeError(f"Command module {info.name} is missing: {', '.join(missing)}")
        name = module.NAME
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RuntimeError(f"Command module {info.name} has an invalid NAME: {name!r}")
        if name in RESERVED_NAMES:
            raise RuntimeError(f"Command module {info.name} uses the reserved name {name!r}")
        if name in seen:
            raise RuntimeError(
                f"Command name {name!r} is used by both {seen[name]} and {info.name}"
            )
        seen[name] = info.name
        modules.append(module)
    return modules
