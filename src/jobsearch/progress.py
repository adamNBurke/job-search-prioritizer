"""Progress: which intake step comes next, read from the files in the data folder.

Purpose: a returning user must land back at the right step of the one-sitting flow, and
the answer must come from code reading files, never from the agent's memory of a past session.
Logic: STAGES names the stage a folder is in, which is the first step whose file is missing, in
the order resume, answers, criteria, title families, scan, calibration, ranking. stage_of walks
that order and returns the first missing one, or "ranked" when none is. next_step gives the
one-sentence instruction for a stage. Every check is "does the file exist and parse": nothing in
a file is executed, followed or trusted beyond its shape.
Guardrails: a file that exists but does not parse (hand-edited, half-written) counts as missing
for its stage and never raises. Calibration only counts when it was saved for the current
criteria_version, and a ranked file only counts when it is for the newest scan's date. No
network, no clock.
QA checklist: tests/test_progress.py has one seeded folder per stage; a new stage needs one plus
an entry in NEXT_STEPS.
"""

from __future__ import annotations

from pathlib import Path

from jobsearch import fsio, paths, roles_store

NEXT_STEPS: dict[str, str] = {
    "no_resume": "Ask for the resume file (PDF, DOCX or Markdown) and run profile-import "
                 "with --resume.",
    "no_answers": "Read profile/resume.md, run the intake interview, then write "
                  "profile/answers.json.",
    "no_criteria": "Run criteria-build to turn the interview answers into a scoring config.",
    "no_families": "Run sample, then titles-check and titles-apply, to settle which job "
                   "titles count.",
    "no_scan": "Run scan to read the job boards and keep the roles that match.",
    "uncalibrated": "Run criteria-test to preview the tiers, then calibrate with the A and "
                    "B cutoffs the user chooses.",
    "not_ranked": "Run rank to sort the latest scan into tiers.",
    "ranked": "Show the ranked list from the latest ranked file, or run scan again for fresh "
              "roles.",
}


STAGES = tuple(NEXT_STEPS)  # dict order is the stage order


def next_step(stage: str) -> str:
    return NEXT_STEPS[stage]


def _json_object(path: Path) -> dict | None:
    """The file as a JSON object, or None when it is missing, unreadable or not an object."""
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def _has_resume(folder: Path) -> bool:
    try:
        return bool(fsio.read_text(folder / paths.LAYOUT["resume_md"]).strip())
    except (OSError, ValueError):
        return False


def _families(criteria: dict) -> list:
    title = criteria.get("title")
    families = title.get("families") if isinstance(title, dict) else None
    return families if isinstance(families, list) else []


def _latest_scan_date(folder: Path) -> str | None:
    scan_dir = roles_store.latest_scan_dir(folder)
    if scan_dir is None:
        return None
    meta = _json_object(scan_dir / "scan-meta.json")
    return scan_dir.name if meta is not None else None


def stage_of(folder: Path) -> str:
    """The first intake step whose file is missing, or "ranked" when none is."""
    if not _has_resume(folder):
        return "no_resume"
    if _json_object(folder / paths.LAYOUT["answers"]) is None:
        return "no_answers"
    criteria = _json_object(folder / paths.LAYOUT["criteria"])
    if criteria is None or not isinstance(criteria.get("criteria_version"), str):
        return "no_criteria"
    if not _families(criteria):
        return "no_families"
    scan_date = _latest_scan_date(folder)
    if scan_date is None:
        return "no_scan"
    calibration = _json_object(folder / paths.LAYOUT["calibration"])
    if (calibration is None or not isinstance(calibration.get("accepted"), dict)
            or calibration.get("criteria_version") != criteria["criteria_version"]):
        return "uncalibrated"
    if _json_object(folder / paths.LAYOUT["ranked"] / f"ranked-{scan_date}.json") is None:
        return "not_ranked"
    return "ranked"
