"""Hard filters: one named reason per rejected role, and the unknown policy.

Purpose: prove that every role either passes all hard filters or fails exactly one named filter
with a plain-English reason, and that "the posting does not say" follows the user's policy.
Logic: unit tests call filters.apply_filters and filters.gap_filters on hand-built RoleFacts;
the end-to-end test drives cli.main (scan, then rank) on the fixture board the pipeline tracer
uses, whose postings already carry parsed pay.
Guardrails: fixtures are fictional (Acme Robotics, example.com); no test opens a socket; every
test builds its own data under tmp_path.
QA checklist: each filter type has a pass, a fail and an unknown case; the three edge cases
(adjacency, empty, ordering) each have a named test.
"""

from __future__ import annotations

import json

import pytest
from helpers import LIST_FIXTURE, TODAY, TRACER, facts_for, run_cli

from jobsearch import cli, families, filters
from jobsearch.roles_store import RoleFacts

PAY_FLOOR = {
    "id": "pay_floor", "type": "min_number", "field": "comp_max", "value": 90000,
    "currency": "USD", "unknown": "gap", "label": "Pay",
    "reject_reason": "Top of the stated range is {comp_max}, below your {value} floor.",
}


def criteria_with(*hard_filters: dict) -> dict:
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    data["hard_filters"] = list(hard_filters)
    return data


def check(facts: RoleFacts, *hard_filters: dict) -> filters.FilterFailure | None:
    criteria = criteria_with(*hard_filters)
    return filters.apply_filters(facts, criteria, families.compile_families(criteria))


def gaps(facts: RoleFacts, *hard_filters: dict) -> list[str]:
    criteria = criteria_with(*hard_filters)
    entries = filters.gap_filters(facts, criteria, families.compile_families(criteria))
    return [filters._label(entry) for entry in entries]


def with_policy(entry: dict, policy: str) -> dict:
    return dict(entry, unknown=policy)


def without(entry: dict, key: str) -> dict:
    return {k: v for k, v in entry.items() if k != key}


# annualize ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, interval, expected", [
    (90000, "year", 90000), (7500, "month", 90000), (1000, "week", 52000),
    (400, "day", 104000), (45, "hour", 93600),
])
def test_annualize_uses_the_documented_factors(value, interval, expected):
    assert filters.annualize(value, interval) == expected


def test_annualize_reads_a_missing_interval_as_year_only_for_a_year_sized_number():
    assert filters.annualize(120000, None) == 120000
    assert filters.annualize(9999, None) is None


def test_annualize_cannot_read_an_interval_it_does_not_know():
    assert filters.annualize(100, "other") is None
    assert filters.annualize(100, "fortnight") is None


# min_number (the tracer path) -----------------------------------------------------------------

def test_pay_under_the_floor_fails_with_the_filter_named_and_the_numbers_in_the_reason():
    failure = check(facts_for(comp_max=80000.0, comp_currency="USD", comp_interval="year"),
                    PAY_FLOOR)
    assert failure == filters.FilterFailure(
        "pay_floor", "Pay", "Top of the stated range is 80,000 USD, below your 90,000 USD floor.")


def test_pay_exactly_at_the_floor_passes():
    assert check(facts_for(comp_max=90000.0, comp_currency="USD", comp_interval="year"),
                 PAY_FLOOR) is None


def test_an_hourly_rate_is_annualized_before_the_comparison():
    facts = facts_for(comp_max=45.0, comp_currency="USD", comp_interval="hour")
    assert check(facts, PAY_FLOOR) is None


def test_an_hourly_rate_under_the_floor_fails_and_the_reason_shows_the_annual_figure():
    facts = facts_for(comp_max=40.0, comp_currency="USD", comp_interval="hour")
    failure = check(facts, PAY_FLOOR)
    assert failure is not None
    assert failure.reason == "Top of the stated range is 83,200 USD, below your 90,000 USD floor."


