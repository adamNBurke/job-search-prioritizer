"""A Markdown resume and interview answers become a valid, versioned scoring config.

Purpose: prove that resume import and criteria-build are deterministic code, so the same answers
always give the same config, and that every built config passes criteria.validate_criteria.
Logic: unit tests drive criteria_build functions on plain dicts; integration tests drive cli.main
the way a user does (init, profile-import, criteria-build) on a data folder under tmp_path and
read the files back. The directory file is monkeypatched where a test needs a known board count.
Guardrails: personas under tests/fixtures/personas are fictional (example.com, 555-01xx). No test
touches the network or the wall clock.
QA checklist: a changed importance table or filter default needs a case here; a changed file
shape needs docs/data-files.md updated in the same commit.
"""

from __future__ import annotations

import copy
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from helpers import PERSONA_NAMES, cli_result, facts_for

from jobsearch import criteria, criteria_build, directory, families, fsio, scoring
from jobsearch.errors import UserError
from jobsearch.roles_store import RoleFacts

PERSONAS = Path(__file__).resolve().parent / "fixtures" / "personas"
CASEY = PERSONAS / "casey-ops"


def load_persona(name: str) -> tuple[dict, str]:
    answers = json.loads((PERSONAS / name / "answers.json").read_text(encoding="utf-8"))
    resume = (PERSONAS / name / "resume.md").read_text(encoding="utf-8")
    return answers, resume


def install_persona(folder: Path, name: str) -> None:
    (folder / "profile").mkdir(exist_ok=True)
    shutil.copyfile(PERSONAS / name / "answers.json", folder / "profile" / "answers.json")
    shutil.copyfile(PERSONAS / name / "resume.md", folder / "profile" / "resume.md")


def written_config(folder: Path) -> dict:
    return json.loads((folder / "config" / "criteria.json").read_text(encoding="utf-8"))


def built(name: str) -> dict:
    """The config build_criteria makes from a persona, with no previous config."""
    answers, resume = load_persona(name)
    return criteria_build.build_criteria(answers, resume, None)


def built_folder(folder: Path, capsys, name: str) -> dict:
    """Install a persona, run criteria-build through the CLI, and return the written config."""
    install_persona(folder, name)
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0, out
    return written_config(folder)


# profile-import ---------------------------------------------------------------------------


def test_profile_import_writes_resume_and_keeps_the_original(folder, tmp_path, capsys):
    source = tmp_path / "my resume.md"
    source.write_bytes(b"\xef\xbb\xbf# Casey Example\r\n\r\n- Ran a team\r\n- Cut cost")
    code, out = cli_result(capsys, "profile-import", "--data-dir", str(folder), "--resume", str(source))
    assert code == 0 and out["ok"] is True
    assert out["data"]["format"] == "markdown"
    saved = (folder / "profile" / "resume.md").read_bytes()
    assert saved == b"# Casey Example\n\n- Ran a team\n- Cut cost\n"
    assert (folder / "profile" / "source" / "my resume.md").read_bytes() == source.read_bytes()


def test_profile_import_counts_headings_and_bullets(folder, capsys):
    code, out = cli_result(capsys, "profile-import", "--data-dir", str(folder),
                    "--resume", str(CASEY / "resume.md"))
    assert code == 0
    assert out["data"]["headings"] == 7
    assert out["data"]["bullets"] == 8


def test_profile_import_names_the_accepted_formats_for_anything_else(folder, tmp_path, capsys):
    source = tmp_path / "resume.rtf"
    source.write_text("{\\rtf1 hello}", encoding="utf-8")
    code, out = cli_result(capsys, "profile-import", "--data-dir", str(folder), "--resume", str(source))
    assert code == 1
    for name in ("PDF", "DOCX", "Markdown"):
        assert name in out["error"]
    assert not (folder / "profile" / "resume.md").exists()


def test_profile_import_missing_file_is_a_plain_sentence(folder, tmp_path, capsys):
    code, out = cli_result(capsys, "profile-import", "--data-dir", str(folder),
                    "--resume", str(tmp_path / "nope.md"))
    assert code == 1 and "resume file" in out["error"].lower()


# importance table and weights ---------------------------------------------------------------


def test_importance_points_is_the_one_fixed_table():
    assert criteria_build.IMPORTANCE_POINTS == {1: 1, 2: 2, 3: 4, 4: 6, 5: 9}


def test_importance_mapping_follows_the_interview_rules():
    answers = {"top_three": ["pay"], "dont_care": ["level"], "skipped": []}
    assert criteria_build.importance_for("pay", answers, True) == 5
    assert criteria_build.importance_for("level", answers, True) == 1
    assert criteria_build.importance_for("skills", answers, True) == 5
    assert criteria_build.importance_for("freshness", answers, True) == 2
    assert criteria_build.importance_for("skills", answers, False) is None


def test_skills_default_to_the_top_importance_unless_the_user_does_not_care():
    # The default, not a hard-coded weight.
    neither = {"top_three": ["pay"], "dont_care": [], "skipped": []}
    top = {"top_three": ["skills"], "dont_care": [], "skipped": []}
    low = {"top_three": [], "dont_care": ["skills"], "skipped": []}
    assert criteria_build.SKILLS_IMPORTANCE == criteria_build.TOP_IMPORTANCE == 5
    assert criteria_build.importance_for("skills", neither, True) == 5
    assert criteria_build.importance_for("skills", top, True) == 5
    assert criteria_build.importance_for("skills", low, True) == 1
    assert criteria_build.importance_for("skills", neither, False) is None


def test_freshness_follows_top_three_and_dont_care_over_its_default():
    top = {"top_three": ["freshness"], "dont_care": [], "skipped": []}
    low = {"top_three": [], "dont_care": ["freshness"], "skipped": []}
    assert criteria_build.importance_for("freshness", top, True) == 5
    assert criteria_build.importance_for("freshness", low, True) == 1


# casey end to end ---------------------------------------------------------------------------


def test_criteria_build_writes_valid_versioned_config_and_scope(folder, capsys):
    install_persona(folder, "casey-ops")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0 and out["ok"] is True
    assert out["next"] == "Run sample for title discovery."
    config = written_config(folder)
    assert config["criteria_version"] == "1.0"
    assert config["created_from"] == "intake"
    assert criteria.validate_criteria(config) == []
    scope = json.loads((folder / "config" / "scan-scope.json").read_text(encoding="utf-8"))
    assert scope == {"schema_version": 1, "criteria_version": "1.0", "industries": [],
                     "exclude_industries": [], "sizes": []}
    assert out["counts"]["criteria"] == len(config["criteria"])
    assert out["counts"]["hard_filters"] == len(config["hard_filters"])


