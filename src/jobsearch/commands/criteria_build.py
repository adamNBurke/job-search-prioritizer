"""criteria-build: turn your interview answers into your scoring config and scan scope.

Purpose: after the interview, profile/answers.json holds what the user said. This command
builds config/criteria.json (hard filters, weights, title families, keywords) and
config/scan-scope.json from it with fixed arithmetic, so the same answers give the same config
and a re-run keeps the previous version.
Logic: resolve the data folder, load and check the answers against profile/resume.md, build the
config with criteria_build.build_criteria, and save it through criteria_store.save_new_version
(created_from intake for a first build, retune after that, which also keeps the replaced version
in config/criteria-history). When nothing would change, no new version is made. A config that
fails validation does not block the build: it is kept in the history, said plainly, and not
carried forward (a file with no readable version is moved there first). The scan scope
is written for the version just saved. After every successful build profile/summary.md is
rendered by summaries.render_intake and written only when its bytes changed, so an
unchanged rerun keeps its wrote list. The starter directory is counted against the user's
industries and sizes, and fewer than 10 matching boards is said plainly.
Guardrails: nothing is written when the answers have problems; they come back as a list. The
config is validated before it is written. No network.
resume_text and same_config are public because titles-apply rebuilds the config through the same
builder and compares with the same rule.
QA checklist: tests/test_criteria_build.py and tests/test_answers.py drive this through cli.main.
"""

from __future__ import annotations

import argparse

from jobsearch import answers as answers_module
from jobsearch import criteria_build, criteria_store, directory, fsio, paths, summaries, workspace
from jobsearch.models import Result

NAME = "criteria-build"
HELP = "Build your scoring config from your interview answers."
MIN_STARTER_BOARDS = 10
NEXT = "Run sample for title discovery."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def resume_text(folder) -> str:
    path = folder / paths.LAYOUT["resume_md"]
    return fsio.read_text(path) if path.is_file() else ""


def same_config(previous: dict, candidate: dict) -> bool:
    """True when the candidate would not change anything but the version and its source."""
    skip = ("criteria_version", "created_from")
    return ({k: v for k, v in previous.items() if k not in skip}
            == {k: v for k, v in candidate.items() if k not in skip})


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    answers = answers_module.load_answers(folder)
    config_path = folder / paths.LAYOUT["criteria"]
    previous = None
    set_aside: str | None = None
    if config_path.is_file():
        try:
            previous = criteria_store.load_criteria(folder)
        except criteria_store.CriteriaError:
            # The error for a bad config says to run the interview again, which ends here, so
            # build anyway. The broken file stays in the history; nothing is carried from it.
            set_aside = criteria_store.set_aside_unversioned(folder)
            warnings = [*warnings, (
                "Your existing scoring config could not be used (run criteria-validate to see "
                "why), so this one is built fresh from your answers. Title families you "
                "approved earlier and your tier cutoffs are not carried over. The old file is "
                "kept in config/criteria-history.")]
    try:
        config = criteria_build.build_criteria(answers, resume_text(folder), previous)
    except answers_module.AnswersError as exc:
        return Result(ok=False, command=NAME, data={"errors": exc.errors[:10]},
                      counts={"errors": len(exc.errors)}, warnings=warnings, error=str(exc),
                      next="Fix profile/answers.json, then run criteria-build again.")
    unchanged = previous is not None and same_config(previous, config)
    created_from = "retune" if config_path.is_file() else "intake"
    wrote: list[str] = []
    if unchanged:
        assert previous is not None
        version, created_from = previous["criteria_version"], previous["created_from"]
    else:
        version = criteria_store.save_new_version(folder, config, created_from)
        wrote.append(paths.LAYOUT["criteria"])
        if previous is not None:
            wrote.append(f"{paths.LAYOUT['criteria_history']}/"
                         f"criteria-v{previous['criteria_version']}.json")
        elif set_aside is not None:
            wrote.append(set_aside)
    summary_path = paths.LAYOUT["intake_summary"]
    if fsio.write_text_if_changed(
            folder / summary_path,
            summaries.render_intake(answers, {**config, "criteria_version": version})):
        wrote.append(summary_path)
    scope = criteria_build.build_scan_scope(answers, version)
    fsio.write_json_atomic(folder / paths.LAYOUT["scan_scope"], scope)
    wrote.append(paths.LAYOUT["scan_scope"])

    matching = directory.count_matching(
        directory.load_directory(), scope["industries"], scope["sizes"],
        scope["exclude_industries"])
    notes = list(warnings)
    if matching < MIN_STARTER_BOARDS:
        notes.append(f"Only {matching} starter boards match your industries and company sizes. "
                     "Add your own target companies or paste postings to see more roles.")
    families = len(config["title"]["families"])
    if families == 0:
        notes.append("No title families yet; title discovery will propose some.")
    return Result(
        ok=True,
        command=NAME,
        data={"criteria_version": version, "created_from": created_from,
              "unchanged": unchanged},
        counts={"criteria": len(config["criteria"]), "hard_filters": len(config["hard_filters"]),
                "families": families, "directory_boards_matching": matching},
        wrote=wrote,
        warnings=notes,
        next=NEXT,
    )
