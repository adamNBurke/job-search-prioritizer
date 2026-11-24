"""paste: add a role the directory misses, from a text file or a link.

Purpose: the user-facing command behind "I found this job myself, rank it too".
Logic: resolve the data folder, then either read --text-file (Company and Title lines, a blank
line, then the posting text) or read --url (a Greenhouse, Lever or Ashby posting link through the
job-board API, any other https page through the user-URL fetch path). One Posting is stored in
roles/pasted.jsonl and one Result prints the role_id, company, title and source. The next rank run
scores it like any scanned role.
Guardrails: the posting text is untrusted. It is stored as data and never printed, so stdout
holds only the id, the company and the title. A pasted file outside the data folder is
refused, and a LinkedIn link is refused with a request for the text (no request is made).
QA checklist: tests/test_paste.py drives this through cli.main.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jobsearch import clock, output, paste, roles_store, workspace
from jobsearch.models import Result

NAME = "paste"
HELP = "Add a job you found yourself, from a text file or a link, to the ranking."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text-file")
    source.add_argument("--url")


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    today = clock.today_iso()
    if args.url is not None:
        posting, notes = paste.paste_url(folder, args.url, today)
    else:
        parsed = paste.parse_paste_file(Path(args.text_file), folder)
        posting, notes = paste.posting_from_text(
            parsed.company, parsed.title, parsed.location, parsed.link, parsed.text, today)
    paste.save_pasted(folder, posting, today)
    if roles_store.latest_scan_dir(folder) is None:
        notes = [*notes, "No scan yet. Run scan first; rank scores pasted roles with the scan."]
    return Result(
        ok=True,
        command=NAME,
        data={"role_id": posting.role_id, "company": output.shown(posting.company),
              "title": output.shown(posting.title), "source": posting.source},
        wrote=["roles/pasted.jsonl"],
        warnings=[*warnings, *notes],
        next="Run rank to see where it lands.",
    )
