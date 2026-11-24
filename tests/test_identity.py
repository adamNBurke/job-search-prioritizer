"""Company identity checks used by the starter directory and by target resolution.

Purpose: prove a board is accepted only when it names the company, and never on a near miss.
Logic: synthetic payloads in the live key shapes (Greenhouse company_name, Lever and Ashby
descriptionPlain) plus the real fixtures under tests/fixtures/ats, which are fictional.
Guardrails: every case is offline; the companies are invented.
QA checklist: a new job-board system adds a match, a mismatch and a malformed-payload case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch.identity import identity_from_payload, normalize_company

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ats"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", [
    "Acme Robotics, Inc.", "acme robotics", "ACME  ROBOTICS LLC", "Acme-Robotics Corp.",
    "Acme Robotics Co", "Acme Robotics GmbH", "Acme Robotics PLC", "Acme Robotics Limited",
])
def test_normalize_company_folds_case_punctuation_and_suffixes(name: str) -> None:
    assert normalize_company(name) == normalize_company("acme robotics") == "acme robotics"


@pytest.mark.parametrize("suffix", ["inc", "llc", "ltd", "limited", "corp", "corporation",
                                    "co", "gmbh", "plc", "sa", "ag"])
def test_every_listed_suffix_is_dropped(suffix: str) -> None:
    assert normalize_company(f"Acme {suffix}") == "acme"


def test_a_name_that_is_only_a_suffix_is_kept_not_emptied() -> None:
    assert normalize_company("Co") == "co"


def test_normalize_uses_compatibility_folding() -> None:
    assert normalize_company("Ａcme Robotics") == "acme robotics"


def test_greenhouse_company_name_match() -> None:
    payload = {"jobs": [{"id": 1, "company_name": "Acme Robotics, Inc."}]}
    assert identity_from_payload("greenhouse", payload, "Acme Robotics") == "company_name"


def test_greenhouse_company_name_mismatch_is_none() -> None:
    payload = {"jobs": [{"id": 1, "company_name": "Other Robotics"}]}
    assert identity_from_payload("greenhouse", payload, "Acme Robotics") is None


def test_greenhouse_near_miss_is_not_a_match() -> None:
    payload = {"jobs": [{"id": 1, "company_name": "Acme Robotics Labs"}]}
    assert identity_from_payload("greenhouse", payload, "Acme Robotics") is None


def test_greenhouse_without_a_company_name_is_none() -> None:
    assert identity_from_payload("greenhouse", {"jobs": [{"id": 1}]}, "Acme Robotics") is None


def test_greenhouse_fixture_matches_its_fictional_company() -> None:
    payload = _load("greenhouse_list.json")
    assert identity_from_payload("greenhouse", payload, "Acme Robotics") == "company_name"


def _lever(texts: list[str]) -> list[dict]:
    return [{"id": f"id-{i}", "descriptionPlain": text} for i, text in enumerate(texts)]


def test_lever_needs_half_of_five_postings_to_mention_the_company() -> None:
    hit, miss = "Acme Robotics builds things.", "We build other things."
    assert identity_from_payload(
        "lever", _lever([hit, hit, hit, miss, miss]), "Acme Robotics") == "description_mentions"
    assert identity_from_payload(
        "lever", _lever([hit, hit, miss, miss, miss]), "Acme Robotics") is None


def test_half_of_an_even_sample_is_enough() -> None:
    hit, miss = "Join Acme Robotics today", "Nothing here"
    assert identity_from_payload(
        "lever_eu", _lever([hit, miss]), "Acme Robotics") == "description_mentions"


def test_only_the_first_five_postings_by_id_are_sampled() -> None:
    hit, miss = "Acme Robotics", "no name"
    jobs = [{"id": f"a{i}", "descriptionPlain": miss} for i in range(5)]
    jobs += [{"id": f"b{i}", "descriptionPlain": hit} for i in range(5)]
    assert identity_from_payload("lever", list(reversed(jobs)), "Acme Robotics") is None


def test_the_company_must_appear_as_a_whole_phrase() -> None:
    texts = ["Acme Roboticsx is hiring", "Not Acme Robotic at all"]
    assert identity_from_payload("ashby", {"jobs": _lever(texts)}, "Acme Robotics") is None
    assert identity_from_payload(
        "ashby", {"jobs": _lever(["(Acme Robotics), a firm."])}, "Acme Robotics",
    ) == "description_mentions"


def test_lever_and_ashby_fixtures_match_their_fictional_company() -> None:
    assert identity_from_payload(
        "lever", _load("lever_us_list.json"), "Acme Robotics") == "description_mentions"
    assert identity_from_payload(
        "lever_eu", _load("lever_eu_list.json"), "Acme Robotics") == "description_mentions"
    assert identity_from_payload(
        "ashby", _load("ashby_list.json"), "Acme Robotics") == "description_mentions"
    assert identity_from_payload("ashby", _load("ashby_list.json"), "Zeta Foods") is None


@pytest.mark.parametrize("ats", ["greenhouse", "lever", "lever_eu", "ashby", "unknown"])
@pytest.mark.parametrize("payload", [None, 7, "text", [], {}, {"jobs": None}, {"jobs": [1, "x"]},
                                     {"jobs": [{"descriptionPlain": 5, "company_name": 5}]}])
def test_malformed_payloads_return_none_and_never_raise(ats: str, payload: object) -> None:
    assert identity_from_payload(ats, payload, "Acme Robotics") is None


def test_an_empty_company_name_never_matches() -> None:
    assert identity_from_payload("lever", _lever(["anything"]), "  ,. ") is None
