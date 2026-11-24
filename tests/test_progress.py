"""Progress names the next intake step from the files on disk.

Purpose: a returning user must land back at the right step of the one-sitting flow, so the stage
is derived by code from the data folder and never remembered or guessed by the agent.
Logic: each test seeds a folder with the smallest files that make one stage the first missing one
(stage_of only checks existence and that JSON parses, so the seeds are not full contract files)
and reads the stage back, directly and through cli.main("progress").
Guardrails: a file that exists but does not parse counts as missing for its stage and never
raises. No test touches the network or the wall clock.
QA checklist: a new stage needs a seeded test, an entry in NEXT_STEPS, and the SUMMARY shape
note for the progress command updated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import cli_result

from jobsearch import fsio, progress

SCAN_DATE = "2026-11-16"
VERSION = "1.3"


def put_resume(folder: Path) -> None:
    fsio.write_text_atomic(folder / "profile" / "resume.md", "## Experience\n- Did things\n")


def put_answers(folder: Path) -> None:
    fsio.write_json_atomic(folder / "profile" / "answers.json", {"schema_version": 1})


def put_criteria(folder: Path, families: list | None = None) -> None:
    fsio.write_json_atomic(folder / "config" / "criteria.json", {
        "criteria_version": VERSION,
        "title": {"families": [{"id": "tf-s-ops"}] if families is None else families},
    })


def put_scan(folder: Path, date: str = SCAN_DATE) -> None:
    fsio.write_json_atomic(folder / "scans" / date / "scan-meta.json", {"scan_date": date})


def put_calibration(folder: Path, version: str = VERSION) -> None:
    fsio.write_json_atomic(folder / "config" / "calibration.json", {
        "criteria_version": version, "accepted": {"a_min": 80, "b_min": 60}})


def put_ranked(folder: Path, date: str = SCAN_DATE) -> None:
    fsio.write_json_atomic(folder / "ranked" / f"ranked-{date}.json", {"scan_date": date})


def seed_through(folder: Path, last: str) -> None:
    """Seed every step up to and including the named one."""
    steps = [("resume", put_resume), ("answers", put_answers), ("criteria", put_criteria),
             ("scan", put_scan), ("calibration", put_calibration), ("ranked", put_ranked)]
    for name, put in steps:
        put(folder)
        if name == last:
            return
    raise AssertionError(last)


def test_stages_run_in_the_order_of_the_flow():
    assert progress.STAGES == (
        "no_resume", "no_answers", "no_criteria", "no_families", "no_scan",
        "uncalibrated", "not_ranked", "ranked")


def test_stage_no_resume_on_a_fresh_folder(folder):
    assert progress.stage_of(folder) == "no_resume"


def test_stage_no_answers_once_the_resume_is_imported(folder):
    put_resume(folder)
    assert progress.stage_of(folder) == "no_answers"


def test_stage_no_criteria_once_the_answers_are_written(folder):
    seed_through(folder, "answers")
    assert progress.stage_of(folder) == "no_criteria"


def test_stage_no_families_when_the_config_has_no_title_family(folder):
    seed_through(folder, "answers")
    put_criteria(folder, families=[])
    assert progress.stage_of(folder) == "no_families"


def test_stage_no_scan_once_a_family_exists(folder):
    seed_through(folder, "criteria")
    assert progress.stage_of(folder) == "no_scan"


def test_stage_uncalibrated_after_the_first_scan(folder):
    seed_through(folder, "scan")
    assert progress.stage_of(folder) == "uncalibrated"


def test_stage_uncalibrated_when_calibration_belongs_to_an_older_config(folder):
    seed_through(folder, "scan")
    put_calibration(folder, version="1.2")
    assert progress.stage_of(folder) == "uncalibrated"


def test_stage_not_ranked_after_calibration(folder):
    seed_through(folder, "calibration")
    assert progress.stage_of(folder) == "not_ranked"


def test_stage_not_ranked_when_the_ranked_file_is_for_an_older_scan(folder):
    seed_through(folder, "calibration")
    put_ranked(folder, date="2026-11-09")
    assert progress.stage_of(folder) == "not_ranked"


def test_stage_ranked_when_everything_is_done(folder):
    seed_through(folder, "ranked")
    assert progress.stage_of(folder) == "ranked"


def test_the_newest_scan_decides_which_ranked_file_counts(folder):
    seed_through(folder, "ranked")
    put_scan(folder, date="2026-11-23")
    assert progress.stage_of(folder) == "not_ranked"


@pytest.mark.parametrize("target, garbage", [
    ("profile/answers.json", "{not json"),
    ("profile/answers.json", "[1, 2]"),
    ("config/criteria.json", "{not json"),
    ("config/criteria.json", "[]"),
    ("config/calibration.json", "{not json"),
    ("ranked/ranked-2026-11-16.json", "{not json"),
    ("scans/2026-11-16/scan-meta.json", "{not json"),
])
def test_an_unparseable_file_counts_as_its_step_not_done_and_never_raises(
        folder, target, garbage):
    seed_through(folder, "ranked")
    path = folder / target
    path.write_text(garbage, encoding="utf-8")
    expected = {
        "profile/answers.json": "no_answers",
        "config/criteria.json": "no_criteria",
        "scans/2026-11-16/scan-meta.json": "no_scan",
        "config/calibration.json": "uncalibrated",
        "ranked/ranked-2026-11-16.json": "not_ranked",
    }[target]
    assert progress.stage_of(folder) == expected


def test_an_empty_resume_counts_as_no_resume(folder):
    fsio.write_text_atomic(folder / "profile" / "resume.md", "\n")
    assert progress.stage_of(folder) == "no_resume"


def test_every_stage_has_one_sentence_next_step():
    for stage in progress.STAGES:
        step = progress.next_step(stage)
        assert step.endswith(".") and step.count(". ") == 0, stage


def test_progress_command_reports_the_stage_and_its_next_step(folder, capsys):
    put_resume(folder)
    code, result = cli_result(capsys, "progress", "--data-dir", str(folder))
    assert code == 0 and result["ok"] is True
    assert result["command"] == "progress"
    assert result["data"]["stage"] == "no_answers"
    assert result["data"]["step"] == 2 and result["data"]["steps_total"] == 8
    assert result["data"]["next_step"] == progress.next_step("no_answers")
    assert result["next"] == result["data"]["next_step"]
    assert result["wrote"] == []


def test_progress_command_on_an_unset_folder_says_to_set_one_up(tmp_path, capsys):
    code, result = cli_result(capsys, "progress", "--data-dir", str(tmp_path / "missing"))
    assert code != 0 and result["ok"] is False