def test_unknown_pay_passes_with_a_gap_by_default():
    facts = facts_for()
    assert check(facts, PAY_FLOOR) is None
    assert gaps(facts, PAY_FLOOR) == ["Pay"]


def test_unknown_pay_fails_when_the_user_marked_the_filter_strict():
    failure = check(facts_for(), with_policy(PAY_FLOOR, "reject"))
    assert failure == filters.FilterFailure(
        "pay_floor", "Pay", "Pay is not stated in the posting, and you marked it as a must.")


def test_a_currency_that_differs_from_the_floors_is_unknown_not_a_failure():
    facts = facts_for(comp_max=50000.0, comp_currency="EUR", comp_interval="year")
    assert check(facts, PAY_FLOOR) is None
    assert gaps(facts, PAY_FLOOR) == ["Pay"]


def test_a_missing_interval_reads_as_year_when_the_number_is_year_sized():
    assert check(facts_for(comp_max=85000.0, comp_currency="USD"), PAY_FLOOR) is not None
    assert check(facts_for(comp_max=95000.0, comp_currency="USD"), PAY_FLOOR) is None


def test_a_missing_interval_on_a_small_number_is_unknown():
    facts = facts_for(comp_max=45.0, comp_currency="USD")
    assert check(facts, PAY_FLOOR) is None
    assert gaps(facts, PAY_FLOOR) == ["Pay"]


def test_the_floor_can_read_the_bottom_of_the_range():
    entry = dict(without(PAY_FLOOR, "reject_reason"), field="comp_min")
    failure = check(facts_for(comp_min=70000.0, comp_max=120000.0, comp_currency="USD"), entry)
    assert failure is not None
    assert failure.reason == "Bottom of the stated range is 70,000 USD, below your 90,000 USD floor."


def test_reason_templates_fill_only_the_documented_fields_and_leave_other_braces_alone():
    entry = dict(PAY_FLOOR, reject_reason="{company} pays {comp_max} ({comp_currency}); {team} {x}")
    failure = check(facts_for(comp_max=80000.0, comp_currency="USD", team="Ops"), entry)
    assert failure is not None
    assert failure.reason == "Acme Robotics pays 80,000 USD (USD); {team} {x}"


# End to end through the CLI -------------------------------------------------------------------

# The fixture board already states pay for two roles: 7000001 up to 210,000 USD, 7000002 up to
# 95,000 USD, and none for 7000003. A 100,000 USD floor therefore separates all three.
FLOOR_100K = dict(PAY_FLOOR, value=100000)


@pytest.fixture
def env(scan_env):
    (scan_env.folder / "config" / "criteria.json").write_text(
        json.dumps(criteria_with(FLOOR_100K)), encoding="utf-8")
    return scan_env


def test_a_role_under_the_pay_floor_leaves_the_ranking_with_a_named_reason(env):
    run_cli(env, "scan")
    run_cli(env, "rank")
    ranked = json.loads(
        (env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))
    kept = {r["role_id"] for r in ranked["roles"]}
    assert "greenhouse:acme-robotics:7000002" not in kept
    assert "greenhouse:acme-robotics:7000001" in kept
    rejected_file = json.loads(
        (env.folder / "ranked" / "rejected-2026-11-17.json").read_text(encoding="utf-8"))
    rejected = {r["role_id"]: r for r in rejected_file["rejected"]}
    under = rejected["greenhouse:acme-robotics:7000002"]
    assert under["filter_id"] == "pay_floor"
    assert under["reason"] == (
        "Top of the stated range is 95,000 USD, below your 100,000 USD floor.")
    unpaid = next(r for r in ranked["roles"] if r["role_id"].endswith(":7000003"))
    assert "Pay" in unpaid["gaps"]


# The five filter types and the unknown policy --------------------------------------------

REGION_US = {"id": "region", "type": "region_allow", "allow": ["US"], "unknown": "gap",
             "label": "Region"}
