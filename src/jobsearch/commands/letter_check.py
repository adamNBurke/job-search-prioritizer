"""letter-check: check a draft letter is mostly in the user's own words, flag the tone, save it.

Purpose: the second code step of the letter skill. It is what makes "at least 80% of the letter
body is the user's own words" a fact the code enforces, not a promise the model keeps.
Logic: resolve the data folder, then letters.check_letter reads draft.md and answers.md from
applications/<role key>/, counts the body words that are not the user's, runs the tone gate,
writes check.md, and writes letter.md only when those words are at most 20% of the body.
Guardrails: a draft over the limit returns ok false, names the share and check.md, and writes no
letter. The tone gate only advises. The sentences themselves are in check.md, which the skill
quotes. The counts are changed_words and body_words, tone_hits and borrowed.
QA checklist: tests/test_letter.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import letters, workspace
from jobsearch.models import Result

NAME = "letter-check"
HELP = "Check a draft letter is in the user's own words, flag machine-sounding lines, and save it."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    parser.add_argument("--role", required=True)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    checked = letters.check_letter(folder, args.role)
    counts = {"changed_words": checked.changed_words, "body_words": checked.body_words,
              "tone_hits": len(checked.hits), "borrowed": len(checked.borrowed)}
    if not checked.saved:
        return Result(
            ok=False,
            command=NAME,
            data={"folder": checked.folder, "saved": False},
            counts=counts,
            wrote=checked.wrote,
            warnings=warnings,
            error=(f"{checked.share}% of the letter body is not in the user's own words, and the "
                   f"most allowed is {letters.EDIT_SHARE_MAX}%. The sentences are listed in "
                   f"{checked.folder}/{letters.CHECK_FILE}."),
            next=("Bring the listed sentences back to the user's words, or ask the user for those "
                  "thoughts and add the answers to answers.md, then cut the draft again and rerun "
                  "letter-check."),
        )
    data: dict[str, object] = {"folder": checked.folder, "saved": True,
                               "letter": f"{checked.folder}/{letters.LETTER_FILE}"}
    if checked.kept_earlier:
        data["kept_earlier"] = f"{checked.folder}/{checked.kept_earlier}"
    return Result(
        ok=True,
        command=NAME,
        data=data,
        counts=counts,
        wrote=checked.wrote,
        warnings=warnings,
        next=(f"Show the user {checked.folder}/{letters.CHECK_FILE} word for word, then tell them "
              "where letter.md is."),
    )
