"""resume_finder: list resume-like file names in the usual folders without opening anything.

Purpose: a user who does not know where their resume lives can let the tool look by file name, so
the start skill can show the matches in a picker.
Logic: find_resume_files walks Desktop, Documents and Downloads under the home folder, plus the
Desktop and Documents of every home folder whose name starts with OneDrive (OneDrive, and the work
or school "OneDrive - <Org>"), one level of subfolders deep, with os.scandir. A file matches when
it ends in .pdf or .docx and its stem, with accents removed and case folded, holds "resume" or
"curriculum", or holds "cv" as a whole word. Matches sort newest first by modification time, then
by path. The first `limit` usable matches are listed.
Guardrails: only directory entries are read, never a file, so nothing is opened. Links and Windows
directory junctions are never followed, names that start with a dot are skipped, and each top
folder (with its subfolders) has its own budget of MAX_ENTRIES entries, so a huge folder cannot
slow the start or starve the others; when any folder runs out, `truncated` is true. A match whose
path holds a double quote, backtick, dollar sign, semicolon, ampersand, pipe, backslash or control
character cannot be passed safely on a command line or shown in a picker, so it is counted in
`unusable` and left out of the list. A file that cannot be statted (it vanished, it is locked) is
skipped. `found` counts usable matches before the limit.
QA checklist: tests/test_find_resume.py covers the name rules, the depth, links, junctions, hidden
folders, unusable paths (test_paths_the_skill_cannot_pass_are_counted_not_listed), the per-folder
budget (test_each_folder_has_its_own_entry_budget), the vanished file, the OneDrive twins, the
limit and a monkeypatched open that fails on any PDF or DOCX path.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path

SEARCH_FOLDERS: tuple[str, ...] = ("Desktop", "Documents", "Downloads")
ONEDRIVE_FOLDERS: tuple[str, ...] = ("Desktop", "Documents")
SUFFIXES: tuple[str, ...] = (".pdf", ".docx")
MAX_ENTRIES = 5000
# as_posix() turns Windows separators into "/", so a backslash left in that string is a literal one
# in a macOS or Linux file name; that is why it is safe to refuse on every OS.
UNSAFE_CHARACTERS = frozenset('"`$;&|\\\x7f').union(map(chr, range(32)))
# A Windows directory junction is a reparse point with this tag (Windows SDK value); OneDrive cloud
# files are reparse points with other tags and stay searchable. Same test as shutil._rmtree_isdir.
_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def reads_like_a_resume(stem: str) -> bool:
    """True when the stem holds resume or curriculum anywhere, or cv as a whole word."""
    plain = _plain(stem)
    return "resume" in plain or "curriculum" in plain or "cv" in _NOT_ALNUM.split(plain)


def _is_junction(entry: os.DirEntry) -> bool:
    """True for a Windows directory junction, or a folder whose stat fails (do not enter it)."""
    try:
        return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) == _MOUNT_POINT
    except OSError:
        return True


def _scan(folder: Path, budget: list[int]) -> tuple[list[os.DirEntry], list[Path]]:
    """The plain files and the real subfolders of one folder, each entry counted against the budget.

    The budget goes negative when an entry past the cap was seen, which is how the caller knows the
    walk was cut short.
    """
    files: list[os.DirEntry] = []
    subfolders: list[Path] = []
    try:
        with os.scandir(folder) as scanner:
            for entry in scanner:
                budget[0] -= 1
                if budget[0] < 0:
                    break
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if _is_junction(entry):
                        continue
                    subfolders.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry)
    except OSError:
        pass
    return files, subfolders


def _is_wanted_name(name: str) -> bool:
    stem, dot, suffix = name.rpartition(".")
    return bool(dot) and f".{suffix}".casefold() in SUFFIXES and reads_like_a_resume(stem)


# the start skill's picker shows four
def find_resume_files(home: Path, limit: int = 4) -> tuple[list[Path], int, int, bool]:
    """Return (listed, found, unusable, truncated) for resume-like PDF and DOCX names under home."""
    tops = [home / name for name in SEARCH_FOLDERS]
    for onedrive in home.glob("OneDrive*"):
        tops.extend(onedrive / name for name in ONEDRIVE_FOLDERS)
    matches: list[tuple[float, Path]] = []
    unusable = 0
    truncated = False
    for top in tops:
        budget = [MAX_ENTRIES]
        files, subfolders = _scan(top, budget)
        for subfolder in subfolders:
            files.extend(_scan(subfolder, budget)[0])
        truncated = truncated or budget[0] < 0
        for entry in files:
            if not _is_wanted_name(entry.name):
                continue
            path = Path(entry.path)
            if UNSAFE_CHARACTERS & set(path.as_posix()):
                unusable += 1
                continue
            try:
                matches.append((entry.stat(follow_symlinks=False).st_mtime, path))
            except OSError:
                continue
    matches.sort(key=lambda item: (-item[0], item[1].as_posix()))
    return [path for _, path in matches[:limit]], len(matches), unusable, truncated
