"""Find, check and confirm the data folder for a command.

Purpose: every command that touches the data folder starts the same way, so the steps live in
one place and a new command module never re-implements them.
Logic: require(command, args) finds the folder (the --data-dir argument, else the nearest
ancestor holding the marker), collects the safety warnings from paths.check_data_dir, and
returns (folder, warnings). It raises WorkspaceRefused when the folder is missing or was never
set up. add_folder_args() adds the two shared options, and --refresh for the commands that read
boards (require then adds the courtesy sentence to the warnings); optional_path() reads an empty
option as absent.
Guardrails: require never creates a folder; only init does. A failure is raised as
WorkspaceRefused carrying the failure Result, which cli.main prints, so stdout and the exit
code are the same as for any other failed command.
QA checklist: a new command calls add_folder_args in add_arguments and require first in run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jobsearch import paths
from jobsearch.models import Result

NO_FOLDER_NEXT = "Run init with --data-dir set to a folder you choose."


REFRESH_NOTE = "Refresh skips the cache. Please do not repeat it often; boards are shared."


def add_folder_args(parser: argparse.ArgumentParser, *, refresh: bool = False) -> None:
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--plugin-data", default=None)
    if refresh:
        parser.add_argument("--refresh", action="store_true")


def optional_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value)


class WorkspaceRefused(Exception):
    """The data folder is missing or not set up; .result is the failure to print."""

    def __init__(self, result: Result) -> None:
        super().__init__(result.error)
        self.result = result


def require(command: str, args: argparse.Namespace) -> tuple[Path, list[str]]:
    """Find, check and confirm the data folder. Returns (folder, warnings)."""
    folder = paths.find_data_dir(args.data_dir, Path.cwd())
    if folder is None:
        raise WorkspaceRefused(Result(ok=False, command=command, error="No data folder found.",
                                      next=NO_FOLDER_NEXT))
    warnings = paths.check_data_dir(folder, optional_path(args.plugin_data))
    if not (folder / paths.MARKER).is_file():
        raise WorkspaceRefused(Result(ok=False, command=command, warnings=warnings,
                                      error="That folder has not been set up yet.",
                                      next=NO_FOLDER_NEXT))
    if getattr(args, "refresh", False):
        warnings = [*warnings, REFRESH_NOTE]
    return folder, warnings
