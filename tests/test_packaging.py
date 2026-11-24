"""The plugin manifest, marketplace entry and dependency pins.

Purpose: a stranger's install must resolve the same name, the same version and the same three
runtime packages every time, on every OS. These facts are settled, so tests hold them.
Logic: validate_manifest and validate_marketplace return rule ids for the name rules (present,
ASCII, lowercase-hyphen pattern, no reserved prefix on the NFKC-casefolded form). Other tests read
the real files: version lives only in plugin.json, no email anywhere in it, the PEP 723 block in
scripts/jobsearch.py names exactly requests, pypdf and python-docx with == pins that equal the dev
group in pyproject.toml, and git tracks no bin/ directory, *.sh file or package.json.
Guardrails: the reserved-prefix check normalizes first, so "Claude-x" and the fullwidth spelling
of "claude-x" are caught. An empty or missing name fails.
QA checklist: bumped a dependency? Change the PEP 723 block, pyproject.toml and both locks
together. Renamed the plugin? That is a settled decision; stop and ask.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
SHIM = ROOT / "scripts" / "jobsearch.py"
PYPROJECT = ROOT / "pyproject.toml"

PLUGIN_NAME = "job-search-prioritizer"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MARKETPLACE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RESERVED_PREFIXES = ("claude-", "anthropic-")
RESERVED_MARKETPLACE_NAMES = {"uv", "pip", "npm", "github", "gh", "cargo"}
RUNTIME_PACKAGES = {"requests", "pypdf", "python-docx"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _reserved_prefix(name: str) -> bool:
    folded = unicodedata.normalize("NFKC", name).casefold()
    return folded.startswith(RESERVED_PREFIXES)


def validate_manifest(manifest: dict) -> list[str]:
    """Rule ids for the plugin.json name and version. Empty list means valid."""
    problems: list[str] = []
    name = manifest.get("name")
    if name is None:
        return ["name-missing"]
    if not isinstance(name, str) or not name.strip():
        return ["name-empty"]
    if not name.isascii():
        problems.append("name-non-ascii")
    if not NAME_PATTERN.fullmatch(name):
        problems.append("name-pattern")
    if _reserved_prefix(name):
        problems.append("name-reserved-prefix")
    version = manifest.get("version")
    if version is not None and not re.fullmatch(r"\d+\.\d+\.\d+", str(version)):
        problems.append("version-pattern")
    return problems


def validate_marketplace(marketplace: dict, plugin_name: str) -> list[str]:
    """Rule ids for marketplace.json. Empty list means valid."""
    problems: list[str] = []
    name = marketplace.get("name")
    if not isinstance(name, str) or not name:
        return ["marketplace-name-missing"]
    if not MARKETPLACE_PATTERN.fullmatch(name):
        problems.append("marketplace-name-pattern")
    if name.casefold() in RESERVED_MARKETPLACE_NAMES:
        problems.append("marketplace-name-reserved")
    if _reserved_prefix(name):
        problems.append("marketplace-name-reserved-prefix")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        problems.append("marketplace-plugins-missing")
    elif plugins[0].get("name") != plugin_name:
        problems.append("marketplace-entry-name-mismatch")
    if _has_key(marketplace, "version"):
        problems.append("marketplace-version-present")
    return problems


def _has_key(node: object, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_has_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_has_key(item, key) for item in node)
    return False


def _pep723_block(source: str) -> dict:
    match = re.search(r"^# /// script\n((?:#.*\n)+?)# ///$", source, re.MULTILINE)
    assert match, "scripts/jobsearch.py has no PEP 723 script block"
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in match.group(1).splitlines())
    return tomllib.loads(body)


def _pins(requirements: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for requirement in requirements:
        found = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)", requirement.strip())
        assert found, f"not an exact == pin: {requirement}"
        pins[re.sub(r"[-_.]+", "-", found.group(1)).lower()] = found.group(2)
    return pins


# --- name rules -------------------------------------------------------------------


def test_the_plugin_name_is_settled_and_well_formed() -> None:
    manifest = _load(PLUGIN_JSON)
    assert manifest["name"] == PLUGIN_NAME
    assert NAME_PATTERN.fullmatch(manifest["name"])
    assert validate_manifest(manifest) == []


@pytest.mark.parametrize("manifest", [{}, {"name": ""}, {"name": "   "}, {"name": None}])
def test_an_empty_or_missing_name_fails(manifest: dict) -> None:
    problems = validate_manifest(manifest)
    assert problems and problems[0] in {"name-missing", "name-empty"}


def _fullwidth(text: str) -> str:
    return "".join(chr(0xFF00 + ord(c) - 0x20) if c.isalpha() else c for c in text)


@pytest.mark.parametrize(
    "name",
    ["claude-tools", "anthropic-x", "Claude-tools", "ANTHROPIC-x", _fullwidth("claude-") + "x"],
    ids=["claude", "anthropic", "mixed-case", "upper-case", "fullwidth"],
)
def test_reserved_prefixes_are_caught_after_normalization(name: str) -> None:
    assert "name-reserved-prefix" in validate_manifest({"name": name})
    assert "marketplace-name-reserved-prefix" in validate_marketplace(
        {"name": name, "plugins": [{"name": PLUGIN_NAME}]}, PLUGIN_NAME
    )


@pytest.mark.parametrize("name", ["Job-Search", "job_search", "-job", "job--search", "jöb", "job search"])
def test_malformed_names_fail_the_pattern(name: str) -> None:
    assert "name-pattern" in validate_manifest({"name": name})


def test_the_fullwidth_spelling_is_not_ascii() -> None:
    assert "name-non-ascii" in validate_manifest({"name": _fullwidth("claude-") + "x"})


# --- manifest and marketplace --------------------------------------------


def test_the_marketplace_entry_name_equals_the_plugin_name() -> None:
    marketplace = _load(MARKETPLACE_JSON)
    assert marketplace["plugins"][0]["name"] == _load(PLUGIN_JSON)["name"] == PLUGIN_NAME
    assert validate_marketplace(marketplace, PLUGIN_NAME) == []


@pytest.mark.parametrize("name", sorted(RESERVED_MARKETPLACE_NAMES))
def test_reserved_marketplace_names_fail(name: str) -> None:
    problems = validate_marketplace({"name": name, "plugins": [{"name": PLUGIN_NAME}]}, PLUGIN_NAME)
    assert "marketplace-name-reserved" in problems


def test_an_entry_name_that_differs_from_the_plugin_fails() -> None:
    problems = validate_marketplace({"name": "market", "plugins": [{"name": "other"}]}, PLUGIN_NAME)
    assert problems == ["marketplace-entry-name-mismatch"]


def test_version_lives_only_in_plugin_json() -> None:
    manifest = _load(PLUGIN_JSON)
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    marketplace = _load(MARKETPLACE_JSON)
    assert not _has_key(marketplace, "version")
    assert "marketplace-version-present" in validate_marketplace(
        {"name": "market", "version": "1.0.0", "plugins": [{"name": PLUGIN_NAME}]}, PLUGIN_NAME
    )


def test_plugin_json_carries_no_email_at_any_depth() -> None:
    assert not _has_key(_load(PLUGIN_JSON), "email")
    assert "@" not in PLUGIN_JSON.read_text(encoding="utf-8")


# --- dependencies --------------------------------------------------------


def test_the_script_block_names_exactly_the_three_runtime_packages() -> None:
    block = _pep723_block(SHIM.read_text(encoding="utf-8"))
    assert block["requires-python"] == ">=3.11,<3.16"
    assert set(_pins(block["dependencies"])) == RUNTIME_PACKAGES


def test_the_script_pins_equal_the_dev_group_pins() -> None:
    block = _pep723_block(SHIM.read_text(encoding="utf-8"))
    script_pins = _pins(block["dependencies"])
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev_pins = _pins(project["dependency-groups"]["dev"])
    assert {name: dev_pins[name] for name in RUNTIME_PACKAGES} == script_pins


def test_the_script_lock_is_committed() -> None:
    lock = ROOT / "scripts" / "jobsearch.py.lock"
    assert lock.is_file() and lock.stat().st_size > 0


def test_the_pin_parser_rejects_a_range() -> None:
    with pytest.raises(AssertionError):
        _pins(["requests>=2"])


# --- files that must not exist ----------------------------------------------------


def _tracked_files() -> list[str]:
    try:
        done = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
        )
    except FileNotFoundError:
        pytest.skip("git is not installed")
    if done.returncode != 0:
        pytest.skip("not inside a git checkout")
    return done.stdout.splitlines()


def test_git_tracks_no_bin_directory_no_shell_script_and_no_package_json() -> None:
    tracked = _tracked_files()
    assert tracked, "git ls-files returned nothing"
    in_bin = [p for p in tracked if "bin" in Path(p).parts[:-1]]
    scripts = [p for p in tracked if p.endswith(".sh")]
    node = [p for p in tracked if Path(p).name == "package.json"]
    assert (in_bin, scripts, node) == ([], [], [])


def test_gitignore_lists_ds_store() -> None:
    # A stray macOS .DS_Store in an allowlisted folder fails the leak gate as a binary file.
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".DS_Store" in [line.strip() for line in lines]
