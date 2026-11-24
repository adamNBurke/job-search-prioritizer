"""The code half of the own-words cover letter.

Purpose: prove the README promise that at least 80% of a letter body is the user's own words.
letter-start makes the folder for one ranked role;
letter-check refuses to save letter.md when more than 20% of the words of draft.md are not in the
user's answers (or saved writing samples), and lists tone patterns and lines copied from the
posting without ever blocking the save for those.
Logic: every test drives cli.main the way the letter skill does. A small data folder holds a
hand-written ranked JSON file; one end-to-end test uses the staged casey-ops workspace from
test_stranger_pipeline (offline fixture boards) so the role comes from a real ranked file.
Guardrails: fixtures are fictional; no network; each test builds its own folder under tmp_path.
QA checklist: a new provenance rule gets a positive and a negative test here; a new letter file
gets a line in docs/data-files.md and a check in test_data_files_doc.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from helpers import cli_result, ranked_role, write_ranked
from test_stranger_pipeline import Env, stage, tier_roles

from jobsearch import letters
from jobsearch.errors import UserError

ROLE = "greenhouse:acme-robotics:7000001"
OTHER_ROLE = "greenhouse:acme:robotics-7000001"

ANSWERS = (
    "## What is a project you are proud of?\n"
    "I rebuilt the intake process for a regional clinic network. Wait times fell by a third.\n"
    "\n"
    "## Why this company?\n"
    "Your team ships warehouse tools that people actually use. I want to work close to the floor.\n"
)
ROLE_FIELDS = {"title": "Operations Analyst", "company": "Acme Robotics",
               "url_apply": "https://example.com/jobs/7000001"}
TRACED_DRAFT = (
    "I want to work close to the floor. Wait times fell by a third.\n"
    "I rebuilt the intake process for a regional clinic network.\n"
)


@pytest.fixture
def folder(folder) -> Path:
    write_ranked(folder, "2026-11-17", [ranked_role(ROLE, "A", 70, **ROLE_FIELDS)])
    return folder


@pytest.fixture
def casey(tmp_path, monkeypatch, capsys) -> Env:
    return stage(tmp_path, monkeypatch, capsys, "casey-ops")


def run_cmd(capsys, folder: Path, *argv: str) -> dict:
    return cli_result(capsys, *argv, "--data-dir", str(folder))[1]


def letter_folder(folder: Path, role_id: str = ROLE) -> Path:
    return folder / "applications" / letters.role_key(role_id)


def started(folder: Path, capsys, answers: str = ANSWERS, draft: str = TRACED_DRAFT) -> Path:
    """Run letter-start, then write the user-owned files the letter skill would write."""
    out = run_cmd(capsys, folder, "letter-start", "--role", ROLE)
    assert out["ok"] is True, out
    target = letter_folder(folder)
    (target / "answers.md").write_text(answers, encoding="utf-8")
    (target / "draft.md").write_text(draft, encoding="utf-8")
    return target


def check(capsys, folder: Path, role_id: str = ROLE) -> dict:
    return run_cmd(capsys, folder, "letter-check", "--role", role_id)


# Task 1: the tracer ---------------------------------------------------------------------------

def test_role_key_is_windows_safe_and_unique():
    key = letters.role_key("greenhouse:acme-robotics:7000001")
    assert re.fullmatch(r"[a-z0-9._-]+-[0-9a-f]{8}", key)
    assert ":" not in key and len(key) <= 69
    assert key != letters.role_key("greenhouse:acme:robotics-7000001")
    assert letters.role_key(OTHER_ROLE) != letters.role_key(ROLE)


def test_role_key_survives_hostile_and_empty_ids():
    for hostile in ("../../etc/passwd", "CON", "a" * 400, "\u00e9\u00e9\u00e9", "...", ":::", ""):
        key = letters.role_key(hostile)
        assert re.fullmatch(r"[a-z0-9._-]+", key), (hostile, key)
        assert not key.startswith((".", "-")) and not key.endswith((".", " ")) and len(key) <= 69
    assert letters.role_key("pasted:-:0123456789ab").startswith("pasted-")


def test_letter_start_writes_role_and_posting_and_keeps_user_files(casey):
    casey.rank_from_answers()
    role = tier_roles(casey.ranked_json(), "A", "B")[0]
    out = casey.cli("letter-start", "--role", role["role_id"])
    key = letters.role_key(role["role_id"])
    target = casey.folder / "applications" / key
    assert out["command"] == "letter-start" and out["data"]["folder"] == f"applications/{key}"
    assert out["data"]["has_answers"] is False and out["data"]["has_draft"] is False
    assert (target / "role.json").is_file() and (target / "posting.txt").is_file()
    facts = json.loads((target / "role.json").read_text(encoding="utf-8"))
    assert facts["role_id"] == role["role_id"] and facts["schema_version"] == 1
    assert facts["tier"] == role["tier"] and facts["title"] == role["title"]
    assert (target / "posting.txt").read_text(encoding="utf-8").strip()
    assert len(casey.last_stdout.splitlines()) == 1
    (target / "answers.md").write_text(ANSWERS, encoding="utf-8")
    (target / "draft.md").write_text(TRACED_DRAFT, encoding="utf-8")
    (target / "fixes.json").write_text('{"fixes": [], "schema_version": 1}\n', encoding="utf-8")
    before = {name: (target / name).read_bytes()
              for name in ("answers.md", "draft.md", "fixes.json", "role.json", "posting.txt")}
    again = casey.cli("letter-start", "--role", role["role_id"])
    assert again["data"]["has_answers"] is True and again["data"]["has_draft"] is True
    assert {name: (target / name).read_bytes() for name in before} == before


def test_letter_start_refuses_a_role_not_in_the_newest_ranked_file(folder, capsys):
    out = run_cmd(capsys, folder, "letter-start", "--role", "greenhouse:acme-robotics:9999999")
    assert out["ok"] is False
    assert "greenhouse:acme-robotics:9999999" in out["error"] and "ranked list" in out["error"]
    assert not list((folder / "applications").glob("*"))


def test_letter_start_reads_the_newest_ranked_file(folder, capsys):
    write_ranked(folder, "2026-11-20", [ranked_role(OTHER_ROLE, "B", 52, **ROLE_FIELDS)])
    assert run_cmd(capsys, folder, "letter-start", "--role", ROLE)["ok"] is False
    assert run_cmd(capsys, folder, "letter-start", "--role", OTHER_ROLE)["ok"] is True


def test_a_traced_draft_saves_letter_md(casey):
    casey.rank_from_answers()
    role = tier_roles(casey.ranked_json(), "A", "B")[0]
    casey.cli("letter-start", "--role", role["role_id"])
    target = casey.folder / "applications" / letters.role_key(role["role_id"])
    (target / "answers.md").write_text(ANSWERS, encoding="utf-8")
    (target / "draft.md").write_text(TRACED_DRAFT, encoding="utf-8")
    out = casey.cli("letter-check", "--role", role["role_id"])
    assert out["ok"] is True and out["data"]["saved"] is True
    assert out["counts"]["changed_words"] == 0
    assert (target / "letter.md").read_text(encoding="utf-8") == TRACED_DRAFT
    report = (target / "check.md").read_text(encoding="utf-8")
    assert "Every line is in your own words." in report
    key = letters.role_key(role["role_id"])
    assert f"applications/{key}/letter.md" in out["wrote"]


OVER_DRAFT = ("I want to work close to the floor.\n"
              "I rebuilt the intake process for a large regional clinic network.\n"
              "My manager called it the best rollout in years.\n")


def test_a_draft_over_the_cap_blocks_the_save_and_names_the_share(folder, capsys):
    target = started(folder, capsys, draft=OVER_DRAFT)
    out = check(capsys, folder)
    assert out["ok"] is False
    assert out["counts"]["changed_words"] == 10 and out["counts"]["body_words"] == 28
    assert "36%" in out["error"] and "check.md" in out["error"]
    report = (target / "check.md").read_text(encoding="utf-8")
    first = report.split("## Lines that may")[0]
    assert ("10 of 28 words in the letter body (36%) are not from your answers or samples. "
            "The most allowed is 20%.") in first
    assert first.index("- Line 3:") < first.index("- Line 2:")
    assert "(9 of 9 words)" in first and "(1 of 11 words)" in first and "Line 1:" not in first
    assert "was not saved, because more than 20% of the body" in report
    assert not (target / "letter.md").exists()


def test_a_change_of_one_word_in_five_saves_and_two_does_not(folder, capsys):
    answers = "## Q\nWait times fell by a third.\n"
    target = started(folder, capsys, answers=answers, draft="Wait times fell by half.\n")
    out = check(capsys, folder)
    assert out["ok"] is True and out["counts"]["changed_words"] == 1
    report = (target / "check.md").read_text(encoding="utf-8")
    assert "1 of 5 words in the letter body (20%)" in report
    assert '- Line 1: "Wait times fell by half." (1 of 5 words)' in report
    assert (target / "letter.md").is_file()
    (target / "draft.md").write_text("Wait times fell to half.\n", encoding="utf-8")
    out = check(capsys, folder)
    assert out["ok"] is False and "(40%)" in (target / "check.md").read_text(encoding="utf-8")
    assert "40%" in out["error"]


def test_a_question_heading_is_not_a_source(folder, capsys):
    target = started(folder, capsys, draft="What is a project you are proud of?\n")
    out = check(capsys, folder)
    assert out["ok"] is False
    assert out["counts"]["changed_words"] == 8 and out["counts"]["body_words"] == 8
    assert not (target / "letter.md").exists()


def rows(draft: str, source: str = ANSWERS, fixes=None, frame=frozenset()) -> list[tuple]:
    return letters.changed_words(draft, [letters.answer_sources(source)], fixes, frame)


@pytest.mark.parametrize("sentence, expected", [
    ("Wait times fell by a third and I want to work close to the floor.", (1, 15)),  # a join
    ("I rebuilt the intake process for a very regional clinic network.", (1, 11)),   # a word added
    ("Wait times fell by a half.", (1, 6)),                                          # a word swapped
    ("I rebuilt the process for a regional clinic network.", (0, 9)),                # a cut
    ("What is a project you are proud of?", (8, 8)),                                 # a heading
    ("I rebuilt the intake process for a regional clinic netw.", (1, 10)),           # half a word
])
def test_changed_words_counts_the_words_that_are_not_the_users(sentence, expected):
    [(number, text, changed, words)] = rows(sentence + "\n")
    assert (number, text, (changed, words)) == (1, sentence, expected)


def test_reordering_costs_nothing():
    assert [changed for _, _, changed, _ in rows(TRACED_DRAFT)] == [0, 0, 0]


def test_a_join_costs_only_the_words_added():
    [row] = rows("Wait times fell by a third and I want to work close to the floor.\n")
    assert row[2:] == (1, 15)


def test_a_frame_line_is_left_out_of_the_count_and_is_never_a_source():
    answers = ("## Greeting\nDear Ms. Rao,\n\n## Sign-off\nWith thanks,\nCasey Example\n"
               "casey.example@example.com | 555-0142\n\n" + ANSWERS)
    draft = ("Dear Ms. Rao,\nWait times fell by a third.\nWith thanks,\nCasey Example\n"
             "casey.example@example.com | 555-0142\n")
    frame = letters.frame_lines(answers)
    assert len(frame) == 4 and "dear ms. rao," in frame
    assert "Rao" not in letters.answer_sources(answers)
    assert rows(draft, answers, frame=frame) == [(2, "Wait times fell by a third.", 0, 6)]
    unframed = rows(draft, answers)
    assert sum(changed for _, _, changed, _ in unframed) > 0
    assert {number for number, _, changed, _ in unframed if changed} == {1, 3, 4, 5}


def test_a_line_longer_than_eight_words_under_a_frame_heading_is_not_frame():
    long_line = "With the warmest thanks and every good wish for you"
    answers = "## Sign-off\n" + long_line + "\n\n" + ANSWERS
    assert letters.frame_lines(answers) == frozenset()
    rows_ = rows(long_line + "\n", answers, frame=letters.frame_lines(answers))
    assert [row[2:] for row in rows_] == [(10, 10)]


def test_a_framed_draft_saves_end_to_end(folder, capsys):
    answers = ("## Greeting\nDear Ms. Rao,\n\n## Sign-off\nWith thanks,\nCasey Example\n"
               "casey.example@example.com | 555-0142\n\n" + ANSWERS)
    draft = ("Dear Ms. Rao,\nWait times fell by a third.\nWith thanks,\nCasey Example\n"
             "casey.example@example.com | 555-0142\n")
    target = started(folder, capsys, answers=answers, draft=draft)
    out = check(capsys, folder)
    assert out["ok"] is True
    assert out["counts"]["changed_words"] == 0 and out["counts"]["body_words"] == 6
    assert (target / "letter.md").read_text(encoding="utf-8") == draft
    (target / "answers.md").write_text(ANSWERS, encoding="utf-8")
    assert check(capsys, folder)["ok"] is False


def test_curly_apostrophes_and_case_do_not_hide_a_match():
    sources = ["We didn\u2019t mind the rain.\n"]
    assert [c for _, _, c, _ in letters.changed_words("we DIDN'T mind the rain.\n", sources)] == [0]


def test_an_em_dash_is_named_with_its_line(folder, capsys):
    answers = "## Q\nI rebuilt intake \u2014 and wait times fell.\n"
    draft = "Wait times fell.\nI rebuilt intake \u2014 and wait times fell.\n"
    target = started(folder, capsys, answers=answers, draft=draft)
    out = check(capsys, folder)
    assert out["ok"] is True and out["counts"]["tone_hits"] == 1
    report = (target / "check.md").read_text(encoding="utf-8")
    assert "Line 2: em dash" in report
    assert "\u2014" in (target / "letter.md").read_text(encoding="utf-8")  # the words are never edited


def test_an_empty_draft_is_refused_and_nothing_is_written(folder, capsys):
    target = started(folder, capsys, draft="  \n\n")
    out = check(capsys, folder)
    assert out["ok"] is False and out["error"] == "The draft is empty. Write draft.md first."
    assert not (target / "check.md").exists() and not (target / "letter.md").exists()


def test_a_missing_draft_is_refused_with_a_plain_sentence(folder, capsys):
    run_cmd(capsys, folder, "letter-start", "--role", ROLE)
    out = check(capsys, folder)
    assert out["ok"] is False and "draft.md" in out["error"]


def test_the_answers_file_is_optional_but_then_nothing_traces(folder, capsys):
    target = started(folder, capsys)
    (target / "answers.md").unlink()
    out = check(capsys, folder)
    assert out["ok"] is False
    assert out["counts"]["changed_words"] == 24 and out["counts"]["body_words"] == 24


def test_letter_check_output_is_deterministic(folder, capsys):
    target = started(folder, capsys)
    check(capsys, folder)
    first = {name: (target / name).read_bytes() for name in ("check.md", "letter.md")}
    check(capsys, folder)
    assert {name: (target / name).read_bytes() for name in first} == first


def test_letter_md_is_normalized(folder, capsys):
    target = started(folder, capsys)
    (target / "draft.md").write_bytes(
        b"I want to work close to the floor.   \r\nWait times fell by a third.\r\n\r\n\r\n")
    out = check(capsys, folder)
    assert out["ok"] is True
    assert (target / "letter.md").read_bytes() == (
        b"I want to work close to the floor.\nWait times fell by a third.\n")


def test_letters_module_refuses_bad_paths_with_a_user_error(folder):
    with pytest.raises(UserError):
        letters.find_ranked_role(folder, "not:a:role")


# Task 2: the posting-text check ----------------------------------------------------------------

POSTING = "We are looking for a builder who thrives on ambiguity and change.\n"


def _with_posting(folder: Path, capsys, draft: str, answers: str, posting: str = POSTING) -> Path:
    target = started(folder, capsys, answers=answers, draft=draft)
    (target / "posting.txt").write_text(posting, encoding="utf-8")
    return target


def test_a_line_sharing_eight_words_with_the_posting_is_listed(folder, capsys):
    answers = "## Q\nI am a builder who thrives on ambiguity and change. I like small teams.\n"
    draft = "I am a builder who thrives on ambiguity and change.\nI like small teams.\n"
    target = _with_posting(folder, capsys, draft, answers)
    out = check(capsys, folder)
    assert out["ok"] is True and out["counts"]["borrowed"] == 1  # advisory: letter.md still saved
    assert (target / "letter.md").is_file()
    report = (target / "check.md").read_text(encoding="utf-8")
    section = report.split("## Lines that match the posting")[1].split("## Saved")[0]
    assert "- Line 1:" in section and "Line 2:" not in section


def test_seven_shared_words_in_a_row_is_not_listed(folder, capsys):
    line = "I think builder who thrives on ambiguity and change fits.\n"  # 7 words in a row
    target = _with_posting(folder, capsys, line, "## Q\n" + line)
    out = check(capsys, folder)
    assert out["ok"] is True and out["counts"]["borrowed"] == 0
    report = (target / "check.md").read_text(encoding="utf-8")
    assert "No line shares 8 or more words in a row with the posting." in report


def test_the_word_run_ignores_case_punctuation_and_line_breaks_in_the_posting(folder, capsys):
    answers = "## Q\nWe are LOOKING for a builder, who thrives.\n"
    draft = "We are LOOKING for a builder, who thrives.\n"
    _with_posting(folder, capsys, draft, answers, posting="we are looking\nfor a builder\nwho thrives\n")
    assert check(capsys, folder)["counts"]["borrowed"] == 1


def test_no_posting_text_says_so(folder, capsys):
    target = started(folder, capsys)
    out = check(capsys, folder)
    assert out["ok"] is True and out["counts"]["borrowed"] == 0
    report = (target / "check.md").read_text(encoding="utf-8")
    assert "The posting text was not found, so this was not checked." in report


def test_a_draft_with_no_hits_says_no_patterns_found(folder, capsys):
    target = started(folder, capsys)
    check(capsys, folder)
    assert "No patterns found." in (target / "check.md").read_text(encoding="utf-8")


def test_borrowed_lines_numbers_lines_and_skips_short_posting_runs():
    found = letters.borrowed_lines("Hi.\n\none two three four five six seven eight nine\n",
                                   "x one two three four five six seven eight y")
    assert [n for n, _ in found] == [3]
    assert letters.borrowed_lines("one two three\n", "one two three") == []
    assert letters.borrowed_lines("anything at all\n", "") == []


def test_posting_text_cannot_inject_markdown_into_check_md(folder, capsys):
    answers = "## Q\nWe want **bold** [link](x) words here now ok.\n"
    draft = "We want **bold** [link](x) words here now ok.\n"
    target = _with_posting(folder, capsys, draft, answers, posting=draft)
    check(capsys, folder)
    report = (target / "check.md").read_text(encoding="utf-8")
    assert "\\*\\*bold\\*\\*" in report and "\\[link\\]" in report


# Task 3: samples, approved typo fixes, earlier letters, caps and refusals -----------------------

SENTENCE = "I led the intake team at a clinic."


def _sample(folder: Path, name: str, text: str) -> Path:
    path = folder / "profile" / "samples" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _no_output(target: Path) -> None:
    assert not (target / "check.md").exists() and not (target / "letter.md").exists()


def test_every_source_kind_traces_a_sentence(folder, capsys):
    target = started(folder, capsys, answers="## Q\n" + SENTENCE + "\n", draft=SENTENCE + "\n")
    assert check(capsys, folder)["ok"] is True  # answers
    (target / "answers.md").write_text("## Q\n", encoding="utf-8")
    assert check(capsys, folder)["ok"] is False  # nothing left to trace to
    _sample(folder, "earlier-letter.md", "Dear team,\n" + SENTENCE + "\n")
    assert check(capsys, folder)["ok"] is True  # a .md sample
    (folder / "profile" / "samples" / "earlier-letter.md").unlink()
    _sample(folder, "notes.txt", SENTENCE + "\n")
    assert check(capsys, folder)["ok"] is True  # a .txt sample
    (folder / "profile" / "samples" / "notes.txt").unlink()
    _sample(folder, "other.rtf", SENTENCE + "\n")
    assert check(capsys, folder)["ok"] is False  # another suffix is ignored


def _fixes(*pairs: tuple[str, str]) -> str:
    return json.dumps({"schema_version": 1, "fixes": [{"from": a, "to": b} for a, b in pairs]})


def test_an_approved_typo_fix_lets_the_corrected_word_trace(folder, capsys):
    target = started(folder, capsys, answers="## Q\nI led teh team.\n", draft="I led the team.\n")
    assert check(capsys, folder)["ok"] is False
    (target / "fixes.json").write_text(_fixes(("teh", "the")), encoding="utf-8")
    out = check(capsys, folder)
    assert out["ok"] is True
    assert (target / "letter.md").read_text(encoding="utf-8") == "I led the team.\n"


def test_a_fix_does_not_excuse_other_changes(folder, capsys):
    target = started(folder, capsys, answers="## Q\nI led teh team.\n",
                     draft="I managed the team.\n")
    (target / "fixes.json").write_text(_fixes(("teh", "the")), encoding="utf-8")
    assert check(capsys, folder)["ok"] is False


def test_a_fix_is_a_whole_word_replacement():
    sources = ["I led tehran and teh team.\n"]
    fixes = [("teh", "the")]
    assert [c for _, _, c, _ in letters.changed_words("I led tehran and the team.\n", sources, fixes)] == [0]
    assert [c for _, _, c, _ in letters.changed_words("I led theran and the team.\n", sources, fixes)] != [0]


@pytest.mark.parametrize("fixes", [
    _fixes(("two words", "the")),                      # white space in the from word
    _fixes(("teh", "the team")),                       # white space in the to word
    _fixes(("recieve", "receive")),                    # from is not in any source
    _fixes(("led", "managed")),                        # edit distance above 2
    _fixes(("teh", "teh")),                            # not a change
    _fixes(*[("teh", "the")] * 51),                    # more than 50 fixes
    json.dumps({"schema_version": 1, "fixes": [], "note": "x"}),            # an extra key
    json.dumps({"schema_version": 1, "fixes": [{"from": "teh", "to": "the", "why": "x"}]}),
    json.dumps({"schema_version": 2, "fixes": []}),
    json.dumps({"schema_version": 1, "fixes": "teh"}),
    json.dumps([1, 2]),
    "not json at all",
])
def test_fixes_are_refused_when_they_are_not_typos(folder, capsys, fixes):
    target = started(folder, capsys, answers="## Q\nI led teh team.\n", draft="I led the team.\n")
    (target / "fixes.json").write_text(fixes, encoding="utf-8")
    out = check(capsys, folder)
    assert out["ok"] is False and "fixes.json" in out["error"]
    _no_output(target)


def test_a_changed_draft_keeps_the_earlier_letter(folder, capsys):
    target = started(folder, capsys)
    check(capsys, folder)
    first = (target / "letter.md").read_bytes()
    check(capsys, folder)
    assert (target / "letter.md").read_bytes() == first
    assert sorted(p.name for p in target.glob("letter*.md")) == ["letter.md"]
    second_draft = "Wait times fell by a third.\n"
    (target / "draft.md").write_text(second_draft, encoding="utf-8")
    out = check(capsys, folder)
    assert (target / "letter-v1.md").read_bytes() == first
    assert (target / "letter.md").read_text(encoding="utf-8") == second_draft
    assert any(name.endswith("letter-v1.md") for name in out["wrote"])
    (target / "draft.md").write_text("I want to work close to the floor.\n", encoding="utf-8")
    check(capsys, folder)
    assert (target / "letter-v2.md").read_text(encoding="utf-8") == second_draft
    assert (target / "letter-v1.md").read_bytes() == first
    check(capsys, folder)  # the same draft again changes nothing
    assert sorted(p.name for p in target.glob("letter*.md")) == [
        "letter-v1.md", "letter-v2.md", "letter.md"]


def test_a_refused_draft_leaves_the_saved_letter_alone(folder, capsys):
    target = started(folder, capsys)
    check(capsys, folder)
    saved = (target / "letter.md").read_bytes()
    (target / "draft.md").write_text("Words the user never wrote.\n", encoding="utf-8")
    assert check(capsys, folder)["ok"] is False
    assert (target / "letter.md").read_bytes() == saved
    assert not (target / "letter-v1.md").exists()


def test_oversized_files_are_refused_with_a_plain_sentence(folder, capsys):
    target = started(folder, capsys)
    (target / "draft.md").write_text("a" * (letters.DRAFT_MAX_BYTES + 1), encoding="utf-8")
    out = check(capsys, folder)
    assert out["ok"] is False and "draft.md" in out["error"] and "too long" in out["error"]
    (target / "draft.md").write_text(TRACED_DRAFT, encoding="utf-8")
    (target / "answers.md").write_text("a" * (letters.ANSWERS_MAX_BYTES + 1), encoding="utf-8")
    out = check(capsys, folder)
    assert out["ok"] is False and "answers.md" in out["error"]
    (target / "answers.md").write_text(ANSWERS, encoding="utf-8")
    _sample(folder, "big.md", "a" * (letters.SAMPLE_MAX_BYTES + 1))
    out = check(capsys, folder)
    assert out["ok"] is False and "big.md" in out["error"]
    _no_output(target)


def test_too_many_samples_are_refused(folder, capsys):
    target = started(folder, capsys)
    for number in range(letters.SAMPLES_MAX_FILES + 1):
        _sample(folder, f"s{number:02}.md", "Some words.\n")
    out = check(capsys, folder)
    assert out["ok"] is False and str(letters.SAMPLES_MAX_FILES) in out["error"]
    _no_output(target)


def test_a_file_that_resolves_outside_the_data_folder_is_refused(folder, capsys, tmp_path):
    target = started(folder, capsys)
    outside = tmp_path / "outside.md"
    outside.write_text(TRACED_DRAFT, encoding="utf-8")
    (target / "draft.md").unlink()
    try:
        (target / "draft.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow symbolic links")
    out = check(capsys, folder)
    assert out["ok"] is False and "inside your data folder" in out["error"]
    _no_output(target)


def test_an_unreadable_draft_is_a_plain_sentence(folder, capsys):
    target = started(folder, capsys)
    (target / "draft.md").write_bytes(b"\xff\xfe\x00bad")
    out = check(capsys, folder)
    assert out["ok"] is False and "could not be read as text" in out["error"]


# Negation cuts, tiny pieces, heading levels, a kept posting --------------------

def test_a_negation_cut_is_untraced_end_to_end(folder, capsys):
    target = started(folder, capsys, answers="## Q\nI did not lead the team.\n",
                     draft="Lead the team.\n")
    out = check(capsys, folder)
    assert out["ok"] is False and out["counts"]["changed_words"] == 3
    assert not (target / "letter.md").exists()


@pytest.mark.parametrize("source, draft", [
    ("I did not lead the team.", "Lead the team."),
    ("I didn't lead the team.", "Lead the team."),
    ("I did not personally lead the team.", "Lead the team."),
    ("We never missed a deadline in two years.", "Missed a deadline in two years."),
    ("I cannot say the audit went well.", "Say the audit went well."),
    ("There was no plan to cut the budget.", "Plan to cut the budget."),
    ("I went without sleep to finish the audit.", "Sleep to finish the audit."),
])
def test_a_cut_that_drops_a_negation_costs_every_word(source, draft):
    [(_, _, changed, words)] = letters.changed_words(draft + "\n", [source + "\n"])
    assert changed == words > 0


def test_a_negator_in_an_earlier_sentence_or_a_whole_sentence_still_traces():
    source = "I did not lead the team. I built the tool.\n"
    assert [c for _, _, c, _ in letters.changed_words("I built the tool.\n", [source])] == [0]
    assert [c for _, _, c, _ in letters.changed_words("I did not lead the team.\n", [source])] == [0]


def test_every_markdown_heading_level_is_dropped_from_the_sources():
    answers = ("# Why this role matters to me\n### What you built last year\n"
               "  ## An indented question about the team\n###### The smallest heading here\n"
               "#1 priority was the warehouse floor.\nI led the team.\n")
    sources = [letters.answer_sources(answers)]
    for heading in ("Why this role matters to me.", "What you built last year.",
                    "An indented question about the team.", "The smallest heading here."):
        [(_, _, changed, words)] = letters.changed_words(heading + "\n", sources)
        assert changed == words > 0, heading
    kept = letters.changed_words("#1 priority was the warehouse floor.\nI led the team.\n", sources)
    assert [c for _, _, c, _ in kept] == [0, 0]


def test_a_short_sentence_costs_nothing_only_as_a_whole_source_sentence():
    sources = ["I led the team. Yes.\nThanks,\nCasey\n"]
    for sentence, words in (("I.", 1), ("Led.", 1), ("The team.", 2)):
        assert letters.changed_words(sentence + "\n", sources) == [(1, sentence, words, words)]
    for sentence in ("Yes.", "Thanks,", "Casey", "Led the team."):
        [(_, _, changed, _)] = letters.changed_words(sentence + "\n", sources)
        assert changed == 0, sentence


def test_a_hyphenated_word_is_one_word_and_a_cut_inside_it_is_a_change():
    sources = ["I went to the follow-up meeting.\n"]
    assert letters.changed_words("I went to the follow.\n", sources) == [
        (1, "I went to the follow.", 1, 5)]
    assert [c for _, _, c, _ in letters.changed_words("I went to the follow-up meeting.\n",
                                                         sources)] == [0]


def test_an_approved_typo_fix_costs_nothing_in_the_count():
    sources = ["I led teh team.\n"]
    assert letters.changed_words("I led the team.\n", sources) == [(1, "I led the team.", 4, 4)]
    assert letters.changed_words("I led the team.\n", sources, [("teh", "the")]) == [
        (1, "I led the team.", 0, 4)]


def test_a_rerun_of_letter_start_keeps_a_saved_posting(folder, capsys):
    # posting_found is in start_letter's dict, not the command's data; the command shows it as
    # the "posting text was not found" warning.
    assert letters.start_letter(folder, ROLE)["posting_found"] is False
    posting = letter_folder(folder) / "posting.txt"
    posting.write_text("We build warehouse robots.\n", encoding="utf-8")
    before = posting.read_bytes()
    assert letters.start_letter(folder, ROLE)["posting_found"] is True
    assert posting.read_bytes() == before
    out = run_cmd(capsys, folder, "letter-start", "--role", ROLE)
    assert out["warnings"] == [] and posting.read_bytes() == before
