"""JSONL files are split on "\\n" only.

The writers use ensure_ascii=False, so U+2028, U+2029 and U+0085 stay raw inside a JSON string.
str.splitlines() treats all three as line breaks and cut a record in half, which made every later
read fail with "damaged line". These tests round-trip such text through the real writers and
readers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch import paste, roles_store
from jobsearch.directory import BoardEntry
from jobsearch.errors import UserError
from jobsearch.roles_store import RoleRecord
from jobsearch.sources import parse_board

SEPARATORS = "  \u0085"
FETCHED_AT = "2026-11-17T12:00:00Z"
ENTRY = BoardEntry(
    company="Acme Robotics", ats="greenhouse", slug="acme-robotics",
    industry="manufacturing-hardware", size="51-200", stage="growth",
)


def _posting(description: str):
    job = {
        "id": 101, "title": "Field Technician", "absolute_url": "https://example.com/j/101",
        "content": f"<p>{description}</p>", "location": {"name": "Remote"},
    }
    (posting,) = parse_board(ENTRY, {"jobs": [job]}, FETCHED_AT)
    return posting


def _record(title: str) -> RoleRecord:
    return RoleRecord(
        role_id="greenhouse:acme-robotics:101", board_key="greenhouse:acme-robotics",
        company="Acme Robotics", title=title, dedupe_key=None, first_seen="2026-11-17",
        last_seen="2026-11-17", status="open",
    )


def test_role_history_with_line_separators_in_a_title_round_trips(tmp_path: Path) -> None:
    title = f"Field{SEPARATORS}Technician"
    roles_store.save_roles(tmp_path, {"a": _record(title)})
    assert roles_store.load_roles(tmp_path)["greenhouse:acme-robotics:101"].title == title


def test_pasted_file_with_line_separators_in_the_description_round_trips(
    tmp_path: Path,
) -> None:
    description = f"Run the plan.{SEPARATORS}Then report."
    posting = _posting(description)
    assert all(ch in posting.description_text for ch in SEPARATORS)
    paste.save_pasted(tmp_path, posting, "2026-11-17")
    ((pasted_on, loaded),) = roles_store.load_pasted(tmp_path)
    assert pasted_on == "2026-11-17"
    assert loaded.description_text == posting.description_text


def test_a_second_paste_save_after_a_separator_posting_does_not_fail(tmp_path: Path) -> None:
    first = _posting(f"One{SEPARATORS}two")
    paste.save_pasted(tmp_path, first, "2026-11-17")
    paste.save_pasted(tmp_path, first, "2026-11-18")  # reads the file written above
    ((pasted_on, _),) = roles_store.load_pasted(tmp_path)
    assert pasted_on == "2026-11-17"


def test_windows_line_endings_are_tolerated(tmp_path: Path) -> None:
    roles_store.save_roles(tmp_path, {"a": _record("Field Technician")})
    path = roles_store.roles_path(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert roles_store.load_roles(tmp_path)["greenhouse:acme-robotics:101"].title == (
        "Field Technician")


def test_a_truly_damaged_line_still_reports_its_number(tmp_path: Path) -> None:
    path = roles_store.roles_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"role_id": "x"}) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(UserError, match=r"damaged line \(2\)"):
        roles_store._read_lines(path, "role history")
