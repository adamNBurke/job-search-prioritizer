"""Right and wrong marks, stored against the config version that produced them.

Purpose: marks are the evidence base for 1.1 learning, so each one must say which tier,
score and config version the user was judging, and must never change any of them.
Logic: every test drives cli.main the way the review skill does. A small data folder holds a
hand-written ranked JSON file, a note file written inside the data folder, and a frozen clock.
Guardrails: fixtures are fictional; no network; each test builds its own folder under tmp_path.
QA checklist: a new rule about marks gets a named test here, and the append-only test keeps
checking that earlier lines stay byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import cli_result, ranked_role, write_ranked

from jobsearch import cli, clock, marks
from jobsearch.errors import UserError

NOW = datetime(2026, 11, 18, 9, 30, 0, tzinfo=timezone.utc)
ROLE_A = "greenhouse:acme-robotics:7000001"
ROLE_B = "greenhouse:acme-robotics:7000002"


@pytest.fixture
def folder(folder, monkeypatch) -> Path:
    monkeypatch.setattr(clock, "now_utc", lambda: NOW)
    write_ranked(folder, "2026-11-17", [ranked_role(ROLE_A, "A", 70), ranked_role(ROLE_B, "B", 57)])
    return folder


def run_mark(capsys, folder: Path, *extra: str) -> dict:
    return cli_result(capsys, "mark", *extra, "--data-dir", str(folder))[1]


def mark_lines(folder: Path) -> list[dict]:
    text = (folder / "feedback" / "marks.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_mark_records_the_tier_score_and_config_version_that_produced_it(folder, capsys):
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right")
    assert out["ok"] is True and out["command"] == "mark"
    assert out["data"]["role_id"] == ROLE_A and out["data"]["criteria_version"] == "1.2"
    assert out["data"]["tier_at_time"] == "A" and out["counts"]["marks_total"] == 1
    (line,) = mark_lines(folder)
    assert line == {
        "criteria_version": "1.2", "marked_at": "2026-11-18T09:30:00Z", "note": None,
        "ranked_file": "ranked/ranked-2026-11-17.json", "role_id": ROLE_A,
        "score_at_time": 70, "tier_at_time": "A", "verdict": "right",
    }


def test_mark_reads_the_newest_ranked_file(folder, capsys):
    write_ranked(folder, "2026-11-20", [ranked_role(ROLE_A, "B", 52)], "1.3")
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "wrong")
    assert out["ok"] is True
    (line,) = mark_lines(folder)
    assert (line["criteria_version"], line["tier_at_time"], line["score_at_time"]) == ("1.3", "B", 52)
    assert line["ranked_file"] == "ranked/ranked-2026-11-20.json"


def test_mark_stores_the_note_from_a_file_in_the_data_folder(folder, capsys):
    note = folder / "feedback" / "note.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("  Too junior for me.  \n", encoding="utf-8")
    out = run_mark(capsys, folder, "--role", ROLE_B, "--verdict", "wrong", "--note-file", str(note))
    assert out["ok"] is True
    assert mark_lines(folder)[0]["note"] == "Too junior for me."


def _note(folder: Path, text: str | bytes, name: str = "note.txt") -> Path:
    path = folder / "feedback" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def test_marking_the_same_role_twice_appends_two_lines_and_the_latest_is_effective(folder, capsys):
    run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right")
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "wrong")
    assert out["ok"] is True and out["counts"]["marks_total"] == 2
    assert [m["verdict"] for m in mark_lines(folder)] == ["right", "wrong"]


@pytest.mark.parametrize("text", ["", "   \n\t\r\n"])
def test_an_empty_note_file_records_note_null(folder, capsys, text):
    note = _note(folder, text)
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right", "--note-file", str(note))
    assert out["ok"] is True
    assert mark_lines(folder)[0]["note"] is None


def test_a_missing_note_argument_records_note_null(folder, capsys):
    run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "wrong")
    assert mark_lines(folder)[0]["note"] is None


def test_a_role_not_in_the_latest_ranked_file_is_refused_by_name(folder, capsys):
    out = run_mark(capsys, folder, "--role", "greenhouse:acme-robotics:9999999", "--verdict", "right")
    assert out["ok"] is False and "greenhouse:acme-robotics:9999999" in out["error"]
    assert not (folder / "feedback" / "marks.jsonl").exists()


def test_a_control_character_in_a_refused_role_id_is_not_printed(folder, capsys):
    out = run_mark(capsys, folder, "--role", "greenhouse:acme:\x1b[31m9", "--verdict", "right")
    assert out["ok"] is False and "\x1b" not in out["error"]


def test_a_role_that_only_an_older_ranked_file_holds_is_refused(folder, capsys):
    write_ranked(folder, "2026-11-20", [ranked_role(ROLE_B, "C", 40)], "1.3")
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right")
    assert out["ok"] is False and ROLE_A in out["error"]


def test_no_ranked_file_is_refused_with_the_next_step(tmp_path, capsys):
    ws = tmp_path / "empty"
    assert cli.main(["init", "--data-dir", str(ws)]) == 0
    capsys.readouterr()
    out = run_mark(capsys, ws, "--role", ROLE_A, "--verdict", "right")
    assert out["ok"] is False and "Run rank first" in out["error"]


def test_a_new_mark_leaves_every_earlier_line_byte_identical(folder, capsys):
    path = folder / "feedback" / "marks.jsonl"
    before: list[bytes] = []
    for role, verdict in ((ROLE_A, "right"), (ROLE_B, "wrong"), (ROLE_A, "wrong")):
        run_mark(capsys, folder, "--role", role, "--verdict", verdict)
        after = path.read_bytes()
        assert after.startswith(b"".join(before))
        before = after.splitlines(keepends=True)
    assert len(before) == 3 and all(line.endswith(b"\n") for line in before)


def test_marked_at_never_goes_backwards_in_the_file(folder, capsys, monkeypatch):
    for hour in (9, 10, 11):
        monkeypatch.setattr(clock, "now_utc", lambda hour=hour: datetime(2026, 11, 18, hour, 0, 0, tzinfo=timezone.utc))
        run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right")
    stamps = [m["marked_at"] for m in mark_lines(folder)]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3


def test_a_note_path_outside_the_data_folder_is_refused(folder, tmp_path, capsys):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not mine", encoding="utf-8")
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right", "--note-file", str(outside))
    assert out["ok"] is False and "data folder" in out["error"]
    assert not (folder / "feedback" / "marks.jsonl").exists()


def test_a_note_path_that_climbs_out_of_the_data_folder_is_refused(folder, tmp_path, capsys):
    (tmp_path / "secret.txt").write_text("not mine", encoding="utf-8")
    sneaky = folder / "feedback" / ".." / ".." / "secret.txt"
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right", "--note-file", str(sneaky))
    assert out["ok"] is False and "data folder" in out["error"]


def test_a_note_of_1001_characters_is_refused_and_1000_is_kept(folder, capsys):
    refused = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right",
                       "--note-file", str(_note(folder, "x" * 1001)))
    assert refused["ok"] is False and refused["error"] == "Keep the note under 1000 characters."
    assert not (folder / "feedback" / "marks.jsonl").exists()
    kept = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right",
                    "--note-file", str(_note(folder, "y" * 1000)))
    assert kept["ok"] is True and mark_lines(folder)[0]["note"] == "y" * 1000


def test_a_note_file_over_the_byte_cap_is_refused_before_it_is_read(folder, capsys):
    # The byte cap now comes from fsio.read_user_file, so its message names the cap.
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right",
                   "--note-file", str(_note(folder, "x" * 5000)))
    assert out["ok"] is False
    assert out["error"] == "The note file is too long. Keep it under 4016 bytes."
    assert not (folder / "feedback" / "marks.jsonl").exists()


def test_a_crlf_note_with_a_bom_is_stored_as_lf_text_without_the_bom(folder, capsys):
    raw = b"\xef\xbb\xbfFirst line\r\nSecond line\r\n"
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right",
                   "--note-file", str(_note(folder, raw)))
    assert out["ok"] is True
    assert mark_lines(folder)[0]["note"] == "First line\nSecond line"
    assert b"\r" not in (folder / "feedback" / "marks.jsonl").read_bytes()
    assert not (folder / "feedback" / "marks.jsonl").read_bytes().startswith(b"\xef\xbb\xbf")


def test_a_note_that_is_not_text_is_refused(folder, capsys):
    out = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right",
                   "--note-file", str(_note(folder, b"\xff\xfe\x00bad")))
    assert out["ok"] is False and "text" in out["error"]


def test_a_mark_changes_no_score_tier_config_or_ranked_file(folder, capsys):
    """Marks are recorded only."""
    criteria = folder / "config" / "criteria.json"
    criteria.parent.mkdir(parents=True, exist_ok=True)
    criteria.write_bytes(b'{"criteria_version": "1.2"}\n')
    watched = [criteria, *sorted((folder / "ranked").glob("*"))]
    before = {p: p.read_bytes() for p in watched}
    run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "wrong",
             "--note-file", str(_note(folder, "Disagree.")))
    assert {p: p.read_bytes() for p in watched} == before
    assert sorted(p.name for p in (folder / "ranked").glob("*")) == ["ranked-2026-11-17.json"]
    assert not list((folder / "config").glob("criteria-history*"))


def test_an_unknown_verdict_is_refused_before_anything_is_written(folder):
    with pytest.raises(UserError):
        marks.add_mark(folder, ROLE_A, "maybe", None)
    assert not (folder / "feedback" / "marks.jsonl").exists()


def test_a_damaged_line_is_skipped_on_read_and_kept_on_append(folder, capsys):
    path = folder / "feedback" / "marks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n", encoding="utf-8")
    run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right")
    assert path.read_text(encoding="utf-8").startswith("not json\n")
    assert [m.role_id for m in marks.scan_marks(folder)[0]] == [ROLE_A]


def test_a_non_utf8_line_is_skipped_with_a_warning_and_later_marks_still_succeed(folder, capsys):
    path = folder / "feedback" / "marks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = b'{"note": "caf\xe9 saved in ANSI"}\n'
    path.write_bytes(damaged)
    first = run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right")
    assert first["ok"] is True
    assert any("damaged" in w and "skipped" in w for w in first["warnings"])
    second = run_mark(capsys, folder, "--role", ROLE_B, "--verdict", "wrong")
    assert second["ok"] is True
    assert path.read_bytes().startswith(damaged)  # the damaged bytes are kept as written
    assert [m.role_id for m in marks.scan_marks(folder)[0]] == [ROLE_A, ROLE_B]


def test_a_unicode_line_separator_in_a_note_does_not_split_its_mark(folder, capsys):
    note = folder / "note.txt"
    note.write_text("one two three\u0085four", encoding="utf-8")
    assert run_mark(capsys, folder, "--role", ROLE_A, "--verdict", "right",
                    "--note-file", str(note))["ok"] is True
    # json.dumps(ensure_ascii=False) writes these characters raw; splitlines() would cut the line
    assert marks.scan_marks(folder)[0][0].note == "one two three\u0085four"
    assert run_mark(capsys, folder, "--role", ROLE_B, "--verdict", "wrong")["ok"] is True
    assert len(marks.scan_marks(folder)[0]) == 2


def test_a_crlf_marks_file_and_a_bom_are_read(folder):
    line = json.dumps({"marked_at": "2026-11-18T09:00:00Z", "role_id": ROLE_A, "verdict": "right",
                       "note": None, "tier_at_time": "A", "score_at_time": 70,
                       "criteria_version": "1.2", "ranked_file": "ranked/ranked-2026-11-17.json"})
    path = folder / "feedback" / "marks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xef\xbb\xbf" + line.encode() + b"\r\n" + line.encode() + b"\r\n")
    assert len(marks.scan_marks(folder)[0]) == 2
