"""progress: show which step of the intake flow comes next.

Purpose: a returning user resumes at the right step; the start skill runs this first and follows
data.next_step instead of asking the user where they left off.
Logic: resolve the data folder, then progress.stage_of reads the files on disk and returns the
stage. The result data is {stage, step, steps_total, next_step}: stage is one of progress.STAGES,
step is its 1-based position, steps_total is len(STAGES), and next_step is one sentence that the
result's next field repeats.
Guardrails: read-only, so wrote is empty. A damaged file counts as its step not done and never
fails the command. No network.
QA checklist: tests/test_progress.py drives this through cli.main for one stage and for an unset
data folder.
"""

from __future__ import annotations

import argparse

from jobsearch import progress, workspace
from jobsearch.models import Result

NAME = "progress"
HELP = "Show which step comes next."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    stage = progress.stage_of(folder)
    step = progress.next_step(stage)
    return Result(
        ok=True, command=NAME,
        data={"stage": stage, "step": progress.STAGES.index(stage) + 1,
              "steps_total": len(progress.STAGES), "next_step": step},
        warnings=warnings, next=step,
    )
