"""criteria-validate: check the scoring config for mistakes before anything uses it.

Purpose: the user, or the model drafting for them, edits config/criteria.json; this command
says in plain English whether it is ready, and which fields to fix if not. A config
must pass before it is approved.
Logic: resolve the data folder, read config/criteria.json directly (criteria_store.load_criteria
would raise on the first problem; this command lists them all), run criteria.validate_criteria
and report. A valid config returns the counts of criteria, hard filters and title families. An
invalid one returns ok false, at most 10 errors (sorted, so the same file always reads the same
way), the total, and the next step.
Guardrails: read-only; it never writes, never touches the network and never changes the config.
A missing or unreadable file is a plain sentence with the next step, never a stack trace.
QA checklist: tests/test_criteria.py drives this through cli.main for the good, bad, missing and
unreadable cases.
"""

from __future__ import annotations

import argparse

from jobsearch import criteria, fsio, paths, workspace
from jobsearch.models import Result

NAME = "criteria-validate"
HELP = "Check the scoring config for mistakes."
MAX_SHOWN = 10
FIX_NEXT = "Fix the listed fields and run criteria-validate again."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    path = folder / paths.LAYOUT["criteria"]
    if not path.is_file():
        return Result(ok=False, command=NAME, warnings=warnings,
                      error="No scoring config yet. Run the interview first.",
                      next="Run the interview, then run criteria-validate.")
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        return Result(ok=False, command=NAME, warnings=warnings,
                      error="The scoring config is not readable JSON. Fix it or run the "
                            "interview again.",
                      next="Fix config/criteria.json by hand or run the interview again.")
    errors = criteria.validate_criteria(data)
    if errors:
        shown = errors[:MAX_SHOWN]
        note = "" if len(errors) <= MAX_SHOWN else f"; the first {MAX_SHOWN} are listed"
        return Result(
            ok=False,
            command=NAME,
            data={"errors": shown},
            counts={"errors": len(errors)},
            warnings=warnings,
            error=f"The scoring config has {len(errors)} problem"
                  f"{'' if len(errors) == 1 else 's'}{note}.",
            next=FIX_NEXT,
        )
    assert isinstance(data, dict)
    return Result(
        ok=True,
        command=NAME,
        data={"criteria_version": data["criteria_version"]},
        counts={
            "criteria": len(data["criteria"]),
            "hard_filters": len(data["hard_filters"]),
            "families": len(data["title"]["families"]),
        },
        warnings=warnings,
    )
