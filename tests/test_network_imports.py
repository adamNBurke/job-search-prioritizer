"""Only src/jobsearch/fetch.py can reach the network.

Purpose: the deny and pacing policy lives in one module; a second module that opens a socket
would bypass it. This test fails the build if one appears.
Logic: walk every .py file under src/, scripts/ and tools/ with ast, resolve each import to a
dotted name, and compare it with the forbidden list. A per-file allowlist names the few
sanctioned exceptions. A negative control proves the checker really flags a bad source.
Guardrails: a missing tools/ file is not an error. Dynamic imports
with a literal forbidden name and os.system / os.popen calls are flagged too. This is a
static check: it cannot see a name built at runtime, which is why review still matters.
QA checklist: new network need? Add it to fetch.py, not to the allowlist below.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = (
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
    "socket",
    "ssl",
    "ftplib",
    "smtplib",
    "telnetlib",
    "aiohttp",
    "httpx",
    "webbrowser",
    "subprocess",
)

# Relative posix path -> the forbidden names that file may import.
ALLOWED = {
    "src/jobsearch/fetch.py": {"requests", "socket"},
    "tools/leak_check.py": {"subprocess"},
    "tools/export_public.py": {"subprocess"},
    "tools/install_hooks.py": {"subprocess"},
}

SCAN_DIRS = ("src", "scripts", "tools")


def _forbidden_match(dotted: str) -> str | None:
    for name in FORBIDDEN:
        if dotted == name or dotted.startswith(name + "."):
            return name
    return None


def find_violations(source: str, rel_path: str) -> list[str]:
    """Return one message per problem in `source`, treating it as living at `rel_path`."""
    tree = ast.parse(source)
    allowed = ALLOWED.get(rel_path, set())
    problems: list[str] = []

    def flag(name: str, line: int) -> None:
        if name not in allowed:
            problems.append(f"{rel_path}:{line}: forbidden import of {name}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = _forbidden_match(alias.name)
                if hit:
                    flag(hit, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # relative imports stay inside the package
            module = node.module or ""
            hit = _forbidden_match(module)
            if hit:
                flag(hit, node.lineno)
            for alias in node.names:
                hit = _forbidden_match(f"{module}.{alias.name}")
                if hit:
                    flag(hit, node.lineno)
            if module == "os" and any(a.name in ("system", "popen") for a in node.names):
                problems.append(f"{rel_path}:{node.lineno}: imports os.system or os.popen")
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                rel_path.startswith("src/")
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr in ("system", "popen")
            ):
                problems.append(f"{rel_path}:{node.lineno}: calls os.{func.attr}")
            dynamic = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
                and func.attr == "import_module"
            ) or (isinstance(func, ast.Name) and func.id == "__import__")
            if dynamic and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    hit = _forbidden_match(first.value)
                    if hit:
                        flag(hit, node.lineno)
    return problems


def _python_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if base.is_dir():
            files.extend(sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def test_only_fetch_py_reaches_the_network() -> None:
    problems: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        problems.extend(find_violations(source, rel))
    assert problems == []


def test_scan_actually_covers_the_source_tree() -> None:
    rels = {p.relative_to(ROOT).as_posix() for p in _python_files()}
    assert "src/jobsearch/fetch.py" in rels
    assert "src/jobsearch/netguard.py" in rels
    assert "scripts/jobsearch.py" in rels


def test_fetch_py_really_imports_requests_so_the_allowlist_is_not_vacuous() -> None:
    source = (ROOT / "src/jobsearch/fetch.py").read_text(encoding="utf-8")
    assert "import requests" in source
    # With no allowlist entry the same file would be flagged.
    assert find_violations(source, "src/jobsearch/other.py") != []


def test_fetch_py_allowance_does_not_extend_to_other_network_modules() -> None:
    assert find_violations("import ssl\n", "src/jobsearch/fetch.py") != []
    assert find_violations("import urllib.request\n", "src/jobsearch/fetch.py") != []
    assert find_violations("import requests\n", "src/jobsearch/fetch.py") == []
    # socket is allowed in fetch.py only, for the name lookup that vets a user URL.
    assert find_violations("import socket\n", "src/jobsearch/fetch.py") == []
    assert find_violations("import socket\n", "src/jobsearch/netguard.py") != []


def test_subprocess_is_allowed_only_in_the_listed_tools() -> None:
    assert find_violations("import subprocess\n", "tools/leak_check.py") == []
    assert find_violations("import subprocess\n", "tools/export_public.py") == []
    assert find_violations("import subprocess\n", "tools/install_hooks.py") == []
    assert find_violations("import subprocess\n", "tools/other.py") != []
    assert find_violations("import subprocess\n", "src/jobsearch/paths.py") != []


def test_negative_control_flags_forbidden_imports() -> None:
    source = "import socket\nfrom http import client\n"
    problems = find_violations(source, "src/jobsearch/rogue.py")
    assert len(problems) == 2
    assert any("socket" in p for p in problems)
    assert any("http.client" in p for p in problems)


def test_negative_control_flags_every_import_form() -> None:
    forms = [
        "import requests",
        "import requests.adapters",
        "from requests import get",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "import urllib3",
        "import http.client",
        "from http.client import HTTPSConnection",
        "import ssl",
        "import ftplib",
        "import smtplib",
        "import telnetlib",
        "import aiohttp",
        "import httpx",
        "import webbrowser",
        "import subprocess",
    ]
    for form in forms:
        assert find_violations(form + "\n", "src/jobsearch/rogue.py") != [], form


def test_negative_control_flags_os_system_popen_and_dynamic_imports() -> None:
    assert find_violations("import os\nos.system('x')\n", "src/jobsearch/rogue.py") != []
    assert find_violations("import os\nos.popen('x')\n", "src/jobsearch/rogue.py") != []
    assert find_violations("from os import system\n", "src/jobsearch/rogue.py") != []
    assert find_violations("__import__('socket')\n", "src/jobsearch/rogue.py") != []
    assert find_violations(
        "import importlib\nimportlib.import_module('requests')\n", "src/jobsearch/rogue.py"
    ) != []


def test_harmless_imports_are_not_flagged() -> None:
    source = (
        "import json\nimport threading\nfrom urllib.parse import urlsplit\n"
        "import urllib.parse\nfrom pathlib import Path\nimport importlib\n"
        "importlib.import_module('json')\n"
    )
    assert find_violations(source, "src/jobsearch/ok.py") == []