REGION_EU = dict(REGION_US, allow=["EU"])
WORK_MODE = {"id": "work_mode", "type": "enum_allow", "field": "work_mode", "allow": ["remote"],
             "unknown": "gap", "label": "Work arrangement"}
NO_SPONSOR = {"id": "sponsorship", "type": "deny_terms", "field": "description_text",
              "terms": ["unable to sponsor"], "unknown": "pass", "label": "Sponsorship",
              "reject_reason": "{company} says it cannot sponsor ({term})."}
NO_GO = {"id": "no_go_companies", "type": "deny_list", "names": ["acme robotics"],
         "unknown": "pass", "label": "Companies you ruled out"}


def test_the_registry_holds_exactly_the_five_types_the_schema_allows():
    assert sorted(filters.FILTER_TYPES) == [
        "deny_list", "deny_terms", "enum_allow", "min_number", "region_allow"]


def test_region_allow_fails_a_london_posting_and_names_the_location():
    failure = check(facts_for(locations=["London, UK"]), REGION_US)
    assert failure is not None and failure.filter_id == "region"
    assert "London, UK" in failure.reason and "US" in failure.reason


def test_region_allow_passes_a_us_posting():
    assert check(facts_for(locations=["Austin, TX"]), REGION_US) is None


def test_region_allow_treats_a_posting_with_no_parseable_location_as_unknown():
    for locations in ([], ["Somewhere imaginary"]):
        facts = facts_for(locations=locations)
        assert check(facts, REGION_US) is None
        assert gaps(facts, REGION_US) == ["Region"]


def test_region_allow_does_not_reject_a_bare_remote_posting_on_a_guess():
    facts = facts_for(locations=["Remote"], work_mode="remote")
    assert check(facts, REGION_US) is None
    assert gaps(facts, REGION_US) == ["Region"]


def test_region_allow_reads_eu_membership_from_the_shared_parser():
    assert check(facts_for(locations=["Berlin, Germany"]), REGION_EU) is None
    failure = check(facts_for(locations=["Austin, TX"]), REGION_EU)
    assert failure is not None and failure.filter_id == "region"


def test_region_allow_uses_a_template_when_the_config_has_one():
    entry = dict(REGION_US, reject_reason="{company} hires outside your regions.")
    failure = check(facts_for(locations=["London, UK"]), entry)
    assert failure is not None
    assert failure.reason == "Acme Robotics hires outside your regions."


# Region from the places a posting states, then from a place in the title ----------

TITLE_FAMILY = {
    "id": "tf-region-test", "label": "Any manager or executive", "rung": 1,
    "terms": ["manager", "account executive"], "regex": [],
    "origin": "default",
}


def region_check(facts: RoleFacts, *hard_filters: dict) -> filters.FilterFailure | None:
    """check(), but with a title family wide enough that the region filter is what decides."""
    criteria = criteria_with(*hard_filters)
    criteria["title"]["families"] = [*criteria["title"]["families"], TITLE_FAMILY]
    return filters.apply_filters(facts, criteria, families.compile_families(criteria))


@pytest.mark.parametrize("title, locations", [
    ("Revenue Operations Manager, Japan", ["Japan"]),
    ("Principal HR - APJC Benefits and Program Manager", ["Hybrid"]),
    ("Program Manager, Workplace Experience & Real Estate (APAC)", ["Shanghai, China"]),
    ("Workplace Operations Manager - Singapore", ["In-Office"]),
])
def test_region_allow_rejects_the_uat_postings_for_a_us_only_user(title, locations):
    failure = region_check(facts_for(title=title, locations=locations), REGION_US)
    assert failure is not None and failure.filter_id == "region"


@pytest.mark.parametrize("title", [
    "Revenue Operations Manager, New Mexico", "Revenue Operations Manager - Lima, Ohio",
    "Revenue Operations Manager Paris, Texas", "Revenue Operations Manager Cairo, IL",
    "Revenue Operations Manager, Dublin, CA", "Revenue Operations Manager, New Jersey",
])
def test_a_title_place_that_is_also_a_us_place_never_rejects_a_us_only_user(title):
    """A US state or a city followed by a state is unknown, never a reject."""
    facts = facts_for(title=title, locations=["Remote"])
    assert region_check(facts, REGION_US) is None


