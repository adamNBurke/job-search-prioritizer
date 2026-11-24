"""update-check: say in one line when a newer release of this plugin exists.

Purpose: the command behind the release note the start and scan skills may show.
Logic: resolve the data folder, then notices.update_notice reads the latest GitHub release
through the shared fetch layer and returns one line, or an empty notice. At most one request a
day. --turn-off and --turn-on write the user's switch to config/settings.json and make no request.
Guardrails: the check sends nothing about the user and every failure is silent.
QA checklist: tests/test_notices.py drives this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import notices, paths, workspace
from jobsearch.models import Result

NAME = "update-check"
HELP = "Say in one line when a newer release of this plugin exists."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)
    switch = parser.add_mutually_exclusive_group()
    switch.add_argument("--turn-off", action="store_true",
                        help="Stop checking for newer releases.")
    switch.add_argument("--turn-on", action="store_true",
                        help="Check for newer releases once a day again.")


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    if args.turn_off or args.turn_on:
        enabled = bool(args.turn_on)
        notices.save_settings(folder, enabled)
        next_step = ("Update notes are on. Run update-check with --turn-off to turn them off."
                     if enabled else
                     "Update notes are off. Run update-check with --turn-on to turn them back on.")
        return Result(ok=True, command=NAME, data={"update_check": enabled},
                      wrote=[paths.LAYOUT["settings"]], warnings=warnings, next=next_step)
    outcome = notices.update_notice(folder)
    return Result(
        ok=True,
        command=NAME,
        data={"notice": outcome.notice, "current": paths.plugin_version(),
              "latest": outcome.latest, "checked": outcome.checked},
        wrote=outcome.wrote,
        warnings=[*warnings, *outcome.warnings],
        next="Show the notice line to the user only when it is not empty.",
    )
