"""titles-apply: write the families the user approved into the scoring config.

Purpose: a family only drives the scan once it is in config/criteria.json, and only the user's
approval puts it there.
Logic: resolve the data folder, load the config, the latest evidence, titles/proposal.json,
titles/decisions.json and the interview answers, check the answers, run titles.apply_decisions,
then rebuild the whole config through criteria_build.build_criteria (the builder criteria-build
uses) with the approved families carried in, and save the result as a new config version through
criteria_store.save_new_version, which keeps the old version in the history. Approved and edited
families get origin stated when the user listed the title and discovered otherwise, with an
evidence block and the approval time. Every rung is set by code from the user's first choice and
the evidence counts, so a decision file never sets one; the rebuild also keeps title_fit
and the weights in step with the family set: title_fit is present only when the rungs differ.
Guardrails: nothing is written when the decisions or the answers have problems; they come back as
a list. A rung written in decisions.json is ignored, and a notice for it is added to the warnings
of every result. A result with no family at all is refused. The config is validated before it is
written, and the user's stated families are never removed. After a successful apply that
changed the config, titles/summary-<sample date>.md (summaries.render_titles: kept and dropped
families, each count as "N of M sampled postings") is written when its bytes differ.
An apply that changes nothing only refreshes a summary that already exists, so its wrote
list stays empty as tests/test_titles.py pins.
QA checklist: tests/test_titles.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import answers as answers_module
from jobsearch import clock, criteria_build, criteria_store, fsio, paths, summaries, titles, workspace
from jobsearch.commands import criteria_build as build_command
from jobsearch.models import Result, errors_result

NAME = "titles-apply"
HELP = "Write the job title families you approved into your scoring config."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    config = criteria_store.load_criteria(folder)
    evidence = titles.latest_evidence(folder)
    proposal = titles.read_agent_file(
        folder, "proposal.json", "Group the evidence titles into families first.")
    decisions = titles.read_agent_file(
        folder, "decisions.json", "Write the user's decisions there first.")
    answers = answers_module.load_answers(folder)
    resume = build_command.resume_text(folder)
    problems = answers_module.validate_answers(answers, resume)
    if problems:
        return errors_result(NAME, problems, "The interview answers have problems: ",
                             "Fix profile/answers.json, then run criteria-build again.", warnings)
    outcome = titles.apply_decisions(
        config, proposal, evidence, decisions, answers.get("target_titles", []),
        clock.iso_z(clock.now_utc()), first_choice_title=answers.get("first_choice_title"))
    warnings = [*warnings, *outcome.notices]
    if outcome.errors or outcome.config is None:
        return errors_result(NAME, outcome.errors, "The decisions have problems: ",
                             "Fix titles/decisions.json, then run titles-apply again.", warnings)
    # The same builder as criteria-build, with the approved families carried in, so title_fit
    # and the weights follow the full family set.
    rebuilt = criteria_build.build_criteria(answers, resume, outcome.config)
    created_from = criteria_store.created_from_after_edit(config)
    counts = {"approved": outcome.approved, "edited": outcome.edited,
              "rejected": outcome.rejected, "families": len(rebuilt["title"]["families"])}
    summary_text = summaries.render_titles(
        evidence, decisions, rebuilt, proposal, answers.get("first_choice_title"))
    summary_path = f"{paths.LAYOUT['titles']}/summary-{evidence['sample_date']}.md"
    if build_command.same_config(config, rebuilt):
        # An apply that changes nothing only refreshes a summary that already exists, so it
        # still writes nothing the user did not already have (wrote stays empty).
        wrote_summary = ((folder / summary_path).is_file()
                         and fsio.write_text_if_changed(folder / summary_path, summary_text))
        return Result(
            ok=True, command=NAME,
            data={"criteria_version": config["criteria_version"],
                  "created_from": config["created_from"], "unchanged": True},
            counts=counts, warnings=warnings, wrote=[summary_path] if wrote_summary else [],
            next="Nothing changed. Run scan to fetch the full set of open roles.")
    version = criteria_store.save_new_version(folder, rebuilt, created_from)
    wrote = [paths.LAYOUT["criteria"]]
    if fsio.write_text_if_changed(folder / summary_path, summary_text):
        wrote.append(summary_path)
    return Result(
        ok=True,
        command=NAME,
        data={"criteria_version": version, "created_from": created_from, "unchanged": False},
        counts=counts,
        wrote=wrote,
        warnings=warnings,
        next="Run scan to fetch the full set of open roles.",
    )
