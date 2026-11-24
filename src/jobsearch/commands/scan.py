"""scan: read the selected public job boards and keep the roles in your title families.

Purpose: the user-facing command behind "scan the starter directory".
Logic: resolve the data folder, load and validate the scoring config, run scan.run_scan and
print one Result with counts: boards_ok, boards_failed, boards_not_found, fetched, kept,
duplicates_merged, new_roles, closed_roles, reopened_roles and from_cache. Boards are read
through the shared fetch layer, so a second run inside the cache window makes no network calls;
--refresh skips the cache and warns about it.
Guardrails: stdout holds counts and paths only; unreadable boards are one summary warning
plus at most five names, and a scan that reads no board at all fails without writing anything.
QA checklist: tests/test_pipeline_tracer.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import criteria_store, scan, workspace
from jobsearch.models import Result

NAME = "scan"
HELP = "Read the selected job boards and keep the roles that match your title families."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser, refresh=True)


def _notes_line(notes: list[str]) -> list[str]:
    if not notes:
        return []
    return [f"{len(notes)} postings listed more than one pay range; the widest range is shown."]


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    criteria = criteria_store.load_criteria(folder)
    outcome = scan.run_scan(folder, criteria, refresh=args.refresh)
    boards = [f.status for f in outcome.fetches]
    return Result(
        ok=True,
        command=NAME,
        data={"scan_date": outcome.scan_date, "criteria_version": criteria["criteria_version"]},
        counts={
            "boards": len(boards),
            "boards_ok": boards.count("ok"),
            "boards_failed": boards.count("failed"),
            "boards_not_found": boards.count("no_board"),
            "fetched": outcome.fetched_total,
            "kept": outcome.kept,
            "title_gate_dropped": outcome.title_gate_dropped,
            "duplicates_merged": outcome.duplicates_merged,
            "new_roles": len(outcome.new_roles),
            "closed_roles": len(outcome.closed_roles),
            "reopened_roles": len(outcome.reopened_roles),
            "from_cache": sum(1 for f in outcome.fetches if f.from_cache),
        },
        wrote=sorted(outcome.wrote),
        warnings=[*warnings, *outcome.warnings, *_notes_line(outcome.notes)],
        next="Run rank to rank the roles you just scanned.",
    )
