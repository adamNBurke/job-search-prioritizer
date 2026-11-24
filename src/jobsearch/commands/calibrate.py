"""calibrate: save the tier cutoffs you chose as part of your scoring config.

Purpose: after criteria-test shows how many roles each tier would hold, this writes the accepted
A and B cutoffs so rank uses them.
Logic: resolve the data folder, score the latest scan, compute the proposal again so
config/calibration.json records it next to what was accepted, then calibrate.accept writes the
integers into the tiers as a new criteria_version and the calibration record. The proposal and
the tier counts use calibrate.passing_with_caps, so a role a tier cap holds at B counts as B,
the same as in the ranked files.
Guardrails: the cutoffs arrive as text and are checked in code, so a typo is a plain sentence and
changes nothing. An A cutoff not above the B cutoff, a value outside 1 to 100 and a non-integer
are refused before any file is written. No network.
QA checklist: tests/test_calibrate.py drives this through cli.main, including the refusals.
"""

from __future__ import annotations

import argparse

from jobsearch import calibrate, workspace
from jobsearch.models import Result

NAME = "calibrate"
HELP = "Save the A and B score cutoffs you chose."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    parser.add_argument("--a-min", required=True,
                        help="lowest score for tier A, a whole number from 1 to 100")
    parser.add_argument("--b-min", required=True,
                        help="lowest score for tier B, a whole number from 1 to 100")


def _whole_number(text: str, name: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise calibrate.CalibrationError(f"The {name} cutoff must be a whole number.") from None


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    a_min = _whole_number(args.a_min, "A")
    b_min = _whole_number(args.b_min, "B")
    criteria, rank_input, scored = calibrate.score_latest(folder)
    scores = calibrate.passing_with_caps(scored)
    version = calibrate.accept(
        folder, criteria, a_min, b_min, scan_date=rank_input.scan_date,
        proposal=calibrate.propose(scores))
    mix = calibrate.counts_at(scores, a_min, b_min)
    return Result(
        ok=True, command=NAME,
        data={"criteria_version": version, "a_min": a_min, "b_min": b_min,
              "scan_date": rank_input.scan_date},
        counts={"tier_a": mix["A"], "tier_b": mix["B"], "tier_c": mix["C"]},
        wrote=["config/calibration.json", "config/criteria.json"],
        warnings=warnings,
        next="Run rank to see your tiers.",
    )
