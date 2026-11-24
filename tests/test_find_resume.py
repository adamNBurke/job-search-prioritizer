"""Find-resume lists resume-like file names and never reads a file.

Purpose: a user who does not know their resume's path can let the tool look by file name, so the
search must stay narrow (three folders and their OneDrive twins, two levels deep, PDF and DOCX
only), must list names only, and must never open anything it finds.
Logic: each test builds a tmp home folder with the smallest set of files that shows one rule,
calls resume_finder.find_resume_files directly, and the command test drives cli.main with the
home folder pointed at tmp_path.
Guardrails: no test touches the real home folder or the network. The no-file-opened test makes
every open call on a PDF or DOCX path fail.
QA checklist: a new match rule needs a positive and a negative case here. A new search folder
needs a test that it is searched.
"""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

from jobsearch import cli, resume_finder


RESUME_ACCENTED = "r" + chr(0xE9) + "sum" + chr(0xE9) + "-final.PDF"


def touch(path: Path, age: int = 0) -> Path:
    """Create an empty file whose modification time is `age` seconds before a fixed base."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = 1_800_000_000 - age
    os.utime(path, (stamp, stamp))
    return path


def names(paths: list[Path], home: Path) -> list[str]:
    return [p.relative_to(home).as_posix() for p in paths]


class _Listing(list):
    """A scandir result: a list of entries that also works as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Proxy:
    """A directory entry that answers some methods with fakes and delegates the rest."""

    def __init__(self, real, methods):
        self._real, self._methods = real, methods

    def __getattr__(self, attr):
        return self._methods[attr] if attr in self._methods else getattr(self._real, attr)


class _Stat:
    """A stat result that carries extra attributes and delegates the rest."""

    def __init__(self, real, **extra):
        self._real, self._extra = real, extra

    def __getattr__(self, attr):
        return self._extra[attr] if attr in self._extra else getattr(self._real, attr)


def wrap_entry(monkeypatch, folder: Path, name: str, **methods) -> None:
    """Make the `name` entry of `folder` answer `methods` with fakes; every other scandir is real."""
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path) != folder:
            return real_scandir(path)
        with real_scandir(path) as scanner:
            return _Listing(_Proxy(e, methods) if e.name == name else e for e in scanner)

    monkeypatch.setattr(os, "scandir", fake_scandir)


def test_it_finds_resume_and_cv_files_by_name_only(tmp_path: Path) -> None:
    touch(tmp_path / "Desktop" / "Jane_Resume.pdf", age=300)
    touch(tmp_path / "Documents" / "jobs" / "CV 2026.docx", age=100)
    touch(tmp_path / "Downloads" / RESUME_ACCENTED, age=200)
    touch(tmp_path / "Downloads" / "notes.pdf")
    touch(tmp_path / "Documents" / "a" / "b" / "c" / "resume.pdf")
    listed, found, unusable, truncated = resume_finder.find_resume_files(tmp_path)
    assert names(listed, tmp_path) == [
        "Documents/jobs/CV 2026.docx", f"Downloads/{RESUME_ACCENTED}", "Desktop/Jane_Resume.pdf"]
    assert (found, unusable, truncated) == (3, 0, False)


def test_equal_times_sort_by_path(tmp_path: Path) -> None:
    touch(tmp_path / "Documents" / "b-resume.pdf")
    touch(tmp_path / "Documents" / "a-resume.pdf")
    listed = resume_finder.find_resume_files(tmp_path)[0]
    assert names(listed, tmp_path) == ["Documents/a-resume.pdf", "Documents/b-resume.pdf"]


@pytest.mark.parametrize(("name", "matches"), [
    ("cvs-receipt.pdf", False), ("ACV_report.docx", False), ("my-cv.pdf", True),
    ("CV.docx", True), ("Curriculum Vitae.pdf", True), ("resume2026.pdf", True),
    ("preresume-notes.docx", True), ("cv2026.pdf", False),
])
def test_cv_needs_a_whole_word(tmp_path: Path, name: str, matches: bool) -> None:
    touch(tmp_path / "Desktop" / name)
    listed, found, *_ = resume_finder.find_resume_files(tmp_path)
    assert (len(listed) == 1) is matches and found == (1 if matches else 0)


@pytest.mark.parametrize("twin", ["OneDrive", "OneDrive - Example Org"])
def test_onedrive_folders_are_searched(tmp_path: Path, twin: str) -> None:
    touch(tmp_path / twin / "Documents" / "resume.docx")
    touch(tmp_path / twin / "Desktop" / "cv.pdf")
    listed, found, *_ = resume_finder.find_resume_files(tmp_path)
    assert sorted(names(listed, tmp_path)) == [f"{twin}/Desktop/cv.pdf", f"{twin}/Documents/resume.docx"]
    assert found == 2


