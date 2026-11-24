"""rank: score the latest scan against your criteria and write the ranked files.

Purpose: the user-facing command behind "rank my roles", writing ranked/ranked-<scan-date>.md,
.json and .csv with the reason first, the funnel and a link to apply.
Logic: resolve the data folder, load and validate the scoring config, load the latest scan with
roles_store.load_rankable, score every role with scoring.score_all and write the files with
output.write_ranked. Ages are measured to the scan date, so ranking again gives identical bytes.
After the files are written, the once-ever win invite comes back as data.notice, stamped in
config/notices.json first (notices.feedback_invite).
It warns when config/calibration.json is missing or was made for another criteria_version, since
the tier cutoffs are then not yet set from the user's own results.
Guardrails: ranking never touches the network and never changes the scan; with no scan it fails
with the next step.
QA checklist: tests/test_pipeline_tracer.py and tests/test_output.py drive this through cli.main;
test_rank_carries_the_win_invite_once_as_data_notice checks the invite.
"""

from __future__ import annotations

import argparse
from datetime import date

from jobsearch import (criteria_store, fsio, notices, output, paths, roles_store, scoring,
                       workspace)
from jobsearch.models import Result

NAME = "rank"
HELP = "Rank the latest scan into tiers, with the reason first and a link to apply."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


UNCALIBRATED = ("Tier cutoffs have not been set from your own results yet. Run criteria-test, "
                "then calibrate.")


def _calibrated(folder, criteria: dict) -> bool:
    path = folder / paths.LAYOUT["calibration"]
    if not path.is_file():
        return False
    try:
        record = fsio.read_json(path)
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("criteria_version") == criteria[
        "criteria_version"]


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    criteria = criteria_store.load_criteria(folder)
    rank_input = roles_store.load_rankable(folder)
    scored = scoring.score_all(
        rank_input.facts, criteria, date.fromisoformat(rank_input.scan_date))
    wrote = output.write_ranked(folder, rank_input, scored, criteria)
    invite = notices.feedback_invite(folder)  # after the write: a ranked file exists now
    funnel = output.build_funnel(rank_input, scored, criteria)
    tiers = funnel["tiers"]
    counts = {
        "scanned": funnel["scanned"], "pasted": funnel["pasted"],
        "duplicates_merged": funnel["duplicates_merged"], "passed": funnel["passed"],
        "rejected": funnel["rejected_by_filters"], "tier_a": tiers["A"], "tier_b": tiers["B"],
        "tier_c": tiers["C"], "closed": len(output.closed_roles(folder, rank_input.scan_date)),
    }
    warnings = [*warnings, *invite.warnings]
    if not _calibrated(folder, criteria):
        warnings = [*warnings, UNCALIBRATED]
    return Result(
        ok=True,
        command=NAME,
        data={"scan_date": rank_input.scan_date, "criteria_version": criteria["criteria_version"],
              "funnel_line": output.funnel_text(funnel),
              "notice": invite.notice},
        counts=counts,
        wrote=sorted([*wrote, *invite.wrote]),
        warnings=warnings,
        next=(f"Open {paths.LAYOUT['ranked']}/ranked-{rank_input.scan_date}.md and start "
              "with the A roles."),
    )
