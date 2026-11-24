"""letter-start: set up the folder for a cover letter beside one ranked role.

Purpose: the first code step of the letter skill. It gives the skill one folder per role to put
the user's answers and the draft in, and a copy of the role and the posting text to ask about.
Logic: resolve the data folder, then letters.start_letter finds the role in the newest ranked
JSON, creates applications/<role key>/ and writes role.json and posting.txt. The folder name is
built in code from the role id, never by the skill.
Guardrails: a role that is not in the latest ranked file is refused by name and nothing is
created. An existing answers.md, draft.md or fixes.json is never touched.
QA checklist: tests/test_letter.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import letters, workspace
from jobsearch.models import Result

NAME = "letter-start"
HELP = "Set up the folder for a cover letter beside one ranked role."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    parser.add_argument("--role", required=True)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    found = letters.start_letter(folder, args.role)
    rel = found["folder"]
    if not found["posting_found"]:
        warnings = [*warnings, "The posting text was not found, so lines copied from the posting "
                    "cannot be checked for this role."]
    return Result(
        ok=True,
        command=NAME,
        data={"role_id": found["role_id"], "title": found["title"], "company": found["company"],
              "folder": rel, "has_answers": found["has_answers"],
              "has_draft": found["has_draft"]},
        wrote=[f"{rel}/{letters.ROLE_FILE}", f"{rel}/{letters.POSTING_FILE}"],
        warnings=warnings,
        next="Ask the user the letter questions and save their answers to answers.md in that folder.",
    )