def test_casey_weights_follow_the_table_and_sum_to_100(folder, capsys):
    weights = {c["id"]: c["weight"] for c in built_folder(folder, capsys, "casey-ops")["criteria"]}
    # Casey never named a first choice, so every stated family is rung 3 and title_fit is left
    # out: its points are shared by the other criteria.
    assert weights == {"skills": 30, "level": 3, "pay": 30, "location": 30, "freshness": 7}
    assert sum(weights.values()) == 100


def test_casey_filters_use_the_documented_unknown_defaults(folder, capsys):
    filters = {f["id"]: f for f in built_folder(folder, capsys, "casey-ops")["hard_filters"]}
    assert set(filters) == {"pay_floor", "work_mode", "region"}
    pay = filters["pay_floor"]
    assert (pay["type"], pay["field"], pay["value"], pay["currency"], pay["unknown"]) == (
        "min_number", "comp_max", 95000, "USD", "gap")
    assert filters["work_mode"]["allow"] == ["hybrid", "remote"]
    assert filters["work_mode"]["unknown"] == "gap"
    assert filters["region"]["allow"] == ["US"] and filters["region"]["unknown"] == "gap"


def test_pay_bands_are_1_3_1_15_and_1_0_times_the_floor():
    config = built("casey-ops")
    pay = next(c for c in config["criteria"] if c["id"] == "pay")
    assert pay["field"] == "comp_max"
    assert pay["params"] == {"bands": [[123500, 3], [109250, 2], [95000, 1]], "currency": "USD"}


def test_each_target_title_becomes_a_stated_family(folder, capsys):
    families = built_folder(folder, capsys, "casey-ops")["title"]["families"]
    assert [f["id"] for f in families] == [
        "tf-s-business-operations-manager", "tf-s-operations-manager", "tf-s-program-manager"]
    for family in families:
        assert family["rung"] == 3 and family["origin"] == "stated"
    assert families[1]["terms"] == ["operations manager"]


def test_one_per_company_is_on_in_every_built_config():
    config = built("casey-ops")
    assert config["one_per_company"] == {"enabled": True, "keep": "best"}
    assert [t["id"] for t in config["tiers"]] == ["A", "B", "C"]
    assert [t["min_score"] for t in config["tiers"]] == [70, 50, 0]


def test_scan_scope_names_industries_ruled_out_and_sizes():
    answers, _ = load_persona("casey-ops")
    answers = copy.deepcopy(answers)
    answers["industries_wanted"] = ["software", "healthcare"]
    answers["industries_ruled_out"] = ["fintech"]
    answers["company_sizes"] = ["51-200", "1-50"]
    scope = criteria_build.build_scan_scope(answers, "2.3")
    assert scope == {"schema_version": 1, "criteria_version": "2.3",
                     "industries": ["healthcare", "software"], "exclude_industries": ["fintech"],
                     "sizes": ["1-50", "51-200"]}


def test_few_matching_starter_boards_is_said_plainly(folder, tmp_path, monkeypatch, capsys):
    entries = [{
        "company": f"Fictional Co {n}", "ats": "greenhouse", "slug": f"fictional-{n}",
        "industry": "software", "size": "51-200", "stage": "growth",
        "last_verified": "2026-11-17", "identity": "company_name", "postings_at_verify": 3,
    } for n in range(3)]
    small = tmp_path / "boards.json"
    small.write_text(json.dumps({"schema_version": 1, "generated_on": "2026-11-17",
                                 "entries": entries}), encoding="utf-8")
    monkeypatch.setattr(directory, "DIRECTORY_PATH", small)
    install_persona(folder, "casey-ops")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0
    assert out["counts"]["directory_boards_matching"] == 3
    assert ("Only 3 starter boards match your industries and company sizes. Add your own "
            "target companies or paste postings to see more roles.") in out["warnings"]


def test_a_full_directory_gives_no_board_warning(folder, capsys):
    install_persona(folder, "casey-ops")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0
    assert not [w for w in out["warnings"] if "starter boards" in w]


def test_cli_init_import_build_validate_all_exit_zero(tmp_path, capsys):
    target = tmp_path / "fresh"
    assert cli_result(capsys, "init", "--data-dir", str(target))[0] == 0
    assert cli_result(capsys, "profile-import", "--data-dir", str(target),
               "--resume", str(CASEY / "resume.md"))[0] == 0
    (target / "profile").mkdir(exist_ok=True)
    shutil.copyfile(CASEY / "answers.json", target / "profile" / "answers.json")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(target))[0] == 0
    code, out = cli_result(capsys, "criteria-validate", "--data-dir", str(target))
    assert code == 0 and out["ok"] is True
    assert criteria.validate_criteria(fsio.read_json(target / "config" / "criteria.json")) == []


def test_criteria_build_without_answers_says_what_to_do(folder, capsys):
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 1
    assert "interview" in out["error"].lower()


# Edges: adjacency, empty, ordering ------------------------------------------------------


def blank_answers() -> dict:
    return {
        "schema_version": 1, "answered_on": "2026-11-17", "target_titles": [],
        "target_level": None, "years_experience": None, "skills": [], "pay_floor": None,
        "work_modes": [], "locations": [], "regions": [], "needs_sponsorship": None,
        "industries_wanted": [], "industries_ruled_out": [], "company_sizes": [],
        "company_stages": [], "deal_breaker_terms": [], "companies_ruled_out": [], "strict": [],
        "top_three": [], "dont_care": [], "skipped": [],
    }


def test_equal_importance_gives_weights_within_one_and_the_tie_goes_to_the_lower_id():
    weights = criteria_build.weights_from_points({"c": 4, "a": 4, "b": 4})
    assert weights == {"a": 34, "b": 33, "c": 33}
    assert sum(weights.values()) == 100
    assert max(weights.values()) - min(weights.values()) <= 1


def test_weights_always_sum_to_100_for_awkward_point_sets():
    for points in ({"a": 1}, {"a": 1, "b": 9}, {"a": 9, "b": 9, "c": 1, "d": 2, "e": 4, "f": 6}):
        assert sum(criteria_build.weights_from_points(points).values()) == 100


