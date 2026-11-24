"""Command line entry. Every command prints one Result as JSON.

init and status live here; every other command is a module in jobsearch.commands,
discovered at startup, so adding a command never edits this file.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import traceback

from jobsearch import commands, errors, fetch, fsio, paths, workspace
from jobsearch.models import Result
from jobsearch.netguard import UrlRefused


_COMMANDS = commands.discover()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobsearch",
        description="Rank open jobs against your own criteria.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create the data folder.")
    init.add_argument("--data-dir", required=True)
    init.add_argument("--plugin-data", default=None)

    status = sub.add_parser("status", help="Check the data folder and the runtime.")
    workspace.add_folder_args(status)

    for module in _COMMANDS:
        module_parser = sub.add_parser(module.NAME, help=module.HELP)
        module.add_arguments(module_parser)
    return parser


def _run_init(args: argparse.Namespace) -> Result:
    target = paths.parse_data_dir_arg(args.data_dir)
    outcome = paths.init_data_dir(target, workspace.optional_path(args.plugin_data), paths.plugin_version())
    return Result(
        ok=True,
        command="init",
        data={"data_dir": str(paths.normalize_path(target))},
        wrote=outcome.wrote,
        warnings=outcome.warnings,
        next="Run progress to see where you left off.",
    )


def _run_status(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require("status", args)
    marker = fsio.read_json(folder / paths.MARKER)
    schema = marker.get("workspace_schema") if isinstance(marker, dict) else None
    entries = sum(1 for _ in paths.cache_http_dir(folder).rglob("*.json.gz"))
    return Result(
        ok=True,
        command="status",
        data={
            "data_dir": str(folder),
            "plugin_version": paths.plugin_version(),
            "python": platform.python_version(),
            "platform": sys.platform,
            "workspace_schema": schema,
        },
        counts={"cache_entries": entries},
        warnings=warnings,
        next="The data folder is ready.",
    )


_HANDLERS = {
    "init": _run_init,
    "status": _run_status,
    **{module.NAME: module.run for module in _COMMANDS},
}


def _os_error_sentence(exc: OSError) -> str:
    """A file-system failure as a sentence that names the path and the next step."""
    reason = exc.strerror or type(exc).__name__
    where = f"The file or folder {exc.filename}" if exc.filename else "A file or folder"
    return (f"{where} could not be read or written ({reason}). Check that it exists, that the "
            "data folder is writable and has free space, and that no other program has it "
            "open, then run the command again.")


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    command = args.command
    try:
        result = _HANDLERS[command](args)
    except workspace.WorkspaceRefused as exc:
        result = exc.result
    except (paths.DataDirRefused, UrlRefused, fetch.FetchError, errors.UserError) as exc:
        result = Result(ok=False, command=command, error=str(exc))
    except OSError as exc:
        result = Result(ok=False, command=command, error=_os_error_sentence(exc))
    except json.JSONDecodeError as exc:
        result = Result(
            ok=False, command=command,
            error=(f"A file in your data folder is not valid JSON (line {exc.lineno}, column "
                   f"{exc.colno}). If you edited it by hand, fix that line or move the file "
                   "out of the folder, then run the command again."))
    except Exception as exc:
        if os.environ.get("JSP_DEBUG") == "1":
            traceback.print_exc()
        result = Result(ok=False, command=command, error=f"internal error: {type(exc).__name__}")
    print(result.to_json())
    return 0 if result.ok else 1
