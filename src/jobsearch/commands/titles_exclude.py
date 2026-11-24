"""titles-exclude: write the words the user approved for ruling out other functions.

Purpose: a title that matches a family can still belong to another function, such as a legal
operations manager. The user approves, per title family, plain words that mark such titles, and
rank then rejects a title that carries one of its own family's words.
Logic: resolve the data folder, load the config and titles/exclusions.json, run
titles.apply_exclusions, and save the result as a new config version through
criteria_store.save_new_version, which keeps the old version in the history. The file shape is
{"families": [{"id": "<family id>", "exclude": ["word", ...]}]}. A family the file does not name
keeps its words, and an empty list clears them. It edits only the exclude lists: no weight, rung
or term changes.
Guardrails: nothing is written when the file has a problem; the problems come back as a list
that names each path, such as families[0].exclude[1]. The agent writes only words the user
approved and this command never invents one. A word is matched as escaped whole words, never as
a pattern, and a word that matches one of the family's own terms is refused. An unchanged file
makes no new version.
QA checklist: tests/test_titles.py drives this through cli.main (sorted words, no new version
when unchanged, unnamed families kept, an empty list clears, each refusal writes nothing).
"""

from __future__ import annotations

import argparse

from jobsearch import criteria_store, paths, titles, workspace
from jobsearch.models import Result, errors_result

NAME = "titles-exclude"
HELP = "Rule out other functions in a title family with words the user approved."
FILE = f"{paths.LAYOUT['titles']}/exclusions.json"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    config = criteria_store.load_criteria(folder)
    data = titles.read_agent_file(
        folder, "exclusions.json", "Write the words the user approved there first.")
    outcome = titles.apply_exclusions(config, data)
    if outcome.errors or outcome.config is None:
        return errors_result(NAME, outcome.errors, "The exclusion words have problems: ",
                             f"Fix {FILE}, then run titles-exclude again.", warnings)
    counts = {"families_with_words": outcome.families_with_words, "words": outcome.words}
    if outcome.unchanged:
        return Result(
            ok=True, command=NAME,
            data={"criteria_version": config["criteria_version"],
                  "created_from": config["created_from"], "unchanged": True},
            counts=counts, warnings=warnings,
            next="Nothing changed. Run scan to fetch the full set of open roles.")
    created_from = criteria_store.created_from_after_edit(config)
    version = criteria_store.save_new_version(folder, outcome.config, created_from)
    return Result(
        ok=True, command=NAME,
        data={"criteria_version": version, "created_from": created_from, "unchanged": False},
        counts=counts, wrote=[paths.LAYOUT["criteria"]], warnings=warnings,
        next="Run scan to fetch the full set of open roles.",
    )