def test_all_questions_skipped_still_builds_a_valid_config():
    config = criteria_build.build_criteria(blank_answers(), "", None)
    assert criteria.validate_criteria(config) == []
    assert {c["id"]: c["weight"] for c in config["criteria"]} == {"freshness": 100}
    assert config["hard_filters"] == []
    assert config["title"]["families"] == []


def test_empty_answers_through_the_command_warn_about_no_families(folder, capsys):
    (folder / "profile").mkdir(exist_ok=True)
    (folder / "profile" / "answers.json").write_text(json.dumps(blank_answers()), encoding="utf-8")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0
    assert "No title families yet; title discovery will propose some." in out["warnings"]


def shuffled(value):
    """The same data with every dict reordered and every list reversed."""
    if isinstance(value, dict):
        return {k: shuffled(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [shuffled(v) for v in reversed(value)]
    return value


@pytest.mark.parametrize("name, first_choice", [(n, "absent") for n in PERSONA_NAMES]
                         + [("casey-ops", "Operations Manager")])
def test_criteria_bytes_do_not_depend_on_answer_order(name, first_choice):
    answers, resume = load_persona(name)
    if first_choice != "absent":
        answers["first_choice_title"] = first_choice
    first = criteria_build.build_criteria(answers, resume, None)
    second = criteria_build.build_criteria(shuffled(answers), resume, None)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert json.dumps(first) == json.dumps(second)


# filters ------------------------------------------------------------------------------------


def filters_by_id(config: dict) -> dict:
    return {f["id"]: f for f in config["hard_filters"]}


def test_strict_pay_floor_flips_that_filter_to_reject_and_no_other():
    answers, resume = load_persona("casey-ops")
    answers["strict"] = ["pay_floor"]
    filters = filters_by_id(criteria_build.build_criteria(answers, resume, None))
    assert filters["pay_floor"]["unknown"] == "reject"
    assert filters["work_mode"]["unknown"] == "gap" and filters["region"]["unknown"] == "gap"


@pytest.mark.parametrize("value", [None, False])
def test_no_sponsorship_filter_unless_the_user_said_they_need_it(value):
    answers, resume = load_persona("casey-ops")
    answers["needs_sponsorship"] = value
    answers["strict"] = ["sponsorship"]
    assert "sponsorship" not in filters_by_id(criteria_build.build_criteria(answers, resume, None))


def test_needs_sponsorship_true_adds_the_filter_with_unknown_pass():
    answers, resume = load_persona("casey-ops")
    answers["needs_sponsorship"] = True
    entry = filters_by_id(criteria_build.build_criteria(answers, resume, None))["sponsorship"]
    assert entry["type"] == "deny_terms" and entry["unknown"] == "pass"
    for phrase in ("unable to sponsor", "not able to sponsor", "cannot sponsor",
                   "no visa sponsorship", "without sponsorship", "not sponsor"):
        assert phrase in entry["terms"]
    assert entry["terms"] == sorted(entry["terms"])


def test_strict_sponsorship_flips_it_to_reject():
    answers, resume = load_persona("devon-analyst")
    assert answers["strict"] == ["sponsorship"]
    entry = filters_by_id(criteria_build.build_criteria(answers, resume, None))["sponsorship"]
    assert entry["unknown"] == "reject"


def test_ruled_out_industries_deal_breakers_and_companies_become_deny_filters():
    answers, resume = load_persona("casey-ops")
    answers["industries_ruled_out"] = ["fintech", "energy-climate"]
    answers["deal_breaker_terms"] = ["travel required", "commission only"]
    answers["companies_ruled_out"] = ["Zeta Example Corp", "Acme Fiction"]
    filters = filters_by_id(criteria_build.build_criteria(answers, resume, None))
    industry = filters["industry_ruled_out"]
    assert (industry["type"], industry["field"], industry["unknown"]) == (
        "deny_terms", "company_industry", "pass")
    assert industry["terms"] == ["energy-climate", "fintech"]
    deal = filters["deal_breakers"]
    assert (deal["type"], deal["field"], deal["unknown"]) == ("deny_terms", "text", "pass")
    assert deal["terms"] == ["commission only", "travel required"]
    names = filters["no_go_companies"]
    assert (names["type"], names["names"]) == ("deny_list", ["Acme Fiction", "Zeta Example Corp"])


def test_the_filter_order_is_fixed():
    ids = [f["id"] for f in built("devon-analyst")["hard_filters"]]
    assert ids == ["pay_floor", "work_mode", "region", "sponsorship", "industry_ruled_out",
                   "deal_breakers"]


def test_intern_exclusion_is_dropped_for_an_entry_level_target():
    answers, resume = load_persona("casey-ops")
    assert criteria_build.build_criteria(answers, resume, None)["title"]["exclude"] == [
        {"terms": ["intern", "internship"], "origin": "default"}]
    answers["target_level"] = "entry"
    assert criteria_build.build_criteria(answers, resume, None)["title"]["exclude"] == []


def skills_criterion(config: dict) -> dict:
    return next(c for c in config["criteria"] if c["id"] == "skills")


def skill_weights(config: dict) -> dict:
    return {i["term"]: i["weight"] for i in skills_criterion(config)["params"]["must_have"]}


def test_priority_skills_weigh_two_and_the_rest_one():
    answers, resume = load_persona("casey-ops")
    for skill in answers["skills"]:
        skill.pop("priority", None)
    for skill in answers["skills"]:
        if skill["term"] in ("Excel", "Six Sigma"):
            skill["priority"] = True
    weights = skill_weights(criteria_build.build_criteria(answers, resume, None))
    assert weights == {"budgeting": 1, "Excel": 2, "process improvement": 1, "Six Sigma": 2,
                       "vendor management": 1}


def test_skills_without_priority_all_weigh_one():
    answers, resume = load_persona("devon-analyst")
    for skill in answers["skills"]:
        skill.pop("priority", None)
    weights = skill_weights(criteria_build.build_criteria(answers, resume, None))
    assert set(weights.values()) == {1}


@pytest.mark.parametrize("name", PERSONA_NAMES[1:])
def test_personas_that_never_answered_the_priority_question_build_weight_one(name):
    weights = skill_weights(built(name))
    assert weights and set(weights.values()) == {1}


def test_casey_answered_the_priority_question_so_two_skills_weigh_two(folder, capsys):
    assert skill_weights(built_folder(folder, capsys, "casey-ops")) == {
        "budgeting": 2, "vendor management": 2, "Excel": 1, "process improvement": 1,
        "Six Sigma": 1}


def test_skills_params_use_the_share_and_mark_only_the_chosen_skills_as_priority(folder, capsys):
    params = skills_criterion(built_folder(folder, capsys, "casey-ops"))["params"]
    assert params["share"] is True and params["rarity"] is True and params["nice"] == []
    assert "bands" not in params
    items = {i["term"]: i for i in params["must_have"]}
    assert items["vendor management"]["priority"] is True
    assert items["vendor management"]["weight"] == 2
    assert items["budgeting"]["priority"] is True and items["budgeting"]["weight"] == 2
    for name in ("Excel", "process improvement", "Six Sigma"):
        assert "priority" not in items[name] and items[name]["weight"] == 1


def test_four_priority_skills_stop_the_build():
    answers, resume = load_persona("casey-ops")
    for skill in answers["skills"][:4]:
        skill["priority"] = True
    with pytest.raises(UserError, match="at most three"):
        criteria_build.build_criteria(answers, resume, None)


def test_the_skills_criterion_has_a_zero_hit_reason_next_to_the_default():
    reasons = skills_criterion(built("casey-ops"))["reasons"]
    assert reasons["0"] == "None of your {total} key skills appear in the description."
    assert reasons["default"].startswith("{hit} of your {total} key skills")


def test_a_resume_skill_the_resume_lacks_stops_the_build():
    answers, resume = load_persona("casey-ops")
    answers["skills"].append({"term": "Kubernetes", "aliases": [], "origin": "resume"})
    with pytest.raises(UserError, match="Kubernetes"):
        criteria_build.build_criteria(answers, resume, None)


def test_the_command_lists_answer_problems_and_writes_nothing(folder, capsys):
    install_persona(folder, "casey-ops")
    path = folder / "profile" / "answers.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["regions"] = ["USA"]
    path.write_text(json.dumps(data), encoding="utf-8")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 1 and out["ok"] is False
    assert any("regions[0]" in e for e in out["data"]["errors"])
    assert not (folder / "config" / "criteria.json").exists()


def _break(folder: Path, mutate) -> bytes:
    path = folder / "config" / "criteria.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path.read_bytes()


def test_criteria_build_rebuilds_over_a_config_that_fails_validation(folder, capsys):
    """The validation error tells the user to run the interview again, which ends in
    criteria-build, so it must run."""
    install_persona(folder, "casey-ops")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    broken = _break(folder, lambda d: d["criteria"][0].update(evaluator="magic"))
    assert cli_result(capsys, "criteria-validate", "--data-dir", str(folder))[0] == 1
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0 and out["ok"], out
    assert criteria.validate_criteria(written_config(folder)) == []
    assert any("could not be used" in w for w in out["warnings"])
    history = folder / "config" / "criteria-history"
    assert broken in [p.read_bytes() for p in history.iterdir()]


@pytest.mark.parametrize("version", ["not a version", None])
def test_criteria_build_rebuilds_over_a_config_with_no_readable_version(folder, capsys, version):
    install_persona(folder, "casey-ops")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    broken = _break(folder, lambda d: d.update(criteria_version=version))
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0 and out["ok"], out
    assert criteria.validate_criteria(written_config(folder)) == []
    history = folder / "config" / "criteria-history"
    assert broken in [p.read_bytes() for p in history.iterdir()]
    # and a second break does not overwrite the first copy
    second = _break(folder, lambda d: d.update(criteria_version=version, note="second"))
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    kept = [p.read_bytes() for p in history.iterdir()]
    assert broken in kept and second in kept


# personas -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", PERSONA_NAMES)
def test_every_persona_builds_a_config_that_passes_the_validator(name, folder, capsys):
    config = built_folder(folder, capsys, name)
    assert criteria.validate_criteria(config) == []
    assert cli_result(capsys, "criteria-validate", "--data-dir", str(folder))[0] == 0
    assert sum(c["weight"] for c in config["criteria"]) == 100


def test_the_three_personas_have_different_filter_sets():
    sets = {}
    for name in PERSONA_NAMES:
            sets[name] = [f["id"] for f in built(name)[
            "hard_filters"]]
    assert len({tuple(v) for v in sets.values()}) == 3
    assert "sponsorship" in sets["devon-analyst"] and "sponsorship" not in sets["rowan-designer"]
    assert "no_go_companies" in sets["rowan-designer"]


def test_devon_hourly_floor_is_annualized_and_rowan_keeps_euros():
    answers, resume = load_persona("devon-analyst")
    pay = filters_by_id(criteria_build.build_criteria(answers, resume, None))["pay_floor"]
    assert (pay["value"], pay["currency"]) == (99840, "USD")
    answers, resume = load_persona("rowan-designer")
    config = criteria_build.build_criteria(answers, resume, None)
    pay = filters_by_id(config)["pay_floor"]
    assert (pay["value"], pay["currency"]) == (62000, "EUR")
    assert filters_by_id(config)["region"]["allow"] == ["DE", "EU", "NL"]
    assert filters_by_id(config)["region"]["unknown"] == "reject"


def test_rowan_skipped_sponsorship_stays_null_and_sets_no_filter():
    answers, resume = load_persona("rowan-designer")
    assert answers["needs_sponsorship"] is None
    assert "sponsorship" not in filters_by_id(criteria_build.build_criteria(answers, resume, None))


def test_devon_scope_names_industries_and_sizes(folder, capsys):
    install_persona(folder, "devon-analyst")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    scope = json.loads((folder / "config" / "scan-scope.json").read_text(encoding="utf-8"))
    assert scope["industries"] == ["biotech", "healthcare"]
    assert scope["exclude_industries"] == ["fintech"]
    assert scope["sizes"] == ["1001-5000", "201-1000"]


# retune and history ---------------------------------------------------------------

DISCOVERED = {
    "id": "tf-d-supply-chain", "label": "Supply chain manager", "rung": 2,
    "terms": ["supply chain manager"], "regex": [],
    "origin": "discovered", "approved_at": "2026-11-17T10:00:00Z",
}


def test_rerun_after_a_change_keeps_history_and_carries_discovery_forward(folder, capsys):
    install_persona(folder, "casey-ops")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    config_path = folder / "config" / "criteria.json"
    config = written_config(folder)
    config["title"]["families"].append(DISCOVERED)
    config["tiers"][0]["min_score"] = 75
    config["tiers"][1]["min_score"] = 55
    fsio.write_json_atomic(config_path, config)
    before = config_path.read_bytes()

    answers_path = folder / "profile" / "answers.json"
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    answers["pay_floor"]["amount"] = 105000
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))

    assert code == 0 and out["data"]["criteria_version"] == "1.1"
    assert (folder / "config" / "criteria-history" / "criteria-v1.0.json").read_bytes() == before
    new = written_config(folder)
    assert new["criteria_version"] == "1.1" and new["created_from"] == "retune"
    # No evidence block: the rung rule gives a discovered family rung 1.
    assert dict(DISCOVERED, rung=1) in new["title"]["families"]
    assert [t["min_score"] for t in new["tiers"]] == [75, 55, 0]
    assert filters_by_id(new)["pay_floor"]["value"] == 105000
    assert "config/criteria-history/criteria-v1.0.json" in out["wrote"]
    scope = json.loads((folder / "config" / "scan-scope.json").read_text(encoding="utf-8"))
    assert scope["criteria_version"] == "1.1"


