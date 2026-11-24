"""Counted title discovery from a stratified sample of real postings.

Purpose: prove that every title count the user sees is computed by code from the sample, that the
user's approved families land in the config, and that discovery is repeatable.
Logic: tests drive cli.main the way a user does (init, criteria-build, sample, titles-check,
titles-apply) on a data folder under tmp_path. directory.DIRECTORY_PATH points at a four-board
fixture directory, fetch._default_transport serves scripted Greenhouse lists, and clock.now_utc
is frozen. Unit tests call titles.py functions on plain dicts.
Guardrails: every company, person and posting here is fictional. No test opens a socket or reads
the wall clock.
QA checklist: a new discovery rule gets a named test here; a changed file shape is documented in
docs/data-files.md in the same commit.
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import cli_result

from jobsearch import answers as answers_module
from jobsearch import clock, cli, criteria_build, directory, fetch, scoring, titles
from jobsearch.fetch import TransportResponse

PERSONA = Path(__file__).resolve().parent / "fixtures" / "personas" / "casey-ops"
TODAY = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)

# slug -> (company, industry, size, [(id, title, description)])
BOARDS = {
    "northwind": ("Northwind Labs", "software", "51-200", [
        (1, "Operations Manager", "Lead process improvement and vendor management."),
        (2, "Sr. Operations Manager", "Own budgeting and Excel models."),
        (3, "senior operations manager", "Run six sigma and process improvement work."),
        (4, "Business Operations Analyst", "Strong Excel skills."),
    ]),
    "harbor": ("Harbor Health", "healthcare", "1001-5000", [
        (1, "Revenue Operations Lead", "Budgeting, vendor management and Excel."),
        (2, "Revenue Operations Manager", "Continuous improvement and spreadsheets."),
        (3, "Data Wrangler", "SQL only."),
        (4, "Operations Manager", "Supplier management and budget management."),
    ]),
    "cinder": ("Cinder Pay", "fintech", "201-1000", [
        (1, "Program Manager", "Process improvement across teams."),
        (2, "Operations Program Manager", "Vendor management, budgeting, Excel."),
    ]),
    "quill": ("Quill Press", "media-entertainment", "1-50", [
        (1, "Office Coordinator", "Scheduling."),
    ]),
}
SKILL_PHRASES = {
    "process improvement": ("process improvement", "continuous improvement"),
    "vendor management": ("vendor management", "supplier management"),
    "budgeting": ("budgeting", "budget management"),
    "Excel": ("excel", "spreadsheets"),
    "Six Sigma": ("six sigma",),
}


def _entry(slug: str, company: str, industry: str, size: str) -> dict:
    return {"company": company, "ats": "greenhouse", "slug": slug, "industry": industry,
            "size": size, "stage": "growth", "last_verified": "2026-11-17",
            "identity": "company_name", "postings_at_verify": 1}


def _board_body(slug: str) -> bytes:
    company, _, _, jobs = BOARDS[slug]
    return json.dumps({"jobs": [
        {"id": job_id, "title": title, "company_name": company, "content": text,
         "absolute_url": f"https://job-boards.example.com/{slug}/jobs/{job_id}",
         "location": {"name": "Remote"}, "first_published": "2026-11-10T12:00:00Z"}
        for job_id, title, text in jobs]}).encode("utf-8")


class Transport:
    """Serves each board's scripted list by the slug in the URL and counts the calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url, headers, clock=None):
        self.calls.append(url)
        slug = re.search(r"/boards/([^/]+)/jobs", url).group(1)
        return TransportResponse(200, {"content-type": "application/json"}, _board_body(slug))


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    """A data folder with the casey-ops persona built, four fixture boards and a frozen clock."""
    board_file = tmp_path / "boards.json"
    board_file.write_text(json.dumps({"schema_version": 1, "generated_on": "2026-11-17",
                                      "entries": [_entry(s, c, i, z)
                                                  for s, (c, i, z, _) in BOARDS.items()]}),
                          encoding="utf-8")
    monkeypatch.setattr(directory, "DIRECTORY_PATH", board_file)
    monkeypatch.setattr(clock, "now_utc", lambda: TODAY)
    transport = Transport()
    monkeypatch.setattr(fetch, "_default_transport", transport)
    folder = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    (folder / "profile").mkdir(exist_ok=True)
    shutil.copyfile(PERSONA / "answers.json", folder / "profile" / "answers.json")
    shutil.copyfile(PERSONA / "resume.md", folder / "profile" / "resume.md")
    code, out = cli_result(capsys, "criteria-build", "--data-dir", str(folder))
    assert code == 0, out

    class Env:
        pass

    e = Env()
    e.folder, e.transport, e.capsys, e.tmp = folder, transport, capsys, tmp_path
    return e


def cmd(env, name: str, *extra: str) -> tuple[int, dict]:
    return cli_result(env.capsys, name, "--data-dir", str(env.folder), *extra)


def write_json(env, name: str, data: object) -> None:
    path = env.folder / "titles" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def evidence(env) -> dict:
    return json.loads((env.folder / "titles" / "evidence-2026-11-17.json").read_text("utf-8"))


def config(env) -> dict:
    return json.loads((env.folder / "config" / "criteria.json").read_text("utf-8"))


def independent_count(normalized_titles: set[str]) -> tuple[int, int]:
    """(postings with these titles that ask for the skills, all sampled postings), from BOARDS."""
    asking = total = 0
    for _, _, _, jobs in BOARDS.values():
        for _, title, text in jobs:
            total += 1
            hits = sum(any(p in text.lower() for p in phrases)
                       for phrases in SKILL_PHRASES.values())
            if titles._plain_title(title) in normalized_titles and hits >= 2:
                asking += 1
    return asking, total


