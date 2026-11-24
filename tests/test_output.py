"""Ranked outputs: Markdown, JSON and CSV from one scored list, with the funnel and safe text.

Purpose: the user reads one ranked file that leads with reasons and says exactly what was filtered
out and why; posting text is attacker-controlled and must never become a live link, a heading, a
hidden character or a spreadsheet formula.
Logic: the tests build ScoredRole and RankInput values directly, so they do not depend on the
scorer, and call output.write_ranked into a tmp data folder. Two tests drive cli.main end to end
(byte-identical reruns, the calibration warning). No socket is opened.
Guardrails: fixtures are fictional (Acme, example.com). Each test owns its folder under tmp_path.
The dates are fixed strings, never the wall clock.
QA checklist: a changed line format needs the matching assertion here and in
tests/test_pipeline_tracer.py; a changed file shape needs docs/data-files.md in the same commit.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import pytest
from helpers import TRACER, run_cli

from jobsearch import fsio, output
from jobsearch.filters import FilterFailure
from jobsearch.roles_store import RankInput
from jobsearch.scoring import CriterionResult, ScoredRole

DATE = "2026-11-17"


@pytest.fixture
def env(scan_env):
    return scan_env


def criteria(one_per_company: bool = True) -> dict:
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    data["one_per_company"] = {"enabled": one_per_company, "keep": "best"}
    return data


def role(role_id: str, score: int, *, company: str = "Acme", title: str = "Engineer",
         coverage: int = 100, provisional: bool = False, gaps: list[str] | None = None,
         url: str | None = None, rejected: FilterFailure | None = None,
         locations: list[str] | None = None, tier_cap: str | None = None,
         cap_reasons: list[str] | None = None,
         reason_lines: list[tuple[str, str]] | None = None,
         posted: str | None = None) -> ScoredRole:
    # A block prints the role's reason_lines; the default is the one Why it fits line, so
    # the tests that count lines below the heading read the same as before. reason_text is the
    # same lines joined, as the scorer writes it.
    lines = reason_lines if reason_lines is not None else [
        ("Why it fits", f"Reason for {role_id}.")]
    return ScoredRole(
        role_id=role_id, source="greenhouse", company=company, title=title,
        locations=locations if locations is not None else ["Remote"],
        url_apply=url if url is not None else f"https://example.com/jobs/{role_id}",
        first_seen="2026-11-10", last_seen=DATE, family_id="f", family_label="Family",
        rejected=rejected, score=score, coverage=coverage, provisional=provisional,
        details=[CriterionResult("pay", "Pay", 3, 2, 3, "Pay is fine.", False)],
        gaps=gaps or [], reason_text=" ".join(f"{label}: {text}" for label, text in lines),
        reason_lines=lines,
        tier_cap=tier_cap, cap_reasons=cap_reasons or [], posted=posted,
    )


def rejected_role(role_id: str, filter_id: str = "work_mode", label: str = "Work arrangement"):
    return role(role_id, 0, rejected=FilterFailure(filter_id, label, "No."))


def rank_input(scored: list[ScoredRole], *, scanned: int | None = None, pasted: int = 0,
               merged: int = 0, gate: int = 0) -> RankInput:
    return RankInput(
        scan_date=DATE, fetched_total=len(scored) + merged + gate - pasted if scanned is None
        else scanned, title_gate_dropped=gate, duplicates_merged=merged, pasted_total=pasted,
        facts=[])


def write(tmp_path, scored, cfg=None, **kwargs) -> dict:
    cfg = cfg or criteria()
    wrote = output.write_ranked(tmp_path, rank_input(scored, **kwargs), scored, cfg)
    base = tmp_path / "ranked" / f"ranked-{DATE}"
    return {
        "wrote": wrote,
        "md": base.with_suffix(".md").read_text(encoding="utf-8"),
        "json": json.loads(base.with_suffix(".json").read_text(encoding="utf-8")),
        "csv": base.with_suffix(".csv").read_text(encoding="utf-8-sig"),
    }


def rejected_file(tmp_path) -> dict:
    path = tmp_path / "ranked" / f"rejected-{DATE}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def a_mixed_list() -> list[ScoredRole]:
    """2 A, 3 B, 8 C across 13 companies, plus 2 rejected."""
    scored = [role(f"a{i}", 90 - i, company=f"Alpha{i}") for i in range(2)]
    scored += [role(f"b{i}", 60 - i, company=f"Beta{i}") for i in range(3)]
    scored += [role(f"c{i}", 40 - i, company=f"Gamma{i}") for i in range(8)]
    scored += [rejected_role("x1"), rejected_role("x2", "region", "Location")]
    return scored


# ---------------------------------------------------------------------------- funnel line

def test_funnel_line_has_the_exact_form_and_inserts_pasted():
    assert output.funnel_line(scanned=412, pasted=0, passed=120, a=9, b=22, c=89) == (
        "412 scanned, 120 passed filters, 9 A, 22 B, 89 C")
    assert output.funnel_line(scanned=412, pasted=2, passed=120, a=9, b=22, c=89) == (
        "412 scanned, 2 pasted, 120 passed filters, 9 A, 22 B, 89 C")


# ----------------------------------------------------------------------- markdown layout

def test_markdown_shows_a_and_b_in_full_c_as_count_plus_top_five_then_funnel(tmp_path):
    out = write(tmp_path, a_mixed_list(), gate=5)
    md = out["md"]
    assert md.index("\n## A\n") < md.index("\n## B\n") < md.index("\n## C\n")
    assert md.index("\n## C\n") < md.index("\n## Funnel\n")
    assert md.count("### ") == 2 + 3  # A and B only
    assert "8 roles. Top 5:" in md
    c_section = md.split("\n## C\n")[1].split("\n## Funnel\n")[0]
    assert c_section.count("\n- ") == 5
    assert "Gamma0" in c_section and "Gamma4" in c_section and "Gamma5" not in c_section
    assert "20 scanned, 13 passed filters, 2 A, 3 B, 8 C" in md


def test_top_three_rejection_reasons_with_counts_and_title_gate_drops(tmp_path):
    scored = a_mixed_list() + [rejected_role("x3", "region", "Location"),
                               rejected_role("x4", "deny_terms", "Terms"),
                               rejected_role("x5", "pay", "Pay"), rejected_role("x6", "pay", "Pay")]
    md = write(tmp_path, scored, gate=7)["md"]
    block = md.split("Top reasons roles were filtered out:\n")[1].split("\n\n")[0]
    assert block.splitlines() == ["- Title outside your approved families: 7", "- Pay: 2",
                                  "- Location: 2"]


SEVEN = [("Why it fits", "Title matches Operations Manager, your first choice."),
         ("Where and how", "Remote, which fits."), ("Pay", "up to 218,400 USD"),
         ("Skills", "2 of 5 (budgeting, vendor management)"),
         ("Level", "manager, you aim for manager"), ("Posted", "4 days ago"),
         ("Company", "not one of your criteria")]


def test_each_role_leads_with_reason_then_tier_seen_date_and_apply(tmp_path):
    # Was "reason, tier, Gaps, seen, apply"; the reason is now the labeled lines with
    # Why it fits first, and there is no Gaps line (a missing fact reads "not stated" in place).
    scored = [role("a1", 90, gaps=["Pay"], provisional=True, coverage=60, reason_lines=SEVEN),
              role("b1", 55, company="Beta")]
    md = write(tmp_path, scored)["md"]
    block = md.split("### Engineer at Acme")[1].split("### ")[0]
    lines = [ln for ln in block.splitlines() if ln]
    assert lines[0] == "Why it fits: Title matches Operations Manager, your first choice."
    assert lines[1:7] == ["Where and how: Remote, which fits.", "Pay: up to 218,400 USD",
                          "Skills: 2 of 5 \\(budgeting, vendor management\\)",
                          "Level: manager, you aim for manager", "Posted: 4 days ago",
                          "Company: not one of your criteria"]
    assert lines[7] == "Tier A, score 90, provisional (based on 60 percent of your criteria)"
    assert lines[8] == "Last seen open: 2026-11-17"
    assert lines[9] == "Apply: <https://example.com/jobs/a1>"
    assert "Gaps:" not in md
    other = md.split("### Engineer at Beta")[1]
    assert "provisional" not in other.split("##")[0]


def test_the_block_prints_skills_and_level_on_their_own_lines(tmp_path):
    # Skills is the fourth line after the heading and Level the fifth, each escaped.
    lines = [("Why it fits", "Title fits."), ("Where and how", "Remote."), ("Pay", "not stated"),
             ("Skills", "2 of 5 (budgeting, a_b)"), ("Level", "manager *only*"),
             ("Posted", "4 days ago"), ("Company", "not one of your criteria")]
    md = write(tmp_path, [role("a1", 90, reason_lines=lines)])["md"]
    block = md.split("### Engineer at Acme")[1].split("\n## ")[0]
    shown = [ln for ln in block.splitlines() if ln]
    assert shown[3] == "Skills: 2 of 5 \\(budgeting, a\\_b\\)"
    assert shown[4] == "Level: manager \\*only\\*"
    assert shown[6] == "Company: not one of your criteria"


def test_boundary_scores_land_in_the_tier_whose_minimum_they_equal(tmp_path):
    assert output.assign_tier(70, criteria()) == "A"
    assert output.assign_tier(69, criteria()) == "B"
    assert output.assign_tier(50, criteria()) == "B"
    assert output.assign_tier(49, criteria()) == "C"
    out = write(tmp_path, [role("a", 70, company="P"), role("b", 50, company="Q"),
                           role("c", 49, company="R")])
    assert [r["tier"] for r in out["json"]["roles"]] == ["A", "B", "C"]


def test_zero_passing_roles_still_shows_funnel_and_reasons(tmp_path):
    scored = [rejected_role("x1"), rejected_role("x2")]
    out = write(tmp_path, scored, gate=3)
    md = out["md"]
    assert "No roles passed your filters" in md
    assert md.count("None this time.") == 2
    assert "5 scanned, 0 passed filters, 0 A, 0 B, 0 C" in md
    assert "Work arrangement: 2" in md
    assert out["json"]["roles"] == [] and out["json"]["funnel"]["passed"] == 0
    assert out["csv"].splitlines()[0].startswith("tier,score")
    assert len(out["csv"].splitlines()) == 1


def test_within_a_tier_score_then_coverage_then_company_title_and_id(tmp_path):
    cfg = criteria(one_per_company=False)
    scored = [
        role("r5", 80, company="Beta", title="Z"), role("r4", 80, company="Acme", title="Z"),
        role("r3", 80, company="Acme", title="Y", coverage=90),
        role("r2", 80, company="Acme", title="Y", coverage=100),
        role("r1", 80, company="Acme", title="Y", coverage=100),
        role("r0", 85, company="Zed"),
    ]
    ids = [r["role_id"] for r in write(tmp_path, scored, cfg)["json"]["roles"]]
    assert ids == ["r0", "r1", "r2", "r4", "r5", "r3"]
    csv_ids = [row["role_id"] for row in csv.DictReader(io.StringIO(
        (tmp_path / "ranked" / f"ranked-{DATE}.csv").read_text(encoding="utf-8-sig")))]
    assert csv_ids == ids


# -------------------------------------------------------------------------- one per company

def test_one_per_company_shows_the_best_role_with_a_more_line_and_counts_all(tmp_path):
    scored = [role("a1", 90, company="Acme", title="Lead"),
              role("a2", 85, company="acme", title="Second"),
              role("a3", 40, company="ACME", title="Third"),
              role("b1", 60, company="Beta")]
    out = write(tmp_path, scored)
    md = out["md"]
    # The count line became "Best of N roles at <company> for you"; the tier A sibling
    # (Second, 85) now has an Also line so no tier A role is hidden, and the C one (Third) has none.
    assert "### Lead at Acme" in md and "Third" not in md
    assert "Best of 3 roles at Acme for you" in md and "more at this company" not in md
    assert "Also tier A here: Second, score 85. Apply: <https://example.com/jobs/a2>" in md
    assert "4 scanned, 4 passed filters, 2 A, 1 B, 1 C" in md
    shown = {r["role_id"]: (r["shown"], r["more_at_company"]) for r in out["json"]["roles"]}
    assert shown == {"a1": (True, 2), "a2": (False, 0), "a3": (False, 0), "b1": (True, 0)}
    assert out["json"]["funnel"]["tiers"] == {"A": 2, "B": 1, "C": 1}


def a_section(md: str) -> str:
    return md.split("## A", 1)[1].split("## B", 1)[0]


def test_the_kept_role_says_best_of_n_and_lists_the_other_tier_a_roles(tmp_path):
    """The shown role is labeled, and every other tier A role gets one short line."""
    scored = [role("a1", 90, company="Acme", title="First"),
              role("a2", 85, company="Acme", title="Second"),
              role("a3", 80, company="Acme", title="Third"),
              role("a4", 40, company="Acme", title="Fourth")]
    out = write(tmp_path, scored)
    md = out["md"]
    assert md.count("### ") == 1 and "### First at Acme" in md
    assert md.count("Best of 4 roles at Acme for you") == 1
    assert "more at this company" not in md
    second = "Also tier A here: Second, score 85. Apply: <https://example.com/jobs/a2>"
    third = "Also tier A here: Third, score 80. Apply: <https://example.com/jobs/a3>"
    lines = md.splitlines()
    assert lines.count(second) == 1 and lines.count(third) == 1
    assert lines.index(second) + 1 == lines.index(third)
    assert lines.index("Best of 4 roles at Acme for you") + 1 == lines.index(second)
    assert "Fourth" not in a_section(md) and "Also tier A here: Fourth" not in md
    shown = {r["role_id"]: (r["shown"], r["more_at_company"]) for r in out["json"]["roles"]}
    assert shown == {"a1": (True, 3), "a2": (False, 0), "a3": (False, 0), "a4": (False, 0)}
    assert out["json"]["funnel"]["tiers"] == {"A": 3, "B": 0, "C": 1}


def test_every_tier_a_role_is_visible_in_the_a_section(tmp_path):
    scored = [role("g1", 95, company="GitLab", title="Staff One"),
              role("g2", 92, company="GitLab", title="Staff Two"),
              role("g3", 71, company="gitlab", title="Staff Three"),
              role("z1", 88, company="Zoox", title="Zoox One"),
              role("z2", 80, company="Zoox", title="Zoox Two"),
              role("m1", 60, company="Mongo", title="Mongo One"),
              role("m2", 55, company="Mongo", title="Mongo Two"),
              role("s1", 75, company="Samsara", title="Samsara One")]
    out = write(tmp_path, scored)
    section = a_section(out["md"])
    tier_a = [r for r in out["json"]["roles"] if r["tier"] == "A"]
    assert len(tier_a) == out["json"]["funnel"]["tiers"]["A"] == 6
    for r in tier_a:
        title = output.md_escape(r["title"])
        assert any(title in ln and (ln.startswith("### ") or ln.startswith("Also tier A here: "))
                   for ln in section.splitlines()), r["title"]
    assert "Best of 3 roles at GitLab for you" in section
    assert "Best of 2 roles at Mongo for you" in out["md"]
    assert "Also tier A here" not in out["md"].split("## B", 1)[1]


def test_a_tie_at_one_company_goes_to_the_newer_posting(tmp_path):
    old, new = "2026-08-01T09:00:00Z", "2026-11-10T09:00:00Z"
    scored = [role("a1", 80, company="Acme", title="Alpha", posted=old),
              role("a2", 80, company="Acme", title="Zulu", posted=new)]
    md = write(tmp_path, scored)["md"]
    assert md.count("### ") == 1 and "### Zulu at Acme" in md
    assert "Best of 2 roles at Acme for you" in md
    assert "Also tier A here: Alpha, score 80. Apply: <https://example.com/jobs/a1>" in md
    swapped = [role("a1", 80, company="Acme", title="Alpha", posted=new),
               role("a2", 80, company="Acme", title="Zulu", posted=old)]
    md = write(tmp_path, swapped)["md"]
    assert md.count("### ") == 1 and "### Alpha at Acme" in md
    assert "Also tier A here: Zulu, score 80." in md


def test_a_dated_role_beats_an_undated_one_at_a_tie(tmp_path):
    scored = [role("a1", 80, company="Acme", title="Alpha", posted=None),
              role("a2", 80, company="Acme", title="Zulu", posted="2026-01-05T09:00:00Z")]
    out = write(tmp_path, scored)
    assert "### Zulu at Acme" in out["md"] and "### Alpha" not in out["md"]
    shown = {r["role_id"]: r["shown"] for r in out["json"]["roles"]}
    assert shown == {"a1": False, "a2": True}


def test_the_score_still_beats_a_newer_posting(tmp_path):
    scored = [role("a1", 82, company="Acme", title="Alpha", posted="2026-01-05T09:00:00Z"),
              role("a2", 80, company="Acme", title="Zulu", posted="2026-11-10T09:00:00Z")]
    assert "### Alpha at Acme" in write(tmp_path, scored)["md"]


def test_one_company_role_has_no_best_of_line(tmp_path):
    md = write(tmp_path, [role("a1", 90, company="Acme"), role("b1", 60, company="Beta")])["md"]
    assert "Best of" not in md and "Also tier A here" not in md


def test_one_per_company_off_has_no_best_of_or_also_lines(tmp_path):
    scored = [role("a1", 90, title="One"), role("a2", 85, title="Two")]
    md = write(tmp_path, scored, criteria(one_per_company=False))["md"]
    assert "### One at Acme" in md and "### Two at Acme" in md
    assert "Best of" not in md and "Also tier A here" not in md


def test_an_also_line_escapes_the_title_and_refuses_a_non_https_link(tmp_path):
    scored = [role("a1", 90, company="Acme *Co*", title="Real"),
              role("a2", 85, company="Acme *Co*", title="[Click](javascript:x) *bold*",
                   url="javascript:alert(1)")]
    md = write(tmp_path, scored)["md"]
    assert "Best of 2 roles at Acme \\*Co\\* for you" in md
    also = next(ln for ln in md.splitlines() if ln.startswith("Also tier A here: "))
    assert "\\[Click\\]\\(javascript:x\\) \\*bold\\*" in also
    assert "Apply link not available" in also and "javascript:alert" not in also


def test_one_per_company_off_shows_every_role(tmp_path):
    scored = [role("a1", 90, title="One"), role("a2", 85, title="Two")]
    out = write(tmp_path, scored, criteria(one_per_company=False))
    assert "### One at Acme" in out["md"] and "### Two at Acme" in out["md"]
    assert "more at this company" not in out["md"] and "Best of" not in out["md"]


def c_line(md: str) -> str:
    return md.split("\n## C\n")[1].split("\n\n")[1]


def tier_section(md: str, tier: str) -> str:
    return md.split(f"\n## {tier}\n")[1].split("\n## ")[0]


def test_the_c_count_says_how_many_roles_one_per_company_hid(tmp_path):
    scored = [role("a1", 90, company="Acme"), role("c1", 40, company="Acme", title="One"),
              role("c2", 39, company="acme", title="Two"), role("c3", 38, company="Beta")]
    md = write(tmp_path, scored)["md"]
    assert c_line(md) == "3 roles, 2 not shown because their company is already listed. Top 1:"
    assert md.split("\n## C\n")[1].split("\n## Funnel\n")[0].count("\n- ") == 1


def test_one_hidden_c_role_reads_in_the_singular(tmp_path):
    scored = [role("a1", 90, company="Acme"), role("c1", 40, company="Acme", title="One")]
    md = write(tmp_path, scored)["md"]
    assert c_line(md) == "1 role, 1 not shown because its company is already listed."
    assert "\n- " not in tier_section(md, "C")


def test_a_tier_one_per_company_hid_entirely_says_how_many_and_why(tmp_path):
    scored = [role("a1", 90, company="Acme"), role("b1", 60, company="Acme", title="One"),
              role("b2", 55, company="acme", title="Two"), role("a2", 85, company="Beta"),
              role("b3", 52, company="Beta", title="Three")]
    md = write(tmp_path / "many", scored)["md"]
    b = tier_section(md, "B")
    assert "3 roles, 3 not shown because their company is already listed." in b
    assert "### " not in b
    assert md.count("None this time.") == 0
    assert "3 B" in md.split("\n## Funnel\n")[1]
    one = [role("a1", 90, company="Acme"), role("b1", 60, company="Acme", title="One")]
    md = write(tmp_path / "one", one)["md"]
    assert "1 role, 1 not shown because its company is already listed." in tier_section(md, "B")


def test_the_number_after_top_is_the_number_of_c_roles_listed(tmp_path):
    cases = [
        ([role("c1", 40, company="P"), role("c2", 39, company="Q")], "2 roles. Top 2:"),
        ([role("a1", 90, company="Acme"), role("c1", 40, company="Acme", title="One"),
          role("c2", 39, company="acme", title="Two"), role("c3", 38, company="Beta")],
         "3 roles, 2 not shown because their company is already listed. Top 1:"),
        ([role(f"c{i}", 40 - i, company=f"Co{i}") for i in range(7)], "7 roles. Top 5:"),
    ]
    for n, (scored, expected) in enumerate(cases):
        md = write(tmp_path / f"case{n}", scored)["md"]
        assert c_line(md) == expected
        listed = tier_section(md, "C").count("\n- ")
        assert int(re.search(r"Top (\d+):", c_line(md)).group(1)) == listed


def test_the_c_line_has_no_clause_when_one_per_company_is_off_or_the_cap_hid_them(tmp_path):
    same = [role(f"c{i}", 40 - i, title=f"T{i}") for i in range(7)]  # 7 C roles at one company
    assert c_line(write(tmp_path, same, criteria(one_per_company=False))["md"]) == (
        "7 roles. Top 5:")
    spread = [role(f"c{i}", 40 - i, company=f"Co{i}") for i in range(7)]
    assert c_line(write(tmp_path, spread)["md"]) == "7 roles. Top 5:"


# --------------------------------------------------------------------------- closed roles

def test_closed_roles_line_and_list_come_from_the_scan_meta(tmp_path):
    meta = tmp_path / "scans" / DATE
    meta.mkdir(parents=True)
    (meta / "scan-meta.json").write_text(json.dumps({"closed_roles": [
        {"role_id": "z2", "company": "Zed", "title": "Old role"},
        {"role_id": "a9", "company": "Acme", "title": "Gone role"}]}), encoding="utf-8")
    out = write(tmp_path, [role("a1", 90)])
    assert "2 roles closed since your last scan" in out["md"]
    assert out["md"].index("Gone role") < out["md"].index("Old role")
    assert [r["company"] for r in out["json"]["closed_since_last"]] == ["Acme", "Zed"]


def test_no_closed_line_when_nothing_closed(tmp_path):
    out = write(tmp_path, [role("a1", 90)])
    assert "closed since your last scan" not in out["md"]
    assert out["json"]["closed_since_last"] == []


# -------------------------------------------------------------------------- JSON and CSV

def test_json_funnel_balances_and_every_file_agrees(tmp_path):
    scored = a_mixed_list()
    out = write(tmp_path, scored, pasted=1, merged=2, gate=3)
    f = out["json"]["funnel"]
    assert f["scanned"] + f["pasted"] == (
        f["duplicates_merged"] + f["title_gate_dropped"] + f["rejected_by_filters"] + f["passed"])
    assert sum(f["tiers"].values()) == f["passed"] == 13
    assert (f["pasted"], f["duplicates_merged"], f["title_gate_dropped"]) == (1, 2, 3)
    assert len(out["json"]["roles"]) == 13 and "rejected" not in out["json"]
    rows = list(csv.DictReader(io.StringIO(out["csv"])))
    assert len(rows) == 13
    assert out["json"]["rejections"] == [
        {"filter_id": "region", "label": "Location", "count": 1},
        {"filter_id": "work_mode", "label": "Work arrangement", "count": 1}]
    first = out["json"]["roles"][0]
    for key in ("source", "first_seen", "shown", "more_at_company"):
        assert key in first
    assert all("details" not in r for r in out["json"]["roles"])  # reasons are in reason_text
    shown = [r["shown"] for r in out["json"]["roles"]]
    assert shown.count(True) == 2 + 3 + 5


def test_rejected_roles_go_to_their_own_file_sorted_with_the_filter_and_reason(tmp_path):
    scored = [role("a1", 90), rejected_role("x2", "region", "Location"), rejected_role("x1")]
    out = write(tmp_path, scored)
    assert rejected_file(tmp_path) == {"schema_version": 1, "scan_date": DATE, "rejected": [
        {"role_id": "x1", "company": "Acme", "title": "Engineer", "filter_id": "work_mode",
         "reason": "No."},
        {"role_id": "x2", "company": "Acme", "title": "Engineer", "filter_id": "region",
         "reason": "No."}]}
    assert f"ranked/rejected-{DATE}.json" in out["wrote"]


def test_the_rejected_file_is_written_empty_when_nothing_was_rejected(tmp_path):
    write(tmp_path, [role("a1", 90), rejected_role("x1")])
    write(tmp_path, [role("a1", 90)])  # a re-rank must not leave the old list behind
    assert rejected_file(tmp_path)["rejected"] == []


def test_the_rejected_file_cleans_hostile_text(tmp_path):
    bad = role("x1", 0, company="Acme\u202e", title="Boss \u2014 Lead\x00",
               rejected=FilterFailure("pay", "Pay", "Too low \u2014 sorry\u200b"))
    write(tmp_path, [bad])
    entry = rejected_file(tmp_path)["rejected"][0]
    assert entry["company"] == "Acme" and "\u2014" not in entry["title"] + entry["reason"]
    assert "\x00" not in entry["title"] and "\u200b" not in entry["reason"]


def test_csv_columns_order_and_joins(tmp_path):
    scored = [role("a1", 90, gaps=["Pay", "Level"], locations=["Remote", "Austin, TX"])]
    out = write(tmp_path, scored)
    assert out["csv"].splitlines()[0] == (
        "tier,score,coverage,provisional,company,title,locations,last_seen,url_apply,reason,"
        "gaps,role_id,criteria_version")
    row = next(csv.DictReader(io.StringIO(out["csv"])))
    assert row["locations"] == "Remote; Austin, TX" and row["gaps"] == "Pay; Level"
    assert row["provisional"] == "false" and row["criteria_version"] == "1.0"
    assert "\r" not in out["csv"]


def test_csv_cells_starting_with_formula_characters_are_prefixed(tmp_path):
    scored = [role("a1", 90, company="=HYPERLINK(1)", title="+cmd", locations=["-1+1"],
                   gaps=["@SUM"])]
    row = next(csv.DictReader(io.StringIO(write(tmp_path, scored)["csv"])))
    assert row["company"] == "'=HYPERLINK(1)" and row["title"] == "'+cmd"
    assert row["locations"] == "'-1+1" and row["gaps"] == "'@SUM"
    assert row["score"] == "90"


# ------------------------------------------------------------------------------ escaping

def test_markdown_syntax_in_a_title_renders_as_literal_text(tmp_path):
    scored = [role("a1", 90, title="[Click](https://evil.example) # Head", company="<b>Co</b>")]
    md = write(tmp_path, scored)["md"]
    heading = next(ln for ln in md.splitlines() if ln.startswith("### "))
    assert "\\[Click\\]\\(https://evil.example\\) \\# Head" in heading
    assert "\\<b\\>Co\\</b\\>" in heading
    # 03-06: the career-ops block holds the raw title inside a fenced block, where it is literal
    # text the user copies, so the link syntax is checked outside that block.
    outside_fence = re.sub(r"```text\n.*?```\n", "", md, flags=re.DOTALL)
    assert "](https://evil.example)" not in outside_fence
    assert md.count("\n# ") == 0


def test_a_non_https_apply_url_is_not_a_link(tmp_path):
    for url in ("javascript:alert(1)", "http://example.com/a", "https://exa mple.com/a",
                "https://example.com/a>x", "data:text/html,hi"):
        md = write(tmp_path, [role("a1", 90, url=url)])["md"]
        assert ("Apply link not available; open the posting from the company's careers page."
                in md)
        assert "Apply: <" not in md


def test_hidden_and_control_characters_are_stripped_from_every_file(tmp_path):
    scored = [role("a1", 90, title="Eng‮ineer​\x07 — Lead", company="Ac⁦me﻿",
                   locations=["Re‍mote"])]
    out = write(tmp_path, scored)
    blob = out["md"] + out["csv"] + json.dumps(out["json"], ensure_ascii=False)
    for bad in ("‮", "​", "\x07", "⁦", "﻿", "‍", "—"):
        assert bad not in blob
    assert out["json"]["roles"][0]["title"] == "Engineer - Lead"
    assert out["json"]["roles"][0]["company"] == "Acme"
    assert output.clean_text("a‮b​c") == "abc"
    assert output.md_escape("a*b_c") == "a\\*b\\_c"


HIDDEN_CHARS = {
    "tag characters": "".join(chr(0xE0000 + ord(c)) for c in "ignore"),
    "arabic letter mark": "\u061c",
    "soft hyphen": "\u00ad",
    "variation selector": "\ufe0f",
    "variation selector supplement": "\U000e0100",
    "deprecated format character": "\u206a",
    "mongolian vowel separator": "\u180e",
    "interlinear annotation anchor": "\ufff9",
    "private use": "\ue000",
}


@pytest.mark.parametrize("hidden", list(HIDDEN_CHARS.values()), ids=list(HIDDEN_CHARS))
def test_other_hidden_characters_are_stripped_from_every_file(tmp_path, hidden):
    scored = [role("a1", 90, title=f"Eng{hidden}ineer", company=f"Ac{hidden}me")]
    out = write(tmp_path, scored)
    blob = out["md"] + out["csv"] + json.dumps(out["json"], ensure_ascii=False)
    assert hidden not in blob
    assert out["json"]["roles"][0]["title"] == "Engineer"
    assert output.clean_text(f"a{hidden}b") == "ab"
    assert output.md_escape(f"a{hidden}b") == "ab"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_line_separators_become_a_plain_newline(separator):
    assert output.clean_text(f"a{separator}b") == "a\nb"
    assert output.md_escape(f"a{separator}b") == "a b"


def test_ordinary_international_text_survives_clean_text():
    text = "Zo\u00eb M\u00fcller \u2013 \u5de5\u7a0b\u5e08 \u05e9\u05dc\u05d5\u05dd \u0645\u0631\u062d\u0628\u0627"
    assert output.clean_text(text) == text


def test_no_em_dash_anywhere_in_the_files(tmp_path):
    out = write(tmp_path, a_mixed_list())
    assert "—" not in out["md"] + out["csv"] + json.dumps(out["json"], ensure_ascii=False)


# -------------------------------------------------------------- determinism and atomic writes

def test_writing_twice_gives_byte_identical_files(tmp_path):
    scored = a_mixed_list()
    write(tmp_path, scored)
    base = tmp_path / "ranked" / f"ranked-{DATE}"
    first = [base.with_suffix(s).read_bytes() for s in (".md", ".json", ".csv")]
    write(tmp_path, list(reversed(scored)))
    assert [base.with_suffix(s).read_bytes() for s in (".md", ".json", ".csv")] == first


def test_every_ranked_file_goes_through_the_atomic_writer(tmp_path, monkeypatch):
    seen: list[str] = []
    # Every text and JSON write ends in write_bytes_atomic, so one spy sees all five.
    real_bytes = fsio.write_bytes_atomic

    def spy_bytes(path, data):
        seen.append(Path(path).name)
        real_bytes(path, data)

    monkeypatch.setattr(fsio, "write_bytes_atomic", spy_bytes)
    write(tmp_path, [role("a1", 90)])
    # The funnel file is the fourth file the one writer produces; the rejected list is the fifth.
    assert sorted(seen) == [f"funnel-{DATE}.md", f"ranked-{DATE}.csv", f"ranked-{DATE}.json",
                            f"ranked-{DATE}.md", f"rejected-{DATE}.json"]


def test_rank_carries_the_win_invite_once_as_data_notice(env):  # noqa: F811
    run_cli(env, "scan")
    first = run_cli(env, "rank")
    notice = first["data"]["notice"]
    assert "discussions/categories/wins" in notice and "/job-search-prioritizer:feedback" in notice
    assert "\n" not in notice and "config/notices.json" in first["wrote"]
    second = run_cli(env, "rank")
    assert second["data"]["notice"] == "" and "config/notices.json" not in second["wrote"]


def test_rank_twice_through_the_cli_is_byte_identical_and_writes_five_files(env):  # noqa: F811
    run_cli(env, "scan")
    first = run_cli(env, "rank")
    # The funnel file joins the three ranked files in wrote, with the rejected list and
    # the invite stamp.
    assert sorted(first["wrote"]) == [
        "config/notices.json", f"ranked/funnel-{DATE}.md", *[f"ranked/ranked-{DATE}.{s}" for s in ("csv", "json", "md")],
        f"ranked/rejected-{DATE}.json"]
    paths = [env.folder / "ranked" / f"ranked-{DATE}.{s}" for s in ("md", "json", "csv")]
    paths.append(env.folder / "ranked" / f"rejected-{DATE}.json")
    before = [p.read_bytes() for p in paths]
    run_cli(env, "rank")
    assert [p.read_bytes() for p in paths] == before
    assert "scanned" in first["data"]["funnel_line"]
    assert first["counts"]["scanned"] >= first["counts"]["passed"]


def test_rank_warns_until_cutoffs_come_from_the_users_own_results(env):  # noqa: F811
    run_cli(env, "scan")
    out = run_cli(env, "rank")
    assert any("Tier cutoffs have not been set from your own results yet. Run criteria-test, "
               "then calibrate." in w for w in out["warnings"])
    run_cli(env, "calibrate", "--a-min", "80", "--b-min", "60")
    again = run_cli(env, "rank")
    assert not any("Tier cutoffs" in w for w in again["warnings"])
    # a calibration that names another config version brings the warning back
    path = env.folder / "config" / "calibration.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["criteria_version"] = "0.1"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert any("Tier cutoffs" in w for w in run_cli(env, "rank")["warnings"])


@pytest.mark.parametrize("text", ["plain", "with | pipe", "a\nb"])
def test_md_escape_is_always_one_line(text):
    assert "\n" not in output.md_escape(text)


def test_the_csv_starts_with_a_byte_order_mark_and_accented_names_round_trip(tmp_path):
    """Excel on Windows reads a BOM-less UTF-8 CSV as the ANSI code page and garbles accents."""
    scored = [role("a1", 90, title="Ing\u00e9nieur", company="Soci\u00e9t\u00e9 G\u00e9n\u00e9rale")]
    write(tmp_path, scored)
    raw = (tmp_path / "ranked" / f"ranked-{DATE}.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert not raw[3:].startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert list(rows[0])[0] == "tier"
    assert rows[0]["company"] == "Soci\u00e9t\u00e9 G\u00e9n\u00e9rale"
    assert rows[0]["title"] == "Ing\u00e9nieur"
    for suffix in ("md", "json"):  # the other two files stay BOM-free
        assert not (tmp_path / "ranked" / f"ranked-{DATE}.{suffix}").read_bytes().startswith(b"\xef")


# ---------------------------------------------------------------------------- tier caps

HELD = "the title reads as lead level and you are aiming for manager"


def held_role(role_id: str = "h1", score: int = 91, **kwargs) -> ScoredRole:
    return role(role_id, score, company="Held Co", tier_cap="B", cap_reasons=[HELD], **kwargs)


def test_a_capped_role_scoring_in_a_is_held_at_b_in_every_file(tmp_path):
    out = write(tmp_path, [held_role(), role("a1", 80, company="Alpha")])
    md = out["md"]
    a_part, rest = md.split("## B", 1)
    assert "Held Co" not in a_part and "### Engineer at Alpha" in a_part
    block = rest.split("## C")[0].split("### Engineer at Held Co")[1]
    lines = [ln for ln in block.splitlines() if ln]
    assert lines[1] == "Tier B, score 91"
    assert lines[2] == f"Held at tier B: {HELD}."
    held = next(r for r in out["json"]["roles"] if r["role_id"] == "h1")
    assert (held["tier"], held["tier_cap"], held["cap_reasons"]) == ("B", "B", [HELD])
    assert out["json"]["funnel"]["tiers"] == {"A": 1, "B": 1, "C": 0}
    assert next(r for r in csv.DictReader(io.StringIO(out["csv"])) if r["role_id"] == "h1"
                )["tier"] == "B"


def test_one_per_company_keeps_an_open_a_over_a_higher_scoring_capped_role(tmp_path):
    """A role held at B must not hide an open tier A role at the same company."""
    capped = role("cap", 85, company="Acme", title="Capped", tier_cap="B", cap_reasons=[HELD])
    open_a = role("open", 72, company="Acme", title="Open")
    out = write(tmp_path, [capped, open_a, role("z", 30, company="Zed")])
    a_part = out["md"].split("## A", 1)[1].split("## B", 1)[0]
    assert "### Open at Acme" in a_part
    # The count line became the Best of label; the capped role is shown at B, so it is
    # not an Also tier A line.
    assert "Best of 2 roles at Acme for you" in a_part and "more at this company" not in a_part
    shown = {r["role_id"]: r["shown"] for r in out["json"]["roles"]}
    assert shown["open"] is True and shown["cap"] is False
    tiers = out["json"]["funnel"]["tiers"]
    assert tiers == {"A": 1, "B": 1, "C": 1}
    # The tier A count and the tier A section agree: the one open A role is on the page.
    assert tiers["A"] == sum(1 for r in out["json"]["roles"]
                             if r["tier"] == "A" and r["shown"])


def test_a_capped_role_that_already_scores_in_b_shows_no_held_line(tmp_path):
    out = write(tmp_path, [held_role(score=60)])
    assert "Held at tier B" not in out["md"]
    assert next(iter(out["json"]["roles"]))["tier"] == "B"


def test_a_role_with_no_cap_has_empty_cap_fields(tmp_path):
    out = write(tmp_path, [role("a1", 90)])
    row = out["json"]["roles"][0]
    assert (row["tier_cap"], row["cap_reasons"]) == (None, [])
    assert "Held at tier" not in out["md"]


def test_a_cap_never_raises_a_tier(tmp_path):
    out = write(tmp_path, [held_role(score=30)])
    assert out["json"]["roles"][0]["tier"] == "C"


def test_role_tier_lowers_to_the_cap_and_never_raises():
    cfg = criteria()
    assert output.role_tier(held_role(score=91), cfg) == "B"
    assert output.role_tier(held_role(score=60), cfg) == "B"
    assert output.role_tier(held_role(score=20), cfg) == "C"
    assert output.role_tier(role("n", 91), cfg) == "A"


def test_held_line_and_cap_reasons_are_cleaned(tmp_path):
    hostile = role("h2", 91, company="Held Co", tier_cap="B",
                   cap_reasons=["it was *bold*\u200b # now\u2014 here"])
    out = write(tmp_path, [hostile])
    assert "Held at tier B: it was \\*bold\\* \\# now- here." in out["md"]
    assert out["json"]["roles"][0]["cap_reasons"] == ["it was *bold* # now- here"]


def test_a_held_role_never_hides_an_open_tier_a_role(tmp_path):
    """The open A role is the Best of block, and the role held at B is
    not an Also tier A line because it is shown at B."""
    capped = role("cap", 85, company="Acme", title="Capped", tier_cap="B", cap_reasons=[HELD])
    open_a = role("open", 72, company="Acme", title="Open")
    out = write(tmp_path, [capped, open_a])
    md = out["md"]
    assert "### Open at Acme" in a_section(md)
    assert "Best of 2 roles at Acme for you" in a_section(md)
    assert "Also tier A here" not in md
    assert out["json"]["funnel"]["tiers"] == {"A": 1, "B": 1, "C": 0}


# ------------------------------------------------- the career-ops block and the next steps

def career_ops_block(md: str) -> str:
    """The text between the Send to career-ops heading and the Next steps heading."""
    return md.split("\n## Send to career-ops\n")[1].split("\n## Next steps\n")[0]


def career_ops_lines(md: str) -> list[str]:
    return [line for line in career_ops_block(md).splitlines() if line.startswith("- [ ] ")]


def test_the_career_ops_block_lists_every_tier_a_role_in_order(tmp_path):
    scored = [
        role("a1", 95, company="Alpha", title="Lead Analyst"),
        role("a2", 92, company="Alpha", title="Senior Analyst"),   # "Also tier A here"
        role("a3", 90, company="Beta", title="Analyst"),
        role("b1", 60, company="Gamma", title="Junior Analyst"),
        role("c1", 40, company="Delta", title="Intern"),
    ]
    md = write(tmp_path, scored)["md"]
    assert "Also tier A here: Senior Analyst" in md  # the sibling is real, not assumed
    assert career_ops_lines(md) == [
        "- [ ] https://example.com/jobs/a1 | Alpha | Lead Analyst",
        "- [ ] https://example.com/jobs/a2 | Alpha | Senior Analyst",
        "- [ ] https://example.com/jobs/a3 | Beta | Analyst",
    ]
    assert "jobs/b1" not in career_ops_block(md) and "jobs/c1" not in career_ops_block(md)


def test_the_block_survives_hostile_posting_text(tmp_path):
    hostile = "Lead | Analyst ```\n- [ ] https://evil.example/x | Evil | Boss ]"
    md = write(tmp_path, [role("a1", 95, company="Ac`me | Co", title=hostile)])["md"]
    lines = career_ops_lines(md)
    assert len(lines) == 1
    assert lines[0].count("|") == 2  # only the two real column separators
    assert "`" not in lines[0] and "\n" not in lines[0]
    assert lines[0].startswith("- [ ] https://example.com/jobs/a1 | Acme / Co | Lead / Analyst ")
    block = career_ops_block(md)
    assert block.count("```") == 2  # one opening and one closing fence, nothing forged
    assert not any(line.startswith("- [ ] https://evil") for line in md.splitlines())


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "http://example.com/jobs/a1",
                                 "https://example.com/a b", "https://example.com/a|b",
                                 "https://example.com/a`b"])
def test_a_role_without_a_plain_https_link_is_left_out_and_counted(tmp_path, bad):
    scored = [role("a1", 95, company="Alpha", url=bad), role("a2", 90, company="Beta")]
    block = career_ops_block(write(tmp_path, scored)["md"])
    assert [line for line in block.splitlines() if line.startswith("- [ ] ")] == [
        "- [ ] https://example.com/jobs/a2 | Beta | Engineer"]
    assert "1 tier A role has no usable apply link and is left out." in block


def test_two_left_out_roles_use_the_plural(tmp_path):
    scored = [role("a1", 95, company="Alpha", url="http://x.example/1"),
              role("a2", 90, company="Beta", url="http://x.example/2")]
    block = career_ops_block(write(tmp_path, scored)["md"])
    assert "2 tier A roles have no usable apply link and are left out." in block
    assert "```" not in block  # nothing to list, so no fence


def test_no_tier_a_roles(tmp_path):
    block = career_ops_block(write(tmp_path, [role("b1", 60)])["md"])
    assert "No tier A roles this time." in block
    assert "```" not in block and "- [ ]" not in block


def test_next_steps_name_the_letter_and_review_skills(tmp_path):
    md = write(tmp_path, a_mixed_list())["md"]
    tail = md.split("\n## Next steps\n")[1]
    assert "/job-search-prioritizer:letter" in tail and "/job-search-prioritizer:review" in tail
    assert md.rstrip("\n").endswith(tail.rstrip("\n"))  # the last block of the file
    assert md.index("\n## Funnel\n") < md.index("\n## Send to career-ops\n")


def test_the_new_blocks_do_not_change_any_role_or_count(tmp_path):
    out = write(tmp_path, a_mixed_list())
    assert [r["tier"] for r in out["json"]["roles"]].count("A") == 2
    assert len(career_ops_lines(out["md"])) == 2
    assert "15 scanned, 13 passed filters, 2 A, 3 B, 8 C" in out["md"]


# ------------------------------------------------- the funnel file beside the ranked files

def funnel_file(tmp_path) -> str:
    return (tmp_path / "ranked" / f"funnel-{DATE}.md").read_text(encoding="utf-8")


def write_scan_meta(tmp_path, boards: list[dict]) -> None:
    folder = tmp_path / "scans" / DATE
    folder.mkdir(parents=True)
    (folder / "scan-meta.json").write_text(json.dumps({
        "schema_version": 1, "scan_date": DATE, "boards": boards, "totals": {},
        "closed_roles": []}), encoding="utf-8")


def test_the_funnel_file_names_every_count(tmp_path):
    write_scan_meta(tmp_path, [
        {"company": "Acme", "ats": "greenhouse", "slug": "acme", "status": "ok"},
        {"company": "Globex", "ats": "lever", "slug": "globex", "status": "ok"},
        {"company": "Initech | `x`", "ats": "lever", "slug": "initech", "status": "failed"},
        {"company": "Hooli", "ats": "ashby", "slug": "hooli", "status": "no_board"}])
    scored = a_mixed_list() + [rejected_role("x3", "region", "Location"),
                               rejected_role("x4", "pay", "Pay"), rejected_role("x5", "pay", "Pay"),
                               rejected_role("x6", "deny_terms", "Terms"),
                               rejected_role("x7", "function_review",
                                             "Other function, read from the description")]
    cfg = criteria()
    cfg_input = rank_input(scored, gate=5, merged=2, pasted=1)
    output.write_ranked(tmp_path, cfg_input, scored, cfg)
    text = funnel_file(tmp_path)
    funnel = output.build_funnel(cfg_input, scored, cfg)
    for line in (
        "# What the scan found",
        "- Boards read: 2",
        "- Boards that failed: Hooli (was not found), Initech \\| \\`x\\` (could not be reached)",
        f"- Postings fetched: {funnel['scanned']}",
        "- Postings you pasted: 1",
        "- Duplicates merged: 2",
        "- Title outside your approved families: 5",
        "- Pay: 2", "- Location: 2", "- Work arrangement: 1", "- Terms: 1",
        "- Hidden after reading the description: 1",
        f"- Passed your filters: {funnel['passed']}",
        "- Tier A: 2", "- Tier B: 3", "- Tier C: 8",
    ):
        assert line in text, line
    # every rejection is listed, not only the top three, most first then by label
    filtered = text.split("## What was filtered out")[1].split("## What is left")[0]
    assert [ln for ln in filtered.splitlines() if ln.startswith("- ")] == [
        "- Title outside your approved families: 5", "- Location: 2", "- Pay: 2",
        "- Terms: 1", "- Work arrangement: 1", "- Hidden after reading the description: 1"]
    # the funnel line is output.funnel_line verbatim, the same text the ranked file shows
    assert output.funnel_text(funnel) in text
    assert output.funnel_text(funnel) in (
        tmp_path / "ranked" / f"ranked-{DATE}.md").read_text(encoding="utf-8")
    assert chr(0x2014) not in text


def test_the_funnel_counts_add_up_to_the_ranked_funnel(tmp_path):
    scored = a_mixed_list() + [rejected_role("x4", "pay", "Pay"),
                               rejected_role("x6", "deny_terms", "Terms")]
    cfg = criteria()
    ranked_input = rank_input(scored, gate=4, merged=1, pasted=1)
    output.write_ranked(tmp_path, ranked_input, scored, cfg)
    funnel = output.build_funnel(ranked_input, scored, cfg)
    text = funnel_file(tmp_path)
    # no keyword list: every "- " line of the filtered section counts, so a dropped label shows
    filtered = text.split("## What was filtered out")[1].split("## What is left")[0]
    merged = int(next(ln for ln in text.splitlines() if ln.startswith("- Duplicates merged: "))
                 .rsplit(": ", 1)[1])
    passed = int(next(ln for ln in text.splitlines() if ln.startswith("- Passed your filters: "))
                 .rsplit(": ", 1)[1])
    listed = merged + passed + sum(int(ln.rsplit(": ", 1)[1]) for ln in filtered.splitlines()
                                   if ln.startswith("- "))
    assert listed == funnel["scanned"] + funnel["pasted"]  # the identity in docs/data-files.md


def test_a_pasted_only_workspace_says_no_boards_were_read(tmp_path):
    write_scan_meta(tmp_path, [])
    scored = [role("a1", 90)]
    output.write_ranked(tmp_path, rank_input(scored, scanned=0, pasted=1), scored, criteria())
    text = funnel_file(tmp_path)
    assert "No boards were read in this scan." in text
    assert "- Boards that failed" not in text and "- Postings you pasted: 1" in text


@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_a_damaged_scan_meta_reads_as_no_boards_and_no_closed_roles(tmp_path, body):
    folder = tmp_path / "scans" / DATE
    folder.mkdir(parents=True)
    (folder / "scan-meta.json").write_text(body, encoding="utf-8")
    out = write(tmp_path, [role("a1", 90)])
    assert "closed since your last scan" not in out["md"]
    assert out["json"]["closed_since_last"] == []
    assert "No boards were read in this scan." in funnel_file(tmp_path)


@pytest.mark.parametrize("url", [" https://example.com/a", "https://example.com/a ",
                                 "https://example.com/a\n",
                                 "https://example.com/a\u2028b"])
def test_apply_link_refuses_any_whitespace(url):
    assert output.apply_link(url).startswith("Apply link not available")


def test_apply_link_wraps_a_plain_https_url():
    assert output.apply_link("https://example.com/a") == "<https://example.com/a>"


def test_no_scan_meta_still_writes_a_funnel_file(tmp_path):
    output.write_ranked(tmp_path, rank_input([role("a1", 90)]), [role("a1", 90)], criteria())
    assert "No boards were read in this scan." in funnel_file(tmp_path)


def test_rank_lists_the_funnel_file_and_rewrites_the_same_bytes(env):  # noqa: F811
    run_cli(env, "scan")
    first = run_cli(env, "rank")
    path = env.folder / "ranked" / f"funnel-{DATE}.md"
    by_run = path.read_bytes()
    assert f"ranked/funnel-{DATE}.md" in first["wrote"]
    path.unlink()
    ranked = run_cli(env, "rank")
    assert f"ranked/funnel-{DATE}.md" in ranked["wrote"]
    assert path.read_bytes() == by_run


def test_the_funnel_file_does_not_change_any_ranked_file(tmp_path):
    scored = a_mixed_list()
    first = write(tmp_path, scored)
    (tmp_path / "ranked" / f"funnel-{DATE}.md").unlink()
    second = write(tmp_path, scored)
    assert first["md"] == second["md"] and first["json"] == second["json"]
