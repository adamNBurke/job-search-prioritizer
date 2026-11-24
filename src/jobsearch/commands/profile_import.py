"""profile-import: read your resume into your profile folder.

Purpose: the interview works from one Markdown copy of the resume, and the original is kept
beside it, so nothing the user supplied is lost or rewritten.
Logic: resolve the data folder, then intake.import_resume reads the file named by --resume
(PDF, DOCX or Markdown), writes profile/resume.md and copies the original to profile/source.
The result reports the format and simple counts so the agent can tell the user what was read.
Guardrails: the file is read, never executed or sent anywhere; no network. A missing file, an
unsupported type or an empty file is a plain sentence with the next step, and writes nothing.
QA checklist: tests/test_criteria_build.py drives this through cli.main for each outcome.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jobsearch import intake, paths, workspace
from jobsearch.models import Result

NAME = "profile-import"
HELP = "Read your resume into your profile folder."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    parser.add_argument("--resume", required=True,
                        help="path to your resume (PDF, DOCX or Markdown)")


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    source = Path(args.resume).expanduser()
    outcome = intake.import_resume(source, folder)
    return Result(
        ok=True,
        command=NAME,
        data={"format": outcome.format, "chars": outcome.chars, "pages": outcome.pages,
              "headings": outcome.headings, "bullets": outcome.bullets},
        wrote=[paths.LAYOUT["resume_md"], f"{paths.LAYOUT['profile_source']}/{source.name}"],
        warnings=[*warnings, *outcome.warnings],
        next="Read profile/resume.md, run the interview, then write profile/answers.json "
             "and run criteria-build.",
    )