def test_sample_writes_sample_and_evidence_files(env):
    code, out = cmd(env, "sample")
    assert code == 0 and out["ok"], out
    assert out["counts"] == {"boards_sampled": 4, "postings_sampled": 11, "titles": 8}
    assert sorted(out["wrote"]) == ["titles/evidence-2026-11-17.json",
                                    "titles/sample-2026-11-17.jsonl"]
    rows = [json.loads(line) for line in
            (env.folder / "titles" / "sample-2026-11-17.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 11 and rows == sorted(rows, key=lambda r: r["role_id"])
    assert evidence(env)["postings_sampled"] == 11


def test_the_evidence_file_holds_one_title_per_line_and_no_company_names(env):
    """One title per line lets the start skill read a long file in parts."""
    cmd(env, "sample")
    text = (env.folder / "titles" / "evidence-2026-11-17.json").read_text(encoding="utf-8")
    evidence = json.loads(text)
    assert evidence["titles"] and all("companies" not in e for e in evidence["titles"])
    lines = text.splitlines()
    assert len(lines) == len(evidence["titles"]) + 2
    for line, entry in zip(lines[1:-1], evidence["titles"]):
        assert json.loads(line.rstrip(",")) == entry


def test_titles_check_states_counts_computed_in_code(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [{
        "id": "tf-operations-manager", "label": "Operations managers",
        "titles": ["operations manager", "revenue operations"],
        "terms": ["operations manager", "revenue operations"]}]})
    code, out = cmd(env, "titles-check")
    assert code == 0 and out["ok"], out
    expected, total = independent_count({"operations manager", "revenue operations"})
    view = (env.folder / "titles" / "proposal-view.md").read_text("utf-8")
    assert f"{expected} of {total} sampled postings" in view
    assert expected > 0
    assert view.index("Sampled 11 postings") < view.index("### Operations managers")
    assert "You listed this: yes" in view


def test_titles_apply_writes_the_approved_family_into_the_config(env):
    cmd(env, "sample")
    before = config(env)
    write_json(env, "proposal.json", {"families": [
        {"id": "tf-revenue-operations", "label": "Revenue operations",
         "titles": ["revenue operations"], "terms": ["revenue operations"]}]})
    write_json(env, "decisions.json", {"decisions": [
        {"id": "tf-revenue-operations", "decision": "approve",
         "label": "Revenue operations", "terms": ["revenue operations"]}]})
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"], out
    after = config(env)
    assert after["criteria_version"] != before["criteria_version"]
    by_id = {f["id"]: f for f in after["title"]["families"]}
    family = by_id["tf-revenue-operations"]
    assert family["origin"] == "discovered" and family["approved_at"] == "2026-11-17T12:00:00Z"
    asking, total = independent_count({"revenue operations"})
    assert family["evidence"]["asking_for_your_skills"] == asking
    assert family["evidence"]["postings_sampled"] == total
    stated = [f["id"] for f in before["title"]["families"] if f["origin"] == "stated"]
    assert stated and all(i in by_id for i in stated)


# Task 2: edges ------------------------------------------------------------------------------

def fake_evidence(*normalized: str, asking: int = 3) -> dict:
    return {
        "sample_date": "2026-11-17", "boards_sampled": 2, "postings_sampled": 100,
        "industries_covered": [], "coverage_note": "Sampled 100 postings.",
        "skills_used": ["alpha", "beta"],
        "titles": [{"normalized": t, "n_postings": asking + 1, "n_with_skills": asking,
                    "examples": [t.title()],
                    "top_skills": ["alpha"]} for t in normalized],
    }


def fam(ident: str, titles_: list[str], terms: list[str] | None = None,
        label: str | None = None) -> dict:
    return {"id": ident, "label": label or ident, "titles": titles_,
            "terms": terms if terms is not None else list(titles_)}


def decision(ident: str, kind: str = "approve", **extra) -> dict:
    return {"id": ident, "decision": kind, **extra}


def check(*families: dict, evidence_titles: tuple[str, ...] = ("alpha role", "beta role", "gamma role")):
    return titles.check_proposal({"families": list(families)}, fake_evidence(*evidence_titles),
                                 ["Alpha Role"])


def apply(env, families: list[dict], decisions: list[dict],
          evidence_titles: tuple[str, ...] = ("alpha role", "beta role", "gamma role"),
          **kwargs):
    return titles.apply_decisions(
        config(env), {"families": families}, fake_evidence(*evidence_titles),
        {"decisions": decisions}, ["Alpha Role"], "2026-11-17T12:00:00Z", **kwargs)


def test_stratified_boards_take_one_from_each_stratum_before_a_second_from_the_large_one():
    def board(slug, industry, size):
        return directory.BoardEntry(
            company=slug, ats="greenhouse", slug=slug, industry=industry, size=size,
            stage="growth")

    entries = [board(f"big{i}", "software", "51-200") for i in range(5)]
    entries += [board("health", "healthcare", "51-200"), board("pay", "fintech", "51-200")]
    picked = [e.slug for e in titles.stratified_boards(entries, limit=4)]
    assert picked == ["pay", "health", "big0", "big1"]


def test_stratified_boards_never_exceed_the_limit_and_are_repeatable():
    entries = [directory.BoardEntry(f"c{i}", "greenhouse", f"s{i}", "software", "51-200",
                                    "growth")
               for i in range(60)]
    first = titles.stratified_boards(entries)
    assert len(first) == titles.SAMPLE_BOARDS == 40
    assert first == titles.stratified_boards(list(entries))


def test_titles_that_differ_only_by_level_word_punctuation_or_case_count_as_one(env):
    cmd(env, "sample")
    by_title = {t["normalized"]: t for t in evidence(env)["titles"]}
    assert by_title["operations manager"]["n_postings"] == 4  # plain, "Sr.", "senior", plain
    assert by_title["operations manager"]["examples"][0] == "Operations Manager"
    assert "sr. operations manager" not in by_title


def test_titles_with_a_different_role_noun_stay_separate(env):
    cmd(env, "sample")
    names = {t["normalized"] for t in evidence(env)["titles"]}
    assert {"operations manager", "business operations analyst", "revenue operations",
            "revenue operations manager"} <= names


def test_sample_with_no_skills_in_the_answers_returns_a_reason_and_no_counts(env):
    answers = json.loads((env.folder / "profile" / "answers.json").read_text("utf-8"))
    answers["skills"] = []
    (env.folder / "profile" / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    code, out = cmd(env, "sample")
    assert code == 1 and not out["ok"] and out["counts"] == {} and out["wrote"] == []
    assert "no skills" in out["error"]
    assert env.transport.calls == []
    assert not (env.folder / "titles").exists() or not list((env.folder / "titles").iterdir())


def test_sample_with_no_readable_postings_returns_a_reason_and_no_counts(env, monkeypatch):
    def empty(url, headers, clock=None):
        return TransportResponse(200, {"content-type": "application/json"},
                                 json.dumps({"jobs": []}).encode("utf-8"))

    monkeypatch.setattr(fetch, "_default_transport", empty)
    code, out = cmd(env, "sample")
    assert code == 1 and not out["ok"] and out["counts"] == {} and out["wrote"] == []
    assert "No postings" in out["error"]


def test_evidence_titles_sort_by_skill_matches_then_normalized_title(env):
    cmd(env, "sample")
    rows = [(t["n_with_skills"], t["normalized"]) for t in evidence(env)["titles"]]
    assert rows == sorted(rows, key=lambda r: (-r[0], r[1]))
    assert len(rows) > 2 and rows[0][0] > rows[-1][0]


def test_count_titles_caps_at_150_but_keeps_the_titles_the_user_listed():
    rows = [{"role_id": f"greenhouse:b:{i}", "company": "Fiction Co", "industry": None,
             "size": None, "title": f"Role {i:03d}", "normalized_title": f"role {i:03d}",
             "skills_hit": ["a", "b"]} for i in range(160)]
    rows.append({**rows[0], "role_id": "greenhouse:b:900", "title": "Listed Role",
                 "normalized_title": "listed role", "skills_hit": []})
    skills = ["a", "b"]
    assert len(titles.count_titles(rows, skills)) == 150
    kept = titles.count_titles(rows, skills, keep={"listed role"})
    assert len(kept) == 151 and any(t["normalized"] == "listed role" for t in kept)


def test_sample_twice_inside_the_cache_window_makes_no_calls_and_identical_files(env):
    cmd(env, "sample")
    first = {p.name: p.read_bytes() for p in (env.folder / "titles").iterdir()}
    calls = len(env.transport.calls)
    assert calls == 4
    cmd(env, "sample")
    assert len(env.transport.calls) == calls
    assert {p.name: p.read_bytes() for p in (env.folder / "titles").iterdir()} == first


def test_titles_check_stdout_stays_under_30_lines_and_names_the_listed_titles_not_covered(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    code, out = cmd(env, "titles-check")
    assert code == 0 and len(json.dumps(out, indent=2).splitlines()) < 30
    view = (env.folder / "titles" / "proposal-view.md").read_text("utf-8")
    assert "## Titles you listed" in view
    assert "program manager: 0 of 11 sampled postings" in view
    assert "You listed this: no" in view


def test_titles_check_refuses_more_than_eight_families():
    names = tuple(f"role {i}" for i in range(9))
    result = check(*(fam(f"tf-r{i}", [names[i]]) for i in range(9)), evidence_titles=names)
    assert any("at most 8" in e for e in result.errors)


def test_titles_check_refuses_a_duplicate_family_id():
    result = check(fam("tf-same", ["alpha role"]), fam("tf-same", ["beta role"]))
    assert any("tf-same" in e and "more than once" in e for e in result.errors)


@pytest.mark.parametrize("ident", ["alpha", "TF-alpha", "tf-", "tf-a b", "tf-s-alpha-role"])
def test_titles_check_refuses_an_id_outside_the_tf_pattern(ident):
    result = check(fam(ident, ["alpha role"]))
    assert any("families[0].id" in e for e in result.errors), result.errors


def test_titles_check_names_a_title_that_is_not_in_the_evidence():
    result = check(fam("tf-a", ["alpha role"]), fam("tf-b", ["data wrangler"]))
    assert 'families[1].titles: "data wrangler" is not in the sample evidence.' in result.errors


def test_titles_check_refuses_a_title_placed_in_two_families():
    result = check(fam("tf-a", ["alpha role", "beta role"]), fam("tf-b", ["beta role"]))
    assert any('"beta role"' in e and "tf-a" in e and "tf-b" in e for e in result.errors)


@pytest.mark.parametrize("key", ["count", "share", "n_postings", "examples", "n_with_skills"])
def test_titles_check_refuses_any_number_or_example_in_the_proposal(key):
    family = {**fam("tf-a", ["alpha role"]), key: 31}
    result = check(family)
    assert result.errors and any(key in e and "computed by code" in e for e in result.errors)
    top = titles.check_proposal({"families": [fam("tf-a", ["alpha role"])], key: 3},
                                fake_evidence("alpha role"), [])
    assert top.errors and top.text == ""


def test_a_clean_proposal_has_no_errors_and_counts_come_from_the_evidence():
    result = check(fam("tf-a", ["alpha role", "beta role"]))
    assert result.errors == [] and result.families[0].count == 6
    assert "6 of 100 sampled postings" in result.text


def test_apply_refuses_a_missing_decision_and_lists_the_missing_ids(env):
    outcome = apply(env, [fam("tf-a", ["alpha role"]), fam("tf-b", ["beta role"]),
                          fam("tf-c", ["gamma role"])], [decision("tf-a")])
    assert outcome.config is None
    assert any("tf-b" in e and "tf-c" in e and "no decision" in e for e in outcome.errors)


def test_apply_refuses_a_duplicate_or_unknown_decision(env):
    families = [fam("tf-a", ["alpha role"])]
    twice = apply(env, families, [decision("tf-a"), decision("tf-a", "reject")])
    assert any("more than one decision" in e for e in twice.errors)
    unknown = apply(env, families, [decision("tf-a"), decision("tf-zzz")])
    assert any("tf-zzz" in e for e in unknown.errors)


def test_an_edit_replaces_label_and_terms_gets_a_code_set_rung_and_validates(env):
    from jobsearch import criteria
    outcome = apply(env, [fam("tf-a", ["alpha role"])],
                    [decision("tf-a", "edit", label="Alpha work", terms=["alpha lead", "alpha"])])
    assert outcome.errors == [] and outcome.edited == 1
    written = next(f for f in outcome.config["title"]["families"] if f["id"] == "tf-a")
    assert written["label"] == "Alpha work" and written["rung"] == 3
    assert written["terms"] == ["alpha", "alpha lead"]
    assert criteria.validate_criteria(outcome.config) == []
    # tf-a is a title the user listed, and the question was skipped, so the rule says rung 3.
    # A rung written in the decision is ignored.
    low = apply(env, [fam("tf-a", ["alpha role"])],
                [decision("tf-a", "edit", label="Alpha work", terms=["alpha"], rung=1)])
    assert next(f for f in low.config["title"]["families"] if f["id"] == "tf-a")["rung"] == 3


@pytest.mark.parametrize("extra", [
    {"label": "Alpha", "terms": []},
    {"label": "Alpha", "terms": ["x" * 81]},
    {"label": "", "terms": ["alpha"]},
    {"terms": ["alpha"]},
])
def test_an_edit_with_a_bad_label_or_terms_is_refused(env, extra):
    outcome = apply(env, [fam("tf-a", ["alpha role"])], [decision("tf-a", "edit", **extra)])
    assert outcome.config is None and any("decisions[0]" in e for e in outcome.errors)


def test_an_approve_that_changes_the_proposal_is_refused_in_favor_of_edit(env):
    outcome = apply(env, [fam("tf-a", ["alpha role"], label="Alpha")],
                    [decision("tf-a", label="Something else", terms=["alpha role"])])
    assert outcome.config is None and any("use edit" in e for e in outcome.errors)


def test_two_approved_families_that_share_a_term_are_refused_naming_both(env):
    outcome = apply(env, [fam("tf-a", ["alpha role"], terms=["alpha role", "ops lead"]),
                          fam("tf-b", ["beta role"], terms=["Ops-Lead"])],
                    [decision("tf-a"), decision("tf-b")])
    assert outcome.config is None
    assert any("ops lead" in e and "tf-a" in e and "tf-b" in e for e in outcome.errors)


def test_a_rejected_family_that_shares_a_term_is_not_a_conflict(env):
    outcome = apply(env, [fam("tf-a", ["alpha role"], terms=["ops lead"]),
                          fam("tf-b", ["beta role"], terms=["ops lead"])],
                    [decision("tf-a"), decision("tf-b", "reject")])
    assert outcome.errors == [] and (outcome.approved, outcome.rejected) == (1, 1)


def test_rejecting_every_family_with_no_stated_family_says_one_is_needed(env, capsys):
    answers = json.loads((env.folder / "profile" / "answers.json").read_text("utf-8"))
    answers["target_titles"] = []
    (env.folder / "profile" / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    code, out = cmd(env, "criteria-build")
    assert code == 0 and config(env)["title"]["families"] == []
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations", "reject")]})
    version = config(env)["criteria_version"]
    code, out = cmd(env, "titles-apply")
    assert code == 1 and not out["ok"] and "at least one" in out["error"]
    assert config(env)["criteria_version"] == version


def test_rerunning_the_same_decisions_later_is_unchanged_and_keeps_the_version(env, monkeypatch):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations")]})
    code, first = cmd(env, "titles-apply")
    assert code == 0 and first["data"]["unchanged"] is False
    written = config(env)
    history = sorted(p.name for p in (env.folder / "config").rglob("*") if p.is_file())
    monkeypatch.setattr(clock, "now_utc", lambda: TODAY + timedelta(minutes=5))
    code, second = cmd(env, "titles-apply")
    assert code == 0 and second["data"]["unchanged"] is True and second["wrote"] == []
    assert config(env) == written
    assert sorted(p.name for p in (env.folder / "config").rglob("*") if p.is_file()) == history


def test_rejecting_everything_with_stated_families_keeps_the_config_unchanged(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations", "reject")]})
    version = config(env)["criteria_version"]
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"] and out["data"]["unchanged"] is True
    assert config(env)["criteria_version"] == version and out["wrote"] == []


def test_families_are_written_in_rung_descending_then_id_order_whatever_the_decision_order(env):
    families = [fam("tf-b-beta", ["beta role"]), fam("tf-a-alpha", ["alpha role"]),
                fam("tf-c-gamma", ["gamma role"])]
    decisions = [decision("tf-b-beta", "edit", label="Beta", terms=["beta"]),
                 decision("tf-a-alpha"), decision("tf-c-gamma")]
    forward = apply(env, families, decisions)
    backward = apply(env, list(reversed(families)), list(reversed(decisions)))
    assert forward.errors == [] and forward.config == backward.config
    written = forward.config["title"]["families"]
    assert [f["id"] for f in written] == [
        i for _, i in sorted((-f["rung"], f["id"]) for f in written)]
    # tf-a-alpha is a title the user listed (rung 3); the two discovered families each have 3
    # postings, which is thin evidence (rung 1), so they follow in id order.
    ids = [f["id"] for f in written if f["id"].startswith("tf-") and not f["id"].startswith("tf-s-")]
    assert ids == ["tf-a-alpha", "tf-b-beta", "tf-c-gamma"]
    assert {f["id"]: f["rung"] for f in written}["tf-b-beta"] == 1


def test_a_listed_family_is_origin_stated_and_an_unlisted_one_is_discovered(env):
    outcome = apply(env, [fam("tf-alpha", ["alpha role"]), fam("tf-beta", ["beta role"])],
                    [decision("tf-alpha"), decision("tf-beta")])
    origins = {f["id"]: f["origin"] for f in outcome.config["title"]["families"]}
    assert origins["tf-alpha"] == "stated" and origins["tf-beta"] == "discovered"
    block = next(f for f in outcome.config["title"]["families"] if f["id"] == "tf-beta")["evidence"]
    assert block == {"sample_date": "2026-11-17", "postings_sampled": 100,
                     "asking_for_your_skills": 3, "share": 0.03, "top_shared_skills": ["alpha"]}


def test_a_retune_after_discovery_keeps_the_discovered_family(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations")]})
    assert cmd(env, "titles-apply")[0] == 0
    answers = json.loads((env.folder / "profile" / "answers.json").read_text("utf-8"))
    answers["years_experience"] = 10
    (env.folder / "profile" / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    assert cmd(env, "criteria-build")[0] == 0
    assert "tf-revenue-operations" in {f["id"] for f in config(env)["title"]["families"]}


# Rungs are set by code and titles-apply rebuilds the config ------------------------------

NOTICE = ("ignored. The tool sets each rung from your first-choice title and the evidence "
          "counts.")


def put_evidence(env, counts: dict[str, int]) -> None:
    """Write a titles/evidence file where each title has the given count asking for the skills."""
    data = fake_evidence(*counts)
    for entry in data["titles"]:
        entry["n_with_skills"] = counts[entry["normalized"]]
        entry["n_postings"] = counts[entry["normalized"]] + 1
    path = env.folder / "titles" / "evidence-2026-11-17.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def set_answers(env, **changes) -> None:
    path = env.folder / "profile" / "answers.json"
    data = json.loads(path.read_text("utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def rungs_of(env) -> dict[str, int]:
    return {f["id"]: f["rung"] for f in config(env)["title"]["families"]}


def weights_in(env) -> dict[str, int]:
    return {c["id"]: c["weight"] for c in config(env)["criteria"]}


STATED_ALL_3 = {"tf-s-business-operations-manager": 3, "tf-s-operations-manager": 3,
                "tf-s-program-manager": 3}


def test_titles_apply_sets_rungs_from_the_evidence_and_the_weights_follow(env):
    put_evidence(env, {"supply chain": 2, "revenue operations": 12})
    write_json(env, "proposal.json", {"families": [
        fam("tf-a", ["supply chain"]), fam("tf-b", ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-a"), decision("tf-b")]})
    before = config(env)
    assert "title_fit" not in weights_in(env)
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"], out
    assert rungs_of(env) == {**STATED_ALL_3, "tf-a": 1, "tf-b": 2}
    assert weights_in(env) == {"title_fit": 23, "skills": 23, "level": 3, "pay": 23,
                               "location": 23, "freshness": 5}
    after = config(env)
    assert after["criteria_version"] != before["criteria_version"]
    history = env.folder / "config" / "criteria-history"
    assert (history / f"criteria-v{before['criteria_version']}.json").is_file()


def test_rejecting_the_discovered_families_later_drops_title_fit_again(env):
    put_evidence(env, {"supply chain": 2, "revenue operations": 12})
    write_json(env, "proposal.json", {"families": [
        fam("tf-a", ["supply chain"]), fam("tf-b", ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-a"), decision("tf-b")]})
    assert cmd(env, "titles-apply")[0] == 0
    version = config(env)["criteria_version"]
    write_json(env, "decisions.json", {"decisions": [decision("tf-a", "reject"),
                                                     decision("tf-b", "reject")]})
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"] and out["data"]["unchanged"] is False, out
    assert rungs_of(env) == STATED_ALL_3
    assert "title_fit" not in weights_in(env)
    assert config(env)["criteria_version"] != version


def test_titles_apply_uses_the_first_choice_for_the_rung_of_a_listed_family(env):
    set_answers(env, first_choice_title="Program Manager")
    put_evidence(env, {"program manager": 12, "revenue operations": 12})
    write_json(env, "proposal.json", {"families": [
        fam("tf-program", ["program manager"]), fam("tf-b", ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-program"), decision("tf-b")]})
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"], out
    assert config(env)["title"]["families"][0]["rung"] == 3
    assert rungs_of(env) == {
        "tf-program": 3, "tf-s-program-manager": 3, "tf-b": 2,
        "tf-s-business-operations-manager": 2, "tf-s-operations-manager": 2}
    program = next(f for f in config(env)["title"]["families"] if f["id"] == "tf-program")
    assert program["origin"] == "stated"


def test_a_decisions_file_that_carries_a_rung_still_applies_with_a_notice_each(env):
    put_evidence(env, {"supply chain": 12, "revenue operations": 12})
    write_json(env, "proposal.json", {"families": [
        fam("tf-b", ["supply chain"]), fam("tf-c", ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [
        decision("tf-b", rung=1), decision("tf-c", rung="high")]})
    version = config(env)["criteria_version"]
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"], out
    assert rungs_of(env)["tf-b"] == rungs_of(env)["tf-c"] == 2
    assert config(env)["criteria_version"] != version
    notices = [w for w in out["warnings"] if w.startswith("decisions[")]
    assert notices == [f"decisions[0].rung: {NOTICE}", f"decisions[1].rung: {NOTICE}"]


def test_the_ignored_rung_notice_is_on_the_outcome_and_a_decision_without_one_has_none(env):
    outcome = apply(env, [fam("tf-a", ["alpha role"]), fam("tf-b", ["beta role"])],
                    [decision("tf-a", rung=1), decision("tf-b")])
    assert outcome.errors == []
    assert outcome.notices == [f"decisions[0].rung: {NOTICE}"]
    clean = apply(env, [fam("tf-a", ["alpha role"])], [decision("tf-a")])
    assert clean.notices == []


def test_a_decision_rung_never_changes_the_rung_the_rule_gives(env):
    low = apply(env, [fam("tf-b", ["beta role"])], [decision("tf-b", rung=3)])
    high = apply(env, [fam("tf-b", ["beta role"])], [decision("tf-b", rung=1)])
    none = apply(env, [fam("tf-b", ["beta role"])], [decision("tf-b")])
    ranks = [next(f for f in o.config["title"]["families"] if f["id"] == "tf-b")["rung"]
             for o in (low, high, none)]
    assert ranks == [1, 1, 1]  # 3 postings is thin evidence for a discovered family


def test_a_refused_decisions_file_still_reports_the_ignored_rung_notice(env):
    outcome = apply(env, [fam("tf-a", ["alpha role"])],
                    [decision("tf-a", rung=1), decision("tf-zzz")])
    assert outcome.config is None and outcome.errors
    assert outcome.notices == [f"decisions[0].rung: {NOTICE}"]


def test_apply_decisions_takes_the_first_choice_for_listed_families(env):
    outcome = apply(env, [fam("tf-alpha", ["alpha role"])], [decision("tf-alpha")],
                    first_choice_title="Alpha Role")
    rung = next(f for f in outcome.config["title"]["families"] if f["id"] == "tf-alpha")["rung"]
    assert rung == 3
    other = apply(env, [fam("tf-alpha", ["alpha role"])], [decision("tf-alpha")],
                  first_choice_title="Program Manager")
    rung = next(f for f in other.config["title"]["families"] if f["id"] == "tf-alpha")["rung"]
    assert rung == 2


def test_titles_apply_twice_is_unchanged_and_the_notice_shows_again(env, monkeypatch):
    put_evidence(env, {"supply chain": 12})
    write_json(env, "proposal.json", {"families": [fam("tf-b", ["supply chain"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-b", rung=1)]})
    code, first = cmd(env, "titles-apply")
    assert code == 0 and first["data"]["unchanged"] is False
    written = config(env)
    monkeypatch.setattr(clock, "now_utc", lambda: TODAY + timedelta(minutes=5))
    code, second = cmd(env, "titles-apply")
    assert code == 0 and second["data"]["unchanged"] is True and second["wrote"] == []
    assert config(env) == written
    assert f"decisions[0].rung: {NOTICE}" in second["warnings"]


def test_criteria_build_right_after_titles_apply_makes_no_new_version(env):
    put_evidence(env, {"supply chain": 2, "revenue operations": 12})
    write_json(env, "proposal.json", {"families": [
        fam("tf-a", ["supply chain"]), fam("tf-b", ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-a"), decision("tf-b")]})
    assert cmd(env, "titles-apply")[0] == 0
    written = (env.folder / "config" / "criteria.json").read_bytes()
    code, out = cmd(env, "criteria-build")
    assert code == 0 and out["data"]["unchanged"] is True
    assert (env.folder / "config" / "criteria.json").read_bytes() == written


def test_titles_apply_builds_exactly_what_criteria_build_would_for_the_same_families(env):
    put_evidence(env, {"supply chain": 2})
    write_json(env, "proposal.json", {"families": [fam("tf-a", ["supply chain"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-a")]})
    assert cmd(env, "titles-apply")[0] == 0
    written = config(env)
    answers = answers_module.load_answers(env.folder)
    resume = (env.folder / "profile" / "resume.md").read_text("utf-8")
    rebuilt = criteria_build.build_criteria(answers, resume, written)
    skip = ("criteria_version", "created_from")
    assert {k: v for k, v in rebuilt.items() if k not in skip} == {
        k: v for k, v in written.items() if k not in skip}


def test_titles_apply_with_answers_that_no_longer_validate_writes_nothing(env):
    put_evidence(env, {"supply chain": 12})
    write_json(env, "proposal.json", {"families": [fam("tf-a", ["supply chain"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-a")]})
    set_answers(env, target_level="boss")
    before = (env.folder / "config" / "criteria.json").read_bytes()
    code, out = cmd(env, "titles-apply")
    assert code == 1 and not out["ok"]
    assert any("target_level" in e for e in out["data"]["errors"])
    assert "criteria-build" in out["next"] and out["counts"]["errors"] >= 1
    assert (env.folder / "config" / "criteria.json").read_bytes() == before
    assert not (env.folder / "config" / "criteria-history").exists()


# Discovery counts skills with scoring's matcher -----------------------------


def casey_matchers() -> list[titles.SkillMatcher]:
    answers = json.loads((PERSONA / "answers.json").read_text("utf-8"))
    return titles.skill_matchers(answers)


SKILL_TEXTS = [
    ("manage budgets across teams", ["budgeting"]),
    ("Excel/Google Sheets", ["Excel"]),
    ("we excel at logistics", []),
    ("a culture of continuous improvement", []),
    ("drive continuous improvement across the supply chain", ["process improvement"]),
    ("Six Sigma certified", ["Six Sigma"]),
    ("vendor\u00a0management and supplier management", ["vendor management"]),
    ("SIX SIGMA and EXCEL", ["Excel", "Six Sigma"]),
    ("Excellent communicator", []),
]


@pytest.mark.parametrize(("text", "expected"), SKILL_TEXTS)
def test_skills_hit_names_the_skills_scoring_would_find(text, expected):
    assert titles.skills_hit(text, casey_matchers()) == sorted(expected)


@pytest.mark.parametrize(("text", "_expected"), SKILL_TEXTS)
def test_skills_hit_is_exactly_what_scoring_skill_mentioned_says(text, _expected):
    normalized = unicodedata.normalize("NFKC", text)
    matchers = casey_matchers()
    by_scoring = sorted(m.term for m in matchers if scoring.skill_mentioned(normalized, m.names))
    assert titles.skills_hit(text, matchers) == by_scoring


def test_a_skill_matcher_holds_the_term_and_its_non_empty_aliases():
    matchers = titles.skill_matchers({"skills": [
        {"term": "Excel", "aliases": ["spreadsheets", "  "], "origin": "stated"},
        {"term": "Six Sigma", "aliases": [], "origin": "stated"},
        {"term": "  ", "aliases": [], "origin": "stated"}]})
    assert [(m.term, m.names) for m in matchers] == [
        ("Excel", ("Excel", "spreadsheets")), ("Six Sigma", ("Six Sigma",))]


def test_titles_compiles_no_pattern_of_its_own_for_skills():
    assert not hasattr(titles, "_phrase_pattern")


# The words the user approved for a family (titles-exclude) -------------------

OPS_FAMILY = "tf-s-operations-manager"


def write_exclusions(env, entries: list[dict]) -> None:
    write_json(env, "exclusions.json", {"families": entries})


def words_of(env, ident: str = OPS_FAMILY) -> list[str] | None:
    family = next(f for f in config(env)["title"]["families"] if f["id"] == ident)
    return family.get("exclude")


def test_titles_exclude_writes_sorted_words_and_a_new_version(env):
    before = config(env)
    write_exclusions(env, [{"id": OPS_FAMILY, "exclude": ["Technical", "legal"]}])
    code, out = cmd(env, "titles-exclude")
    assert code == 0 and out["ok"], out
    assert words_of(env) == ["legal", "Technical"]
    after = config(env)
    assert after["criteria_version"] != before["criteria_version"]
    assert out["data"]["unchanged"] is False
    assert out["counts"] == {"families_with_words": 1, "words": 2}
    history = env.folder / "config" / "criteria-history"
    assert (history / f"criteria-v{before['criteria_version']}.json").is_file()


def test_titles_exclude_unchanged_file_makes_no_new_version(env):
    write_exclusions(env, [{"id": OPS_FAMILY, "exclude": ["legal"]}])
    assert cmd(env, "titles-exclude")[0] == 0
    version = config(env)["criteria_version"]
    code, out = cmd(env, "titles-exclude")
    assert code == 0 and out["ok"] and out["data"]["unchanged"] is True, out
    assert config(env)["criteria_version"] == version and out["wrote"] == []


def test_titles_exclude_keeps_unnamed_families_and_clears_an_empty_list(env):
    write_exclusions(env, [{"id": OPS_FAMILY, "exclude": ["legal"]},
                           {"id": "tf-s-program-manager", "exclude": ["technical"]}])
    assert cmd(env, "titles-exclude")[0] == 0
    write_exclusions(env, [{"id": "tf-s-program-manager", "exclude": []}])
    code, out = cmd(env, "titles-exclude")
    assert code == 0 and out["ok"], out
    assert words_of(env) == ["legal"]
    assert words_of(env, "tf-s-program-manager") is None
    assert "exclude" not in next(
        f for f in config(env)["title"]["families"] if f["id"] == "tf-s-program-manager")


LONG_WORD = "x" * 81


@pytest.mark.parametrize("entries, path_text", [
    ([{"id": "tf-nope", "exclude": ["legal"]}], "families[0].id"),
    ([{"id": OPS_FAMILY, "exclude": ["legal"], "count": 3}], "families[0].count"),
    ([{"id": OPS_FAMILY, "exclude": ["legal"], "tier": "A"}], "families[0].tier"),
    ([{"id": OPS_FAMILY, "exclude": ["legal", 7]}], "families[0].exclude[1]"),
    ([{"id": OPS_FAMILY, "exclude": ["legal", "  "]}], "families[0].exclude[1]"),
    ([{"id": OPS_FAMILY, "exclude": [LONG_WORD]}], "families[0].exclude[0]"),
    ([{"id": OPS_FAMILY, "exclude": [f"w{i}" for i in range(21)]}], "families[0].exclude"),
    ([{"id": OPS_FAMILY, "exclude": ["Legal", "legal"]}], "families[0].exclude[1]"),
    ([{"id": "tf-s-program-manager", "exclude": ["technical"]},
      {"id": OPS_FAMILY, "exclude": ["legal", "operations"]}], "families[1].exclude[1]"),
    ([{"id": OPS_FAMILY}], "families[0]"),
    ([{"id": OPS_FAMILY, "exclude": "legal"}], "families[0].exclude"),
])
def test_titles_exclude_refuses_bad_files(env, entries, path_text):
    before = (env.folder / "config" / "criteria.json").read_bytes()
    write_exclusions(env, entries)
    code, out = cmd(env, "titles-exclude")
    assert code == 1 and out["ok"] is False, out
    assert path_text in out["error"]
    assert "titles/exclusions.json" in out["next"]
    assert (env.folder / "config" / "criteria.json").read_bytes() == before


def test_titles_exclude_names_the_valid_families_for_an_unknown_id(env):
    write_exclusions(env, [{"id": "tf-nope", "exclude": ["legal"]}])
    _, out = cmd(env, "titles-exclude")
    assert f"{OPS_FAMILY} (" in out["error"] and "tf-s-program-manager (" in out["error"]


def test_titles_exclude_refuses_a_word_that_would_rule_out_the_whole_family(env):
    write_exclusions(env, [{"id": OPS_FAMILY, "exclude": ["operations"]}])
    _, out = cmd(env, "titles-exclude")
    assert 'would rule out every title in "Operations Manager"' in out["error"]


@pytest.mark.parametrize("word", ["-", "/", " - "])
def test_titles_exclude_refuses_a_word_with_nothing_to_match(env, word):
    # Such a word would be stored and counted but could never rule out a title.
    write_exclusions(env, [{"id": OPS_FAMILY, "exclude": ["legal", word]}])
    code, out = cmd(env, "titles-exclude")
    assert code == 1 and out["ok"] is False
    assert "has no letters or digits to match" in out["error"]
    assert words_of(env) is None


def test_titles_exclude_without_the_file_says_what_to_write(env):
    code, out = cmd(env, "titles-exclude")
    assert code == 1 and out["ok"] is False
    assert "titles/exclusions.json" in out["error"] and "approved" in out["error"]


def test_titles_apply_carries_words_of_a_kept_family(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations")]})
    assert cmd(env, "titles-apply")[0] == 0
    write_exclusions(env, [{"id": "tf-revenue-operations", "exclude": ["legal"]},
                           {"id": OPS_FAMILY, "exclude": ["technical"]}])
    assert cmd(env, "titles-exclude")[0] == 0
    version = config(env)["criteria_version"]
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"] and out["data"]["unchanged"] is True, out
    assert words_of(env, "tf-revenue-operations") == ["legal"]
    assert words_of(env) == ["technical"]
    assert config(env)["criteria_version"] == version


def test_titles_apply_keeps_words_when_the_user_edits_the_family_terms(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations")]})
    assert cmd(env, "titles-apply")[0] == 0
    write_exclusions(env, [{"id": "tf-revenue-operations", "exclude": ["legal"]}])
    assert cmd(env, "titles-exclude")[0] == 0
    # An edit whose new term holds an approved word cannot keep that word: it would rule out
    # the whole family. The word is dropped with a notice and the rest stay.
    write_json(env, "decisions.json", {"decisions": [decision(
        "tf-revenue-operations", "edit", label="Legal revenue operations",
        terms=["revenue operations", "legal revenue operations"])]})
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"], out
    assert words_of(env, "tf-revenue-operations") is None
    assert any('"legal"' in w and "tf-revenue-operations" in w for w in out["warnings"])


# The shared failure sentence and the refresh courtesy line ----------------------------

def test_titles_check_refuses_a_bad_proposal_in_one_sentence(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-ghost", ["no such title"])]})
    code, out = cmd(env, "titles-check")
    assert code == 1 and out["ok"] is False and out["data"]["errors"]
    assert out["error"] == "The family proposal has problems: " + " ".join(out["data"]["errors"])
    assert out["counts"]["errors"] >= len(out["data"]["errors"])
    assert out["next"] == "Fix titles/proposal.json, then run titles-check again."


def test_titles_apply_refuses_bad_decisions_in_one_sentence(env):
    cmd(env, "sample")
    write_json(env, "proposal.json", {"families": [fam("tf-revenue-operations",
                                                       ["revenue operations"])]})
    write_json(env, "decisions.json", {"decisions": [decision("tf-revenue-operations"),
                                                     decision("tf-zzz")]})
    code, out = cmd(env, "titles-apply")
    assert code == 1 and out["ok"] is False and out["data"]["errors"]
    assert out["error"] == "The decisions have problems: " + " ".join(out["data"]["errors"])
    assert out["next"] == "Fix titles/decisions.json, then run titles-apply again."


def test_sample_refresh_adds_the_courtesy_line(env):
    code, out = cmd(env, "sample", "--refresh")
    assert code == 0 and out["ok"], out
    assert ("Refresh skips the cache. Please do not repeat it often; boards are shared."
            in out["warnings"])
