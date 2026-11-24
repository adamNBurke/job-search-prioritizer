"""Guards for the sanitized ATS fixtures.

Purpose: the fixtures under tests/fixtures/ats freeze the response shapes observed in the live
canary run. docs/ats-live-vs-docs.md holds a machine-readable keysets block; every fixture must
match it, so the doc and the fixtures cannot drift apart.

Guardrails: fixtures carry only fictional identity data (Acme Robotics, example.com hosts and
emails). Strings that must never appear (the social-network domains and the real board slug) are
built from fragments so this file never holds them as literals.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from helpers import ats_keysets

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "ats-live-vs-docs.md"
FIX = ROOT / "tests" / "fixtures" / "ats"

LIST_FIXTURES = {
    "greenhouse_list": "jobs",
    "lever_us_list": None,
    "lever_eu_list": None,
    "ashby_list": "jobs",
}
FILES = {
    "greenhouse_list": "greenhouse_list.json",
    "greenhouse_job": "greenhouse_job.json",
    "greenhouse_404": "greenhouse_404.json",
    "lever_us_list": "lever_us_list.json",
    "lever_eu_list": "lever_eu_list.json",
    "lever_404": "lever_404.json",
    "ashby_list": "ashby_list.json",
    "ashby_404": "ashby_404.txt",
}

# Built at runtime so no literal appears in this file.
SOCIAL_NEEDLES = ("linked" + "in", "li" + "cdn", "lnk" + "d.in")
REAL_SLUG = "git" + "lab"


def _raw(stem: str) -> bytes:
    return (FIX / FILES[stem]).read_bytes()


def _load(stem: str):
    return json.loads(_raw(stem).decode("utf-8"))


def _items(stem: str) -> list[dict]:
    data = _load(stem)
    key = LIST_FIXTURES[stem]
    return data if key is None else data[key]


KEYSETS = ats_keysets()


def test_every_fixture_stem_is_in_the_doc_block():
    assert set(FILES) <= set(KEYSETS)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", KEYSETS["captured_at"])


@pytest.mark.parametrize("stem", sorted(LIST_FIXTURES))
def test_list_fixture_key_sets_match_the_doc(stem):
    expected = KEYSETS[stem]
    data = _load(stem)
    if expected["top"] is None:
        assert isinstance(data, list)
    else:
        assert sorted(data) == expected["top"]
    items = _items(stem)
    assert 3 <= len(items) <= 5
    union = set().union(*(set(i) for i in items))
    common = set.intersection(*(set(i) for i in items))
    assert sorted(union) == expected["item"]
    # The optional keys must be exercised: present on some postings and absent on others.
    assert sorted(union - common) == expected["optional"]
    for item in items:
        assert set(item) >= set(expected["item"]) - set(expected["optional"])


def test_greenhouse_job_key_set_matches_the_doc():
    assert sorted(_load("greenhouse_job")) == KEYSETS["greenhouse_job"]["top"]


@pytest.mark.parametrize("stem", ["greenhouse_404", "lever_404", "ashby_404"])
def test_404_bodies_equal_the_documented_bytes(stem):
    expected = KEYSETS[stem]
    assert expected["status"] == 404
    assert _raw(stem) == expected["body"].encode("utf-8")
    if expected["top"] is not None:
        assert sorted(json.loads(_raw(stem))) == expected["top"]


def test_nested_key_sets_match_the_doc():
    nested = KEYSETS["nested"]
    job = _load("greenhouse_job")
    for rng in job["pay_input_ranges"]:
        assert sorted(rng) == nested["greenhouse_job.pay_input_ranges[]"]
    for item in _items("greenhouse_list"):
        assert sorted(item["location"]) == nested["greenhouse_list.location"]
    for item in _items("lever_us_list"):
        if "salaryRange" in item:
            assert sorted(item["salaryRange"]) == nested["lever_us_list.salaryRange"]
    for item in _items("ashby_list"):
        comp = item["compensation"]
        assert sorted(comp) == nested["ashby_list.compensation"]
        for tier in comp["compensationTiers"]:
            assert sorted(tier) == nested["ashby_list.compensation.compensationTiers[]"]
            for component in tier["components"]:
                assert sorted(component) == nested["ashby_list.compensation.compensationTiers[].components[]"]


def test_value_types_follow_the_live_run():
    gh = _items("greenhouse_list")
    assert all(isinstance(j["id"], int) for j in gh)
    assert all("&lt;" in j["content"] for j in gh)
    assert any("pay-range" in html.unescape(j["content"]) for j in gh)
    assert any("pay-range" not in html.unescape(j["content"]) for j in gh)
    assert any(html.unescape(j["content"]).count('class="pay-range"') >= 2 for j in gh)
    assert all(j["application_deadline"] is None and j["ai_disclaimer"] is None for j in gh)
    assert _load("greenhouse_list")["meta"]["total"] == len(gh)
    job = _load("greenhouse_job")
    assert all(
        isinstance(r["min_cents"], int) and isinstance(r["max_cents"], int) for r in job["pay_input_ranges"]
    )
    for stem in ("lever_us_list", "lever_eu_list"):
        items = _items(stem)
        assert all(isinstance(p["createdAt"], int) and len(str(p["createdAt"])) == 13 for p in items)
        assert all("country" in p for p in items)
    us = _items("lever_us_list")
    assert any("salaryRange" in p for p in us) and any("salaryRange" not in p for p in us)
    assert {p["workplaceType"] for p in us} <= {"onsite", "remote", "hybrid", "unspecified"}
    assert any(p["country"] is None for p in _items("lever_eu_list"))
    ashby = _load("ashby_list")
    assert ashby["apiVersion"] == "1"
    assert all(isinstance(j["compensation"], dict) for j in ashby["jobs"])
    intervals = {
        c["interval"]
        for j in ashby["jobs"]
        for t in j["compensation"]["compensationTiers"]
        for c in t["components"]
    }
    assert "1 YEAR" in intervals


@pytest.mark.parametrize("stem", sorted(FILES))
def test_no_social_network_strings(stem):
    text = _raw(stem).decode("utf-8").lower()
    assert not any(needle in text for needle in SOCIAL_NEEDLES)


@pytest.mark.parametrize("stem", sorted(FILES))
def test_only_fictional_identity_data(stem):
    raw = _raw(stem).decode("utf-8")
    for text in (raw, html.unescape(raw)):
        for email in re.findall(r"[\w.+-]+@[\w.-]+", text):
            assert email.lower().endswith("@example.com"), "email outside example.com"
        for host in re.findall(r"https?://([^/\s\"'<>?#&\\]+)", text):
            host = host.lower()
            assert host == "example.com" or host.endswith(".example.com"), "host outside example.com"
        assert not re.search(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b", text), "phone-shaped number"


def test_fictional_company_present_and_real_slug_absent():
    combined = "".join(_raw(stem).decode("utf-8") for stem in FILES).lower()
    assert "acme-robotics" in combined
    assert "acme robotics" in combined
    assert REAL_SLUG not in combined


def test_doc_names_the_board_generically():
    text = DOC.read_text(encoding="utf-8").lower()
    assert REAL_SLUG not in text
