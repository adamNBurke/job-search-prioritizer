"""Board adapters: Greenhouse pay, Lever US and EU, Ashby, and the pay text parser.

Purpose: prove every supported job-board system normalizes into valid Posting values from the
sanitized live-shape fixtures in tests/fixtures/ats, and that scan then rank works end to end on
a Lever board on either host.
Logic: adapters are called directly with fixture payloads; the end-to-end tests drive cli.main
with a scripted fetch transport (no socket) and a one-entry fixture directory.
Guardrails: fixtures hold fictional data only (Acme Robotics, example.com). Each test builds its
own data folder under tmp_path.
QA checklist: a new adapter or a new live key gets a named test here and a fixture update first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import TRACER, board, run_cli, write_directory

from jobsearch.directory import BoardEntry
from jobsearch.fetch import FetchError
from jobsearch.models import Posting
from jobsearch.sources import parse_board

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "fixtures" / "ats"
FETCHED_AT = "2026-11-17T12:00:00Z"


def load(name: str) -> object:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def entry(ats: str, slug: str = "acme-robotics", company: str = "Acme Robotics") -> BoardEntry:
    return BoardEntry(
        company=company, ats=ats, slug=slug, industry="manufacturing-hardware", size="51-200",
        stage="growth",
    )


# ---------------------------------------------------------------------------------- Lever


def lever(ats: str, fixture: str) -> list[Posting]:
    return parse_board(entry(ats), load(fixture), FETCHED_AT)


def by_title(postings: list[Posting], title: str) -> Posting:
    return next(p for p in postings if p.title == title)


def test_every_lever_us_posting_round_trips_through_posting_from_dict():
    postings = lever("lever", "lever_us_list.json")
    assert len(postings) == len(load("lever_us_list.json"))
    for posting in postings:
        assert Posting.from_dict(posting.to_dict()) == posting


def test_every_lever_eu_posting_round_trips_through_posting_from_dict():
    postings = lever("lever_eu", "lever_eu_list.json")
    assert len(postings) == len(load("lever_eu_list.json"))
    for posting in postings:
        assert Posting.from_dict(posting.to_dict()) == posting


def test_lever_ats_value_follows_the_host_and_is_part_of_the_role_id():
    us = lever("lever", "lever_us_list.json")
    eu = lever("lever_eu", "lever_eu_list.json")
    assert {p.ats for p in us} == {"lever"}
    assert {p.ats for p in eu} == {"lever_eu"}
    assert all(p.role_id.startswith("lever:acme-robotics:") for p in us)
    assert all(p.role_id.startswith("lever_eu:acme-robotics:") for p in eu)


def test_lever_created_at_epoch_milliseconds_become_utc_z():
    payload = [dict(load("lever_us_list.json")[0], createdAt=1700000000000)]
    posting = parse_board(entry("lever"), payload, FETCHED_AT)[0]
    assert posting.posted == "2023-11-14T22:13:20Z"


def test_lever_workplace_type_maps_and_the_raw_value_is_kept():
    postings = lever("lever", "lever_us_list.json")
    assert by_title(postings, "Field Technician").work_mode == "onsite"
    assert by_title(postings, "Senior Robotics Engineer").work_mode == "remote"
    assert by_title(postings, "Product Designer").work_mode == "hybrid"
    unspecified = by_title(postings, "Support Specialist")
    assert unspecified.work_mode == "unknown"
    assert unspecified.work_mode_text == "unspecified"
    assert by_title(postings, "Field Technician").work_mode_text == "onsite"


def test_lever_missing_team_is_none_and_locations_join_without_duplicates():
    postings = lever("lever", "lever_us_list.json")
    assert by_title(postings, "Field Technician").team is None
    assert by_title(postings, "Senior Robotics Engineer").team == "Robotics"
    payload = [dict(load("lever_us_list.json")[0], categories={
        "location": "Berlin", "allLocations": ["Paris", "Berlin", "Paris", "Rome"]})]
    posting = parse_board(entry("lever"), payload, FETCHED_AT)[0]
    assert posting.locations == ["Berlin", "Paris", "Rome"]


def test_lever_salary_range_is_structured_pay_per_year():
    designer = by_title(lever("lever", "lever_us_list.json"), "Product Designer")
    assert (designer.comp_min, designer.comp_max) == (80000.0, 110000.0)
    assert (designer.comp_currency, designer.comp_interval) == ("USD", "year")
    assert designer.comp_source == "structured"


def test_lever_without_salary_range_has_no_pay_fields():
    tech = by_title(lever("lever", "lever_us_list.json"), "Field Technician")
    assert (tech.comp_min, tech.comp_max, tech.comp_currency) == (None, None, None)
    assert (tech.comp_interval, tech.comp_source) == (None, None)


def test_lever_other_interval_words_map_or_fall_to_other():
    base = load("lever_us_list.json")[0]
    for word, expected in (("per-hour-wage", "hour"), ("per-month-salary", "month"),
                           ("per-day-salary", "day"), ("per-week-salary", "week"),
                           ("one-off-fee", "other")):
        payload = [dict(base, salaryRange={"currency": "USD", "interval": word,
                                           "min": 10, "max": 20})]
        assert parse_board(entry("lever"), payload, FETCHED_AT)[0].comp_interval == expected


def test_lever_description_joins_plain_text_lists_and_closing_text():
    posting = by_title(lever("lever", "lever_us_list.json"), "Senior Robotics Engineer")
    text = posting.description_text
    assert "What you will do:\nInvented task one\nInvented task two" in text
    assert text.index("What you will do:") < text.index("Skill Set:")
    assert text.endswith("Invented closing text for the fixture.")
    assert "<" not in text


def test_lever_decodes_a_literal_less_than_in_running_text():
    posting = by_title(lever("lever_eu", "lever_eu_list.json"), "Data Analyst")
    assert "<" not in posting.description_text.replace("1 < 2", "")


def test_lever_non_list_response_is_a_plain_fetch_error():
    with pytest.raises(FetchError, match="shape this tool does not recognize"):
        parse_board(entry("lever"), {"jobs": []}, FETCHED_AT)


def test_lever_skips_a_posting_without_an_id_title_or_apply_link():
    good = load("lever_us_list.json")[0]
    broken = [{k: v for k, v in good.items() if k != "id"},
              {k: v for k, v in good.items() if k != "text"},
              {k: v for k, v in good.items() if k not in ("applyUrl", "hostedUrl")},
              "not a posting", good]
    assert len(parse_board(entry("lever"), broken, FETCHED_AT)) == 1


# ---------------------------------------------------------------------------------- Ashby


def ashby_payload() -> dict:
    return load("ashby_list.json")  # type: ignore[return-value]


def ashby_one(**changes) -> Posting:
    job = dict(ashby_payload()["jobs"][0], **changes)
    return parse_board(entry("ashby"), {"jobs": [job]}, FETCHED_AT)[0]


def salary(low, high, interval="1 YEAR", currency="USD") -> dict:
    return {"compensationType": "Salary", "currencyCode": currency, "id": "c", "interval": interval,
            "minValue": low, "maxValue": high, "summary": "x"}


def tiers(*components_per_tier: list[dict]) -> dict:
    return {"compensationTierSummary": "x", "scrapeableCompensationSalarySummary": "x",
            "summaryComponents": [],
            "compensationTiers": [{"id": str(i), "title": "t", "tierSummary": "x",
                                   "additionalInformation": None, "components": comps}
                                  for i, comps in enumerate(components_per_tier)]}


def test_every_ashby_job_parses_into_a_valid_posting():
    postings = parse_board(entry("ashby"), ashby_payload(), FETCHED_AT)
    assert len(postings) == len(ashby_payload()["jobs"])
    for posting in postings:
        assert posting.ats == "ashby" and posting.role_id.startswith("ashby:acme-robotics:")
        assert Posting.from_dict(posting.to_dict()) == posting


def test_ashby_locations_join_the_main_and_secondary_locations_in_order():
    postings = parse_board(entry("ashby"), ashby_payload(), FETCHED_AT)
    assert by_title(postings, "Senior Robotics Engineer").locations == [
        "Remote - Europe", "Spain"]
    assert by_title(postings, "Data Engineer").locations == [
        "Remote - UK", "Ireland", "France"]


def test_ashby_workplace_type_maps_and_is_kept_as_sent():
    assert ashby_one(workplaceType="Remote").work_mode == "remote"
    assert ashby_one(workplaceType="Hybrid").work_mode == "hybrid"
    assert ashby_one(workplaceType="OnSite").work_mode == "onsite"
    assert ashby_one(workplaceType="Onsite").work_mode == "onsite"
    other = ashby_one(workplaceType="Flexible")
    assert other.work_mode == "unknown" and other.work_mode_text == "Flexible"
    assert ashby_one(workplaceType="OnSite").work_mode_text == "OnSite"


def test_ashby_is_remote_alone_never_sets_remote():
    assert ashby_one(workplaceType="Hybrid", isRemote=True).work_mode == "hybrid"
    assert ashby_one(workplaceType="Something", isRemote=True).work_mode == "unknown"


def test_ashby_maps_published_at_team_urls_and_description():
    posting = ashby_one()
    assert posting.posted == "2025-03-04T14:29:08Z"
    assert posting.team == "Platform"
    assert posting.url_apply.endswith("/application") and posting.url_posting is not None
    assert posting.description_text.startswith("Acme Robotics builds invented")


def test_ashby_single_salary_component_gives_structured_pay():
    posting = ashby_one()
    assert (posting.comp_min, posting.comp_max) == (100000.0, 150000.0)
    assert (posting.comp_currency, posting.comp_interval) == ("EUR", "year")
    assert posting.comp_source == "structured"


def test_ashby_several_salary_components_keep_lowest_min_highest_max_and_warn():
    warnings: list[str] = []
    postings = parse_board(entry("ashby"), ashby_payload(), FETCHED_AT, warnings=warnings)
    posting = by_title(postings, "Account Executive")
    assert (posting.comp_min, posting.comp_max) == (130000.0, 210000.0)
    assert len(warnings) == 1 and "Account Executive" in warnings[0]


def test_ashby_mixed_currency_components_merge_only_the_first_currency_and_warn():
    warnings: list[str] = []
    job = dict(ashby_payload()["jobs"][0], title="Mixed Pay Analyst", compensation=tiers(
        [salary(100000, 140000), salary(120000, 150000, currency="GBP"),
         salary(90000, 99000, currency="EUR"), salary(110000, 160000)]))
    posting = parse_board(entry("ashby"), {"jobs": [job]}, FETCHED_AT, warnings=warnings)[0]
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (
        100000.0, 160000.0, "USD")
    assert len(warnings) == 2 and all("Mixed Pay Analyst" in w for w in warnings)
    assert "2 salary ranges in EUR, GBP" in warnings[0] and "not used" in warnings[0]


def test_ashby_interval_none_gives_no_interval():
    posting = ashby_one(compensation=tiers([salary(50, 60, interval="NONE")]))
    assert (posting.comp_min, posting.comp_interval) == (50.0, None)


def test_ashby_hourly_interval_maps_to_hour():
    posting = ashby_one(compensation=tiers([salary(40, 55, interval="1 HOUR")]))
    assert posting.comp_interval == "hour"


def test_ashby_without_a_salary_component_has_no_pay():
    equity = {"compensationType": "EquityPercentage", "currencyCode": None, "id": "e",
              "interval": "NONE", "minValue": None, "maxValue": None, "summary": "x"}
    posting = ashby_one(compensation=tiers([equity]))
    assert (posting.comp_min, posting.comp_max, posting.comp_source) == (None, None, None)


def test_ashby_skips_a_job_that_is_not_listed():
    payload = ashby_payload()
    payload["jobs"][0]["isListed"] = False
    postings = parse_board(entry("ashby"), payload, FETCHED_AT)
    assert len(postings) == len(payload["jobs"]) - 1
    assert "Senior Robotics Engineer" not in [p.title for p in postings]


def test_ashby_response_without_a_jobs_list_is_a_plain_fetch_error():
    with pytest.raises(FetchError, match="shape this tool does not recognize"):
        parse_board(entry("ashby"), [], FETCHED_AT)


# ------------------------------------------------------------------------------ pay text


def parse_pay(text: str):
    from jobsearch.sources import text as text_module

    return text_module.parse_pay_text(text)


def test_pay_text_dollar_range_with_trailing_code():
    pay = parse_pay("$139,200 - $235,200 USD")
    assert (pay.min, pay.max, pay.currency, pay.interval) == (139200.0, 235200.0, "USD", None)


def test_pay_text_hourly_range():
    pay = parse_pay("$45 - $60 per hour")
    assert (pay.min, pay.max, pay.currency, pay.interval) == (45.0, 60.0, "USD", "hour")


def test_pay_text_leading_code_and_dot_thousands():
    pay = parse_pay("EUR 60.000 - 75.000")
    assert (pay.min, pay.max, pay.currency) == (60000.0, 75000.0, "EUR")


def test_pay_text_k_suffix_and_to_word_and_en_dash():
    assert parse_pay("$90k to $120k a year").max == 120000.0
    pay = parse_pay("\u00a370,000 \u2013 \u00a385,000")
    assert (pay.min, pay.max, pay.currency) == (70000.0, 85000.0, "GBP")
    assert parse_pay("$100,000 \u2014 $150,000").min == 100000.0


def test_pay_text_year_and_month_words():
    assert parse_pay("$100,000 - $120,000 per year").interval == "year"
    assert parse_pay("$100,000 - $120,000 annual").interval == "year"
    assert parse_pay("\u20ac4,000 - \u20ac5,000 per month").interval == "month"
    assert parse_pay("$30 - $40 /hr").interval == "hour"


def test_pay_text_without_an_amount_is_none():
    assert parse_pay("Competitive salary and benefits") is None
    assert parse_pay("") is None


def test_pay_text_with_min_above_max_is_none():
    assert parse_pay("$200,000 - $100,000") is None


def test_pay_text_hostile_input_finishes_quickly():
    import time

    started = time.monotonic()
    parse_pay("1" * 50000 + " - " + "9," * 20000)
    parse_pay("$" + "1,000," * 10000)
    assert time.monotonic() - started < 10.0


# ----------------------------------------------------------------------- Greenhouse pay


def greenhouse(warnings: list[str] | None = None) -> list[Posting]:
    return parse_board(entry("greenhouse"), load("greenhouse_list.json"), FETCHED_AT,
                       warnings=warnings)


def test_greenhouse_single_pay_range_is_parsed_html_pay():
    posting = by_title(greenhouse(), "Field Technician")
    assert (posting.comp_min, posting.comp_max) == (70000.0, 95000.0)
    assert (posting.comp_currency, posting.comp_source) == ("USD", "parsed_html")
    assert posting.comp_interval is None


def test_greenhouse_mixed_currency_ranges_merge_only_the_first_currency_and_warn_once():
    warnings: list[str] = []
    posting = by_title(greenhouse(warnings), "Senior Robotics Engineer")
    assert (posting.comp_min, posting.comp_max) == (120000.0, 180000.0)
    assert posting.comp_currency == "USD"
    assert posting.comp_source == "parsed_html"
    assert len(warnings) == 1 and "Senior Robotics Engineer" in warnings[0]
    assert "1 pay range in CAD" in warnings[0] and "not used" in warnings[0]


def greenhouse_with_ranges(*ranges: str) -> Posting:
    blocks = "".join(f'<div class="pay-range">{text}</div>' for text in ranges)
    job = dict(load("greenhouse_list.json")["jobs"][0], content=blocks)  # type: ignore[index]
    return parse_board(entry("greenhouse"), {"jobs": [job]}, FETCHED_AT)[0]


def test_greenhouse_same_currency_ranges_keep_lowest_min_highest_max():
    posting = greenhouse_with_ranges("$100,000 - $140,000 USD", "$120,000 - $180,000 USD")
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (
        100000.0, 180000.0, "USD")


def test_greenhouse_range_without_a_currency_joins_the_first_currency_when_interval_agrees():
    posting = greenhouse_with_ranges("$100,000 - $140,000 USD", "150,000 - 170,000")
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (
        100000.0, 170000.0, "USD")


def test_greenhouse_without_a_pay_range_has_no_pay():
    posting = by_title(greenhouse(), "Operations Analyst")
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (None, None, None)
    assert (posting.comp_interval, posting.comp_source) == (None, None)


# --------------------------------------------------------------- end to end on a Lever board


def make_env(scan_env, ats: str, body: bytes, families: list[dict]):
    """Re-point the one-board scan world at one `ats` board serving `body`, with these families."""
    write_directory(scan_env.board_file, [board(ats)])
    scan_env.transport.body = body
    criteria = json.loads(TRACER.read_text(encoding="utf-8"))
    criteria["title"]["families"] = families
    (scan_env.folder / "config" / "criteria.json").write_text(json.dumps(criteria), encoding="utf-8")
    return scan_env.folder


def family(fid: str, label: str, term: str) -> dict:
    return {"id": fid, "label": label, "rung": 2, "terms": [term], "regex": [],
            "origin": "stated"}


def test_scan_then_rank_lists_lever_us_roles_with_apply_links(scan_env):
    body = (FIX / "lever_us_list.json").read_bytes().replace(
        b"jobs.example.com", b"jobs.lever.co")
    folder = make_env(scan_env, "lever", body, [
        family("tf-robotics", "Robotics engineer", "robotics engineer"),
        family("tf-field", "Field technician", "field technician")])
    scan = run_cli(scan_env, "scan")
    assert scan["counts"]["kept"] == 2
    run_cli(scan_env, "rank")
    ranked = json.loads(
        (folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))
    assert len(ranked["roles"]) == 2
    for role in ranked["roles"]:
        assert role["role_id"].startswith("lever:acme-robotics:")
        assert role["url_apply"].startswith("https://jobs.lever.co/")


def test_scan_then_rank_lists_lever_eu_roles_with_apply_links(scan_env):
    body = (FIX / "lever_eu_list.json").read_bytes().replace(
        b"jobs.eu.example.com", b"jobs.eu.lever.co")
    folder = make_env(scan_env, "lever_eu", body, [
        family("tf-data", "Data analyst", "data analyst"),
        family("tf-plan", "Warehouse planner", "warehouse planner")])
    scan = run_cli(scan_env, "scan")
    assert scan["counts"]["kept"] == 2
    run_cli(scan_env, "rank")
    ranked = json.loads(
        (folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))
    assert len(ranked["roles"]) == 2
    for role in ranked["roles"]:
        assert role["role_id"].startswith("lever_eu:acme-robotics:")
        assert role["url_apply"].startswith("https://jobs.eu.lever.co/")


@pytest.mark.parametrize("bad_id", ["", "   ", "a:b"])
def test_greenhouse_skips_a_job_with_an_empty_or_colon_id_and_keeps_the_rest(bad_id):
    """One odd id must not fail the whole board."""
    payload = load("greenhouse_list.json")
    good = payload["jobs"]  # type: ignore[index]
    bad = dict(good[0], id=bad_id)
    postings = parse_board(entry("greenhouse"), {"jobs": [bad, *good]}, FETCHED_AT)
    assert len(postings) == len(good)
    assert all(p.posting_id and ":" not in p.posting_id for p in postings)


# hostile pay figures leave the posting in place with no pay -------------------

HUGE = [float("inf"), float("-inf"), float("nan"), 10**400]


@pytest.mark.parametrize("value", HUGE)
def test_lever_pay_that_is_infinite_or_too_big_for_a_float_is_no_pay(value):
    base = load("lever_us_list.json")[0]
    payload = [dict(base, salaryRange={"currency": "USD", "interval": "per-year-salary",
                                       "min": value, "max": value})]
    posting = parse_board(entry("lever"), payload, FETCHED_AT)[0]
    assert (posting.comp_min, posting.comp_max, posting.comp_source) == (None, None, None)


@pytest.mark.parametrize("value", HUGE)
def test_ashby_pay_that_is_infinite_or_too_big_for_a_float_is_no_pay(value):
    job = ashby_payload()["jobs"][0]
    tiers = {"compensationTiers": [{"components": [
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
         "minValue": value, "maxValue": value}]}]}
    posting = parse_board(entry("ashby"), {"jobs": [dict(job, compensation=tiers)]}, FETCHED_AT)[0]
    assert (posting.comp_min, posting.comp_max, posting.comp_source) == (None, None, None)


def test_greenhouse_pay_cents_too_big_for_a_float_skip_that_range():
    from jobsearch.sources import greenhouse
    ranges = [{"currency_type": "USD", "min_cents": 10**400, "max_cents": 10**400},
              {"currency_type": "USD", "min_cents": 9000000, "max_cents": 11000000}]
    pay = greenhouse._structured_pay(ranges, "Role", None)
    assert (pay["comp_min"], pay["comp_max"]) == (90000.0, 110000.0)
    assert greenhouse._structured_pay(ranges[:1], "Role", None)["comp_min"] is None


def test_pay_text_with_a_400_digit_amount_is_no_pay():
    from jobsearch.sources.text import parse_pay_text
    assert parse_pay_text("Pay: $" + "9" * 400 + " per year") is None


@pytest.mark.parametrize("field", ["comp_min", "comp_max"])
@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_a_posting_refuses_a_pay_figure_that_is_not_finite(field, value):
    data = lever("lever", "lever_us_list.json")[0].to_dict()
    data[field] = value
    with pytest.raises(ValueError, match=field):
        Posting.from_dict(data)


# ------------------------------------------------- pay warnings and locations, every source


def _pay_warnings(noun: str) -> list[str]:
    return [f"Pay Analyst: 1 {noun} in CAD was not used; the pay shown is in USD only.",
            f"Pay Analyst: the posting lists 2 {noun}s; showing the lowest minimum and the "
            "highest maximum."]


def test_pay_warnings_are_worded_the_same_by_every_source():
    from jobsearch import paste
    from jobsearch.sources import greenhouse as gh

    blocks = "".join(f'<div class="pay-range">{text}</div>' for text in (
        "$100,000 - $140,000 USD", "$120,000 - $180,000 USD", "CAD 150,000 - 170,000"))
    job = dict(load("greenhouse_list.json")["jobs"][0], title="Pay Analyst",  # type: ignore[index]
               content=blocks)
    warnings: list[str] = []
    posting = parse_board(entry("greenhouse"), {"jobs": [job]}, FETCHED_AT, warnings=warnings)[0]
    assert warnings == _pay_warnings("pay range")
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (
        100000.0, 180000.0, "USD")
    assert (posting.comp_interval, posting.comp_source) == (None, "parsed_html")

    warnings = []
    cents = [{"currency_type": "USD", "min_cents": 10000000, "max_cents": 14000000},
             {"currency_type": "USD", "min_cents": 12000000, "max_cents": 18000000},
             {"currency_type": "CAD", "min_cents": 15000000, "max_cents": 17000000}]
    assert gh._structured_pay(cents, "Pay Analyst", warnings) == {
        "comp_min": 100000.0, "comp_max": 180000.0, "comp_currency": "USD",
        "comp_interval": None, "comp_source": "structured"}
    assert warnings == _pay_warnings("pay range")

    warnings = []
    job = dict(ashby_payload()["jobs"][0], title="Pay Analyst", compensation=tiers(
        [salary(100000, 140000), salary(120000, 180000),
         salary(150000, 170000, currency="CAD")]))
    parse_board(entry("ashby"), {"jobs": [job]}, FETCHED_AT, warnings=warnings)
    assert warnings == _pay_warnings("salary range")

    text = ("Salary: $100,000 - $140,000 USD.\nOr $120,000 - $180,000 USD.\n"
            "CAD 150,000 - 170,000 per year.")
    posting, notes = paste.posting_from_text("Acme", "Pay Analyst", None, None, text,
                                             "2026-11-17")
    assert notes == _pay_warnings("pay range")
    assert (posting.comp_min, posting.comp_max, posting.comp_source) == (
        100000.0, 180000.0, "parsed_html")


def test_locations_split_on_pipes_and_semicolons():
    from jobsearch import paste
    where = "Austin, TX | Remote;  ; Denver, CO"
    job = dict(load("greenhouse_list.json")["jobs"][0], location={"name": where})  # type: ignore[index]
    posting = parse_board(entry("greenhouse"), {"jobs": [job]}, FETCHED_AT)[0]
    assert posting.locations == ["Austin, TX", "Remote", "Denver, CO"]
    pasted, _ = paste.posting_from_text("Acme", "Analyst", where, None, "A role.", "2026-11-17")
    assert pasted.locations == ["Austin, TX", "Remote", "Denver, CO"]
