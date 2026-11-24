"""mark: record that a ranked role was right or wrong, with an optional note.

Purpose: the user-facing command behind the review skill. A mark is stored against the config
version that produced the recommendation, as evidence for the 1.1 learning step.
Logic: resolve the data folder, then marks.add_mark appends one line to feedback/marks.jsonl
using the tier, score and config version in the newest ranked file. The note is read from
--note-file, never from the command line.
Guardrails: a mark never changes a score, a tier or the scoring config. A role that is not in
the latest ranked file is refused by name.
QA checklist: tests/test_marks.py drives this through cli.main.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jobsearch import marks, workspace
from jobsearch.models import Result

NAME = "mark"
HELP = "Mark a ranked role right or wrong, with an optional note."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    parser.add_argument("--role", required=True)
    parser.add_argument("--verdict", required=True, choices=marks.VERDICTS)
    parser.add_argument("--note-file", default=None)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    note_path = Path(args.note_file) if args.note_file else None
    mark = marks.add_mark(folder, args.role, args.verdict, note_path)
    recorded, damaged = marks.scan_marks(folder)
    if damaged:
        warnings = [*warnings,
                    f"{damaged} damaged line(s) in feedback/marks.jsonl were skipped and left "
                    "as they are. Your new mark was saved."]
    return Result(
        ok=True,
        command=NAME,
        data={"role_id": mark.role_id, "verdict": mark.verdict,
              "criteria_version": mark.criteria_version, "tier_at_time": mark.tier_at_time},
        counts={"marks_total": len(recorded)},
        wrote=["feedback/marks.jsonl"],
        warnings=warnings,
        next="Mark another role, or run rank after your next scan.",
    )
