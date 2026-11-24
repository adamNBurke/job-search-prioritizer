"""sample: read a stratified sample of real postings and count which titles ask for your skills.

Purpose: users under-name the titles their skills fit, so discovery starts from a counted sample
of the market instead of from the model's guess.
Logic: resolve the data folder, load the interview answers, pick up to 40 boards across the
industry and size strata of the whole starter directory (not only the user's industries),
read them through scan.fetch_boards so a later full scan hits the same cache, keep up to 30
postings per board and write titles/sample-<date>.jsonl and titles/evidence-<date>.json. Boards
that cannot be read are skipped with one warning.
Guardrails: with no skills in the answers, or no readable posting, nothing is written and no
count is shown. Stdout holds counts and paths only. Boards are read politely through the shared
fetch layer; --refresh skips the cache and says so.
QA checklist: tests/test_titles.py drives this through cli.main with a scripted transport.
"""

from __future__ import annotations

import argparse

from jobsearch import answers as answers_module
from jobsearch import clock, directory, scan, titles, workspace
from jobsearch.models import Result

NAME = "sample"
HELP = "Read a sample of real postings and count which job titles ask for your skills."
_MAX_FAILURE_LINES = 5


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser, refresh=True)


def _failure_warnings(fetches: list[scan.BoardFetch]) -> list[str]:
    failed = [f for f in fetches if f.status != "ok"]
    if not failed:
        return []
    noun = "board" if len(failed) == 1 else "boards"
    lines = [f"Skipped {len(failed)} {noun} that could not be read."]
    lines += [f"{f.entry.company}: {f.error or 'no board was found at that address.'}"
              for f in failed[:_MAX_FAILURE_LINES]]
    return lines


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    answers = answers_module.load_answers(folder)
    matchers = titles.skill_matchers(answers)
    if not matchers:
        return Result(ok=False, command=NAME, warnings=warnings,
                      error="Your interview answers list no skills, so there is nothing to "
                            "count. Add skills in the interview, then try again.",
                      next="Finish the interview, run criteria-build, then run sample again.")
    entries = titles.stratified_boards(directory.load_directory())
    fetches = scan.fetch_boards(folder, entries, refresh=args.refresh)
    rows = titles.sample_rows(fetches, matchers)
    warnings = [*warnings, *_failure_warnings(fetches)]
    if not rows:
        return Result(ok=False, command=NAME, warnings=warnings,
                      error="No postings could be read from the sampled job boards, so there "
                            "are no counts to show.",
                      next="Check your internet connection and run sample again.")
    evidence = titles.build_evidence(
        rows, matchers, clock.today_iso(), titles.target_keys(answers.get("target_titles", [])))
    wrote = titles.write_sample(folder, rows, evidence)
    return Result(
        ok=True,
        command=NAME,
        data={"sample_date": evidence["sample_date"]},
        counts={"boards_sampled": evidence["boards_sampled"],
                "postings_sampled": evidence["postings_sampled"],
                "titles": len(evidence["titles"])},
        wrote=wrote,
        warnings=warnings,
        next="Group the evidence titles into at most 8 families in titles/proposal.json, "
             "then run titles-check.",
    )
