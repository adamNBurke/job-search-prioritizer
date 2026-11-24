"""function-review: list the A and B roles to check against the user's function, and check verdicts.

Purpose: the title words alone cannot catch a role whose title is the user's own target title or
whose function shows only in the description. The agent reads those descriptions and may hide a
role by writing a reject verdict; this command writes the worklist it reads and checks what it
wrote.
Logic: resolve the data folder, check roles/function-verdicts.json (verdicts.py), then score the
latest scan with every verdict applied (calibrate.score_latest) and list each passing role whose
shown tier is A or B (output.role_tier at the config's current cutoffs) and that has no verdict
yet, sorted by tier, score descending, then role_id. roles/function-worklist.json holds role_id,
company, title, team, family, tier, score and description (cleaned, whitespace collapsed, at most
3000 characters, null when the posting has no text). The counts are to_review (roles listed),
hidden (roles a reject verdict hid) and kept (reviewed roles that stay). The agent writes
verdicts, runs this command again, and stops when to_review is 0.
Guardrails: a refused verdict file writes no worklist and hides no role; the errors come back with
their paths. The worklist holds posting text that is already in the scan folder, and it stays in
the user's data folder. No timestamp is written, so the same inputs give the same bytes. The
description is attacker-controlled text: clean_text strips hidden and control characters, and the
skill tells the agent to treat it as data. Cutoffs are not proposed again after a review.
QA checklist: tests/test_function_review.py drives this through cli.main (the listing, every bad
file, the cleaned and bounded text, a reviewed role and a C role never listed).
"""

from __future__ import annotations

import argparse

from jobsearch import calibrate, fsio, output, paths, verdicts, workspace
from jobsearch.models import Result, errors_result

NAME = "function-review"
HELP = ("List the A and B roles to check against the job function the user wants, "
        "and check the verdicts written so far.")
DESCRIPTION_MAX = 3000
_SORT_TIER = {"A": 0, "B": 1}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def _line(text: str) -> str:
    return " ".join(output.clean_text(text).split())


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    problems = verdicts.file_problems(folder)
    if problems:
        return errors_result(NAME, problems, f"{verdicts.FILE} has problems: ",
                             f"Fix {verdicts.FILE}, then run function-review again.", warnings)
    _rejected, reviewed = verdicts.load_verdicts(folder)
    criteria, rank_input, scored = calibrate.score_latest(folder)
    postings = {fact.posting.role_id: fact.posting for fact in rank_input.facts}
    passing = [s for s in scored if s.rejected is None]
    hidden = sum(1 for s in scored
                 if s.rejected is not None and s.rejected.filter_id == "function_review")
    kept = sum(1 for s in passing if s.role_id in reviewed)
    shown = [(output.role_tier(s, criteria), s) for s in passing if s.role_id not in reviewed]
    queue = sorted(
        ((tier, s) for tier, s in shown if tier in _SORT_TIER),
        key=lambda pair: (_SORT_TIER[pair[0]], -pair[1].score, pair[1].role_id))
    items = []
    for tier, role in queue:
        posting = postings[role.role_id]
        description = _line(posting.description_text)[:DESCRIPTION_MAX].strip()
        items.append({
            "role_id": role.role_id,
            "company": _line(role.company),
            "title": _line(role.title),
            "team": (_line(posting.team) or None) if posting.team else None,
            "family": _line(role.family_label) if role.family_label else None,
            "tier": tier,
            "score": role.score,
            "description": description or None,
        })
    fsio.write_json_atomic(folder / paths.LAYOUT["function_worklist"], {
        "schema_version": 1, "scan_date": rank_input.scan_date,
        "criteria_version": criteria["criteria_version"], "roles": items,
    })
    return Result(
        ok=True, command=NAME,
        data={"scan_date": rank_input.scan_date, "criteria_version": criteria["criteria_version"]},
        counts={"to_review": len(items), "hidden": hidden, "kept": kept},
        wrote=[paths.LAYOUT["function_worklist"]], warnings=warnings,
        next=(f"Read {paths.LAYOUT['function_worklist']}, write a verdict for each role in "
              f"{paths.LAYOUT['function_verdicts']}, then run function-review again."
              if items else "Run rank."),
    )
