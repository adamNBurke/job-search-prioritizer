"""Load the user's scoring config from the data folder.

Purpose: every command that scores or scans needs criteria.json, and none of them should trust
it unchecked.
Logic: load_criteria reads config/criteria.json through fsio, runs criteria.validate_criteria
and returns the dict, or raises CriteriaError with every problem listed in plain English. A
missing file says to run the interview first. save_new_version is the only way a config
change is written: it bumps criteria_version by one minor step and keeps the version it
replaces as config/criteria-history/criteria-v<old>.json, so the previous config is never lost.
Guardrails: validation runs on every load and before every save, so a hand edit or a
model-written config can never reach scoring unchecked. The config selects from a closed
evaluator registry and never carries code. A save validates the candidate before writing
anything, writes through fsio (temp file, then replace), never overwrites an existing history
snapshot (a clashing name gets a -saved-<n> suffix), and takes created_from only in the
fixed form intake, retune or proposal:p-NNNN.
QA checklist: a new load path goes through load_criteria and a new change path goes through
save_new_version; do not read or write criteria.json directly.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from jobsearch import fsio, paths
from jobsearch.criteria import validate_criteria
from jobsearch.errors import UserError


class CriteriaError(UserError):
    """The scoring config is missing or invalid. Message is plain English."""


def load_criteria(folder: Path) -> dict:
    path = folder / paths.LAYOUT["criteria"]
    if not path.is_file():
        raise CriteriaError("No scoring config yet. Run the interview first.")
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        raise CriteriaError("The scoring config is not readable JSON. Fix it or run the "
                            "interview again.") from None
    errors = validate_criteria(data)
    if errors:
        raise CriteriaError("The scoring config has problems: " + " ".join(errors))
    assert isinstance(data, dict)
    return data


_VERSION = re.compile(r"([0-9]+)\.([0-9]+)")
_CREATED_FROM = re.compile(r"intake|retune|proposal:p-[0-9]{4,}")
_FIRST_VERSION = "1.0"


def _parse_version(version: object) -> tuple[int, int]:
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise CriteriaError("The scoring config version must look like 1.0 or 2.3.")
    return int(match.group(1)), int(match.group(2))


def next_version(version: str) -> str:
    """One minor step up, compared as numbers: 1.9 becomes 1.10, never 1.10 sorted before 1.9."""
    major, minor = _parse_version(version)
    return f"{major}.{minor + 1}"


def _current_version(path: Path) -> str:
    """The version of the config on disk, or CriteriaError when it cannot be told.

    A save never replaces a file whose version it cannot name, because the previous config
    could not be kept under a trustworthy history name.
    """
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        data = None
    version = data.get("criteria_version") if isinstance(data, dict) else None
    try:
        _parse_version(version)
    except CriteriaError:
        raise CriteriaError(
            "The current scoring config has no readable version, so it cannot be kept in the "
            "history. Fix config/criteria.json by hand or move it aside, then try again."
        ) from None
    assert isinstance(version, str)
    return version


def set_aside_unversioned(folder: Path) -> str | None:
    """Move a config with no readable version into the history so a rebuild can replace it.

    save_new_version refuses to replace a file it cannot name a version for. When the file is
    in that state it is moved to config/criteria-history/criteria-unreadable[-<n>].json (an
    earlier copy is never overwritten) and that path
    is returned. None when there is no config or its version is readable.
    """
    path = folder / paths.LAYOUT["criteria"]
    if not path.is_file():
        return None
    try:
        _current_version(path)
        return None
    except CriteriaError:
        pass
    directory = folder / paths.LAYOUT["criteria_history"]
    number = 1
    while True:
        name = "criteria-unreadable.json" if number == 1 else f"criteria-unreadable-{number}.json"
        if not (directory / name).exists():
            break
        number += 1
    fsio.write_bytes_atomic(directory / name, path.read_bytes())
    path.unlink()
    return f"{paths.LAYOUT['criteria_history']}/{name}"


def _keep_replaced(folder: Path, version: str, content: bytes) -> None:
    """Keep the config being replaced in the history, never overwriting a snapshot.

    The usual name is criteria-v<version>.json. When that name is taken by a different file
    (after a rollback an older snapshot of the same version can be there), the replaced config
    goes to criteria-v<version>-saved-<n>.json.
    """
    directory = folder / paths.LAYOUT["criteria_history"]
    name = f"criteria-v{version}.json"
    number = 1
    while True:
        snapshot = directory / name
        if not snapshot.exists():
            fsio.write_bytes_atomic(snapshot, content)
            return
        if snapshot.read_bytes() == content:
            return
        number += 1
        name = f"criteria-v{version}-saved-{number}.json"


def created_from_after_edit(config: dict) -> str:
    """An edited config stays intake until its first retune."""
    return "intake" if config["created_from"] == "intake" else "retune"


def save_new_version(folder: Path, data: dict, created_from: str) -> str:
    """Validate data, keep the current config in the history, write data as the next version.

    Returns the new criteria_version. With no config on disk the result is 1.0 and no history
    file is written. Nothing is written when validation fails, and data is not modified.
    """
    if not _CREATED_FROM.fullmatch(created_from):
        raise CriteriaError("created_from must be intake, retune or proposal:p-NNNN.")
    if not isinstance(data, dict):
        raise CriteriaError("The scoring config must be a JSON object.")
    path = folder / paths.LAYOUT["criteria"]
    current = _current_version(path) if path.is_file() else None
    new_version = next_version(current) if current is not None else _FIRST_VERSION

    candidate = copy.deepcopy(data)
    candidate["criteria_version"] = new_version
    candidate["created_from"] = created_from
    errors = validate_criteria(candidate)
    if errors:
        raise CriteriaError("The new scoring config has problems: " + " ".join(errors))

    if current is not None:
        _keep_replaced(folder, current, path.read_bytes())
    fsio.write_json_atomic(path, candidate)
    return new_version
