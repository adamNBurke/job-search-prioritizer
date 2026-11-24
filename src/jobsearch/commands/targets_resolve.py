"""targets-resolve: find the public job board of each company the user wants scanned.

Purpose: the user-facing command behind "add my own target companies". It turns the names in
config/targets.json into boards the next scan reads. It is the path jobsearch paste points to
when a whole job-board link is pasted ("Add the company as a target instead").
Logic: resolve the data folder, run targets.resolve_targets, and print one Result with the counts
(resolved, needs_confirmation, unresolved, rejected), the company names that need the user's yes,
the companies that could not be resolved with a reason each, and the next step. Boards found are
written to config/boards.local.json.
Guardrails: at most 10 names are listed (companies that need a yes first); the rest are in
config/boards.local.json. Only company names the user typed are printed. Every request is an
anonymous GET to a public job-board API through the shared fetch layer.
QA checklist: tests/test_targets.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import targets, workspace
from jobsearch.models import Result

NAME = "targets-resolve"
HELP = "Add your target companies: find each one's public job board for the next scan."
_SHOWN = 10
_EMPTY_NEXT = ("Add the companies you want scanned to config/targets.json, then run "
               "targets-resolve again.")
_CONFIRM_NEXT = ("Ask the user to confirm each needs_confirmation board, record confirmed true "
                 "or false in config/targets.json, and run targets-resolve again.")
_SCAN_NEXT = "Run scan to read the resolved boards."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    outcome = targets.resolve_targets(folder)
    counts = {"resolved": outcome.resolved, "needs_confirmation": outcome.needs_confirmation,
              "unresolved": outcome.unresolved, "rejected": outcome.rejected}
    data: dict[str, object] = {}
    if outcome.confirm_names:
        data["needs_confirmation"] = outcome.confirm_names[:_SHOWN]
    room = _SHOWN - len(outcome.confirm_names[:_SHOWN])
    if outcome.unresolved_lines and room > 0:
        data["unresolved"] = outcome.unresolved_lines[:room]
    if outcome.total == 0:
        next_step = _EMPTY_NEXT
    elif outcome.confirm_names:
        next_step = _CONFIRM_NEXT
    else:
        next_step = _SCAN_NEXT
    return Result(ok=True, command=NAME, data=data, counts=counts, wrote=outcome.wrote,
                  warnings=[*warnings, *outcome.warnings], next=next_step)