def test_rerun_with_the_same_answers_makes_no_new_version(folder, capsys):
    install_persona(folder, "casey-ops")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    before = (folder / "config" / "criteria.json").read_bytes()
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0 and out["data"]["unchanged"] is True
    assert (folder / "config" / "criteria.json").read_bytes() == before
    assert not (folder / "config" / "criteria-history").exists()


def test_a_discovered_family_the_new_answers_would_collide_with_is_not_duplicated():
    answers, resume = load_persona("casey-ops")
    previous = criteria_build.build_criteria(answers, resume, None)
    clash = dict(DISCOVERED, id="tf-s-program-manager")
    previous["title"]["families"].append(clash)
    config = criteria_build.build_criteria(answers, resume, previous)
    ids = [f["id"] for f in config["title"]["families"]]
    assert ids.count("tf-s-program-manager") == 1
    assert criteria.validate_criteria(config) == []


def test_a_listed_family_the_user_edited_in_title_discovery_survives_a_retune():
    """titles-apply marks a family the user had listed as origin "stated" under a discovery id.

    Regression: only origin "discovered" families were carried, so an edited listed family
    (extra terms the user approved) was silently dropped on the next criteria-build.
    """
    answers, resume = load_persona("casey-ops")
    previous = criteria_build.build_criteria(answers, resume, None)
    edited = dict(DISCOVERED, id="tf-d-ops-lead", label="Operations lead",
                  terms=["operations lead", "ops lead"], origin="stated")
    previous["title"]["families"].append(edited)
    config = criteria_build.build_criteria(answers, resume, previous)
    # The first-choice question was skipped, so the stated family is rung 3.
    assert dict(edited, rung=3) in config["title"]["families"]
    assert criteria.validate_criteria(config) == []


