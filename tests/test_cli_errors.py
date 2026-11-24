"""A file-system failure prints a plain sentence with the path and a next step.

Purpose: a read-only folder, a full disk or a file another program holds open is something the
user can fix, so it must never show as "internal error". Every command also refuses a missing
or never-set-up data folder the same way.
Logic: a handler is replaced with one that raises, and cli.main prints the Result as usual.
Guardrails: fictional paths only; no network.
QA checklist: a new class of user-fixable failure gets a case here.
"""

from __future__ import annotations

import errno
import json

import pytest

from jobsearch import cli, workspace
from jobsearch.models import errors_result


def run_with(monkeypatch, capsys, exc: BaseException) -> tuple[int, dict]:
    def boom(args):
        raise exc

    monkeypatch.setitem(cli._HANDLERS, "status", boom)
    code = cli.main(["status"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("exc", [
    PermissionError(errno.EACCES, "Permission denied", "/data/ranked/ranked-2026-11-17.md"),
    OSError(errno.ENOSPC, "No space left on device", "/data/ranked/ranked-2026-11-17.json"),
    FileNotFoundError(errno.ENOENT, "No such file or directory", "/data/config/criteria.json"),
])
def test_an_os_error_names_the_path_and_the_next_step(monkeypatch, capsys, exc):
    code, result = run_with(monkeypatch, capsys, exc)
    assert code == 1 and result["ok"] is False
    assert "internal error" not in result["error"]
    assert exc.filename in result["error"]
    assert exc.strerror in result["error"]
    assert "writable" in result["error"] and "open" in result["error"]


def test_an_os_error_without_a_path_is_still_a_plain_sentence(monkeypatch, capsys):
    code, result = run_with(monkeypatch, capsys, OSError(errno.EIO, "Input/output error"))
    assert code == 1 and "internal error" not in result["error"]
    assert "Input/output error" in result["error"]


def test_a_hand_edited_json_file_that_does_not_parse_is_a_plain_sentence(monkeypatch, capsys):
    try:
        json.loads('{"a": }')
    except json.JSONDecodeError as exc:
        decode_error = exc
    code, result = run_with(monkeypatch, capsys, decode_error)
    assert code == 1 and "internal error" not in result["error"]
    assert "not valid JSON" in result["error"] and "line 1" in result["error"]


def test_an_unexpected_exception_is_still_an_internal_error(monkeypatch, capsys):
    code, result = run_with(monkeypatch, capsys, RuntimeError("bug"))
    assert code == 1 and result["error"] == "internal error: RuntimeError"


REQUIRED_ARGS = {
    "calibrate": ["--a-min", "80", "--b-min", "60"],
    "letter-check": ["--role", "greenhouse:acme:1"],
    "letter-start": ["--role", "greenhouse:acme:1"],
    "mark": ["--role", "greenhouse:acme:1", "--verdict", "right"],
    "paste": ["--url", "https://example.com/job"],
    "profile-import": ["--resume", "resume.md"],
}


@pytest.mark.parametrize("case", ["no folder", "not set up"])
@pytest.mark.parametrize("command", sorted(set(cli._HANDLERS) - {"init"}))
def test_every_command_refuses_a_missing_or_unset_data_folder(
        command, case, tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    argv = [command, *REQUIRED_ARGS.get(command, [])]
    if case == "not set up":
        argv += ["--data-dir", str(empty)]
    code = cli.main(argv)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False and out["command"] == command
    assert out["error"] == ("No data folder found." if case == "no folder"
                            else "That folder has not been set up yet.")
    assert out["next"] == workspace.NO_FOLDER_NEXT
    assert list(empty.iterdir()) == []


def test_errors_result_caps_the_list_at_ten_and_counts_them_all():
    errors = [f"problem {n}." for n in range(12)]
    result = errors_result("demo", errors, "The file has problems: ", "Fix it.", ["careful"])
    assert result.ok is False and result.command == "demo"
    assert result.data == {"errors": errors[:10]} and result.counts == {"errors": 12}
    assert result.error == "The file has problems: " + " ".join(errors[:10])
    assert result.next == "Fix it." and result.warnings == ["careful"]
