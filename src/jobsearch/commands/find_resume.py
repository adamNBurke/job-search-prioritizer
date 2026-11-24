"""find-resume: list resume or CV file names in the usual folders, without opening any file.

Purpose: a user who does not know their resume's path can let the tool look by file name, and the
start skill shows the matches in a picker. The skill runs this only after the
user says yes to a search.
Logic: resolve the data folder, then resume_finder.find_resume_files lists up to 4 PDF or DOCX
files whose names read like a resume or CV in Desktop, Documents, Downloads and the Desktop and
Documents of every OneDrive folder (including "OneDrive - <Org>"). The result data is
{matches, counts: {found, unusable, truncated}}: matches are forward-slash path strings, newest
first, and truncated is true when a folder was too big to walk in full.
Guardrails: read-only, so wrote is empty. Only names are read, no file is opened, no link is
followed, and nothing leaves the machine. A path the skill cannot pass safely is counted in
counts.unusable and left out.
QA checklist: tests/test_find_resume.py drives this through cli.main with the home folder
pointed at a temp folder.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jobsearch import resume_finder, workspace
from jobsearch.models import Result

NAME = "find-resume"
HELP = "List resume or CV files by name in Desktop, Documents and Downloads, without opening them."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    listed, found, unusable, truncated = resume_finder.find_resume_files(Path.home())
    return Result(
        ok=True, command=NAME,
        data={"matches": [path.as_posix() for path in listed],
              "counts": {"found": found, "unusable": unusable, "truncated": truncated}},
        warnings=warnings,
        next="Show the matches in the picker and let the user choose one, or ask them to drag the file in.",
    )
