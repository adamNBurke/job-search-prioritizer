"""Shared fixtures. No test may touch DNS, the real home folder or a pacing wait.

Name lookups are stubbed and fetch pacing is zeroed for the whole suite. Every test gets a temp
home, so nothing reads or writes the developer's real folders. The uv folders are pinned to
their real locations because the real-shell test runs uv in a subprocess that inherits this
environment: without the pin uv would look for its cache and managed Pythons under the temp
home and download them again. The scan_env fixture builds the one-board scan world the scan tests
share, and folder builds a bare initialized data folder (see tests/helpers.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import types

import pytest
from helpers import LIST_FIXTURE, TODAY, TRACER, Transport, board, write_directory

from jobsearch import cli, clock, directory, fetch

PUBLIC_ADDRESS = "93.184.216.34"


def _real_uv_folder(variable: str, *uv_args: str) -> str | None:
    """The folder uv really uses: the variable if already set, else ask uv (honors uv.toml)."""
    if os.environ.get(variable):
        return os.environ[variable]
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        return None
    try:
        result = subprocess.run(
            [uv, *uv_args], capture_output=True, text=True, encoding="utf-8", check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


# Resolved once at import, before any fixture moves the home. None means "leave it alone".
_UV_FOLDERS = {
    "UV_CACHE_DIR": _real_uv_folder("UV_CACHE_DIR", "cache", "dir"),
    "UV_PYTHON_INSTALL_DIR": _real_uv_folder("UV_PYTHON_INSTALL_DIR", "python", "dir"),
}


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A USER_URL fetch resolves its host first; tests get a fixed public address.

    Tests that care about the answer pass their own `resolver` to fetch.fetch.
    """
    monkeypatch.setattr(fetch, "_system_resolve", lambda host: [PUBLIC_ADDRESS])


@pytest.fixture(autouse=True)
def _fresh_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test waits on fetch pacing. tests/test_fetch.py puts the real interval back.

    Zeroing is needed as well as the reset: some tests fetch one host twice inside one test.
    """
    monkeypatch.setattr(fetch, "MIN_INTERVAL_SECONDS", 0.0)
    fetch._reset_pacing()


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path_factory, monkeypatch):
    """Point every home variable at a temp folder; drop ambient denylist, Claude and git state.

    LOCALAPPDATA is left alone on purpose: uv's Windows cache lives there.
    """
    home = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(home))
    for var in ("JSP_LEAK_DENYLIST", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PLUGIN_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)
    for var, folder in _UV_FOLDERS.items():
        if folder:
            monkeypatch.setenv(var, folder)
    return home


@pytest.fixture
def scan_env(tmp_path, monkeypatch, capsys):
    """The one-board scan world: Acme Robotics on Greenhouse, a frozen clock, a scripted transport.

    Holds folder (an initialized data folder with the tracer config), transport (serves the
    greenhouse list fixture, change transport.body to change the board), capsys, tmp (tmp_path)
    and board_file (the one-entry directory file; rewrite it to change the world).
    """
    board_file = tmp_path / "boards.json"
    write_directory(board_file, [board()])
    monkeypatch.setattr(directory, "DIRECTORY_PATH", board_file)
    monkeypatch.setattr(clock, "now_utc", lambda: TODAY)
    transport = Transport(LIST_FIXTURE.read_bytes())
    monkeypatch.setattr(fetch, "_default_transport", transport)
    folder = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    capsys.readouterr()
    (folder / "config").mkdir(exist_ok=True)
    (folder / "config" / "criteria.json").write_text(
        TRACER.read_text(encoding="utf-8"), encoding="utf-8")
    return types.SimpleNamespace(
        folder=folder, transport=transport, capsys=capsys, tmp=tmp_path, board_file=board_file)


@pytest.fixture
def folder(tmp_path, capsys):
    """An initialized, empty data folder at tmp_path/ws."""
    target = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(target)]) == 0
    capsys.readouterr()
    return target
