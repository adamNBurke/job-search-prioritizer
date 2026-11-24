"""The saved summaries: what the tool understood, which titles it chose, what the scan found.

Purpose: what the chat explains must also be a file the user can read later. These
tests pin that code renders each file from saved state, that every answered topic is named, that
hostile text stays literal, and that an unchanged rerun rewrites nothing.
Logic: the intake summary is driven through cli.main (criteria-build) on the casey-ops persona
and through summaries.render_intake directly for the edge cases. No socket is opened.
Guardrails: fixtures are fictional. Each test owns its folder under tmp_path.
QA checklist: a changed line in summaries.py needs the matching assertion here and the
"Summary files" section of docs/data-files.md in the same commit.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from jobsearch import cli, summaries

PERSONA = Path(__file__).resolve().parent / "fixtures" / "personas" / "casey-ops"
CRITERIA = {"criteria_version": "1.0"}


def casey() -> dict:
    return json.loads((PERSONA / "answers.json").read_text(encoding="utf-8"))


def build_workspace(tmp_path, capsys, answers: dict | None = None) -> Path:
    folder = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    capsys.readouterr()
    (folder / "profile").mkdir(exist_ok=True)
    (folder / "profile" / "answers.json").write_text(
        json.dumps(answers if answers is not None else casey()), encoding="utf-8")
    (folder / "profile" / "resume.md").write_text(
        (PERSONA / "resume.md").read_text(encoding="utf-8")
        if (PERSONA / "resume.md").is_file() else "# Casey\n", encoding="utf-8")
    return folder


def build(capsys, folder) -> dict:
    code = cli.main(["criteria-build", "--data-dir", str(folder)])
    out = json.loads(capsys.readouterr().out)
    assert code == (0 if out["ok"] else 1), out
    return out


# ------------------------------------------------------------------------------------ intake

def test_the_intake_summary_names_every_answered_topic():
    answers = casey()
    answers.update(first_choice_title="Business Operations Manager", strict=["pay_floor"],
                   locations=["Denver"], needs_sponsorship=True,
                   industries_wanted=["healthcare"], industries_ruled_out=["gambling"],
                   company_sizes=["51-200"], company_stages=["growth"],
                   deal_breaker_terms=["commission only"], companies_ruled_out=["Initech"])
    text = summaries.render_intake(answers, {"criteria_version": "1.3"})
    assert text.startswith("# What the tool understood\n")
    for line in (
        "- Job titles: Operations Manager, Business Operations Manager (first choice), "
        "Program Manager",
        "- Level: manager",
        "- Years of experience: 9",
        "vendor management (matters most, from your resume)",
        "Six Sigma (you added)",
        "- Pay floor: 95000 USD per year",
        "- Work arrangements: remote, hybrid",
        "- Places: Denver",
        "- Regions: US",
        "- Visa sponsorship: needed",
        "- Industries wanted: healthcare",
        "- Industries ruled out: gambling",
        "- Company sizes: 51-200",
        "- Company stages: growth",
        "- Deal-breaker words: commission only",
        "- Companies you will not apply to: Initech",
        "a posting that does not say is left out: pay floor",
        "- Matters most: job title, pay, where and how you work",
        "- Does not matter to you: seniority level",
        "Scoring config version 1.3.",
    ):
        assert line in text, line
    assert text.rstrip("\n").endswith(summaries.CORRECTION)
    assert "tell the start skill what to change" in text and "the previous version is kept" in text


def test_skipped_topics_are_said_once():
    answers = casey()
    answers.update(skipped=["pay_floor", "industries_wanted"], pay_floor=None)
    text = summaries.render_intake(answers, CRITERIA)
    assert text.count("- Pay floor: skipped") == 1
    assert text.count("- Industries wanted: skipped") == 1
    assert text.count("skipped") == 2  # no second list of skipped topics
    assert "pay_floor" not in text
    assert "- Industries ruled out: none given" in text


def test_a_skipped_first_choice_is_said_in_the_titles_line():
    answers = casey()
    answers["skipped"] = ["first_choice_title"]
    text = summaries.render_intake(answers, CRITERIA)
    assert "Program Manager; first choice: skipped" in text


def test_hostile_text_is_escaped():
    answers = casey()
    answers["target_titles"] = ["[Click](https://evil.example)\n# Boss"]
    answers["skills"] = [{"term": "*bold* <b>", "aliases": [], "origin": "stated"}]
    answers["companies_ruled_out"] = ["Acme | `Co`"]
    text = summaries.render_intake(answers, CRITERIA)
    assert "](https://evil.example)" not in text
    assert "\n# Boss" not in text and text.count("\n# ") == 0
    assert "\\*bold\\* \\<b\\>" in text
    assert "Acme \\| \\`Co\\`" in text
    assert chr(0x2014) not in text


def test_the_summary_is_deterministic_and_does_not_change_the_answers():
    answers = casey()
    before = copy.deepcopy(answers)
    first = summaries.render_intake(answers, CRITERIA)
    assert summaries.render_intake(answers, CRITERIA) == first
    assert answers == before
    assert "\r" not in first and first.endswith("\n")


def test_the_summary_is_written_on_every_successful_build_and_never_on_a_refused_one(
        tmp_path, capsys):
    folder = build_workspace(tmp_path, capsys)
    path = folder / "profile" / "summary.md"
    first = build(capsys, folder)
    assert "profile/summary.md" in first["wrote"] and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "- Job titles: Operations Manager" in text and "Scoring config version 1.0." in text

    second = build(capsys, folder)  # nothing changed: the file and the wrote list stay put
    assert "profile/summary.md" not in second["wrote"]
    assert path.read_text(encoding="utf-8") == text

    changed = casey()
    changed["years_experience"] = 12
    (folder / "profile" / "answers.json").write_text(json.dumps(changed), encoding="utf-8")
    third = build(capsys, folder)
    assert "profile/summary.md" in third["wrote"]
    assert "- Years of experience: 12" in path.read_text(encoding="utf-8")

    broken = casey()
    broken["target_level"] = "wizard"
    (folder / "profile" / "answers.json").write_text(json.dumps(broken), encoding="utf-8")
    code = cli.main(["criteria-build", "--data-dir", str(folder)])
    refused = json.loads(capsys.readouterr().out)
    assert code == 1 and refused["ok"] is False
    assert "- Years of experience: 12" in path.read_text(encoding="utf-8")  # left as it was


def test_an_invalid_first_build_writes_no_summary(tmp_path, capsys):
    broken = casey()
    broken["target_level"] = "wizard"
    folder = build_workspace(tmp_path, capsys, broken)
    assert cli.main(["criteria-build", "--data-dir", str(folder)]) == 1
    capsys.readouterr()
    assert not (folder / "profile" / "summary.md").exists()


# ------------------------------------------------------------------------------------ titles

from test_titles import cmd, config, decision, fam, put_evidence, write_json  # noqa: E402
from test_titles import env  # noqa: E402,F401  (the built casey-ops folder with four boards)


def apply_two(env) -> dict:  # noqa: F811
    """Approve one discovered family and reject another, then run titles-apply."""
    put_evidence(env, {"supply chain": 12, "revenue operations": 7})
    write_json(env, "proposal.json", {"families": [
        fam("tf-supply", ["supply chain"], label="Supply chain"),
        fam("tf-revops", ["revenue operations"], label="Revenue ops | `x`")]})
    write_json(env, "decisions.json", {"decisions": [
        decision("tf-supply"),
        decision("tf-revops", "reject")]})
    code, out = cmd(env, "titles-apply")
    assert code == 0 and out["ok"], out
    return out


def test_the_titles_summary_states_counts_and_reasons(env):  # noqa: F811
    out = apply_two(env)
    rel = "titles/summary-2026-11-17.md"
    assert rel in out["wrote"]
    text = (env.folder / rel).read_text(encoding="utf-8")
    evidence_family = next(f for f in config(env)["title"]["families"] if f["id"] == "tf-supply")
    asking = evidence_family["evidence"]["asking_for_your_skills"]
    total = evidence_family["evidence"]["postings_sampled"]
    assert text.startswith("# Job titles chosen\n")
    assert f"- Supply chain (found in the sample): {asking} of {total} sampled postings ask for " \
           "your skills" in text
    kept, dropped = text.split("## Dropped")
    assert "Operations Manager (you listed this title" in kept  # a listed title and why
    assert "Program Manager" in kept
    assert "Revenue ops" not in kept
    assert "- Revenue ops \\| \\`x\\`" in dropped  # dropped, and its text kept literal
    assert "Sampled 100 postings." in text  # the evidence's own coverage note
    assert chr(0x2014) not in text


def test_the_titles_summary_marks_the_first_choice_and_lists_exclude_words(env):  # noqa: F811
    answers = json.loads((env.folder / "profile" / "answers.json").read_text("utf-8"))
    answers["first_choice_title"] = "Program Manager"
    (env.folder / "profile" / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    cmd(env, "criteria-build")
    families = config(env)["title"]["families"]
    ops = next(f for f in families if "Program" in f["label"])
    write_json(env, "exclusions.json", {"families": [{"id": ops["id"], "exclude": ["Legal"]}]})
    cmd(env, "titles-exclude")
    apply_two(env)
    text = (env.folder / "titles" / "summary-2026-11-17.md").read_text(encoding="utf-8")
    first = next(line for line in text.splitlines() if "your first choice" in line)
    assert "Program Manager" in first and "Words that rule a title out: Legal" in first


def test_titles_apply_rewrites_the_summary_only_when_it_changes(env):  # noqa: F811
    apply_two(env)
    path = env.folder / "titles" / "summary-2026-11-17.md"
    first = path.read_bytes()
    code, again = cmd(env, "titles-apply")
    assert code == 0 and again["data"]["unchanged"] is True and again["wrote"] == []
    assert path.read_bytes() == first


def test_render_titles_is_deterministic_and_needs_no_clock():
    from jobsearch import summaries

    evidence = {"sample_date": "2026-11-17", "boards_sampled": 2, "postings_sampled": 100,
                "coverage_note": "Sampled 100 postings."}
    criteria = {"title": {"families": [{
        "id": "tf-a", "label": "Alpha", "rung": 1, "origin": "discovered", "exclude": [],
        "evidence": {"asking_for_your_skills": 4, "postings_sampled": 100,
                     "top_shared_skills": ["Excel"]}}]}}
    decisions = {"decisions": [{"id": "tf-b", "decision": "reject", "label": "Beta"}]}
    text = summaries.render_titles(evidence, decisions, criteria)
    assert text == summaries.render_titles(evidence, decisions, criteria)
    assert "- Alpha (found in the sample): 4 of 100 sampled postings ask for your skills. " \
           "Skills they share: Excel." in text
    assert text.split("## Dropped")[1].strip().startswith("- Beta")
