"""The shipped starter directory and the board selection built on it.

Purpose: the directory needs about 100 live-verified boards spread across industries, so these tests
read the real src/jobsearch/data/boards.json and pin its shape, coverage and freshness.
Logic: offline. The file tests read the shipped file once; the selection tests build a small
in-memory directory so every filter case is exact.
Guardrails: a stale or thin directory fails here before it ships. The tags (industry, size,
stage) are approximate maintainer tags; only existence, postings and identity are verified.
QA checklist: after tools/build_directory.py rewrites the file, this file must pass unchanged.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date

import pytest

from jobsearch import directory
from jobsearch.directory import BoardEntry, board_key, count_matching, select_boards
from jobsearch.errors import UserError


@pytest.fixture(scope="module")
def shipped() -> list[BoardEntry]:
    return directory.load_directory()


def test_the_shipped_file_loads_and_holds_ninety_to_one_hundred_twenty_boards(shipped) -> None:
    assert 90 <= len(shipped) <= 120


def test_every_board_key_is_unique(shipped) -> None:
    keys = [board_key(e) for e in shipped]
    assert len(keys) == len(set(keys))


def _raw_rows() -> list[dict]:
    return json.loads(directory.DIRECTORY_PATH.read_text(encoding="utf-8"))["entries"]


def test_every_row_passed_an_identity_check_and_had_postings(shipped) -> None:
    rows = _raw_rows()
    assert {r["identity"] for r in rows} <= {"company_name", "description_mentions"}
    assert all(r["postings_at_verify"] > 0 for r in rows)


def test_every_last_verified_is_within_thirty_days_before_generated_on(shipped) -> None:
    generated = date.fromisoformat(
        json.loads(directory.DIRECTORY_PATH.read_text(encoding="utf-8"))["generated_on"])
    for row in _raw_rows():
        age = (generated - date.fromisoformat(row["last_verified"])).days
        assert 0 <= age <= 30, row["company"]


def test_rows_are_sorted_by_casefolded_company_then_ats(shipped) -> None:
    order = [(e.company.casefold(), e.ats) for e in shipped]
    assert order == sorted(order)


def test_at_least_twelve_industries_have_at_least_two_boards(shipped) -> None:
    counts = Counter(e.industry for e in shipped)
    assert sum(1 for n in counts.values() if n >= 2) >= 12


def test_the_three_job_board_systems_all_appear(shipped) -> None:
    systems = {e.ats for e in shipped}
    assert "greenhouse" in systems and "ashby" in systems
    assert systems & {"lever", "lever_eu"}


def test_at_least_forty_percent_of_rows_are_outside_software(shipped) -> None:
    outside = sum(1 for e in shipped if e.industry != "software")
    assert outside * 100 >= 40 * len(shipped)


def test_the_shipped_file_names_only_public_company_text(shipped) -> None:
    for entry in shipped:
        assert entry.company.strip() == entry.company
        assert "@" not in entry.company and "://" not in entry.company


def _entry(company: str, industry: str, size: str, ats: str = "greenhouse") -> BoardEntry:
    return BoardEntry(company, ats, company.lower(), industry, size, "growth")


SMALL = [
    _entry("Alpha", "software", "1-50"),
    _entry("Bravo", "fintech", "51-200"),
    _entry("Charlie", "software", "51-200"),
    _entry("Delta", "healthcare", "1001-5000"),
    _entry("Echo", "fintech", "1-50"),
]


def names(entries: list[BoardEntry]) -> list[str]:
    return [e.company for e in entries]


def test_empty_filters_mean_no_restriction_and_keep_file_order() -> None:
    assert names(select_boards(SMALL, [], [])) == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    assert count_matching(SMALL, [], []) == 5


def test_one_industry() -> None:
    assert names(select_boards(SMALL, ["software"], [])) == ["Alpha", "Charlie"]


def test_one_size() -> None:
    assert names(select_boards(SMALL, [], ["51-200"])) == ["Bravo", "Charlie"]


def test_industry_and_size_both_apply() -> None:
    assert names(select_boards(SMALL, ["software"], ["51-200"])) == ["Charlie"]
    assert names(select_boards(SMALL, ["fintech", "healthcare"], ["1-50", "1001-5000"])) == [
        "Delta", "Echo"]


def test_several_industries_keep_directory_order_not_filter_order() -> None:
    assert names(select_boards(SMALL, ["healthcare", "software"], [])) == [
        "Alpha", "Charlie", "Delta"]


def test_a_ruled_out_industry_is_always_removed() -> None:
    assert names(select_boards(SMALL, [], [], exclude_industries=["software"])) == [
        "Bravo", "Delta", "Echo"]
    assert names(select_boards(SMALL, ["software", "fintech"], [],
                               exclude_industries=("software",))) == ["Bravo", "Echo"]
    assert select_boards(SMALL, ["software"], [], exclude_industries=["software"]) == []
    assert count_matching(SMALL, [], [], exclude_industries=["fintech"]) == 3


def test_selection_does_not_change_the_input_list() -> None:
    before = list(SMALL)
    select_boards(SMALL, ["software"], ["1-50"], ["fintech"])
    assert SMALL == before


def test_an_unknown_industry_selects_nothing() -> None:
    assert select_boards(SMALL, ["not-an-industry"], []) == []


def _write(tmp_path, entries: list[dict]):
    path = tmp_path / "boards.json"
    path.write_text(json.dumps(
        {"schema_version": 1, "generated_on": "2026-11-17", "entries": entries}), encoding="utf-8")
    return path


GOOD = {"company": "Acme", "ats": "greenhouse", "slug": "acme", "industry": "software",
        "size": "1-50", "stage": "early", "last_verified": "2026-11-17",
        "identity": "company_name", "postings_at_verify": 2}


def test_load_directory_stays_strict(tmp_path) -> None:
    assert len(directory.load_directory(_write(tmp_path, [GOOD]))) == 1
    for patch in ({"industry": "nope"}, {"size": "tiny"}, {"ats": "workday"},
                  {"identity": "guess"}, {"last_verified": "yesterday"},
                  {"postings_at_verify": -1}, {"extra": 1}):
        with pytest.raises(UserError, match="entry 0"):
            directory.load_directory(_write(tmp_path, [{**GOOD, **patch}]))
    with pytest.raises(UserError, match="appears twice"):
        directory.load_directory(_write(tmp_path, [GOOD, GOOD]))