def test_a_retune_keeps_the_rung_descending_then_id_family_order_titles_apply_writes():
    """The order serves stable bytes: match_family no longer depends on it, but the
    families are still written rung-descending then id, as titles-apply writes them."""
    answers, resume = load_persona("casey-ops")
    previous = criteria_build.build_criteria(answers, resume, None)
    low = discovered("tf-a-low", 1, terms=["shared role"])
    high = discovered("tf-b-high", 12, terms=["shared role"])
    previous["title"]["families"].extend([low, high])
    config = criteria_build.build_criteria(answers, resume, previous)
    families = config["title"]["families"]
    assert families == sorted(families, key=lambda f: (-f["rung"], f["id"]))
    ids = [f["id"] for f in families]
    assert ids.index("tf-b-high") < ids.index("tf-a-low")


# tier caps from the interview ----------------------------------------------------------

def level_params(config: dict) -> dict:
    return next(c for c in config["criteria"] if c["id"] == "level")["params"]


def test_the_provisional_threshold_is_sixty():
    assert criteria_build.PROVISIONAL_BELOW_COVERAGE == 60
    config = built("casey-ops")
    assert config["banding"] == {"provisional_below_coverage": 60}


def freshness_params(config: dict) -> dict:
    return next(c for c in config["criteria"] if c["id"] == "freshness")["params"]


# the casey persona does not care about level, the other two do
LEVEL_CAP_ON = {"casey-ops": False, "devon-analyst": True, "rowan-designer": True}


@pytest.mark.parametrize("name", PERSONA_NAMES)
def test_every_persona_config_carries_the_caps_share_rarity_and_age_rules(name):
    config = built(name)
    assert level_params(config)["cap_off_level"] is LEVEL_CAP_ON[name]
    age = freshness_params(config)
    assert (age["cap_after_days"], age["reject_after_days"]) == (60, 180)
    assert age["bands"] == [[14, 3], [45, 2], [90, 1]] and age["else"] == 0
    params = skills_criterion(config)["params"]
    assert params["share"] is True and params["rarity"] is True
    assert skills_params(config)["cap_no_skill"] is True
    assert criteria.validate_criteria(config) == []


def test_dont_care_freshness_keeps_the_age_rules():
    answers, resume = load_persona("casey-ops")
    answers["dont_care"] = [*answers["dont_care"], "freshness"]
    assert criteria_build.importance_for("freshness", answers, True) == 1
    config = criteria_build.build_criteria(answers, resume, None)
    params = freshness_params(config)
    assert (params["cap_after_days"], params["reject_after_days"]) == (60, 180)
    assert criteria.validate_criteria(config) == []


def skills_params(config: dict) -> dict:
    return next(c for c in config["criteria"] if c["id"] == "skills")["params"]


def test_dont_care_skills_turns_the_skills_cap_off_and_keeps_importance_one():
    answers, resume = load_persona("casey-ops")
    answers["dont_care"] = ["level", "skills"]
    assert criteria_build.importance_for("skills", answers, True) == 1
    config = criteria_build.build_criteria(answers, resume, None)
    assert skills_params(config)["cap_no_skill"] is False
    assert any(c["id"] == "skills" and c["weight"] > 0 for c in config["criteria"])
    assert criteria.validate_criteria(config) == []


def test_skills_in_top_three_keep_the_skills_cap_on():
    answers, resume = load_persona("casey-ops")
    answers["dont_care"], answers["top_three"] = [], ["skills", "pay", "location"]
    assert skills_params(criteria_build.build_criteria(answers, resume, None))["cap_no_skill"]


