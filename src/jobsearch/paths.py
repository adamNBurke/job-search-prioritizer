"""Data-folder resolution, refusal and init.

The data folder is a folder the user chooses. No command derives a default location.
CLAUDE_PLUGIN_ROOT and CLAUDE_PLUGIN_DATA are never read from the environment: they are
not exported to scripts, so the plugin root comes from this file's own location and the
plugin data directory arrives as an argument.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from jobsearch import clock, fsio

MARKER = "jobsearch.workspace.json"
WORKSPACE_SCHEMA = 1

# Short name -> data-folder-relative POSIX path. Names only: init creates the marker, the
# .gitignore and cache/http; later phases create the rest lazily. docs/contracts.md carries
# the same table and tests/test_contracts.py fails when they differ. docs/data-files.md
# carries the shape of every file named here.
LAYOUT: dict[str, str] = {
    "marker": "jobsearch.workspace.json",
    "gitignore": ".gitignore",
    "profile": "profile",
    "criteria": "config/criteria.json",
    "criteria_history": "config/criteria-history",
    "cache_http": "cache/http",
    "scans": "scans",
    "roles": "roles",
    "ranked": "ranked",
    "applications": "applications",
    "feedback": "feedback",
    "answers": "profile/answers.json",
    "resume_md": "profile/resume.md",
    "profile_source": "profile/source",
    "scan_scope": "config/scan-scope.json",
    "targets": "config/targets.json",
    "boards_local": "config/boards.local.json",
    "calibration": "config/calibration.json",
    "roles_file": "roles/roles.jsonl",
    "pasted": "roles/pasted.jsonl",
    "titles": "titles",
    "marks": "feedback/marks.jsonl",
    "function_worklist": "roles/function-worklist.json",
    "function_verdicts": "roles/function-verdicts.json",
    "writing_samples": "profile/samples",
    "settings": "config/settings.json",
    "notices": "config/notices.json",
    "intake_summary": "profile/summary.md",
}

_PLACEHOLDER_CHARS = "{}<>\""
_PLACEHOLDER_MSG = "Replace the data folder placeholder with a real folder path."
_INSIDE_PLUGIN_MSG = (
    "That folder is inside the Claude plugin directory, which is replaced on every update. "
    "Choose a folder you own, such as a new folder in your home directory."
)
_ANCESTOR_MSG = (
    "That folder contains a Claude plugin directory; a narrower folder is safer."
)
_GIT_REMOTE_MSG = (
    "That folder is inside a git repository that has a remote. The data folder ignores its "
    "own files, but double-check that nothing from it is ever added to that repository."
)
_CLOUD_MSG = (
    "That folder looks like it is under a folder that may sync to the cloud (Desktop, "
    "Documents, OneDrive, Dropbox, iCloud Drive, Google Drive or Box). Your resume and notes "
    "would be uploaded."
)
# Compared against folded (NFC, casefolded) path components. "cloudstorage" is where macOS
# mounts Dropbox, Google Drive, OneDrive and Box; "mobile documents" is iCloud Drive.
_CLOUD_COMPONENTS = ("desktop", "documents", "mobile documents", "cloudstorage", "box")
_CLOUD_PREFIXES = ("onedrive", "dropbox", "icloud", "google drive", "googledrive", "box sync")
_NOT_EMPTY_MSG = (
    "That folder already has files in it. init writes a .gitignore that would hide them "
    "from git, so choose a new or empty folder."
)


class DataDirRefused(Exception):
    """Message is plain English and safe to show the user."""


@dataclass
class InitOutcome:
    wrote: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def plugin_version() -> str:
    """The manifest's version, or "unknown" when the manifest is missing or unusable."""
    try:
        return str(fsio.read_json(plugin_root() / ".claude-plugin" / "plugin.json")["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def parse_data_dir_arg(value: str) -> Path:
    """Turn a --data-dir string into a Path, refusing blank and placeholder text.

    Path("") silently becomes ".", so the check has to happen on the string.
    """
    if not value.strip() or any(ch in value for ch in _PLACEHOLDER_CHARS):
        raise DataDirRefused(_PLACEHOLDER_MSG)
    return Path(value)


def find_data_dir(arg: str | None, cwd: Path) -> Path | None:
    """The given argument, else the nearest ancestor of cwd (cwd included) holding MARKER."""
    if arg is not None:
        if not arg.strip():
            raise DataDirRefused(_PLACEHOLDER_MSG)
        return normalize_path(Path(arg))  # same "~" and relative-path handling as init
    for candidate in (cwd, *cwd.parents):
        if (candidate / MARKER).is_file():
            return candidate
    return None


def cache_http_dir(data_dir: Path) -> Path:
    return data_dir / LAYOUT["cache_http"]


def normalize_path(target: Path) -> Path:
    """expanduser, absolute against the cwd, then resolve (symlinks and ".." collapse)."""
    expanded = Path(os.path.expanduser(str(target)))
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.resolve(strict=False)


def _folded(path: Path) -> tuple[str, ...]:
    """Components compared NFC-normalized and casefolded: macOS and Windows volumes are case
    insensitive and APFS ignores normalization form. Refusing more on Linux is harmless."""
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _is_inside(path: tuple[str, ...], base: tuple[str, ...]) -> bool:
    return len(path) >= len(base) and path[: len(base)] == base


def _has_claude_plugins(parts: tuple[str, ...]) -> bool:
    return any(a == ".claude" and b == "plugins" for a, b in zip(parts, parts[1:]))


def _protected_roots(plugin_data: Path | None) -> list[tuple[str, ...]]:
    """Folded component tuples of every directory an update or uninstall can delete."""
    roots: list[Path] = [plugin_root()]
    if plugin_data is not None and str(plugin_data).strip():
        roots.append(plugin_data)
    try:
        roots.append(Path.home() / ".claude" / "plugins")
    except (RuntimeError, KeyError):
        pass
    cache_env = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR", "").strip()
    if cache_env:
        roots.append(Path(cache_env))
    config_env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_env:
        roots.append(Path(config_env) / "plugins")
    return [_folded(normalize_path(root)) for root in roots]


def _git_config_has_remote(folder: Path) -> bool:
    """Walk up for the nearest .git (directory, or file pointing at one) and look for [remote.

    Reads text only; src never runs git (subprocess is reserved for tools).
    """
    for parent in (folder, *folder.parents):
        git = parent / ".git"
        try:
            if git.is_file():
                line = git.read_text(encoding="utf-8", errors="replace").strip()
                if not line.startswith("gitdir:"):
                    return False
                git_dir = parent / line.split(":", 1)[1].strip()
                common = git_dir / "commondir"
                if common.is_file():
                    git_dir = git_dir / common.read_text(encoding="utf-8").strip()
                git = git_dir
            elif not git.is_dir():
                continue
            config = git / "config"
            if not config.is_file():
                return False
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return any(line.strip().startswith("[remote") for line in text.splitlines())
    return False


def check_data_dir(target: Path, plugin_data: Path | None) -> list[str]:
    """Raise DataDirRefused for an unsafe folder; return soft warnings otherwise."""
    raw = str(target)
    if not raw.strip() or any(ch in raw for ch in _PLACEHOLDER_CHARS):
        raise DataDirRefused(_PLACEHOLDER_MSG)

    resolved = normalize_path(target)
    folded = _folded(resolved)
    as_given = _folded(Path(os.path.expanduser(raw)))

    roots = _protected_roots(plugin_data)
    if any(_is_inside(folded, root) for root in roots):
        raise DataDirRefused(_INSIDE_PLUGIN_MSG)
    if _has_claude_plugins(folded) or _has_claude_plugins(as_given):
        raise DataDirRefused(_INSIDE_PLUGIN_MSG)

    warnings: list[str] = []
    if any(_is_inside(root, folded) for root in roots):
        warnings.append(_ANCESTOR_MSG)
    if _git_config_has_remote(resolved):
        warnings.append(_GIT_REMOTE_MSG)
    if any(part in _CLOUD_COMPONENTS or part.startswith(_CLOUD_PREFIXES) for part in folded[1:]):
        warnings.append(_CLOUD_MSG)
    return warnings


def init_data_dir(target: Path, plugin_data: Path | None, plugin_version: str) -> InitOutcome:
    """Create the data folder. Nothing is written until every refusal check has passed.

    Allowed targets: a folder that does not exist, is empty, or already holds MARKER.
    Writing the self-ignoring .gitignore into any other folder would hide the user's files
    from their own git.
    """
    warnings = check_data_dir(target, plugin_data)
    folder = normalize_path(target)

    if folder.exists() and not folder.is_dir():
        raise DataDirRefused("That path is a file, not a folder. Choose a new or empty folder.")
    has_marker = (folder / MARKER).is_file()
    if folder.exists() and not has_marker and any(folder.iterdir()):
        raise DataDirRefused(_NOT_EMPTY_MSG)

    wrote: list[str] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirRefused(
            f"That folder could not be created ({exc.strerror or 'file system error'})."
        ) from exc

    # Marker first: an interrupted run must not leave a non-empty folder without it.
    marker = folder / MARKER
    if not marker.is_file():
        created_at = clock.iso_z(clock.now_utc())
        fsio.write_json_atomic(
            marker,
            {
                "created_at": created_at,
                "created_with_plugin_version": plugin_version,
                "schema_version": 1,
                "workspace_schema": WORKSPACE_SCHEMA,
            },
        )
        wrote.append(MARKER)

    http_dir = cache_http_dir(folder)
    if not http_dir.is_dir():
        http_dir.mkdir(parents=True, exist_ok=True)
        wrote.append("cache/http")

    gitignore = folder / ".gitignore"
    if not gitignore.is_file():
        fsio.write_text_atomic(gitignore, "*\n")
        wrote.append(".gitignore")

    return InitOutcome(wrote=sorted(wrote), warnings=warnings)
