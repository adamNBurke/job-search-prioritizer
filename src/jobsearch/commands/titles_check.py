"""titles-check: show the proposed title families with counts the code computed.

Purpose: the model groups sampled titles into families, but the user must see real numbers, so
this command reads the agent's proposal against the evidence and renders the counted view.
Logic: resolve the data folder, load the latest titles/evidence-<date>.json, titles/proposal.json
and the interview answers, run titles.check_proposal and write titles/proposal-view.md: the
coverage note first, then each family with "N of M sampled postings", examples, whether the user
listed it and its terms, then any title the user listed that no family covers.
Guardrails: nothing is written when the proposal has problems; they come back as a list with the
JSON path to fix. A proposal that carries a count is refused. Details go to the view file so
stdout stays short.
QA checklist: tests/test_titles.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import answers as answers_module
from jobsearch import fsio, paths, titles, workspace
from jobsearch.models import Result, errors_result

NAME = "titles-check"
HELP = "Show the proposed job title families with real counts from the sample."
VIEW = f"{paths.LAYOUT['titles']}/proposal-view.md"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    evidence = titles.latest_evidence(folder)
    proposal = titles.read_agent_file(
        folder, "proposal.json", "Group the evidence titles into families first.")
    answers = answers_module.load_answers(folder)
    check = titles.check_proposal(proposal, evidence, answers.get("target_titles", []))
    if check.errors:
        return errors_result(NAME, check.errors, "The family proposal has problems: ",
                             "Fix titles/proposal.json, then run titles-check again.", warnings)
    fsio.write_text_atomic(folder / VIEW, check.text)
    return Result(
        ok=True,
        command=NAME,
        data={"view": VIEW, "sample_date": evidence["sample_date"]},
        counts={"families": len(check.families),
                "listed_titles_not_covered": len(check.uncovered),
                "postings_sampled": evidence["postings_sampled"]},
        wrote=[VIEW],
        warnings=warnings,
        next="Ask the user to approve, reject or edit each family, write "
             "titles/decisions.json, then run titles-apply.",
    )