@pytest.mark.parametrize("title", [
    "Revenue Operations Manager - Toronto, ON", "Revenue Operations Manager, Vancouver, BC",
])
def test_a_title_place_followed_by_a_canadian_province_rejects_a_us_only_user(title):
    """A province is Canada, not a US place, so a bare Remote does not save it."""
    failure = region_check(facts_for(title=title, locations=["Remote"]), REGION_US)
    assert failure is not None and failure.filter_id == "region"


def test_a_genuinely_foreign_title_place_still_rejects_next_to_a_bare_remote():
    for title in ("Revenue Operations Manager, Paris", "Revenue Operations Manager, Dublin",
                  "Revenue Operations Manager, Lima, Peru"):
        failure = region_check(facts_for(title=title, locations=["Remote"]), REGION_US)
        assert failure is not None and failure.filter_id == "region", title


def test_the_title_decides_when_the_city_and_state_disagree():
    """Lucid's "Amsterdam, NH" reads as no place, so a Europe title is rejected by a
    US-only filter, and the same title at a real US place passes."""
    title = "Body Repair Operations Manager - Europe"
    failure = region_check(facts_for(title=title, locations=["Amsterdam, NH"]), REGION_US)
    assert failure is not None and failure.filter_id == "region"
    assert failure.reason == (
        "The title places the role in Europe, outside the regions you allowed (US).")
    assert region_check(facts_for(title=title, locations=["Fremont, CA"]), REGION_US) is None


def test_a_country_in_the_locations_always_wins_over_the_title():
    facts = facts_for(title="Program Manager, APAC Partnerships", locations=["Austin, TX"])
    assert region_check(facts, REGION_US) is None


def test_a_title_that_names_an_allowed_country_passes():
    facts = facts_for(title="Account Executive, US", locations=["Remote"])
    assert region_check(facts, REGION_US) is None


def test_region_stays_unknown_when_neither_locations_nor_title_name_a_place():
    for locations in ([], ["Somewhere imaginary"], ["Remote"]):
        facts = facts_for(locations=locations)
        assert region_check(facts, REGION_US) is None
        assert gaps(facts, REGION_US) == ["Region"]


def test_a_region_failure_decided_by_the_title_names_the_title_place():
    facts = facts_for(title="Revenue Operations Manager, Japan", locations=["Hybrid"])
    failure = region_check(facts, REGION_US)
    assert failure is not None
    assert failure.reason == (
        "The title places the role in Japan, outside the regions you allowed (US).")


def test_a_title_decided_region_failure_uses_the_template_when_there_is_one():
    entry = dict(REGION_US, reject_reason="{company} hires outside your regions.")
    facts = facts_for(title="Revenue Operations Manager, Japan", locations=["Hybrid"])
    failure = region_check(facts, entry)
    assert failure is not None
    assert failure.reason == "Acme Robotics hires outside your regions."


def test_enum_allow_fails_an_onsite_posting_and_passes_a_remote_one():
    failure = check(facts_for(work_mode="onsite"), WORK_MODE)
    assert failure is not None and failure.filter_id == "work_mode"
    assert "onsite" in failure.reason
    assert check(facts_for(work_mode="remote"), WORK_MODE) is None


def test_enum_allow_treats_an_unknown_work_mode_as_unknown():
    facts = facts_for(work_mode="unknown")
    assert check(facts, WORK_MODE) is None
    assert gaps(facts, WORK_MODE) == ["Work arrangement"]
    assert check(facts, with_policy(WORK_MODE, "reject")) is not None


