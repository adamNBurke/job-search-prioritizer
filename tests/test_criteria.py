"""criteria.validate_criteria and the criteria-validate command.

Purpose: no scoring config reaches the scorer without a plain-English check of every field, so
each rule in the schema and in docs/data-files.md has a test that names the field in a path
such as criteria[1].params.bands and says what to fix.
Logic: tests start from the two fixture configs (both must validate), change one thing, and
look for the error that names it. Command tests run cli.main against a data folder under
tmp_path and read the Result JSON.
Guardrails: fixtures are fictional; nothing here touches the network; every error list must
come back sorted so the same bad config always reads the same way.
QA checklist: every case the validator must handle (unknown evaluator, bad params, bad regex, long
regex, unknown reject_reason field, weights, judged_scale) has a named test below.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from jobsearch import cli, criteria

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "criteria"
MINIMAL = json.loads((FIXTURES / "minimal.json").read_text(encoding="utf-8"))

# Positions in minimal.json that the tests below change.
TITLE_FIT, SKILLS, FRESHNESS, PAY = 0, 1, 2, 3


def errors_after(mutate, base: dict = MINIMAL) -> list[str]:
    data = copy.deepcopy(base)
    mutate(data)
    return criteria.validate_criteria(data)


def assert_error(errors: list[str], *fragments: str) -> None:
    assert any(all(f in e for f in fragments) for e in errors), errors


def test_every_known_evaluator_has_a_params_row_in_the_validator():
    assert set(criteria._EVALUATORS) == set(criteria.KNOWN_EVALUATORS)


# The good configs ----------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["minimal.json", "tracer.json"])
def test_both_fixture_configs_validate(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert criteria.validate_criteria(data) == []


def test_a_config_that_is_not_an_object_is_refused():
    assert criteria.validate_criteria([]) == ["The scoring config must be a JSON object."]


@pytest.mark.parametrize("value", [["x"], {}, 5, None])
def test_an_evaluator_that_is_not_text_is_a_sentence_not_a_traceback(value):
    errors = errors_after(lambda d: d["criteria"][TITLE_FIT].update(evaluator=value))
    assert_error(errors, "criteria[0].evaluator", "not an evaluator")
    errors = errors_after(lambda d: d["bonuses"][0].update(evaluator=value))
    assert_error(errors, "bonuses[0].evaluator", "not an evaluator")


# The must-have rejections --------------------------------------------------------------------

def test_an_unknown_evaluator_is_refused_with_its_path():
    errors = errors_after(lambda d: d["criteria"][SKILLS].update(evaluator="magic"))
    assert_error(errors, "criteria[1].evaluator", "not an evaluator")


def test_an_unknown_bonus_evaluator_is_refused_with_its_path():
    errors = errors_after(lambda d: d["bonuses"][0].update(evaluator="magic"))
    assert_error(errors, "bonuses[0].evaluator", "not an evaluator")


def test_params_keys_must_be_the_documented_ones():
    errors = errors_after(lambda d: d["criteria"][FRESHNESS]["params"].update(extra=1))
    assert_error(errors, "criteria[2].params", '"extra"', "not a setting")
    errors = errors_after(lambda d: d["criteria"][FRESHNESS]["params"].pop("else"))
    assert_error(errors, "criteria[2].params", 'needs "else"')


def test_banded_number_thresholds_must_descend():
    errors = errors_after(
        lambda d: d["criteria"][PAY]["params"].update(bands=[[90000, 1], [150000, 3]]))
    assert_error(errors, "criteria[3].params.bands", "thresholds must be in descending order")


def test_keyword_coverage_thresholds_must_descend_and_end_at_zero():
    errors = errors_after(
        lambda d: d["criteria"][SKILLS]["params"].update(bands=[[0.2, 1], [0.6, 3], [0, 0]]))
    assert_error(errors, "criteria[1].params.bands", "thresholds must be in descending order")
    errors = errors_after(
        lambda d: d["criteria"][SKILLS]["params"].update(bands=[[0.6, 3], [0.2, 1]]))
    assert_error(errors, "criteria[1].params.bands", "last share must be 0")


def test_age_bands_must_ascend_with_points_from_zero_to_three():
    errors = errors_after(
        lambda d: d["criteria"][FRESHNESS]["params"].update(bands=[[45, 2], [14, 3]]))
    assert_error(errors, "criteria[2].params.bands", "max_days must be in ascending order")
    errors = errors_after(
        lambda d: d["criteria"][FRESHNESS]["params"].update(bands=[[14, 4]]))
    assert_error(errors, "criteria[2].params.bands[0]", "points", "0 to 3")
    errors = errors_after(lambda d: d["criteria"][FRESHNESS]["params"].update({"else": 9}))
    assert_error(errors, "criteria[2].params.else", "0 to 3")


def test_an_uncompilable_regex_is_refused():
    def mutate(d):
        d["title"]["families"][0]["regex"] = ["(unclosed"]
    assert_error(errors_after(mutate), "title.families[0].regex[0]", "not a valid pattern")


def test_a_regex_over_200_characters_is_refused():
    def mutate(d):
        d["title"]["families"][0]["regex"] = ["a" * 201]
    assert_error(errors_after(mutate), "title.families[0].regex[0]", "longer than 200")


def test_a_regex_of_exactly_200_characters_is_accepted():
    def mutate(d):
        d["title"]["families"][0]["regex"] = ["a" * 200]
    assert errors_after(mutate) == []


def test_an_unknown_reject_reason_field_is_refused_and_named():
    def mutate(d):
        d["hard_filters"][0]["reject_reason"] = "{company} is {salary_band}."
    assert_error(errors_after(mutate), "hard_filters[0].reject_reason", "{salary_band}")


def test_every_documented_reject_reason_field_is_accepted():
    def mutate(d):
        d["hard_filters"][0]["reject_reason"] = (
            "{company} {title} {value} {comp_max} {comp_currency} {work_mode_text} {term}")
    assert errors_after(mutate) == []


def test_weights_must_add_up_to_100():
    errors = errors_after(lambda d: d["criteria"][TITLE_FIT].update(weight=50))
    assert_error(errors, "criteria", "add up to 110", "must add up to 100")


def test_judged_scale_is_refused_with_the_when():
    errors = errors_after(lambda d: d["criteria"][SKILLS].update(evaluator="judged_scale"))
    assert_error(errors, "criteria[1].evaluator",
                 "judged_scale needs researched values, which arrive in 1.1 with company research.")
    errors = errors_after(lambda d: d["bonuses"][0].update(evaluator="judged_scale"))
    assert_error(errors, "bonuses[0].evaluator", "judged_scale needs researched values")


def test_errors_come_back_sorted_and_the_same_every_time():
    def mutate(d):
        d["criteria"][TITLE_FIT]["weight"] = 90
        d["criteria"][SKILLS]["evaluator"] = "magic"
        d["tiers"][1]["min_score"] = 99
        d["hard_filters"][0]["reject_reason"] = "{nope}"
    first = errors_after(mutate)
    assert len(first) >= 4
    assert first == sorted(first)
    assert first == errors_after(mutate)


# The rest of the params table ----------------------------------------------------------------

def test_an_evaluator_with_no_field_refuses_one():
    errors = errors_after(lambda d: d["criteria"][TITLE_FIT].update(field="title"))
    assert_error(errors, "criteria[0].field", "title_ladder", "does not take a field")


def test_keyword_coverage_terms_are_checked():
    def bad_weight(d):
        d["criteria"][SKILLS]["params"]["must_have"][0]["weight"] = 6
    assert_error(errors_after(bad_weight), "criteria[1].params.must_have[0].weight", "1 to 5")

    def no_term(d):
        d["criteria"][SKILLS]["params"]["must_have"][0]["term"] = ""
    assert_error(errors_after(no_term), "criteria[1].params.must_have[0].term")

    def extra(d):
        d["criteria"][SKILLS]["params"]["nice"] = [
            {"term": "a", "aliases": [], "weight": 1, "colour": "red"}]
    assert_error(errors_after(extra), "criteria[1].params.nice[0]", '"colour"')


def test_keyword_coverage_field_must_be_a_text_field():
    errors = errors_after(lambda d: d["criteria"][SKILLS].update(field="company"))
    assert_error(errors, "criteria[1].field", "text, title or description_text")
    assert errors_after(lambda d: d["criteria"][SKILLS].update(field="title")) == []


def test_banded_number_field_and_currency_are_checked():
    errors = errors_after(lambda d: d["criteria"][PAY].update(field="title"))
    assert_error(errors, "criteria[3].field", "comp_min or comp_max")
    errors = errors_after(lambda d: d["criteria"][PAY].pop("field"))
    assert_error(errors, "criteria[3].field", "comp_min or comp_max")
    errors = errors_after(lambda d: d["criteria"][PAY]["params"].update(currency="usd"))
    assert_error(errors, "criteria[3].params.currency", "ISO 4217")
    assert errors_after(lambda d: d["criteria"][PAY]["params"].update(currency="USD")) == []


def _enum_scale(field: str, values: dict) -> dict:
    return {"id": "size", "label": "Company size", "weight": 0, "evaluator": "enum_scale",
            "field": field, "params": {"values": values}}


def test_enum_scale_values_must_come_from_the_field_vocabulary_with_points_0_to_3():
    def add(entry):
        def mutate(d):
            d["criteria"].append(entry)
        return mutate
    assert errors_after(add(_enum_scale("company_size", {"51-200": 3, "5001+": 1}))) == []
    assert_error(errors_after(add(_enum_scale("company_size", {"huge": 3}))),
                 "criteria[4].params.values", '"huge"', "company_size")
    assert_error(errors_after(add(_enum_scale("company_size", {"51-200": 4}))),
                 "criteria[4].params.values", "0 to 3")
    assert_error(errors_after(add(_enum_scale("company", {"x": 1}))),
                 "criteria[4].field", "company_size, company_stage or work_mode")
    assert errors_after(add(_enum_scale("work_mode", {"remote": 3, "unknown": 0}))) == []


def test_term_scale_terms_are_checked():
    errors = errors_after(lambda d: d["bonuses"][0]["params"].update(terms={"logistics": 5}))
    assert_error(errors, "bonuses[0].params.terms", "0 to 3")
    errors = errors_after(lambda d: d["bonuses"][0]["params"].update(terms={"x" * 81: 1}))
    assert_error(errors, "bonuses[0].params.terms", "80 characters")
    errors = errors_after(lambda d: d["bonuses"][0].update(field="comp_max"))
    assert_error(errors, "bonuses[0].field", "company_industry")


def _extra_criterion(**entry) -> Callable[[dict], None]:
    def mutate(d):
        d["criteria"].append({"id": "extra", "label": "Extra", "weight": 0, **entry})
    return mutate


def _age_criterion(**params) -> Callable[[dict], None]:
    return _extra_criterion(evaluator="age_bands",
                            params={"bands": [[14, 3], [45, 2]], "else": 0, **params})


@pytest.mark.parametrize("params", [
    {"cap_after_days": 60, "reject_after_days": 180},
    {"cap_after_days": 60},
    {"reject_after_days": 180},
    {"cap_after_days": 1, "reject_after_days": 2},
])
def test_age_bands_accepts_whole_day_limits(params):
    assert errors_after(_age_criterion(**params)) == []


@pytest.mark.parametrize("key", ["cap_after_days", "reject_after_days"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "60", None])
def test_age_bands_refuses_bad_day_limits(key, value):
    errors = errors_after(_age_criterion(**{key: value}))
    assert_error(errors, f"criteria[4].params.{key}", "must be a whole number of days, 1 or more.")


@pytest.mark.parametrize("cap, reject", [(60, 60), (90, 60)])
def test_age_bands_refuses_a_reject_limit_not_above_the_cap(cap, reject):
    errors = errors_after(_age_criterion(cap_after_days=cap, reject_after_days=reject))
    assert_error(errors, "criteria[4].params.reject_after_days", "must be more than cap_after_days.")


def test_level_match_params_are_checked():
    ok = _extra_criterion(evaluator="level_match", params={"target": "senior", "years": 6})
    assert errors_after(ok) == []
    nulls = _extra_criterion(evaluator="level_match", params={"target": None, "years": None})
    assert errors_after(nulls) == []
    bad = _extra_criterion(evaluator="level_match", params={"target": "boss", "years": -1})
    errors = errors_after(bad)
    assert_error(errors, "criteria[4].params.target", "entry, mid, senior")
    assert_error(errors, "criteria[4].params.years", "whole number")
    missing = _extra_criterion(evaluator="level_match", params={"target": "mid"})
    assert_error(errors_after(missing), "criteria[4].params", 'needs "years"')


def test_location_match_params_are_checked():
    ok = _extra_criterion(evaluator="location_match",
                          params={"modes": ["remote", "hybrid"], "places": ["Austin, TX", "US"]})
    assert errors_after(ok) == []
    bad = _extra_criterion(evaluator="location_match",
                           params={"modes": ["remote", "mars"], "places": "Austin"})
    errors = errors_after(bad)
    assert_error(errors, "criteria[4].params.modes", '"mars"')
    assert_error(errors, "criteria[4].params.places", "list")


def test_reason_placeholders_must_belong_to_the_evaluator():
    def mutate(d):
        d["criteria"][FRESHNESS]["reasons"] = {"default": "Posted {days} days ago, {mood}."}
    assert_error(errors_after(mutate), "criteria[2].reasons.default", "{mood}")

    def keys(d):
        d["criteria"][FRESHNESS]["reasons"] = {"7": "Posted {days} days ago."}
    assert_error(errors_after(keys), "criteria[2].reasons", '"7"', "0 to 3 or default")
    assert errors_after(
        lambda d: d["criteria"][FRESHNESS].update(reasons={"3": "{days} days.", "default": "x"})
    ) == []


# Criterion and bonus shape --------------------------------------------------------------------

def test_criterion_keys_outside_the_schema_are_refused():
    errors = errors_after(lambda d: d["criteria"][SKILLS].update(colour="red"))
    assert_error(errors, "criteria[1]", '"colour"', "not a setting")


def test_bonus_points_are_capped_at_20():
    assert_error(errors_after(lambda d: d["bonuses"][0].update(max_points=21)),
                 "bonuses[0].max_points", "0 to 20")
    assert errors_after(lambda d: d["bonuses"][0].update(max_points=20)) == []
    assert errors_after(lambda d: d["bonuses"][0].update(max_points=0)) == []


def test_bonus_ids_must_be_unique():
    def mutate(d):
        d["bonuses"].append(copy.deepcopy(d["bonuses"][0]))
    assert_error(errors_after(mutate), "bonuses[1].id", "more than once")


# Title ----------------------------------------------------------------------------------------

def test_a_family_rung_must_be_0_to_3():
    assert_error(errors_after(lambda d: d["title"]["families"][0].update(rung=4)),
                 "title.families[0].rung", "0 to 3")


def test_family_ids_must_be_unique():
    def mutate(d):
        d["title"]["families"][1]["id"] = d["title"]["families"][0]["id"]
    assert_error(errors_after(mutate), "title.families[1].id", "more than once")


def test_family_terms_must_be_non_empty_and_at_most_80_characters():
    def long(d):
        d["title"]["families"][0]["terms"] = ["x" * 81]
    assert_error(errors_after(long), "title.families[0].terms[0]", "80 characters")

    def empty(d):
        d["title"]["families"][0]["terms"] = ["  "]
    assert_error(errors_after(empty), "title.families[0].terms[0]", "empty")


def test_a_family_needs_a_term_or_a_pattern():
    def mutate(d):
        d["title"]["families"][0]["terms"] = []
    assert_error(errors_after(mutate), "title.families[0]", "at least one term or pattern")


def test_a_family_needs_an_origin_from_the_schema():
    assert_error(errors_after(lambda d: d["title"]["families"][0].pop("origin")),
                 "title.families[0].origin", "discovered, stated or default")


def test_exclude_groups_are_checked():
    def mutate(d):
        d["title"]["exclude"] = [{"terms": ["ok", ""], "origin": "stated"}]
    assert_error(errors_after(mutate), "title.exclude[0].terms[1]", "empty")


# Hard filters ---------------------------------------------------------------------------------

FILTERS = {
    "enum_allow": {"id": "f", "type": "enum_allow", "field": "work_mode", "allow": ["remote"]},
    "min_number": {"id": "f", "type": "min_number", "field": "comp_max", "value": 90000,
                   "currency": "USD"},
    "region_allow": {"id": "f", "type": "region_allow", "allow": ["US", "EU"]},
    "deny_terms": {"id": "f", "type": "deny_terms", "field": "text", "terms": ["unpaid"]},
    "deny_list": {"id": "f", "type": "deny_list", "names": ["acme robotics"]},
}


def with_filter(entry: dict):
    def mutate(d):
        d["hard_filters"] = [entry]
    return mutate


@pytest.mark.parametrize("mutate, path, key", [
    (lambda d: d["criteria"][TITLE_FIT].update(
        params={"rungs_from": "title.families"}), "criteria[0].params", "rungs_from"),
    (lambda d: d["criteria"][TITLE_FIT].update(
        params={"order": "first_match"}), "criteria[0].params", "order"),
    (lambda d: d["criteria"][TITLE_FIT].update(
        params={"rung_labels": {"3": "Exact"}}), "criteria[0].params", "rung_labels"),
    (lambda d: d["criteria"][TITLE_FIT].update(anchor=True), "criteria[0]", "anchor"),
    (lambda d: d["criteria"][FRESHNESS].update(decays=True), "criteria[2]", "decays"),
    (lambda d: d["criteria"][SKILLS].update(source="resume"), "criteria[1]", "source"),
    (lambda d: d["title"].update(
        seniority={"tokens_file": "seniority.json", "target": "senior", "bias": 0}),
     "title", "seniority"),
    (lambda d: d["title"]["families"][0].update(require_seniority=False),
     "title.families[0]", "require_seniority"),
    (lambda d: d.update(hard_filters=[dict(FILTERS["min_number"], basis="annual_total")]),
     "hard_filters[0]", "basis"),
])
def test_a_setting_the_scorer_never_read_is_refused(mutate, path, key):
    assert_error(errors_after(mutate), path, f'"{key}"', "is not a setting")


@pytest.mark.parametrize("kind", sorted(FILTERS))
def test_each_filter_type_validates_in_its_documented_shape(kind):
    assert errors_after(with_filter(FILTERS[kind])) == []


def test_an_unknown_filter_type_is_refused():
    errors = errors_after(with_filter(dict(FILTERS["deny_list"], type="deny_everything")))
    assert_error(errors, "hard_filters[0].type", "enum_allow, min_number")


def test_a_filter_may_not_carry_keys_that_belong_to_another_type():
    errors = errors_after(with_filter(dict(FILTERS["deny_list"], value=3)))
    assert_error(errors, "hard_filters[0]", '"value"', "deny_list")


def test_a_filter_with_the_unknown_policy_outside_the_three_is_refused():
    errors = errors_after(with_filter(dict(FILTERS["deny_list"], unknown="maybe")))
    assert_error(errors, "hard_filters[0].unknown", "gap, pass or reject")


@pytest.mark.parametrize("kind, key", [
    ("enum_allow", "field"), ("enum_allow", "allow"), ("min_number", "field"),
    ("min_number", "value"), ("region_allow", "allow"), ("deny_terms", "field"),
    ("deny_terms", "terms"), ("deny_list", "names"),
])
def test_a_filter_missing_a_required_key_names_the_key(kind, key):
    entry = {k: v for k, v in FILTERS[kind].items() if k != key}
    assert_error(errors_after(with_filter(entry)), "hard_filters[0]", f'needs "{key}"')


def test_min_number_only_reads_pay_and_wants_a_number_and_a_code():
    assert_error(errors_after(with_filter(dict(FILTERS["min_number"], field="title"))),
                 "hard_filters[0].field", "comp_min or comp_max")
    assert_error(errors_after(with_filter(dict(FILTERS["min_number"], value="90k"))),
                 "hard_filters[0].value", "number")
    assert_error(errors_after(with_filter(dict(FILTERS["min_number"], value=-1))),
                 "hard_filters[0].value", "zero or more")
    assert_error(errors_after(with_filter(dict(FILTERS["min_number"], currency="dollars"))),
                 "hard_filters[0].currency", "ISO 4217")


def test_region_allow_wants_country_codes_or_eu():
    errors = errors_after(with_filter(dict(FILTERS["region_allow"], allow=["Germany"])))
    assert_error(errors, "hard_filters[0].allow[0]", "two-letter country code or EU")
    assert_error(errors_after(with_filter(dict(FILTERS["region_allow"], allow=[]))),
                 "hard_filters[0].allow", "at least one")


def test_deny_terms_reads_only_text_fields_and_caps_each_term():
    assert_error(errors_after(with_filter(dict(FILTERS["deny_terms"], field="comp_max"))),
                 "hard_filters[0].field", "title, description_text, text or company_industry")
    assert_error(errors_after(with_filter(dict(FILTERS["deny_terms"], terms=["x" * 81]))),
                 "hard_filters[0].terms[0]", "80 characters")


def test_enum_allow_reads_a_documented_field():
    errors = errors_after(with_filter(dict(FILTERS["enum_allow"], field="salary_band")))
    assert_error(errors, "hard_filters[0].field", "salary_band")


def test_filter_ids_must_be_unique():
    def mutate(d):
        d["hard_filters"] = [FILTERS["deny_list"], FILTERS["deny_list"]]
    assert_error(errors_after(mutate), "hard_filters[1].id", "more than once")


# Top level ------------------------------------------------------------------------------------

def test_the_reserved_key_is_tolerated_and_other_unknown_keys_are_not():
    assert errors_after(lambda d: d.update(reserved={"tracks": None})) == []
    assert_error(errors_after(lambda d: d.update(surprise=1)), "unknown key: surprise")


def test_tiers_must_descend():
    errors = errors_after(lambda d: d["tiers"][1].update(min_score=90))
    assert_error(errors, "tiers", "go down from A to C")


# The command ----------------------------------------------------------------------------------

@pytest.fixture
def folder(tmp_path, capsys):
    target = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(target)]) == 0
    capsys.readouterr()
    (target / "config").mkdir(exist_ok=True)
    return target


def write_config(folder: Path, data: dict) -> None:
    (folder / "config" / "criteria.json").write_text(json.dumps(data), encoding="utf-8")


def run_validate(folder: Path, capsys) -> tuple[int, dict]:
    code = cli.main(["criteria-validate", "--data-dir", str(folder)])
    return code, json.loads(capsys.readouterr().out)


def test_criteria_validate_on_a_good_config_reports_counts(folder, capsys):
    write_config(folder, MINIMAL)
    code, out = run_validate(folder, capsys)
    assert code == 0 and out["ok"] is True and out["command"] == "criteria-validate"
    assert out["counts"] == {"criteria": 4, "hard_filters": 3, "families": 2}


def test_criteria_validate_on_a_bad_config_lists_at_most_ten_errors_and_the_next_step(
        folder, capsys):
    data = copy.deepcopy(MINIMAL)
    for index in range(12):
        data["hard_filters"].append({"id": f"f{index}", "type": "nope"})
    write_config(folder, data)
    code, out = run_validate(folder, capsys)
    assert code == 1 and out["ok"] is False
    assert len(out["data"]["errors"]) == 10
    assert out["data"]["errors"] == sorted(out["data"]["errors"])
    assert out["counts"]["errors"] >= 12
    assert out["next"] == "Fix the listed fields and run criteria-validate again."


def test_criteria_validate_without_a_config_says_to_run_the_interview(folder, capsys):
    code, out = run_validate(folder, capsys)
    assert code == 1 and out["ok"] is False
    assert "Run the interview first" in out["error"]


def test_criteria_validate_on_unreadable_json_says_so_without_a_stack_trace(folder, capsys):
    (folder / "config" / "criteria.json").write_text("{not json", encoding="utf-8")
    code, out = run_validate(folder, capsys)
    assert code == 1 and out["ok"] is False
    assert "not readable JSON" in out["error"]


def test_criteria_validate_without_a_data_folder_fails_plainly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["criteria-validate"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False and out["error"] == "No data folder found."


def bool_param(key: str, value: object) -> Callable[[dict], None]:
    """Set one of the four true-or-false params on its criterion."""
    def mutate(data):
        if key == "cap_off_level":
            _extra_criterion(evaluator="level_match",
                             params={"target": "manager", "years": 9, key: value})(data)
        elif key == "share":
            share_instead_of_bands(data)
            data["criteria"][SKILLS]["params"][key] = value
        else:  # rarity, cap_no_skill
            data["criteria"][SKILLS]["params"][key] = value
    return mutate


BOOL_PARAM_PATH = {"cap_off_level": "criteria[4].params.cap_off_level",
                   "rarity": "criteria[1].params.rarity",
                   "cap_no_skill": "criteria[1].params.cap_no_skill",
                   "share": "criteria[1].params.share"}


@pytest.mark.parametrize("key, value", [
    ("cap_off_level", True), ("cap_off_level", False), ("rarity", True), ("rarity", False),
    ("cap_no_skill", True), ("cap_no_skill", False), ("share", True)])
def test_a_true_or_false_param_accepts_a_bool(key, value):
    assert errors_after(bool_param(key, value)) == []


@pytest.mark.parametrize("value", ["yes", 1, 0, None, []])
@pytest.mark.parametrize("key", list(BOOL_PARAM_PATH))
def test_a_true_or_false_param_refuses_anything_that_is_not_a_bool(key, value):
    assert_error(errors_after(bool_param(key, value)), BOOL_PARAM_PATH[key], "must be true or false.")


def share_instead_of_bands(data: dict) -> None:
    params = data["criteria"][SKILLS]["params"]
    del params["bands"]
    params["share"] = True


def test_keyword_coverage_refuses_share_true_with_bands():
    errors = errors_after(lambda d: d["criteria"][SKILLS]["params"].update(share=True))
    assert_error(errors, "criteria[1].params", "use bands or share, not both.")


def test_keyword_coverage_refuses_share_false_with_no_bands():
    def mutate(data):
        share_instead_of_bands(data)
        data["criteria"][SKILLS]["params"]["share"] = False
    assert_error(errors_after(mutate), "criteria[1].params", "needs bands, or share set to true.")


def test_keyword_coverage_refuses_no_share_and_no_bands():
    def mutate(data):
        del data["criteria"][SKILLS]["params"]["bands"]
    assert_error(errors_after(mutate), "criteria[1].params", "needs bands, or share set to true.")


def test_keyword_coverage_accepts_a_priority_flag_on_a_skill():
    def mutate(data):
        data["criteria"][SKILLS]["params"]["must_have"][0]["priority"] = True
    assert errors_after(mutate) == []


@pytest.mark.parametrize("value", ["yes", 1, 0, None])
def test_keyword_coverage_refuses_a_priority_that_is_not_a_bool(value):
    def mutate(data):
        data["criteria"][SKILLS]["params"]["must_have"][0]["priority"] = value
    assert_error(errors_after(mutate), "must_have[0].priority", "must be true or false.")


@pytest.mark.parametrize("word", ["-", "/", " - ", "-/-"])
def test_a_family_exclude_word_with_nothing_to_match_is_refused(word):
    # The compiler drops a word with no letters or digits, so it must not be accepted.
    def mutate(data):
        data["title"]["families"][0]["exclude"] = ["legal", word]
    assert_error(errors_after(mutate), "title.families[0].exclude[1]",
                 "has no letters or digits to match")


def test_a_family_exclude_list_is_validated():
    def with_words(words):
        def mutate(data):
            data["title"]["families"][0]["exclude"] = words
        return mutate

    family = MINIMAL["title"]["families"][0]
    term = family["terms"][0]
    word_in_term = term.split()[0]
    assert errors_after(with_words(["Legal", "technical"])) == []
    assert errors_after(with_words([])) == []
    assert_error(errors_after(with_words("legal")), "title.families[0].exclude", "must be a list")
    assert_error(errors_after(with_words(["legal", 7])),
                 "title.families[0].exclude[1]", "empty")
    assert_error(errors_after(with_words(["x" * 81])),
                 "title.families[0].exclude[0]", "longer than 80 characters")
    assert_error(errors_after(with_words([f"w{i}" for i in range(21)])),
                 "title.families[0].exclude", "more than 20 words")
    assert_error(errors_after(with_words(["Legal", "legal"])),
                 "title.families[0].exclude[1]", '"legal" is listed twice')
    assert_error(errors_after(with_words(["legal", word_in_term])),
                 "title.families[0].exclude[1]", f'"{word_in_term}" would rule out every title in',
                 family["label"])
