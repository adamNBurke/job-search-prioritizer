"""Profile/answers.json is checked field by field before any config is built.

Purpose: the agent writes answers.json from the interview, so every field, vocabulary and cross
rule is checked in code and every problem is a plain sentence that names the field to fix.
Logic: validate_answers is driven on plain dicts built from one blank, valid answer set with one
change per test. load_answers is driven on a data folder under tmp_path.
Guardrails: personas are fictional. A skill marked as from the resume must appear in the resume
text; the same skill marked as stated is accepted. needs_sponsorship is only ever the user's
explicit answer.
QA checklist: a new answers field needs a case for its type, its vocabulary and its absence.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from helpers import PERSONA_NAMES

from jobsearch import answers, errors

PERSONAS = Path(__file__).resolve().parent / "fixtures" / "personas"


def blank() -> dict:
    """The smallest valid answers: every question skipped."""
    return {
        "schema_version": 1, "answered_on": "2026-11-17", "target_titles": [],
        "target_level": None, "years_experience": None, "skills": [], "pay_floor": None,
        "work_modes": [], "locations": [], "regions": [], "needs_sponsorship": None,
        "industries_wanted": [], "industries_ruled_out": [], "company_sizes": [],
        "company_stages": [], "deal_breaker_terms": [], "companies_ruled_out": [], "strict": [],
        "top_three": [], "dont_care": [], "skipped": [],
    }


def changed(**fields) -> dict:
    data = blank()
    data.update(fields)
    return data


def has(errs: list[str], *fragments: str) -> bool:
    return any(all(f in e for f in fragments) for e in errs)


@pytest.mark.parametrize("name", PERSONA_NAMES)
def test_each_persona_answers_are_valid(name):
    data = json.loads((PERSONAS / name / "answers.json").read_text(encoding="utf-8"))
    resume = (PERSONAS / name / "resume.md").read_text(encoding="utf-8")
    assert answers.validate_answers(data, resume) == []


def test_blank_answers_with_every_question_skipped_are_valid():
    assert answers.validate_answers(blank(), "") == []


@pytest.mark.parametrize("key", ["target_titles", "skills"])
@pytest.mark.parametrize("value", [None, 5, "text", {}])
def test_a_non_list_titles_or_skills_value_gives_a_sentence_not_a_traceback(key, value):
    errs = answers.validate_answers(changed(**{key: value}), "resume text")
    assert has(errs, key, "list")


@pytest.mark.parametrize("key", answers.KEYS)
@pytest.mark.parametrize("value", [None, 5, "text", [], {}, [None], [[]]])
def test_validate_answers_never_raises_on_a_wrongly_typed_field(key, value):
    errs = answers.validate_answers(changed(**{key: value}), "resume text")
    assert isinstance(errs, list) and all(isinstance(e, str) for e in errs)


def test_criterion_ids_are_the_nine_fixed_ids():
    assert answers.CRITERION_IDS == (
        "title_fit", "skills", "level", "pay", "location", "freshness", "industry",
        "company_size", "company_stage")


def test_unknown_and_missing_keys_are_named():
    data = blank()
    del data["strict"]
    data["favorite_color"] = "green"
    errs = answers.validate_answers(data, "")
    assert has(errs, '"favorite_color"')
    assert has(errs, 'missing "strict"')


def test_non_object_answers_are_refused():
    assert answers.validate_answers([], "") == ["The interview answers must be a JSON object."]


@pytest.mark.parametrize(("field", "value", "path"), [
    ("target_level", "boss", "target_level"),
    ("years_experience", -1, "years_experience"),
    ("years_experience", True, "years_experience"),
    ("answered_on", "17 Nov 2026", "answered_on"),
    ("answered_on", "2026-13-40", "answered_on"),
    ("work_modes", ["remote", "moon"], "work_modes[1]"),
    ("regions", ["USA"], "regions[0]"),
    ("regions", ["us"], "regions[0]"),
    ("industries_wanted", ["underwater basket weaving"], "industries_wanted[0]"),
    ("company_sizes", ["huge"], "company_sizes[0]"),
    ("company_stages", ["ancient"], "company_stages[0]"),
    ("strict", ["vibes"], "strict[0]"),
    ("top_three", ["salary"], "top_three[0]"),
    ("needs_sponsorship", "yes", "needs_sponsorship"),
    ("target_titles", ["ok", ""], "target_titles[1]"),
    ("deal_breaker_terms", ["x" * 81], "deal_breaker_terms[0]"),
])
def test_bad_values_name_the_field_to_fix(field, value, path):
    errs = answers.validate_answers(changed(**{field: value}), "")
    assert has(errs, path), errs


@pytest.mark.parametrize(("pay", "path"), [
    ({"amount": 0, "currency": "USD", "interval": "year"}, "pay_floor.amount"),
    ({"amount": -5, "currency": "USD", "interval": "year"}, "pay_floor.amount"),
    ({"amount": "90000", "currency": "USD", "interval": "year"}, "pay_floor.amount"),
    ({"amount": 90000, "currency": "usd", "interval": "year"}, "pay_floor.currency"),
    ({"amount": 90000, "currency": "US", "interval": "year"}, "pay_floor.currency"),
    ({"amount": 90000, "currency": "USD", "interval": "fortnight"}, "pay_floor.interval"),
    ({"amount": 90000, "currency": "USD"}, "pay_floor"),
])
def test_pay_floor_problems_are_named(pay, path):
    errs = answers.validate_answers(changed(pay_floor=pay), "")
    assert has(errs, path), errs


def test_top_three_allows_at_most_three_unique_ids():
    four = changed(top_three=["title_fit", "pay", "location", "skills"])
    assert has(answers.validate_answers(four, ""), "top_three", "at most 3")
    dup = changed(top_three=["pay", "pay"])
    assert has(answers.validate_answers(dup, ""), "top_three", "more than once")


def test_a_criterion_cannot_be_both_top_three_and_dont_care():
    errs = answers.validate_answers(changed(top_three=["pay"], dont_care=["pay"]), "")
    assert has(errs, "pay", "top_three", "dont_care")


def test_an_industry_cannot_be_both_wanted_and_ruled_out():
    errs = answers.validate_answers(
        changed(industries_wanted=["software"], industries_ruled_out=["software"]), "")
    assert has(errs, "software", "wanted", "ruled out")


def test_a_target_title_that_is_only_level_words_is_refused():
    errs = answers.validate_answers(changed(target_titles=["Senior"]), "")
    assert has(errs, "target_titles[0]", "Senior")


def test_resume_skill_missing_from_the_resume_names_the_skill():
    skill = {"term": "Kubernetes", "aliases": [], "origin": "resume"}
    errs = answers.validate_answers(changed(skills=[skill]), "I ran warehouses and teams.")
    assert has(errs, "skills[0]", "Kubernetes", "resume")


def test_the_same_skill_marked_stated_is_accepted():
    skill = {"term": "Kubernetes", "aliases": [], "origin": "stated"}
    assert answers.validate_answers(changed(skills=[skill]), "I ran warehouses.") == []


def test_resume_skill_matches_by_alias_ignoring_case():
    skill = {"term": "spreadsheets", "aliases": ["excel"], "origin": "resume"}
    assert answers.validate_answers(changed(skills=[skill]), "Built models in EXCEL daily.") == []


def test_resume_skill_must_match_a_whole_word():
    skill = {"term": "Java", "aliases": [], "origin": "resume"}
    errs = answers.validate_answers(changed(skills=[skill]), "Wrote JavaScript front ends.")
    assert has(errs, "skills[0]", "Java")


def test_resume_skill_with_symbols_matches_whole():
    skill = {"term": "C++", "aliases": [], "origin": "resume"}
    assert answers.validate_answers(changed(skills=[skill]), "Systems work in C++, daily.") == []


def stated(term: str, **extra) -> dict:
    return {"term": term, "aliases": [], "origin": "stated", **extra}


def test_a_skill_without_priority_stays_valid():
    assert answers.validate_answers(changed(skills=[stated("Excel")]), "") == []


def test_a_skill_priority_of_true_or_false_is_valid():
    skills = [stated("Excel", priority=True), stated("Budgeting", priority=False)]
    assert answers.validate_answers(changed(skills=skills), "") == []


@pytest.mark.parametrize("value", ["yes", 1, None, ["true"]])
def test_a_skill_priority_that_is_not_true_or_false_gives_a_sentence(value):
    errs = answers.validate_answers(changed(skills=[stated("Excel", priority=value)]), "")
    assert errs == ["skills[0].priority: must be true or false."]


def test_three_priority_skills_are_allowed_and_four_are_refused():
    three = [stated(t, priority=True) for t in ("A1", "B2", "C3")]
    assert answers.validate_answers(changed(skills=three), "") == []
    four = [*three, stated("D4", priority=True)]
    errs = answers.validate_answers(changed(skills=four), "")
    assert errs == ["skills: mark at most three skills as mattering most."]


# first_choice_title -----------------------------------------------


def test_first_choice_title_is_the_one_optional_key():
    assert answers.OPTIONAL_KEYS == ("first_choice_title",)
    assert "first_choice_title" not in answers.KEYS


@pytest.mark.parametrize("extra", [{}, {"first_choice_title": None}])
def test_an_absent_or_null_first_choice_is_a_skipped_question_and_valid(extra):
    data = changed(target_titles=["Operations Manager"], **extra)
    assert answers.validate_answers(data, "") == []


@pytest.mark.parametrize("value", ["Operations Manager", "Senior Operations Manager",
                                   "  operations   manager "])
def test_a_first_choice_that_normalizes_to_a_target_title_is_accepted(value):
    data = changed(target_titles=["Operations Manager", "Program Manager"],
                   first_choice_title=value)
    assert answers.validate_answers(data, "") == []


def test_a_first_choice_outside_the_target_titles_is_refused_and_named():
    data = changed(target_titles=["Operations Manager"], first_choice_title="Chief of Staff")
    errs = answers.validate_answers(data, "")
    assert len(errs) == 1
    assert errs[0].startswith("first_choice_title:") and "Chief of Staff" in errs[0]


@pytest.mark.parametrize("value", [5, True, "", "   ", ["Operations Manager"], {}])
def test_a_first_choice_that_is_not_text_is_refused(value):
    data = changed(target_titles=["Operations Manager"], first_choice_title=value)
    errs = answers.validate_answers(data, "")
    assert has(errs, "first_choice_title:", "text")


def test_a_first_choice_longer_than_80_characters_is_refused():
    long_title = "Operations Manager " + "x" * 62  # 81 characters
    assert len(long_title) == 81
    data = changed(target_titles=["Operations Manager"], first_choice_title=long_title)
    assert has(answers.validate_answers(data, ""), "first_choice_title:", "80")


def test_a_first_choice_with_no_target_titles_is_refused():
    errs = answers.validate_answers(changed(first_choice_title="Operations Manager"), "")
    assert has(errs, "first_choice_title:", "target_titles")


def test_first_choice_check_never_raises_on_a_wrongly_typed_target_titles():
    data = changed(target_titles="Operations Manager", first_choice_title="Operations Manager")
    errs = answers.validate_answers(data, "")
    assert has(errs, "target_titles", "list")


def test_every_problem_is_listed_not_just_the_first():
    errs = answers.validate_answers(
        changed(target_level="boss", regions=["USA"], strict=["vibes"]), "")
    assert len(errs) == 3 and errs == sorted(errs)


def test_load_answers_without_a_file_says_what_to_do(tmp_path):
    with pytest.raises(errors.UserError, match="interview"):
        answers.load_answers(tmp_path)


def test_load_answers_refuses_text_that_is_not_json(tmp_path):
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "answers.json").write_text("not json", encoding="utf-8")
    with pytest.raises(errors.UserError, match="not readable JSON"):
        answers.load_answers(tmp_path)


def test_load_answers_returns_the_object(tmp_path):
    (tmp_path / "profile").mkdir()
    data = copy.deepcopy(blank())
    (tmp_path / "profile" / "answers.json").write_text(json.dumps(data), encoding="utf-8")
    assert answers.load_answers(tmp_path) == data


# places the tool can read ------------------------------------------------------

def test_a_location_the_tool_cannot_read_is_refused_with_a_request_for_city_st():
    errs = answers.validate_answers(changed(locations=["Phoenix"]), "")
    assert has(errs, "locations[0]", "City, ST")


def test_a_comma_list_with_an_unpaired_city_is_refused_and_names_it():
    errs = answers.validate_answers(changed(locations=["Phoenix, Seattle, WA"]), "")
    assert has(errs, "locations[0]", "Phoenix", "City, ST")


@pytest.mark.parametrize("place", ["Phoenix, AZ", "Berlin", "Raleigh, NC", "Seattle, WA, Phoenix, AZ"])
def test_a_readable_location_is_accepted(place):
    assert answers.validate_answers(changed(locations=[place]), "") == []


def test_a_region_code_the_tool_does_not_know_is_refused():
    assert has(answers.validate_answers(changed(regions=["ZZ"]), ""), "regions[0]", "ZZ")


@pytest.mark.parametrize("code", ["US", "EU", "UK", "CA", "DE"])
def test_a_known_region_code_is_accepted(code):
    assert answers.validate_answers(changed(regions=[code]), "") == []


# a repeated skill and a pay floor past the cap -------------------------

def skill(term: str) -> dict:
    return {"term": term, "aliases": [], "origin": "stated"}


@pytest.mark.parametrize("first, second", [("SQL", "sql"), ("Six Sigma", "six  sigma")])
def test_a_skill_listed_twice_is_refused_naming_both_positions(first, second):
    errs = answers.validate_answers(changed(skills=[skill(first), skill(second)]), "")
    assert len(errs) == 1 and has(errs, "skills[1]", "skills[0]", "once"), errs


def test_distinct_skills_are_not_a_repeat():
    assert answers.validate_answers(changed(skills=[skill("SQL"), skill("Python")]), "") == []


def test_a_pay_floor_above_the_cap_is_refused_and_the_cap_itself_is_accepted():
    over = changed(pay_floor={"amount": 1e30, "currency": "USD", "interval": "year"})
    assert has(answers.validate_answers(over, ""), "pay_floor.amount", "1,000,000,000,000")
    at_cap = changed(pay_floor={"amount": 1e12, "currency": "USD", "interval": "year"})
    assert answers.validate_answers(at_cap, "") == []


def test_a_pay_floor_integer_too_big_for_a_float_is_refused_not_a_crash():
    over = changed(pay_floor={"amount": 10**400, "currency": "USD", "interval": "year"})
    assert has(answers.validate_answers(over, ""), "pay_floor.amount", "1,000,000,000,000")