def test_links_hidden_folders_and_other_types_are_skipped(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    touch(real / "resume.pdf")
    (tmp_path / "Documents").mkdir()
    try:
        os.symlink(real, tmp_path / "Documents" / "linked", target_is_directory=True)
        os.symlink(real / "resume.pdf", tmp_path / "Documents" / "cv-link.pdf")
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symbolic links")
    touch(tmp_path / "Documents" / ".hidden" / "resume.pdf")
    touch(tmp_path / "Documents" / "resume.txt")
    touch(tmp_path / "Documents" / "resume.pdf.bak")
    listed, found, *_ = resume_finder.find_resume_files(tmp_path)
    assert listed == [] and found == 0


def test_a_junction_is_not_entered(tmp_path: Path, monkeypatch) -> None:
    touch(tmp_path / "Documents" / "linked-folder" / "resume.pdf")
    tag = resume_finder._MOUNT_POINT
    wrap_entry(monkeypatch, tmp_path / "Documents", "linked-folder",
               stat=lambda follow_symlinks=True: _Stat(
                   (tmp_path / "Documents" / "linked-folder").stat(), st_reparse_tag=tag))
    listed, found, *_ = resume_finder.find_resume_files(tmp_path)
    assert listed == [] and found == 0


def test_a_file_that_vanishes_during_the_walk_is_skipped(tmp_path: Path, monkeypatch) -> None:
    touch(tmp_path / "Desktop" / "gone-resume.pdf")
    touch(tmp_path / "Desktop" / "resume-two.pdf")

    def vanished(follow_symlinks=True):
        raise FileNotFoundError("gone")

    wrap_entry(monkeypatch, tmp_path / "Desktop", "gone-resume.pdf", stat=vanished)
    listed, found, unusable, _ = resume_finder.find_resume_files(tmp_path)
    assert names(listed, tmp_path) == ["Desktop/resume-two.pdf"]
    assert (found, unusable) == (1, 0)


def test_each_folder_has_its_own_entry_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resume_finder, "MAX_ENTRIES", 5)
    for number in range(6):
        touch(tmp_path / "Documents" / f"filler-{number}.txt")
    touch(tmp_path / "Downloads" / "resume.pdf")
    result = resume_finder.find_resume_files(tmp_path)
    assert names(result[0], tmp_path) == ["Downloads/resume.pdf"]
    assert result[3] is True


@pytest.mark.parametrize("char", [
    '"', "`", "$", ";", "&", "|", "\n", "\r", chr(27), chr(127),
    pytest.param("\\", marks=pytest.mark.skipif(
        os.name == "nt", reason="a backslash is a folder separator on Windows")),
])
def test_paths_the_skill_cannot_pass_are_counted_not_listed(tmp_path: Path, char: str) -> None:
    try:
        touch(tmp_path / "Desktop" / f"resume{char}one.pdf")
    except OSError:
        pytest.skip(f"this OS cannot create a file name holding {char!r}")
    touch(tmp_path / "Desktop" / "resume-two.pdf")
    listed, found, unusable, _ = resume_finder.find_resume_files(tmp_path)
    assert names(listed, tmp_path) == ["Desktop/resume-two.pdf"]
    assert (found, unusable) == (1, 1)


def test_a_quote_in_the_home_folder_path_counts_every_match_as_unusable(tmp_path: Path) -> None:
    home = tmp_path / 'ann "x" home'
    try:
        touch(home / "Desktop" / "resume.pdf")
    except OSError:
        pytest.skip("this OS cannot create a folder name holding a quote")
    assert resume_finder.find_resume_files(home) == ([], 0, 1, False)


def test_at_most_4_are_listed_and_no_file_is_opened(tmp_path: Path, monkeypatch) -> None:
    for number in range(12):
        touch(tmp_path / "Downloads" / f"resume-{number:02d}.pdf", age=number)
    touch(tmp_path / "Downloads" / "cv-extra.docx", age=50)

    def refuse(*args, **kwargs):
        raise AssertionError(f"a file was opened: {args}")

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if str(file).lower().endswith((".pdf", ".docx")):
            refuse(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    listed, found, unusable, _ = resume_finder.find_resume_files(tmp_path)
    assert len(listed) == 4 and found == 13 and unusable == 0
    assert names(listed, tmp_path)[0] == "Downloads/resume-00.pdf"


def test_a_missing_home_folder_finds_nothing(tmp_path: Path) -> None:
    assert resume_finder.find_resume_files(tmp_path / "nowhere") == ([], 0, 0, False)


def test_the_walk_stops_after_the_entry_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resume_finder, "MAX_ENTRIES", 5)
    for number in range(9):
        touch(tmp_path / "Desktop" / f"resume-{number}.pdf")
    listed, found, _, truncated = resume_finder.find_resume_files(tmp_path)
    assert found == 5 and len(listed) == 4 and truncated is True


def test_the_command_lists_names_only_and_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    touch(home / "Desktop" / "Jane Resume.pdf")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    folder = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    capsys.readouterr()
    assert cli.main(["find-resume", "--data-dir", str(folder)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["wrote"] == []
    assert out["data"]["matches"] == [(home / "Desktop" / "Jane Resume.pdf").as_posix()]
    assert out["data"]["counts"] == {"found": 1, "unusable": 0, "truncated": False}
    assert "picker" in out["next"]


def test_the_command_needs_a_set_up_folder(tmp_path: Path, capsys) -> None:
    code = cli.main(["find-resume", "--data-dir", str(tmp_path / "missing")])
    out = json.loads(capsys.readouterr().out)
    assert code != 0 and out["ok"] is False
