"""Every skill line runs as one quoted argument in real shells.

Purpose: a skill line that the lint accepts must also run, after ${...} substitution, from the
plugin's real install location in bash, PowerShell 7 and Windows PowerShell 5.1. The install
location has a deep cache path, and a user's folder may hold a space and non-ASCII characters.
Logic: for two plugin roots (the real cache depth, and a root with a space plus non-ASCII text),
copy the plugin files, substitute the three placeholders into every uv line of every SKILL.md as
forward-slash paths, write each line to its own temp script (UTF-8 for bash, UTF-8 with BOM for
.ps1 so Windows PowerShell 5.1 reads non-ASCII correctly), run it in each available shell, and
require exit 0 and JSON with "ok": true. Skills run in SKILL_RUN_ORDER so init precedes status
and a scan precedes the lines that need one. A listed skill with no SKILL.md yet is skipped, so a
plan can land its skill before the skills that come earlier in the order; a SKILL.md that is not
listed fails the test, so a new skill cannot be forgotten.
After the first init line the harness seeds the data folder from tests/fixtures/shells/seed (its
criteria come from tests/fixtures/criteria/tracer.json, one copy), swaps
the installed board directory for one fictional board, writes that board's job list into the HTTP
cache and runs scan once. Every later line therefore sees scanned data with no network. Each
"{NAME}" token other than {DATA_FOLDER} is read from tests/fixtures/shells/tokens.json.
Guardrails: the real-shell test is marked `shells`; pyproject deselects it by default. The pure
logic tests (order, tokens, seeding) are not marked, so they run in every suite. On Windows under
GitHub Actions a missing shell fails the test; elsewhere a missing pwsh or powershell skips with a
reason. On Windows bash is Git Bash by absolute path, so WSL bash is never picked.
QA checklist: run `uv run --frozen pytest -q -m shells tests/test_skill_shells.py`; local macOS
proves bash only, and the Windows proof is the CI stranger job. A new skill goes in
SKILL_RUN_ORDER, and a token it needs goes in tokens.json (keys sorted).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_skills_lint import SKILLS_DIR, uv_lines

from jobsearch import clock, fetch, letters, paths

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "job-search-prioritizer"
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
STRICT = sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") == "true"
NON_ASCII_ROOT = "Plugin Dir caf" + chr(0xE9) + " " + chr(0x65E5) + chr(0x672C)
NON_ASCII_DATA = "data " + chr(0xE9) + " " + chr(0x65E5) + chr(0x672C)

# Skills run in this order. A name whose skills/<name>/SKILL.md does not exist yet is skipped.
SKILL_RUN_ORDER = ("status", "start", "scan", "review", "letter", "feedback")
FIXTURES_DIR = ROOT / "tests" / "fixtures"
SHELL_FIXTURES = FIXTURES_DIR / "shells"
TOKENS_FILE = SHELL_FIXTURES / "tokens.json"
_TOKEN = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
SCAN_LINE = (
    'uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py" scan '
    '--data-dir "{DATA_FOLDER}" --plugin-data "${CLAUDE_PLUGIN_DATA}"'
)


def _plugin_roots(base: Path) -> dict[str, Path]:
    return {
        "cache-depth": base / "home" / ".claude" / "plugins" / "cache" / PLUGIN_NAME / PLUGIN_NAME / "0.1.0",
        "spaced-non-ascii": base / NON_ASCII_ROOT / PLUGIN_NAME,
    }


def _find_shell(shell: str) -> str | None:
    if shell == "bash":
        if sys.platform == "win32":
            return str(GIT_BASH) if GIT_BASH.is_file() else None
        return shutil.which("bash")
    if shell == "pwsh":
        return shutil.which("pwsh")
    if shell == "powershell":  # Windows PowerShell 5.1 exists only on Windows
        return shutil.which("powershell") if sys.platform == "win32" else None
    raise AssertionError(shell)


def _command(shell: str, exe: str, script: Path) -> list[str]:
    if shell == "bash":
        return [exe, str(script)]
    if shell == "pwsh":
        return [exe, "-NoProfile", "-NonInteractive", "-File", str(script)]
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]


def _install_copy(root: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in (".claude-plugin", "skills", "scripts", "src"):
        shutil.copytree(ROOT / name, root / name, ignore=ignore)


def ordered_skill_files(skills_dir: Path = SKILLS_DIR, order: tuple[str, ...] = SKILL_RUN_ORDER) -> list[Path]:
    """SKILL.md files in run order. A listed skill that is absent is skipped; an unlisted one fails."""
    found = {path.parent.name: path for path in skills_dir.rglob("SKILL.md")}
    unlisted = sorted(set(found) - set(order))
    if unlisted:
        raise AssertionError(
            f"Skill {', '.join(unlisted)} exists but is not in SKILL_RUN_ORDER in tests/test_skill_shells.py")
    return [found[name] for name in order if name in found]


def _skill_lines(skills_dir: Path = SKILLS_DIR, order: tuple[str, ...] = SKILL_RUN_ORDER) -> list[str]:
    lines: list[str] = []
    for skill in ordered_skill_files(skills_dir, order):
        lines.extend(line for _, line in uv_lines(skill.read_text(encoding="utf-8")))
    return lines


def _load_tokens(data_folder: Path, tokens_file: Path = TOKENS_FILE) -> dict[str, str]:
    raw = json.loads(tokens_file.read_text(encoding="utf-8"))
    return {name: value.replace("{DATA_FOLDER}", data_folder.as_posix()) for name, value in raw.items()}


def _substitute(
    line: str, root: Path, plugin_data: Path, data_folder: Path, tokens: dict[str, str] | None = None
) -> str:
    done = (
        line.replace("${CLAUDE_PLUGIN_ROOT}", root.as_posix())
        .replace("${CLAUDE_PLUGIN_DATA}", plugin_data.as_posix())
        .replace("{DATA_FOLDER}", data_folder.as_posix())
    )
    values = tokens if tokens is not None else {}

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise AssertionError(f"Token {{{name}}} has no value in tests/fixtures/shells/tokens.json")
        return values[name]

    return _TOKEN.sub(fill, done)


def _seed_data_folder(data_folder: Path) -> None:
    """Copy the seeded files into a set-up data folder, then the shared tracer criteria as its config."""
    shutil.copytree(SHELL_FIXTURES / "seed", data_folder, dirs_exist_ok=True)
    shutil.copyfile(FIXTURES_DIR / "criteria" / "tracer.json", data_folder / "config" / "criteria.json")


def _seed_cache(data_folder: Path) -> None:
    """Write each fixture board response into the HTTP cache, stamped now so the TTL holds."""
    index = json.loads((SHELL_FIXTURES / "http_index.json").read_text(encoding="utf-8"))
    fetched_at = clock.iso_z(clock.now_utc())
    for url, relative in sorted(index.items()):
        body = (FIXTURES_DIR / relative).read_bytes()
        target = fetch._cache_path(paths.cache_http_dir(data_folder), url)
        mime = "text/html; charset=utf-8" if relative.endswith(".html") else "application/json"
        fetch._write_cache(target, url, 200, {"content-type": mime}, fetched_at, body)


def _seed(root: Path, data_folder: Path) -> None:
    _seed_data_folder(data_folder)
    shutil.copyfile(SHELL_FIXTURES / "boards.json", root / "src" / "jobsearch" / "data" / "boards.json")
    _seed_cache(data_folder)


def _run_line(shell: str, exe: str, command_line: str, script: Path, cwd: Path, env: dict[str, str]) -> dict:
    if shell == "bash":
        script.write_text(command_line + "\n", encoding="utf-8", newline="\n")
    else:
        script.write_text(command_line + "\r\n", encoding="utf-8-sig", newline="")
    done = subprocess.run(
        _command(shell, exe, script),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, f"{script.name} exit {done.returncode}\n{done.stdout}\n{done.stderr}"
    payload = json.loads(done.stdout)
    assert payload.get("ok") is True, f"{script.name} printed {payload}"
    return payload


def test_the_skills_have_lines_to_run() -> None:
    lines = _skill_lines()
    assert len(lines) >= 2
    assert lines[0].split()[4] == "init" and lines[1].split()[4] == "status"


def test_the_test_file_names_the_windows_shell_options() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    assert "Program Files/Git/bin/bash.exe" in source
    assert "-ExecutionPolicy" in source


def _skills_tree(base: Path, names: list[str]) -> Path:
    for name in names:
        (base / name).mkdir(parents=True)
        (base / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    return base


def test_a_listed_skill_with_no_skill_file_yet_is_skipped(tmp_path: Path) -> None:
    tree = _skills_tree(tmp_path / "skills", ["review", "status"])
    ordered = ordered_skill_files(tree, SKILL_RUN_ORDER)
    assert [path.parent.name for path in ordered] == ["status", "review"]


def test_an_existing_skill_missing_from_the_run_order_fails(tmp_path: Path) -> None:
    tree = _skills_tree(tmp_path / "skills", ["status", "review", "surprise"])
    with pytest.raises(AssertionError, match="surprise"):
        ordered_skill_files(tree, SKILL_RUN_ORDER)


def test_every_skill_in_the_repo_is_listed_in_the_run_order() -> None:
    names = {path.parent.name for path in SKILLS_DIR.rglob("SKILL.md")}
    assert names <= set(SKILL_RUN_ORDER)
    assert [path.parent.name for path in ordered_skill_files()][:1] == ["status"]


def test_tokens_file_has_sorted_keys_and_the_review_tokens() -> None:
    raw = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    assert list(raw) == sorted(raw)
    assert {"NOTE_FILE", "ROLE_ID", "VERDICT"} <= set(raw)


def test_substitute_fills_tokens_and_expands_the_data_folder(tmp_path: Path) -> None:
    folder = tmp_path / "jsp"
    tokens = _load_tokens(folder)
    line = 'x --role "{ROLE_ID}" --note-file "{NOTE_FILE}" --data-dir "{DATA_FOLDER}"'
    done = _substitute(line, tmp_path / "root", tmp_path / "pd", folder, tokens)
    assert f'--note-file "{folder.as_posix()}/feedback/note.txt"' in done
    assert '--role "greenhouse:acme-robotics:7000001"' in done
    assert "{" not in done


def test_substitute_names_a_token_that_has_no_value(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="UNKNOWN_TOKEN"):
        _substitute('x "{UNKNOWN_TOKEN}"', tmp_path, tmp_path, tmp_path, {})


FROZEN_NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)  # the tracer tests' date


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch, capsys) -> Path:
    """A seeded, scan-ready data folder that cannot reach the network and runs on a frozen clock."""
    from jobsearch import cli, directory

    def refuse(url, *args, **kwargs):
        raise AssertionError(f"network call: {url}")

    monkeypatch.setattr(directory, "DIRECTORY_PATH", SHELL_FIXTURES / "boards.json")
    monkeypatch.setattr(clock, "now_utc", lambda: FROZEN_NOW)
    monkeypatch.setattr(fetch, "_default_transport", refuse)
    folder = tmp_path / "jsp"
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    _seed_data_folder(folder)
    _seed_cache(folder)
    capsys.readouterr()
    return folder


def test_seeded_state_scans_ranks_and_accepts_the_review_tokens(seeded: Path, capsys) -> None:
    from jobsearch import cli

    folder = seeded
    tokens = _load_tokens(folder)
    for argv in (
        ["scan"],
        ["rank"],
        ["mark", "--role", tokens["ROLE_ID"], "--verdict", tokens["VERDICT"],
         "--note-file", tokens["NOTE_FILE"]],
    ):
        code = cli.main([*argv, "--data-dir", str(folder)])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["ok"] is True, out
        if argv[0] == "scan":
            assert out["counts"]["from_cache"] == 1 and out["counts"]["fetched"] > 0
    lines = (folder / "feedback" / "marks.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["role_id"] for line in lines] == [tokens["ROLE_ID"]]


def test_the_seeded_letter_folder_is_named_by_the_role_key_rule() -> None:
    tokens = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    seeded = SHELL_FIXTURES / "seed" / paths.LAYOUT["applications"] / letters.role_key(tokens["ROLE_ID"])
    assert (seeded / "answers.md").is_file() and (seeded / "draft.md").is_file()


def test_seeded_state_runs_the_letter_lines(seeded: Path, capsys) -> None:
    from jobsearch import cli

    folder = seeded
    role = _load_tokens(folder)["ROLE_ID"]
    for argv in (["scan"], ["rank"], ["letter-start", "--role", role], ["letter-check", "--role", role]):
        code = cli.main([*argv, "--data-dir", str(folder)])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["ok"] is True, out
    assert (folder / letters.letter_rel(role) / letters.LETTER_FILE).is_file()


def test_skill_lines_get_a_temp_home_and_the_real_uv_folders(tmp_path_factory) -> None:
    home = os.environ["HOME"]
    assert os.environ["USERPROFILE"] == home
    assert Path(home).is_relative_to(tmp_path_factory.getbasetemp())
    for name in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"):
        assert os.environ.get(name), f"{name} is not pinned: uv would download again"
        assert not Path(os.environ[name]).is_relative_to(home)


@pytest.mark.shells
@pytest.mark.parametrize("shell", ["bash", "pwsh", "powershell"])
@pytest.mark.parametrize("root_name", ["cache-depth", "spaced-non-ascii"])
def test_every_skill_line_runs_in_a_real_shell(shell: str, root_name: str, tmp_path: Path) -> None:
    exe = _find_shell(shell)
    if exe is None:
        message = f"{shell} is not available on this machine"
        if STRICT:
            pytest.fail(message)
        pytest.skip(message)

    root = _plugin_roots(tmp_path)[root_name]
    _install_copy(root)
    plugin_data = tmp_path / "plugin-data"
    data_folder = tmp_path / NON_ASCII_DATA / "jsp"
    tokens = _load_tokens(data_folder)
    # conftest already pointed the home at a temp folder and pinned uv's real folders
    env = {**os.environ, "UV_NO_PROGRESS": "1", "NO_COLOR": "1"}
    suffix = "sh" if shell == "bash" else "ps1"

    def run(name: str, line: str) -> dict:
        command_line = _substitute(line, root, plugin_data, data_folder, tokens)
        return _run_line(shell, exe, command_line, tmp_path / f"{name}-{shell}.{suffix}", tmp_path, env)

    for index, line in enumerate(_skill_lines()):
        run(f"line{index}", line)
        if index == 0:  # the first line is init: seed the folder and scan once, with no network
            _seed(root, data_folder)
            run("seed-scan", SCAN_LINE)

    marks_file = data_folder / "feedback" / "marks.jsonl"
    written = marks_file.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["role_id"] for line in written] == [tokens["ROLE_ID"]]
