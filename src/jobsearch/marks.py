"""Right and wrong marks on ranked roles, stored against the config version that made them.

Purpose: a mark is the user's own call on one recommendation. Marks are the evidence base for
the 1.1 learning step, so each one records the tier, score and config version the user
was judging.
Logic: add_mark finds the newest ranked/ranked-<date>.json (load_ranked_role, shared with
letters), requires the role in its roles list,
reads the optional note from a file inside the data folder, and appends one sorted-key JSON line to
feedback/marks.jsonl by rewriting the old bytes plus the new line through fsio.
Guardrails: marks are recorded only. Nothing here writes the scoring config or a ranked file,
and an earlier line is never changed. The note is the user's text, stored as data and capped at
NOTE_LIMIT characters.
QA checklist: tests/test_marks.py covers the contract in docs/data-files.md "marks.jsonl".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from jobsearch import clock, fsio, output, paths
from jobsearch.errors import UserError

NOTE_LIMIT = 1000
VERDICTS = ("right", "wrong")
_RANKED_FILE_PREFIX = "ranked-"
_TOO_LONG = f"Keep the note under {NOTE_LIMIT} characters."
_NOTE_FILE_MAX_BYTES = NOTE_LIMIT * 4 + 16  # four bytes a character at most, plus a BOM and newlines


@dataclass(frozen=True)
class Mark:
    marked_at: str
    role_id: str
    verdict: str
    note: str | None
    tier_at_time: str
    score_at_time: int
    criteria_version: str
    ranked_file: str


def _marks_path(folder: Path) -> Path:
    return folder / paths.LAYOUT["marks"]


def newest_ranked_json(folder: Path) -> Path:
    ranked = folder / paths.LAYOUT["ranked"]
    candidates = sorted(ranked.glob(f"{_RANKED_FILE_PREFIX}*.json")) if ranked.is_dir() else []
    if not candidates:
        raise UserError("No ranked file yet. Run rank first.")
    return candidates[-1]


def _read_note(note_path: Path, folder: Path) -> str | None:
    """The note file must resolve inside the data folder. Text is stored with LF line endings."""
    note = fsio.read_user_file(note_path, folder, _NOTE_FILE_MAX_BYTES, "The note file").strip()
    if len(note) > NOTE_LIMIT:
        raise UserError(_TOO_LONG)
    return note or None


def load_ranked_role(folder: Path, role_id: str) -> tuple[Path, dict, dict | None]:
    """(newest ranked file, its JSON, the role with this id or None). A damaged file is refused."""
    ranked_path = newest_ranked_json(folder)
    try:
        ranked = fsio.read_json(ranked_path)
    except (OSError, ValueError):
        raise UserError("The latest ranked file is damaged. Run rank again.") from None
    roles = ranked.get("roles") if isinstance(ranked, dict) else None
    if not isinstance(roles, list):
        raise UserError("The latest ranked file is damaged. Run rank again.")
    role = next((r for r in roles if isinstance(r, dict) and r.get("role_id") == role_id), None)
    return ranked_path, ranked, role


def add_mark(folder: Path, role_id: str, verdict: str, note_path: Path | None) -> Mark:
    if verdict not in VERDICTS:
        raise UserError("The verdict must be right or wrong.")
    ranked_path, ranked, role = load_ranked_role(folder, role_id)
    if role is None:
        raise UserError(
            f"The role {output.shown(role_id)} is not in the latest ranked file. "
            "Run rank, then mark a role from that list.")
    note = _read_note(note_path, folder) if note_path is not None else None
    mark = Mark(
        marked_at=clock.iso_z(clock.now_utc()),
        role_id=role_id,
        verdict=verdict,
        note=note,
        tier_at_time=str(role["tier"]),
        score_at_time=int(role["score"]),
        criteria_version=str(ranked["criteria_version"]),
        ranked_file=f"{paths.LAYOUT['ranked']}/{ranked_path.name}",
    )
    target = _marks_path(folder)
    # Bytes, not text: an earlier line that is not valid UTF-8 is kept exactly as written.
    old = target.read_bytes() if target.is_file() else b""
    if old and not old.endswith(b"\n"):
        old += b"\n"
    line = json.dumps(asdict(mark), ensure_ascii=False, sort_keys=True)
    fsio.write_bytes_atomic(target, old + line.encode("utf-8") + b"\n")
    return mark


def scan_marks(folder: Path) -> tuple[list[Mark], int]:
    """Every readable line in time order, and the number of damaged lines skipped.

    Lines are split on the line feed only (a trailing carriage return is dropped), because the
    writer puts U+2028, U+2029 and U+0085 raw inside a note and str.splitlines() would cut the
    record at them. Each line is decoded on its own, so one bad byte costs one line.
    """
    target = _marks_path(folder)
    if not target.is_file():
        return [], 0
    found: list[Mark] = []
    damaged = 0
    for raw in target.read_bytes().split(b"\n"):
        raw = raw.removesuffix(b"\r")
        if not raw.strip():
            continue
        try:
            found.append(Mark(**json.loads(raw.decode("utf-8-sig"))))
        except (ValueError, TypeError):  # UnicodeDecodeError is a ValueError
            damaged += 1
    return found, damaged
