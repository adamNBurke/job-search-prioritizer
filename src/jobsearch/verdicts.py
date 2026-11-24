"""Function verdicts: the agent's reject-or-keep reading of a role's description.

Purpose: the title words of a family cannot catch a role whose title is the user's own target
title or whose function shows only in the description. The agent reads those descriptions and
writes roles/function-verdicts.json; this module is the only door that file passes through, so an
agent verdict can hide a role and do nothing else.
Logic: the file is {"schema_version": 1, "verdicts": [{"role_id", "verdict", "reason"}]}. verdict is
reject or keep and reason is one line of 1 to 200 characters. validate_verdicts returns every
problem as a sorted, path-prefixed plain-English line (empty when the file is fine). load_verdicts
reads the file from the data folder, validates it, and returns (rejected, reviewed): the reject
reasons by role_id, and every role_id that has any verdict. A keep only marks a role as reviewed;
it never changes a score, a tier or a reason. Verdicts for roles that are not in the latest scan
are kept and ignored, because the role may reopen.
Guardrails: a missing file is empty results. An unreadable, oversized or invalid file raises
UserError naming function-review as the fix, and nothing in it is applied, so a refused file hides
no role. A key other than role_id, verdict and reason is refused, with a named message for tier,
score, rank, points and cap. At most 5000 verdicts, a role_id of 1 to 200 characters, a reason with
no control, format, private-use or surrogate character (the categories output.clean_text strips)
and no line break. This module imports only fsio, paths and errors, never output or scoring, so
roles_store can use it without a cycle.
QA checklist: a new rule is one case in tests/test_function_review.py's bad-file matrix, and a
changed file shape needs docs/data-files.md updated in the same commit.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from jobsearch import fsio, paths
from jobsearch.errors import UserError

SCHEMA_VERSION = 1
KINDS = ("reject", "keep")
MAX_VERDICTS = 5000
MAX_REASON = 200
MAX_ROLE_ID = 200
MAX_FILE_BYTES = 16_000_000  # 5000 entries of 400 characters, even if every one is \uXXXX-escaped
FILE = paths.LAYOUT["function_verdicts"]

_ALLOWED_KEYS = ("role_id", "verdict", "reason")
_RANK_KEYS = frozenset({"tier", "score", "rank", "points", "cap", "tier_cap", "coverage"})
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_LINE_BREAKS = frozenset({"\n", "\r", "\u0085", "\u2028", "\u2029"})
_RANK_MESSAGE = "a verdict can only hide a role; it cannot set a tier, score or rank."


def _reason_problem(reason: object) -> str | None:
    if not isinstance(reason, str):
        return "needs a one-line text reason."
    trimmed = reason.strip()
    if not trimmed:
        return "is empty; give one plain sentence of 1 to 200 characters."
    if len(trimmed) > MAX_REASON:
        return f"is {len(trimmed)} characters; the limit is {MAX_REASON}."
    if any(char in _LINE_BREAKS for char in trimmed):
        return "must be one line, with no line break."
    if any(unicodedata.category(char) in _HIDDEN_CATEGORIES for char in trimmed):
        return "holds a control, hidden or direction character; use plain text."
    return None


def _entry_problems(path: str, entry: object, seen: dict[str, str]) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{path}: needs an object with role_id, verdict and reason."]
    problems: list[str] = []
    for key in sorted(str(k) for k in entry):
        if key in _ALLOWED_KEYS:
            continue
        if key in _RANK_KEYS:
            problems.append(f"{path}.{key}: {_RANK_MESSAGE}")
        else:
            problems.append(f"{path}.{key}: is not allowed; use only role_id, verdict and reason.")
    role_id = entry.get("role_id")
    if not isinstance(role_id, str) or not role_id.strip():
        problems.append(f"{path}.role_id: needs the role id exactly as function-review listed it.")
    elif len(role_id) > MAX_ROLE_ID:
        problems.append(f"{path}.role_id: is longer than {MAX_ROLE_ID} characters.")
    elif role_id in seen:
        problems.append(f"{path}.role_id: repeats the role_id of {seen[role_id]}.")
    else:
        seen[role_id] = path
    if entry.get("verdict") not in KINDS:
        problems.append(f"{path}.verdict: must be reject or keep.")
    problem = _reason_problem(entry.get("reason"))
    if problem is not None:
        problems.append(f"{path}.reason: {problem}")
    return problems


def validate_verdicts(data: object) -> list[str]:
    """Every problem in a verdict file as sorted, path-prefixed plain-English lines."""
    if not isinstance(data, dict):
        return [f'{FILE}: needs an object like {{"schema_version": 1, "verdicts": [...]}}.']
    problems: list[str] = []
    for key in sorted(str(k) for k in data):
        if key in ("schema_version", "verdicts"):
            continue
        if key in _RANK_KEYS:
            problems.append(f"{key}: {_RANK_MESSAGE}")
        else:
            problems.append(f"{key}: is not allowed; use only schema_version and verdicts.")
    if data.get("schema_version") != SCHEMA_VERSION or isinstance(
            data.get("schema_version"), bool):
        problems.append(f"schema_version: must be {SCHEMA_VERSION}.")
    entries = data.get("verdicts")
    if not isinstance(entries, list):
        problems.append("verdicts: needs a list of verdicts.")
    elif len(entries) > MAX_VERDICTS:
        problems.append(f"verdicts: has {len(entries)} entries; the limit is {MAX_VERDICTS}.")
    else:
        seen: dict[str, str] = {}
        for index, entry in enumerate(entries):
            problems += _entry_problems(f"verdicts[{index}]", entry, seen)
    return sorted(problems)


def _refusal(detail: str) -> UserError:
    return UserError(f"{FILE} has problems: {detail} Fix it, then run function-review again.")


def _read(folder: Path) -> tuple[object, list[str]]:
    """(the parsed file, its problems). A missing file is an empty, valid one."""
    path = folder / FILE
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "verdicts": []}, []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, ["the file is far larger than a verdict file can be."]
        data = fsio.read_json(path)
    except (OSError, ValueError):
        return None, ["it is not readable JSON."]
    return data, validate_verdicts(data)


def file_problems(folder: Path) -> list[str]:
    """Every problem in roles/function-verdicts.json, or an empty list when it is fine or absent."""
    return _read(folder)[1]


def load_verdicts(folder: Path) -> tuple[dict[str, str], frozenset[str]]:
    """(reject reasons by role_id, every reviewed role_id) from roles/function-verdicts.json."""
    data, problems = _read(folder)
    if problems:
        more = f" ({len(problems) - 5} more.)" if len(problems) > 5 else ""
        raise _refusal(" ".join(problems[:5]) + more)
    rejected: dict[str, str] = {}
    reviewed: set[str] = set()
    for entry in data["verdicts"]:  # type: ignore[index]
        reviewed.add(entry["role_id"])
        if entry["verdict"] == "reject":
            rejected[entry["role_id"]] = entry["reason"].strip()
    return rejected, frozenset(reviewed)