def test_no_skills_listed_builds_no_skills_criterion():
    answers, resume = load_persona("casey-ops")
    answers["skills"] = []
    config = criteria_build.build_criteria(answers, resume, None)
    assert [c for c in config["criteria"] if c["id"] == "skills"] == []
    assert criteria.validate_criteria(config) == []


def test_the_skills_cap_holds_whether_or_not_a_first_choice_is_named():
    answers, resume = load_persona("casey-ops")
    answers["first_choice_title"] = "Operations Manager"
    named = criteria_build.build_criteria(answers, resume, None)
    del answers["first_choice_title"]
    skipped = criteria_build.build_criteria(answers, resume, None)
    assert skills_params(named)["cap_no_skill"] is True
    assert skills_params(skipped)["cap_no_skill"] is True
    assert "title_fit" in [c["id"] for c in named["criteria"]]
    assert "title_fit" not in [c["id"] for c in skipped["criteria"]]


def test_a_level_in_top_three_keeps_the_cap_on():
    answers, resume = load_persona("casey-ops")
    answers["dont_care"], answers["top_three"] = [], ["level", "pay", "location"]
    assert level_params(criteria_build.build_criteria(answers, resume, None))["cap_off_level"]


def test_dont_care_level_keeps_the_importance_one_weight():
    answers, resume = load_persona("casey-ops")
    assert criteria_build.importance_for("level", answers, True) == 1
    config = criteria_build.build_criteria(answers, resume, None)
    # Title_fit is left out (the question was skipped), so level's 1 point of 30 is weight 3.
    assert next(c for c in config["criteria"] if c["id"] == "level")["weight"] == 3


def test_answers_with_no_level_and_no_years_still_get_no_level_criterion():
    answers, resume = load_persona("casey-ops")
    answers["target_level"], answers["years_experience"] = None, None
    config = criteria_build.build_criteria(answers, resume, None)
    assert all(c["id"] != "level" for c in config["criteria"])


# first choice, rungs and title_fit -----------------------------------------------------

CASEY_RUNGS_WITH_FIRST_CHOICE = {
    "tf-s-operations-manager": 3, "tf-s-business-operations-manager": 2,
    "tf-s-program-manager": 2,
}


def casey_with(first_choice: object = "absent", **changes) -> tuple[dict, str]:
    answers, resume = load_persona("casey-ops")
    if first_choice != "absent":
        answers["first_choice_title"] = first_choice
    answers.update(changes)
    return answers, resume


def rungs(config: dict) -> dict[str, int]:
    return {f["id"]: f["rung"] for f in config["title"]["families"]}


def weights_of(config: dict) -> dict[str, int]:
    return {c["id"]: c["weight"] for c in config["criteria"]}


def discovered(family_id: str, evidence: object = "none", **fields) -> dict:
    family = dict(DISCOVERED, id=family_id, label=family_id, terms=[family_id.replace("-", " ")])
    family.update(fields)
    if evidence != "none":
        family["evidence"] = {"sample_date": "2026-11-17", "postings_sampled": 100,
                              "asking_for_your_skills": evidence, "share": 0.1,
                              "top_shared_skills": []}
    return family


def test_the_rung_constants_follow_the_decision():
    assert (criteria_build.TOP_RUNG, criteria_build.OTHER_RUNG, criteria_build.THIN_RUNG) == (
        3, 2, 1)
    assert criteria_build.THIN_EVIDENCE_POSTINGS == 5


@pytest.mark.parametrize("value", ["Operations Manager", "  Senior OPERATIONS manager "])
def test_first_choice_key_is_the_normalized_title(value):
    assert criteria_build.first_choice_key(value) == "operations manager"


@pytest.mark.parametrize("value", [None, "", "   ", 5, ["x"]])
def test_first_choice_key_is_none_when_the_question_was_skipped(value):
    assert criteria_build.first_choice_key(value) is None


def test_a_skipped_first_choice_keeps_every_stated_family_at_rung_3_and_drops_title_fit():
    answers, resume = casey_with()
    config = criteria_build.build_criteria(answers, resume, None)
    assert set(rungs(config).values()) == {3} and len(rungs(config)) == 3
    assert "title_fit" not in weights_of(config)
    assert weights_of(config) == {"skills": 30, "level": 3, "pay": 30, "location": 30,
                                  "freshness": 7}
    assert sum(weights_of(config).values()) == 100
    assert criteria.validate_criteria(config) == []


def test_a_null_first_choice_builds_the_same_bytes_as_an_absent_one():
    absent = criteria_build.build_criteria(*casey_with(), None)
    null = criteria_build.build_criteria(*casey_with(None), None)
    assert json.dumps(absent) == json.dumps(null)


def test_a_first_choice_makes_its_family_rung_3_and_the_other_stated_families_rung_2():
    answers, resume = casey_with("Operations Manager")
    config = criteria_build.build_criteria(answers, resume, None)
    assert rungs(config) == CASEY_RUNGS_WITH_FIRST_CHOICE
    assert [f["id"] for f in config["title"]["families"]] == [
        "tf-s-operations-manager", "tf-s-business-operations-manager", "tf-s-program-manager"]
    assert weights_of(config) == {"title_fit": 23, "skills": 23, "level": 3, "pay": 23,
                                  "location": 23, "freshness": 5}
    assert sum(weights_of(config).values()) == 100
    assert criteria.validate_criteria(config) == []


def test_first_choice_command_run_writes_the_same_config(folder, capsys):
    install_persona(folder, "casey-ops")
    path = folder / "profile" / "answers.json"
    answers = json.loads(path.read_text(encoding="utf-8"))
    answers["first_choice_title"] = "Program Manager"
    path.write_text(json.dumps(answers), encoding="utf-8")
    assert cli_result(capsys, "criteria-build", "--data-dir", str(folder))[0] == 0
    config = written_config(folder)
    assert rungs(config)["tf-s-program-manager"] == 3
    assert weights_of(config)["title_fit"] == 23
    assert cli_result(capsys, "criteria-validate", "--data-dir", str(folder))[0] == 0