def test_enum_allow_reads_company_facts_too():
    size = {"id": "size", "type": "enum_allow", "field": "company_size",
            "allow": ["51-200"], "unknown": "gap", "label": "Company size"}
    base = facts_for()
    big = RoleFacts(base.posting, None, "5001+", None, "2026-11-17", "2026-11-17")
    small = RoleFacts(base.posting, None, "51-200", None, "2026-11-17", "2026-11-17")
    assert check(big, size) is not None
    assert check(small, size) is None
    assert gaps(base, size) == ["Company size"]


def test_deny_terms_fails_a_posting_that_says_it_cannot_sponsor():
    facts = facts_for(description_text="Sorry, we are unable to sponsor visas for this role.")
    failure = check(facts, NO_SPONSOR)
    assert failure == filters.FilterFailure(
        "sponsorship", "Sponsorship", "Acme Robotics says it cannot sponsor (unable to sponsor).")


def test_deny_terms_matches_whole_phrases_case_insensitively_with_flexible_separators():
    assert check(facts_for(description_text="We are UNABLE   to\nSponsor anyone."),
                 NO_SPONSOR) is not None
    intern = dict(without(NO_SPONSOR, "reject_reason"), terms=["intern"])
    assert check(facts_for(description_text="An international team."), intern) is None
    assert check(facts_for(description_text="You will mentor an intern."), intern) is not None


def test_deny_terms_reads_the_curly_apostrophe_the_way_a_posting_writes_it():
    entry = dict(NO_SPONSOR, terms=["can't sponsor"])
    facts = facts_for(description_text="Unfortunately we can’t sponsor work visas.")
    assert check(facts, entry) is not None


def test_deny_terms_reads_title_plus_description_for_text():
    entry = dict(NO_SPONSOR, field="text", terms=["night shift"])
    assert check(facts_for(title="Night Shift Technician"), entry) is not None
    assert check(facts_for(description_text="Includes night shift cover."), entry) is not None
    assert check(facts_for(), entry) is None


def test_deny_terms_on_the_company_industry_follows_the_company_fact():
    entry = INDUSTRY_DENY
    assert check(facts_for(industry="logistics-transportation"), entry) is not None
    assert check(facts_for(industry="software"), entry) is None
    assert check(facts_for(industry=None), entry) is None


def test_deny_terms_never_treats_a_term_as_a_pattern():
    entry = dict(without(NO_SPONSOR, "reject_reason"), terms=["(a+)+$", "a.*b"])
    nasty = "a" * 5000 + "!"
    assert check(facts_for(description_text=nasty + " axxb"), entry) is None


def test_deny_list_matches_a_company_after_normalizing_both_sides():
    assert check(facts_for(company="Acme Robotics, Inc."), NO_GO) is not None
    assert check(facts_for(company="ACME  ROBOTICS"), NO_GO) is not None
    assert check(facts_for(company="Acme Robotic Arms"), NO_GO) is None
    shouting = dict(NO_GO, names=["Acme Robotics Inc."])
    assert check(facts_for(company="acme robotics"), shouting) is not None


def test_deny_list_with_no_names_passes_everything():
    assert check(facts_for(), dict(NO_GO, names=[])) is None


def test_deny_list_reason_names_the_company_when_the_config_has_no_template():
    failure = check(facts_for(company="Acme Robotics, Inc."), NO_GO)
    assert failure is not None
    assert failure.reason == "Acme Robotics, Inc. is on your list of companies to skip."


INDUSTRY_DENY = {"id": "industry", "type": "deny_terms", "field": "company_industry",
                 "terms": ["logistics"], "unknown": "pass", "label": "Industry"}

# One entry per filter type, with the posting overrides that make its answer unknown.
UNKNOWN_CASES = {
    "enum_allow": (WORK_MODE, {"work_mode": "unknown"}),
    "min_number": (PAY_FLOOR, {}),
    "region_allow": (REGION_US, {"locations": []}),
    "deny_terms": (INDUSTRY_DENY, {}),
    "deny_list": (NO_GO, {"company": ""}),
}


