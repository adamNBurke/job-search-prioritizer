"""Drift tests: docs/contracts.md, models.Posting, the criteria schema and paths.LAYOUT agree."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from helpers import bullets, first_column, md_section

from jobsearch import models, paths

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts.md"
SCHEMA = ROOT / "src" / "jobsearch" / "schemas" / "criteria.schema.json"
MINIMAL = ROOT / "tests" / "fixtures" / "criteria" / "minimal.json"


def _section(heading: str) -> str:
    return md_section(CONTRACTS.read_text(encoding="utf-8"), heading, doc="docs/contracts.md")


def _full_posting() -> models.Posting:
    return models.Posting(
        schema_version=1,
        role_id="lever_eu:acme-robotics:abc",
        source="ats",
        ats="lever_eu",
        board_slug="acme-robotics",
        posting_id="abc",
        company="Acme Robotics",
        title="Operations Manager",
        team="Operations",
        locations=["Berlin", "Remote"],
        work_mode_text="hybrid",
        work_mode="hybrid",
        posted="2026-11-01T12:00:00Z",
        url_apply="https://jobs.eu.lever.co/acme-robotics/abc/apply",
        url_posting="https://jobs.eu.lever.co/acme-robotics/abc",
        description_text="Run the ops.",
        comp_min=100000.0,
        comp_max=140000.0,
        comp_currency="USD",
        comp_interval="year",
        comp_source="structured",
        fetched_at="2026-11-12T09:00:00Z",
    )


def _empty_posting() -> models.Posting:
    return models.Posting(
        schema_version=1,
        role_id="pasted:-:0123456789ab",
        source="pasted",
        ats=None,
        board_slug=None,
        posting_id="0123456789ab",
        company="Acme Robotics",
        title="Operations Manager",
        team=None,
        locations=[],
        work_mode_text=None,
        work_mode="unknown",
        posted=None,
        url_apply="https://example.com/apply",
        url_posting=None,
        description_text="",
        comp_min=None,
        comp_max=None,
        comp_currency=None,
        comp_interval=None,
        comp_source=None,
        fetched_at="2026-11-12T09:00:00Z",
    )


def test_posting_has_22_fields_in_documented_order() -> None:
    names = [f.name for f in dataclasses.fields(models.Posting)]
    assert len(names) == 22
    assert names == first_column(_section("Posting"))


@pytest.mark.parametrize("make", [_full_posting, _empty_posting])
def test_posting_round_trips_through_dict(make) -> None:
    posting = make()
    assert models.Posting.from_dict(posting.to_dict()) == posting
    # to_dict must survive JSON, which is how it travels in JSONL.
    assert models.Posting.from_dict(json.loads(json.dumps(posting.to_dict()))) == posting


def test_from_dict_rejects_unknown_key() -> None:
    data = _full_posting().to_dict()
    data["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        models.Posting.from_dict(data)


def test_from_dict_rejects_missing_required_key() -> None:
    data = _full_posting().to_dict()
    del data["title"]
    with pytest.raises(ValueError, match="title"):
        models.Posting.from_dict(data)


@pytest.mark.parametrize(
    ("key", "bad"),
    [("work_mode", "on-site"), ("ats", "workday"), ("comp_source", "guessed"),
     ("source", "feed"), ("comp_interval", "fortnight")],
)
def test_from_dict_rejects_values_outside_vocabularies(key: str, bad: str) -> None:
    data = _full_posting().to_dict()
    data[key] = bad
    with pytest.raises(ValueError, match=key):
        models.Posting.from_dict(data)


def test_vocabularies_match_the_live_canary_findings() -> None:
    assert models.WORK_MODES == ("remote", "hybrid", "onsite", "unknown")
    assert models.ATS_IDS == ("greenhouse", "lever", "lever_eu", "ashby")


def test_make_role_id_forms() -> None:
    assert models.make_role_id("lever_eu", "acme-robotics", "abc") == "lever_eu:acme-robotics:abc"
    assert models.make_role_id("greenhouse", "acme", "12345") == "greenhouse:acme:12345"
    pasted = models.make_pasted_role_id("https://example.com/jobs/1")
    assert re.fullmatch(r"pasted:-:[0-9a-f]{12}", pasted)
    assert len(pasted.split(":")) == 3
    # Same input, same id; normalization ignores case and surrounding space.
    assert models.make_pasted_role_id("  HTTPS://Example.com/jobs/1 ") == pasted


@pytest.mark.parametrize(
    "args",
    [("workday", "a", "b"), ("greenhouse", "a:b", "1"), ("greenhouse", "", "1"),
     ("greenhouse", "a", "")],
)
def test_make_role_id_rejects_bad_parts(args) -> None:
    with pytest.raises(ValueError):
        models.make_role_id(*args)


def test_criteria_schema_required_list_matches_docs() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    documented = bullets(_section("criteria.json"))
    assert sorted(schema["required"]) == sorted(documented)
    assert set(schema["required"]) <= set(schema["properties"])


def test_minimal_criteria_fixture_obeys_the_schema_rules() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    assert set(schema["required"]) <= set(data)
    assert set(data) <= set(schema["properties"])
    assert data["schema_version"] == 1
    evaluators = set(schema["properties"]["criteria"]["items"]["properties"]["evaluator"]["enum"])
    assert {c["evaluator"] for c in data["criteria"]} <= evaluators
    assert {b["evaluator"] for b in data["bonuses"]} <= evaluators
    assert sum(c["weight"] for c in data["criteria"]) == 100
    tiers = data["tiers"]
    assert [t["id"] for t in tiers] == ["A", "B", "C"]
    mins = [t["min_score"] for t in tiers]
    assert all(a > b for a, b in zip(mins, mins[1:]))


def test_layout_matches_documented_table() -> None:
    section = _section("Data-folder layout")
    rows = [ln for ln in section.splitlines() if ln.startswith("|")][2:]
    documented = {r.split("|")[1].strip().strip("`"): r.split("|")[2].strip().strip("`")
                  for r in rows}
    assert documented == paths.LAYOUT


def test_docs_have_the_required_headings() -> None:
    text = CONTRACTS.read_text(encoding="utf-8")
    for heading in ("Posting", "Result", "Exit codes", "Data-folder layout", "Versioning",
                    "Guardrails", "QA checklist"):
        assert re.search(rf"^## {re.escape(heading)}$", text, re.M), heading


def test_result_fields_match_documented_envelope() -> None:
    names = [f.name for f in dataclasses.fields(models.Result)]
    assert names == first_column(_section("Result"))


def test_a_result_prints_as_one_compact_line_with_every_key() -> None:
    """One compact line, and the empty keys stay (the Result table is the contract)."""
    line = models.Result(ok=True, command="x").to_json()
    assert line == ('{"command":"x","counts":{},"data":{},"error":null,"next":null,'
                    '"ok":true,"warnings":[],"wrote":[]}')