def test_two_target_titles_that_normalize_to_one_family_share_its_first_choice_rung():
    answers, resume = casey_with(
        "Senior Operations Manager",
        target_titles=["Operations Manager", "Senior Operations Manager", "Program Manager"])
    config = criteria_build.build_criteria(answers, resume, None)
    assert rungs(config) == {"tf-s-operations-manager": 3, "tf-s-program-manager": 2}
    assert criteria_build.build_criteria(
        *casey_with("Operations Manager", target_titles=answers["target_titles"]), None) == config


def test_one_target_title_and_no_other_family_leaves_title_fit_out_either_way():
    for first in ("absent", None, "Operations Manager"):
        answers, resume = casey_with(first, target_titles=["Operations Manager"])
        config = criteria_build.build_criteria(answers, resume, None)
        assert set(rungs(config).values()) == {3}
        assert "title_fit" not in weights_of(config)


def test_no_target_titles_and_no_previous_config_builds_with_no_title_fit():
    config = criteria_build.build_criteria(blank_answers(), "", None)
    assert config["title"]["families"] == []
    assert "title_fit" not in weights_of(config)
    assert criteria.validate_criteria(config) == []


@pytest.mark.parametrize(("count", "rung"), [(4, 1), (5, 2), (0, 1), (12, 2)])
def test_a_discovered_family_is_rung_1_below_five_postings_and_rung_2_from_five(count, rung):
    family = discovered("tf-d-supply-chain", count)
    assert criteria_build.family_rung(family, None) == rung
    assert criteria_build.family_rung(family, "operations manager") == rung


def test_a_discovered_family_with_no_evidence_block_or_a_bad_count_is_thin():
    assert criteria_build.family_rung(discovered("tf-d-a"), None) == 1
    for bad in (None, "12", 12.5, True, [12]):
        family = discovered("tf-d-b")
        family["evidence"] = {"asking_for_your_skills": bad}
        assert criteria_build.family_rung(family, None) == 1
    family = discovered("tf-d-c")
    family["evidence"] = "not a dict"
    assert criteria_build.family_rung(family, None) == 1


def test_carried_discovered_families_get_rungs_from_their_evidence():
    answers, resume = casey_with()
    previous = criteria_build.build_criteria(answers, resume, None)
    previous["title"]["families"].extend([
        discovered("tf-d-four", 4), discovered("tf-d-five", 5), discovered("tf-d-none")])
    config = criteria_build.build_criteria(answers, resume, previous)
    assert rungs(config) == {
        "tf-s-business-operations-manager": 3, "tf-s-operations-manager": 3,
        "tf-s-program-manager": 3, "tf-d-five": 2, "tf-d-four": 1, "tf-d-none": 1}
    assert "title_fit" in weights_of(config)


def test_a_skipped_question_with_a_well_evidenced_discovered_family_keeps_title_fit():
    answers, resume = casey_with()
    previous = criteria_build.build_criteria(answers, resume, None)
    previous["title"]["families"].append(discovered("tf-d-supply-chain", 12))
    config = criteria_build.build_criteria(answers, resume, previous)
    assert rungs(config)["tf-d-supply-chain"] == 2
    assert {r for i, r in rungs(config).items() if i.startswith("tf-s-")} == {3}
    assert weights_of(config) == {"title_fit": 23, "skills": 23, "level": 3, "pay": 23,
                                  "location": 23, "freshness": 5}
    assert sum(weights_of(config).values()) == 100


def test_a_carried_stated_origin_family_follows_the_first_choice():
    edited = dict(DISCOVERED, id="tf-d-ops-lead", label="Operations lead",
                  terms=["operations lead", "program manager"], origin="stated")
    edited.pop("approved_at")
    assert criteria_build.family_rung(edited, "program manager") == 3
    assert criteria_build.family_rung(edited, None) == 3
    assert criteria_build.family_rung(edited, "operations manager") == 2


def test_no_target_titles_and_carried_families_gives_2_and_1_with_no_rung_3():
    previous = criteria_build.build_criteria(blank_answers(), "", None)
    previous["title"]["families"].extend([discovered("tf-d-big", 12), discovered("tf-d-small", 2)])
    config = criteria_build.build_criteria(blank_answers(), "", previous)
    assert rungs(config) == {"tf-d-big": 2, "tf-d-small": 1}
    assert "title_fit" in weights_of(config)
    assert criteria.validate_criteria(config) == []


def test_title_fit_separates_only_with_two_distinct_rungs():
    assert criteria_build.title_fit_separates([]) is False
    assert criteria_build.title_fit_separates([{"rung": 3}, {"rung": 3}]) is False
    assert criteria_build.title_fit_separates([{"rung": 3}, {"rung": 2}]) is True


def test_with_rungs_returns_copies_sorted_rung_descending_then_id():
    original = [discovered("tf-d-b", 12), discovered("tf-d-a", 1)]
    result = criteria_build.with_rungs(original, None)
    assert [(f["id"], f["rung"]) for f in result] == [("tf-d-b", 2), ("tf-d-a", 1)]
    assert original[0]["rung"] == DISCOVERED["rung"]  # the input is not mutated


@pytest.mark.parametrize("first", ["absent", "Operations Manager"])
def test_title_fit_reasons_use_the_wording_set_for_the_question(first):
    config = criteria_build.build_criteria(*casey_with(first), None)
    previous = copy.deepcopy(config)
    previous["title"]["families"].append(discovered("tf-d-x", 12))
    config = criteria_build.build_criteria(*casey_with(first), previous)
    item = next(c for c in config["criteria"] if c["id"] == "title_fit")
    assert criteria.validate_criteria(config) == []
    if first == "absent":
        assert item["reasons"]["3"] == 'Title matches your target title "{family}".'
        assert item["reasons"]["2"] == (
            'Title matches "{family}", a family title discovery found.')
    else:
        assert item["reasons"]["3"] == 'Title matches "{family}", your first choice.'
        assert item["reasons"]["2"] == 'Title matches your "{family}" family.'
    assert item["reasons"]["1"] == (
        'Title matches your "{family}" family, which few sampled postings tied to your skills.')
    assert item["reasons"]["0"] == "Title is outside every family you approved."


# scoring with real rungs ---------------------------------------------------------------------

TODAY = date(2026, 11, 17)


def facts_titled(title: str, index: int) -> RoleFacts:
    return facts_for(title=title, role_id=f"greenhouse:acme:{index}", posting_id=str(index),
                     description_text="We need vendor management and budgeting.")


def title_fit_of(scored: scoring.ScoredRole):
    return next(d for d in scored.details if d.criterion_id == "title_fit")