@pytest.mark.parametrize("kind", sorted(UNKNOWN_CASES))
def test_unknown_policy_gap_passes_and_lists_the_label(kind):
    entry, posting = UNKNOWN_CASES[kind]
    facts = facts_for(**posting)
    assert check(facts, with_policy(entry, "gap")) is None
    assert gaps(facts, with_policy(entry, "gap")) == [entry["label"]]


@pytest.mark.parametrize("kind", sorted(UNKNOWN_CASES))
def test_unknown_policy_pass_passes_silently(kind):
    entry, posting = UNKNOWN_CASES[kind]
    facts = facts_for(**posting)
    assert check(facts, with_policy(entry, "pass")) is None
    assert gaps(facts, with_policy(entry, "pass")) == []


@pytest.mark.parametrize("kind", sorted(UNKNOWN_CASES))
def test_unknown_policy_reject_fails_naming_the_filter(kind):
    entry, posting = UNKNOWN_CASES[kind]
    facts = facts_for(**posting)
    failure = check(facts, with_policy(entry, "reject"))
    assert failure == filters.FilterFailure(
        entry["id"], entry["label"],
        f"{entry['label']} is not stated in the posting, and you marked it as a must.")
    assert gaps(facts, with_policy(entry, "reject")) == []


# Edges: empty, ordering ---------------------------------------------------------------

def test_with_no_hard_filters_only_the_title_gate_can_reject_a_role():
    assert check(facts_for(title="Robotics Engineer")) is None
    failure = check(facts_for(title="Pastry Chef"))
    assert failure == filters.FilterFailure(
        "title_family", "Title", "Title is outside every family you approved.")


def test_the_empty_filter_list_still_reports_the_title_rejection_in_the_ranking():
    from jobsearch import scoring

    criteria = criteria_with()
    compiled = families.compile_families(criteria)
    scored = scoring.score_role(
        facts_for(title="Pastry Chef"), criteria, compiled, TODAY.date())
    assert scored.rejected is not None and scored.rejected.filter_id == "title_family"


def test_the_title_gate_runs_before_any_hard_filter():
    failure = check(facts_for(title="Pastry Chef", work_mode="onsite"), WORK_MODE)
    assert failure is not None and failure.filter_id == "title_family"


def test_a_role_failing_two_filters_is_reported_against_the_first_in_config_order():
    facts = facts_for(locations=["London, UK"],
                      description_text="We are unable to sponsor visas.")
    first = check(facts, REGION_US, NO_SPONSOR)
    flipped = check(facts, NO_SPONSOR, REGION_US)
    assert first is not None and first.filter_id == "region"
    assert flipped is not None and flipped.filter_id == "sponsorship"


def test_an_unknown_policy_reject_earlier_in_the_list_wins_over_a_later_failure():
    facts = facts_for(locations=["London, UK"])
    failure = check(facts, with_policy(PAY_FLOOR, "reject"), REGION_US)
    assert failure is not None and failure.filter_id == "pay_floor"


# Words that rule out another function, per title family ---------------------

OPS_ANALYST_ID = ":7000003"


def set_family_words(env, words: list[str] | None, rename: str | None = None) -> None:
    """Write the tracer config with tf-ops carrying `words`; optionally rename job 7000003."""
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    data["hard_filters"] = []
    if words is not None:
        next(f for f in data["title"]["families"] if f["id"] == "tf-ops")["exclude"] = words
    (env.folder / "config" / "criteria.json").write_text(json.dumps(data), encoding="utf-8")
    body = json.loads(LIST_FIXTURE.read_text(encoding="utf-8"))
    if rename is not None:
        next(j for j in body["jobs"] if j["id"] == 7000003)["title"] = rename
    env.transport.body = json.dumps(body).encode("utf-8")


def scan_and_rank(env) -> dict:
    for command in ("scan", "rank"):
        run_cli(env, command)
    return json.loads(
        (env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))


def rejected_list(env) -> list[dict]:
    path = env.folder / "ranked" / "rejected-2026-11-17.json"
    return json.loads(path.read_text(encoding="utf-8"))["rejected"]


def test_a_family_word_rejects_the_title_at_rank_and_is_counted(scan_env):
    set_family_words(scan_env, ["legal"], rename="Legal Operations Analyst")
    ranked = scan_and_rank(scan_env)
    rejected = {r["role_id"].rsplit(":", 1)[1]: r for r in rejected_list(scan_env)}
    assert rejected["7000003"]["filter_id"] == "title_function"
    assert rejected["7000003"]["reason"] == (
        'Title contains "legal", which you ruled out for Operations analyst.')
    assert ranked["funnel"]["rejected_by_filters"] == 1
    text = (scan_env.folder / "ranked" / "ranked-2026-11-17.md").read_text(encoding="utf-8")
    assert "- Function you ruled out: 1" in text


def test_the_scan_keeps_a_title_a_family_word_rejects(scan_env):
    def scanned() -> tuple[list[str], int]:
        code = cli.main(["scan", "--data-dir", str(scan_env.folder)])
        out = json.loads(scan_env.capsys.readouterr().out)
        assert code == 0 and out["ok"], out
        d = scan_env.folder / "scans" / "2026-11-17"
        rows = (d / "postings.jsonl").read_text(encoding="utf-8").splitlines()
        meta = json.loads((d / "scan-meta.json").read_text(encoding="utf-8"))
        return [json.loads(r)["role_id"] for r in rows], meta["totals"]["title_gate_dropped"]

    set_family_words(scan_env, None, rename="Legal Operations Analyst")
    plain_ids, plain_dropped = scanned()
    set_family_words(scan_env, ["legal"], rename="Legal Operations Analyst")
    word_ids, word_dropped = scanned()
    assert any(r.endswith(OPS_ANALYST_ID) for r in word_ids)
    assert word_ids == plain_ids and word_dropped == plain_dropped


def test_a_title_without_the_word_still_passes(scan_env):
    set_family_words(scan_env, None)
    before = next(r for r in scan_and_rank(scan_env)["roles"]
                  if r["role_id"].endswith(OPS_ANALYST_ID))
    set_family_words(scan_env, ["legal"])
    ranked = scan_and_rank(scan_env)
    after = next(r for r in ranked["roles"] if r["role_id"].endswith(OPS_ANALYST_ID))
    assert after["score"] == before["score"] and after["tier"] == before["tier"]
    assert after["reason_text"] == before["reason_text"]
    assert all(r["filter_id"] != "title_function" for r in rejected_list(scan_env))


def _with_words(family_id: str, words: list[str]) -> dict:
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    next(f for f in data["title"]["families"] if f["id"] == family_id)["exclude"] = words
    return data


def test_a_family_word_matches_literally_and_only_for_its_family():
    data = _with_words("tf-ops", ["c++ (r&d)", "legal"])
    compiled = families.compile_families(data)
    ops = "Operations Analyst"
    # A word with pattern characters is plain text: it matches itself and nothing else.
    assert families.function_excluded(f"{ops} C++ (R&D)", compiled) == (
        "c++ (r&d)", "Operations analyst")
    assert families.function_excluded(f"{ops} C R D", compiled) is None
    assert families.function_excluded(f"{ops} CCCC", compiled) is None
    # The whole plain title is read, so text after " - " and after a comma counts.
    assert families.function_excluded(f"{ops} - Legal", compiled) == (
        "legal", "Operations analyst")
    assert families.function_excluded(f"{ops}, Legal", compiled) == (
        "legal", "Operations analyst")
    assert families.function_excluded(f"{ops} Illegalities", compiled) is None  # whole words
    # A word on one family never rejects a title another family wins.
    other = families.compile_families(_with_words("tf-field", ["legal"]))
    assert families.function_excluded("Legal Operations Analyst", other) is None
    assert families.function_excluded("Legal Field Technician", other) == (
        "legal", "Field technician")
    # A title in no family has no function to rule out.
    assert families.function_excluded("Pastry Chef Legal", compiled) is None