def test_three_roles_that_differ_only_in_title_score_by_family_rung():
    config = criteria_build.build_criteria(*casey_with("Operations Manager"), None)
    titles = ["Operations Manager", "Program Manager", "Business Operations Manager"]
    scored = {s.title: s for s in scoring.score_all(
        [facts_titled(t, i) for i, t in enumerate(titles)], config, TODAY)}
    assert [title_fit_of(scored[t]).points for t in titles] == [3, 2, 2]
    bom = scored["Business Operations Manager"]
    assert bom.family_id == "tf-s-business-operations-manager"
    assert "Business Operations Manager" in title_fit_of(bom).reason
    first = scored["Operations Manager"].score
    assert first > scored["Program Manager"].score
    assert first > bom.score


def test_with_title_fit_omitted_a_title_outside_every_family_is_still_rejected():
    config = criteria_build.build_criteria(*casey_with(), None)
    assert "title_fit" not in weights_of(config)
    scored = scoring.score_all([facts_titled("Pastry Chef", 1)], config, TODAY)[0]
    assert scored.rejected is not None
    assert scored.rejected.filter_id == "title_family" and scored.rejected.label == "Title"
    kept = scoring.score_all([facts_titled("Program Manager", 2)], config, TODAY)[0]
    assert kept.rejected is None


# the most specific family wins a shared title --------------


def family(family_id: str, rung: int, terms: list[str], regex: list[str] | None = None) -> dict:
    return {"id": family_id, "label": family_id, "rung": rung, "terms": terms,
            "regex": regex or [], "origin": "stated"}


def match_id(family_list: list[dict], title: str) -> str | None:
    compiled = families.compile_families({"title": {"families": family_list, "exclude": []}})
    match = families.match_family(title, compiled)
    return match.family_id if match else None


CASEY_FAMILIES = [
    family("tf-s-operations-manager", 3, ["operations manager"]),
    family("tf-s-business-operations-manager", 2, ["business operations manager"]),
    family("tf-s-program-manager", 2, ["program manager"]),
]


def test_the_family_with_the_most_words_in_its_term_wins_even_at_a_lower_rung():
    assert match_id(CASEY_FAMILIES, "Senior Business Operations Manager") == (
        "tf-s-business-operations-manager")
    assert match_id(CASEY_FAMILIES, "Business Operations Manager") == (
        "tf-s-business-operations-manager")


def test_a_title_the_longer_term_does_not_match_falls_to_the_shorter_family():
    assert match_id(CASEY_FAMILIES, "Revenue Operations Manager") == "tf-s-operations-manager"
    assert match_id(CASEY_FAMILIES, "Operations Manager") == "tf-s-operations-manager"


def test_a_title_outside_every_family_still_matches_none():
    assert match_id(CASEY_FAMILIES, "Pastry Chef") is None


def test_equal_words_go_to_the_higher_rung_then_the_lower_id():
    shared = [family("tf-a-low", 1, ["shared role"]), family("tf-b-high", 3, ["shared role"])]
    assert match_id(shared, "Shared Role") == "tf-b-high"
    equal = [family("tf-z", 2, ["shared role"]), family("tf-m", 2, ["shared role"])]
    assert match_id(equal, "Shared Role") == "tf-m"


@pytest.mark.parametrize("title", [
    "Senior Business Operations Manager", "Revenue Operations Manager", "Operations Manager",
    "Program Manager", "Pastry Chef", "Head of Operations Manager"])
def test_the_match_never_depends_on_family_order(title):
    forward = match_id(CASEY_FAMILIES, title)
    assert match_id(list(reversed(CASEY_FAMILIES)), title) == forward


def test_a_regex_only_match_loses_to_a_one_word_term():
    mixed = [family("tf-a-regex", 3, [], regex=["manager"]), family("tf-b-term", 1, ["manager"])]
    assert match_id(mixed, "Operations Manager") == "tf-b-term"
    assert match_id(list(reversed(mixed)), "Operations Manager") == "tf-b-term"


def test_a_regex_only_family_still_matches_when_no_term_does():
    only = [family("tf-a-regex", 3, [], regex=["^manager$"])]
    assert match_id(only, "Manager") == "tf-a-regex"


def test_hyphens_and_slashes_count_as_word_separators_in_a_term():
    both = [family("tf-short", 3, ["ops manager"]), family("tf-long", 1, ["ops/program-manager"])]
    assert match_id(both, "Ops Program Manager") == "tf-long"


def test_the_title_gate_passes_any_title_some_family_matches_and_rejects_the_rest():
    from jobsearch import filters
    compiled = families.compile_families(
        {"title": {"families": CASEY_FAMILIES, "exclude": []}})
    for title in ("Senior Business Operations Manager", "Revenue Operations Manager",
                  "Program Manager"):
        assert filters.title_gate(title, compiled) is None
    rejected = filters.title_gate("Pastry Chef", compiled)
    assert rejected is not None and rejected.filter_id == "title_family"


def test_criteria_build_carries_stated_family_words():
    answers, resume = load_persona("casey-ops")
    previous = criteria_build.build_criteria(answers, resume, None)
    stated = [f for f in previous["title"]["families"] if f["id"].startswith("tf-s-")]
    stated[0]["exclude"] = ["legal", "technical"]
    ghost = copy.deepcopy(stated[1])
    ghost["id"] = "tf-s-no-longer-listed"
    ghost["exclude"] = ["security"]
    previous["title"]["families"].append(ghost)
    rebuilt = criteria_build.build_criteria(answers, resume, previous)
    by_id = {f["id"]: f for f in rebuilt["title"]["families"]}
    assert by_id[stated[0]["id"]]["exclude"] == ["legal", "technical"]
    assert "exclude" not in by_id[stated[1]["id"]]
    assert "tf-s-no-longer-listed" not in by_id  # a family that is gone takes its words with it
    assert criteria.validate_criteria(rebuilt) == []
    assert criteria_build.build_criteria(answers, resume, rebuilt) == rebuilt  # idempotent


@pytest.mark.parametrize("interval", list(criteria_build.ANNUAL_FACTORS))
def test_a_pay_floor_at_the_intake_cap_builds_bands_for_every_interval(interval):
    floor = criteria_build.annual_floor({"amount": 1e12, "currency": "USD", "interval": interval})
    assert criteria_build._pay_bands(floor)
