"""Own-words cover letter: the folder beside a ranked role, the word count, the save.

Purpose: the README promises at least 80% of a letter body is the user's own words, and that the
tool never adds a fact the user did not give. This module makes the 80% checkable. letter-start makes
applications/<role key>/ and letter-check saves letter.md only when at most EDIT_SHARE_MAX percent
of the draft body's words are not the user's.
Logic: role_key turns a role id into a folder name that is valid on Windows and never shared by
two ids. start_letter finds the role in the newest ranked JSON (marks.load_ranked_role), writes
role.json and posting.txt (a saved posting.txt is kept when the posting is no longer found), and
touches nothing the user or the letter skill wrote. changed_words counts, for every body sentence,
the words that are not the user's: a word is the user's when it sits in a run of 3 or more words
in a row that appears in order inside one sentence of a source (answers.md or a writing sample),
compared without case, quote style or punctuation. Reordering, cutting and joining the user's
sentences cost nothing; an added or swapped word costs one. A run that drops a negation does not
count (not, no, never, without, cannot, or a word ending in n't, before the run in its source
sentence). A sentence of under 3 words is the user's only as a whole sentence of a source. Markdown
headings of answers.md are never a source. The body leaves out a draft line that matches a line of
at most FRAME_MAX_WORDS words under "## Greeting" or "## Sign-off" in answers.md; those lines are
the picked opening and closing and are never a source. borrowed_lines lists
draft lines that share a run of 8 or more words with the posting. check_letter runs the
count, the tone gate and the posting check, writes check.md, and writes letter.md only when the
count is within the limit; the tone gate and the posting check advise and never block the save.
Saved writing samples (profile/samples, .md and .txt) are sources equal to the answers. An
approved typo fix in fixes.json lets one corrected word count as the user's: load_fixes
accepts only single-word, edit-distance 1 or 2 changes of a word that is in a source. A letter.md
that differs from the one being saved is kept as letter-v<N>.md first.
Guardrails: user files are read through fsio.read_user_file (inside the data folder, byte cap before
the read, utf-8-sig, LF). Every write goes through fsio. start_letter never changes answers.md,
draft.md or fixes.json. A refusal writes no letter.md. The share is integer math, so the percent
shown and the save decision always agree. Posting text is data: it passes output.clean_text, and
report excerpts pass output.md_escape. The code counts words, not facts; the skill forbids a new
fact and the user reads the letter before sending it.
QA checklist: tests/test_letter.py holds the positive tests and a negative test for the 20% limit
and its boundary, a join, a changed word, a heading-only sentence, a dropped negation, a tiny
piece and a frame line over 8 words; docs/data-files.md "Letter files" documents every file
named here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from jobsearch import fsio, marks, output, paths, roles_store, tone_gate
from jobsearch.errors import UserError

ROLE_KEY_MAX = 60
DRAFT_MAX_BYTES = 40000
ANSWERS_MAX_BYTES = 200000
POSTING_MAX_CHARS = 20000
POSTING_READ_MAX_BYTES = 100000
SAMPLE_MAX_BYTES = 100000
SAMPLES_MAX_FILES = 20
SAMPLE_SUFFIXES = (".md", ".txt")
FIXES_MAX = 50
FIXES_MAX_BYTES = 20000
FIXES_SCHEMA = 1
FIX_DISTANCE_MAX = 2
BORROWED_MIN_WORDS = 8
MIN_SENTENCE_WORDS = 3
# The most percent of body words that may be other than the user's.
EDIT_SHARE_MAX = 20
FRAME_MAX_WORDS = 8
_NOT_RANKED = "is not in your latest ranked list. Run rank, then pick a role from it."
_EXCERPT_CHARS = 80
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_JOINERS = "'-"
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_KEY_JUNK = re.compile(r"[^a-z0-9._-]+")
_WORDS = re.compile(r"\w+(?:'\w+)*")
_TOKENS = re.compile(r"\w+(?:['-]\w+)*")
_FRAME_HEADINGS = frozenset({"greeting", "sign-off"})
_HEADING = re.compile(r"\s*#{1,6}(?:\s|$)")
_NEGATORS = frozenset({"not", "no", "never", "without", "cannot"})

DRAFT_FILE = "draft.md"
ANSWERS_FILE = "answers.md"
CHECK_FILE = "check.md"
LETTER_FILE = "letter.md"
POSTING_FILE = "posting.txt"
FIXES_FILE = "fixes.json"
_VERSION_FILE = re.compile(r"letter-v(\d+)\.md")
ROLE_FILE = "role.json"
ROLE_SCHEMA = 1


# Folder -------------------------------------------------------------------------------------

def role_key(role_id: str) -> str:
    """A folder name for a role id: a-z, 0-9, period, underscore and hyphen, plus a hash.

    The readable part is cut to ROLE_KEY_MAX characters; the first 8 hex characters of the
    sha256 of the id keep two ids that read alike (a colon and a hyphen both become a hyphen)
    in separate folders. No colon, no leading dot or hyphen, no trailing dot: valid on Windows.
    """
    readable = _KEY_JUNK.sub("-", role_id.casefold()).strip("-.")[:ROLE_KEY_MAX].strip("-.")
    digest = hashlib.sha256(role_id.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{readable or 'role'}-{digest}"


def letter_rel(role_id: str) -> str:
    return f"{paths.LAYOUT['applications']}/{role_key(role_id)}"


def letter_dir(folder: Path, role_id: str) -> Path:
    return folder / paths.LAYOUT["applications"] / role_key(role_id)


# Reading the user's files -------------------------------------------------------------------

def _sections(text: str):
    """(kind, line) for every line of answers.md: "heading", "frame" (a line under Greeting or
    Sign-off) or "body". A section runs from its heading to the next heading line."""
    frame = False
    for line in text.split("\n"):
        if _HEADING.match(line):
            frame = line.strip().lstrip("#").strip().casefold() in _FRAME_HEADINGS
            yield "heading", line
        else:
            yield ("frame" if frame else "body"), line


def answer_sources(text: str) -> str:
    """The user's answers: answers.md without heading lines (#{1,6}) and without the lines of its
    Greeting and Sign-off sections, which are the frame of the letter and never a source."""
    return "\n".join(line for kind, line in _sections(text) if kind == "body")


def frame_lines(text: str) -> frozenset[str]:
    """The folded lines under Greeting and Sign-off in answers.md of at most FRAME_MAX_WORDS words."""
    folded = (_fold(line) for kind, line in _sections(text) if kind == "frame")
    return frozenset(line for line in folded
                     if line and len(_TOKENS.findall(line)) <= FRAME_MAX_WORDS)


# letter-start -------------------------------------------------------------------------------

def find_ranked_role(folder: Path, role_id: str) -> dict:
    """The role's facts from the newest ranked JSON, with text passed through clean_text."""
    _, ranked, role = marks.load_ranked_role(folder, role_id)
    if role is None:
        raise UserError(f"The role {output.shown(role_id)} {_NOT_RANKED}")
    return {
        "role_id": role_id,
        "title": output.clean_text(str(role.get("title", ""))),
        "company": output.clean_text(str(role.get("company", ""))),
        "url_apply": output.clean_text(str(role.get("url_apply", ""))),
        "tier": str(role.get("tier", "")),
        "score": role.get("score"),
        "scan_date": str(ranked.get("scan_date", "")),
        "criteria_version": str(ranked.get("criteria_version", "")),
    }


def _posting_text(folder: Path, role_id: str) -> str:
    try:
        facts = roles_store.load_rankable(folder).facts
    except UserError:
        return ""
    for fact in facts:
        if fact.posting.role_id == role_id:
            return output.clean_text(fact.posting.description_text).strip()[:POSTING_MAX_CHARS]
    return ""


def start_letter(folder: Path, role_id: str) -> dict:
    """Make applications/<role key>/ with role.json and posting.txt. Returns what it found."""
    role = find_ranked_role(folder, role_id)
    target = letter_dir(folder, role_id)
    target.mkdir(parents=True, exist_ok=True)
    fsio.write_json_atomic(target / ROLE_FILE, {"schema_version": ROLE_SCHEMA, **role})
    posting = _posting_text(folder, role_id)
    posting_file = target / POSTING_FILE
    # A closed posting cannot be fetched again, so an earlier saved text is never emptied.
    if posting or not posting_file.exists():
        fsio.write_text_atomic(posting_file, posting + "\n" if posting else "")
    return {
        **role,
        "folder": letter_rel(role_id),
        "has_answers": (target / ANSWERS_FILE).is_file(),
        "has_draft": (target / DRAFT_FILE).is_file(),
        "posting_found": bool(posting_file.read_bytes().strip()),
    }


# Samples and approved typo fixes ----------------------------------------------------------

def samples_dir(folder: Path) -> Path:
    return folder / paths.LAYOUT["writing_samples"]


def load_samples(folder: Path) -> list[str]:
    """The text of every .md and .txt file directly in profile/samples, sorted by name."""
    directory = samples_dir(folder)
    if not directory.is_dir():
        return []
    files = sorted((p for p in directory.iterdir()
                    if p.is_file() and p.suffix.lower() in SAMPLE_SUFFIXES),
                   key=lambda p: p.name)
    if len(files) > SAMPLES_MAX_FILES:
        raise UserError(f"There are more than {SAMPLES_MAX_FILES} writing samples in "
                        f"{paths.LAYOUT['writing_samples']}. Keep the best {SAMPLES_MAX_FILES}.")
    return [fsio.read_user_file(p, folder, SAMPLE_MAX_BYTES,
                           f"The writing sample {output.clean_text(p.name)}") for p in files]


def _distance(a: str, b: str) -> int:
    """Levenshtein distance, cut short once it is clearly above FIX_DISTANCE_MAX."""
    if abs(len(a) - len(b)) > FIX_DISTANCE_MAX:
        return FIX_DISTANCE_MAX + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        row = [i]
        for j, char_b in enumerate(b, start=1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = row
    return previous[-1]


def _bad_fixes(reason: str) -> UserError:
    return UserError(f"{FIXES_FILE} was not used: {reason}")


def load_fixes(path: Path, folder: Path, sources: list[str]) -> list[tuple[str, str]]:
    """The approved typo fixes in fixes.json as (from, to) pairs; none when the file is absent.

    A fix is one word to one word, an edit distance of 1 or 2 apart, and the from word must be in
    a source. Anything else is refused, because a bigger edit is a change of the user's words.
    """
    if not path.is_file():
        return []
    raw = fsio.read_user_file(path, folder, FIXES_MAX_BYTES, FIXES_FILE)
    try:
        data = json.loads(raw)
    except ValueError:
        raise _bad_fixes("it is not valid JSON.") from None
    if (not isinstance(data, dict) or set(data) != {"schema_version", "fixes"}
            or isinstance(data["schema_version"], bool) or data["schema_version"] != FIXES_SCHEMA
            or not isinstance(data["fixes"], list)):
        raise _bad_fixes('it must be {"schema_version": 1, "fixes": [{"from": "...", "to": "..."}]}.')
    if len(data["fixes"]) > FIXES_MAX:
        raise _bad_fixes(f"it lists more than {FIXES_MAX} fixes.")
    haystacks = [block for block in (_source_lines(s) for s in sources) if block]
    fixes: list[tuple[str, str]] = []
    for entry in data["fixes"]:
        if (not isinstance(entry, dict) or set(entry) != {"from", "to"}
                or not all(isinstance(entry[k], str) for k in ("from", "to"))):
            raise _bad_fixes('each fix must be exactly {"from": "...", "to": "..."}.')
        old, new = (unicodedata.normalize("NFC", entry[k]) for k in ("from", "to"))
        if not old or not new or any(c.isspace() for c in old + new):
            raise _bad_fixes("each fix must change one word into one word.")
        if not _found_in(_fold(old), haystacks):
            raise _bad_fixes(f'"{output.md_escape(old[:40])}" is not a word in your answers or samples.')
        if not 1 <= _distance(old, new) <= FIX_DISTANCE_MAX:
            raise _bad_fixes(f'"{output.md_escape(old[:40])}" to "{output.md_escape(new[:40])}" '
                             "is more than a typo fix.")
        fixes.append((old, new))
    return fixes


# Provenance ---------------------------------------------------------------------------------

def _fold(text: str) -> str:
    """NFC, straight quotes, casefolded, every run of white space one space."""
    text = unicodedata.normalize("NFC", text).translate(_QUOTES)
    return " ".join(text.casefold().split())


def _is_word(char: str) -> bool:
    return bool(char) and (char == "_" or unicodedata.category(char)[0] in "LNM")


def _bounded(big: str, start: int, end: int) -> bool:
    """True when the match big[start:end] has no word character, hyphen or apostrophe joined to it."""
    before = big[start - 1] if start else ""
    after = big[end] if end < len(big) else ""
    if _is_word(before) or _is_word(after):
        return False
    if before and before in _JOINERS and _is_word(big[start - 2] if start > 1 else ""):
        return False
    if after and after in _JOINERS and _is_word(big[end + 1] if end + 1 < len(big) else ""):
        return False
    return True


def _drops_negation(big: str, at: int) -> bool:
    """True when the front of the source sentence that a cut at big[at] removes holds a negator."""
    front = _SENTENCE_BREAK.split(big[big.rfind("\n", 0, at) + 1:at])[-1]
    return any(word in _NEGATORS or word.endswith("n't") for word in _WORDS.findall(front))


def _found_in(sentence: str, haystacks: list[str], *, skip_negation_cuts: bool = False) -> bool:
    """Is sentence inside a haystack, word-bounded? skip_negation_cuts skips a cut that drops a negator."""
    for big in haystacks:
        at = big.find(sentence)
        while at != -1:
            if _bounded(big, at, at + len(sentence)) and not (
                    skip_negation_cuts and _drops_negation(big, at)):
                return True
            at = big.find(sentence, at + 1)
    return False


def _source_lines(source: str) -> str:
    """One searchable block: every non-empty source line folded, joined by a newline."""
    return "\n".join(line for line in (_fold(raw) for raw in source.split("\n")) if line)


def _word_pattern(word: str) -> re.Pattern[str]:
    """A whole word: no word character, and no hyphen or apostrophe joined to a letter, around it."""
    return re.compile(rf"(?<!\w)(?<!\w['-]){re.escape(word)}(?!\w)(?!['-]\w)")


def _apply_fixes(block: str, fixes: list[tuple[str, str]]) -> str:
    for old, new in fixes:
        block = _word_pattern(_fold(old)).sub(lambda _m, n=_fold(new): n, block)
    return block


def _word_lines(source: str) -> str:
    """One searchable block: each source sentence as one line of its words joined by one space."""
    lines = []
    for raw in source.split("\n"):
        for piece in _SENTENCE_BREAK.split(_fold(raw)):
            words = _TOKENS.findall(piece)
            if words:
                lines.append(" ".join(words))
    return "\n".join(lines)


def _covered(words: list[str], haystacks: list[str]) -> int:
    """How many of words sit in a run of MIN_SENTENCE_WORDS or more found in a haystack.

    A prefix of a clean match is a clean match, so walking each start once and only growing past
    the furthest end found so far gives the exact union of every such run.
    """
    covered = end = 0
    for i in range(len(words) - MIN_SENTENCE_WORDS + 1):
        j = max(i + MIN_SENTENCE_WORDS, end + 1)
        while j <= len(words) and _found_in(" ".join(words[i:j]), haystacks, skip_negation_cuts=True):
            covered += j - max(i, end)
            end = j
            j += 1
    return covered


def changed_words(draft: str, sources: list[str], fixes: list[tuple[str, str]] | None = None,
                  frame: frozenset[str] = frozenset()) -> list[tuple[int, str, int, int]]:
    """(line number, sentence, changed words, words) for every body sentence of the draft.

    A changed word is one that is not in a run of 3 or more words in a row inside one sentence of
    a source. A sentence of under 3 words costs nothing only as a whole sentence of a source. A
    draft line that matches a frame line is left out. Approved fixes are applied to the sources
    as whole-word replacements; the draft itself is never changed.
    """
    haystacks: list[str] = []
    for source in sources:
        block = _word_lines(source)
        if block:
            haystacks.append(block)
            fixed = _apply_fixes(block, fixes) if fixes else block
            if fixed != block:
                haystacks.append(fixed)
    whole = {line for big in haystacks for line in big.split("\n")}
    rows: list[tuple[int, str, int, int]] = []
    for number, line in enumerate(tone_gate.normalize(draft).split("\n"), start=1):
        if _fold(line) in frame:
            continue
        for part in _SENTENCE_BREAK.split(line.strip()):
            words = _TOKENS.findall(_fold(part))
            if not words:
                continue
            if len(words) < MIN_SENTENCE_WORDS:
                changed = 0 if " ".join(words) in whole else len(words)
            else:
                changed = len(words) - _covered(words, haystacks)
            rows.append((number, part.strip(), changed, len(words)))
    return rows


# Posting text -------------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    """Words for the posting check: letters, digits and inner apostrophes, casefolded."""
    return _WORDS.findall(_fold(text))


def borrowed_lines(draft: str, posting_text: str,
                   min_words: int = BORROWED_MIN_WORDS) -> list[tuple[int, str]]:
    """(line number, line) for each draft line sharing min_words or more words in a row with the
    posting. The posting is one stream of words, because its line breaks are only wrapping."""
    posting_words = _words(posting_text)
    runs = {tuple(posting_words[i:i + min_words])
            for i in range(len(posting_words) - min_words + 1)}
    if not runs:
        return []
    found: list[tuple[int, str]] = []
    for number, line in enumerate(tone_gate.normalize(draft).split("\n"), start=1):
        words = _words(line)
        if any(tuple(words[i:i + min_words]) in runs for i in range(len(words) - min_words + 1)):
            found.append((number, line.strip()))
    return found


# letter-check -------------------------------------------------------------------------------

@dataclass(frozen=True)
class LetterCheck:
    folder: str
    changed: list[tuple[int, str, int, int]]
    hits: list[tone_gate.Hit]
    borrowed: list[tuple[int, str]]
    posting_found: bool
    saved: bool
    wrote: list[str]
    kept_earlier: str | None = None

    @property
    def changed_words(self) -> int:
        return sum(row[2] for row in self.changed)

    @property
    def body_words(self) -> int:
        return sum(row[3] for row in self.changed)

    @property
    def share(self) -> int:
        """The percent of body words that are changed, rounded up; 0 for an empty body."""
        return _share(self.changed_words, self.body_words)


def _share(changed: int, total: int) -> int:
    return -(-changed * 100 // total) if total else 0


def _excerpt(text: str) -> str:
    cut = text if len(text) <= _EXCERPT_CHARS else text[:_EXCERPT_CHARS] + "..."
    return output.md_escape(cut)


def _render_report(check: LetterCheck) -> str:
    out = ["# Letter check", "", "## Lines not yet in your own words", ""]
    rows = sorted((r for r in check.changed if r[2]), key=lambda r: -r[2])  # stable: ties keep draft order
    if rows:
        out += [f"{check.changed_words} of {check.body_words} words in the letter body "
                f"({check.share}%) are not from your answers or samples. "
                f"The most allowed is {EDIT_SHARE_MAX}%.", ""]
        out += [f'- Line {number}: "{_excerpt(sentence)}" ({changed} of {words} words)'
                for number, sentence, changed, words in rows]
    else:
        out.append("Every line is in your own words.")
    out += ["", "## Lines that may sound machine-written", ""]
    out += tone_gate.render_hits(check.hits) if check.hits else ["No patterns found."]
    out += ["", "## Lines that match the posting", ""]
    if not check.posting_found:
        out.append("The posting text was not found, so this was not checked.")
    elif check.borrowed:
        out += [f'- Line {number}: "{_excerpt(line)}"' for number, line in check.borrowed]
    else:
        out.append(f"No line shares {BORROWED_MIN_WORDS} or more words in a row with the posting.")
    out += ["", "## Saved", ""]
    if check.saved:
        out.append(f"{LETTER_FILE} was saved.")
    else:
        out.append(f"{LETTER_FILE} was not saved, because more than {EDIT_SHARE_MAX}% of the body "
                   "is not in your own words. Bring the lines above back to your own words first.")
    return "\n".join(out) + "\n"


def _normalized_letter(draft: str) -> str:
    lines = [line.rstrip() for line in tone_gate.normalize(draft).split("\n")]
    return "\n".join(lines).strip("\n") + "\n"


def _save_letter(target: Path, text: str) -> tuple[bool, str | None]:
    """Write letter.md. A different letter already there is kept as letter-v<N>.md first.

    Returns (letter.md was written, the name of the kept earlier letter or None). An identical
    letter is left alone, so a rerun with the same draft changes no byte.
    """
    letter = target / LETTER_FILE
    new = text.encode("utf-8")
    kept: str | None = None
    if letter.is_file():
        old = letter.read_bytes()
        if old == new:
            return False, None
        numbers = [int(m.group(1)) for p in target.iterdir()
                   if (m := _VERSION_FILE.fullmatch(p.name))]
        kept = f"letter-v{max(numbers, default=0) + 1}.md"
        fsio.write_bytes_atomic(target / kept, old)
    fsio.write_bytes_atomic(letter, new)
    return True, kept


def check_letter(folder: Path, role_id: str) -> LetterCheck:
    target = letter_dir(folder, role_id)
    rel = letter_rel(role_id)
    draft_path = target / DRAFT_FILE
    if not draft_path.is_file():
        raise UserError("There is no draft.md in that letter folder yet. Write draft.md first.")
    draft = fsio.read_user_file(draft_path, folder, DRAFT_MAX_BYTES, DRAFT_FILE)
    if not draft.strip():
        raise UserError("The draft is empty. Write draft.md first.")
    answers_path = target / ANSWERS_FILE
    raw = (fsio.read_user_file(answers_path, folder, ANSWERS_MAX_BYTES, ANSWERS_FILE)
           if answers_path.is_file() else "")
    posting_path = target / POSTING_FILE
    posting = (fsio.read_user_file(posting_path, folder, POSTING_READ_MAX_BYTES, POSTING_FILE)
               if posting_path.is_file() else "")
    sources = [answer_sources(raw), *load_samples(folder)]
    fixes = load_fixes(target / FIXES_FILE, folder, sources)
    changed = changed_words(draft, sources, fixes, frame_lines(raw))
    hits = tone_gate.check(draft)
    borrowed = borrowed_lines(draft, posting)
    result = LetterCheck(folder=rel, changed=changed, hits=hits, borrowed=borrowed,
                         posting_found=bool(posting.strip()), saved=False, wrote=[])
    saved = result.changed_words * 100 <= EDIT_SHARE_MAX * result.body_words
    result = replace(result, saved=saved)
    fsio.write_text_atomic(target / CHECK_FILE, _render_report(result))
    wrote = [f"{rel}/{CHECK_FILE}"]
    kept: str | None = None
    if saved:
        written, kept = _save_letter(target, _normalized_letter(draft))
        if kept:
            wrote.append(f"{rel}/{kept}")
        if written:
            wrote.append(f"{rel}/{LETTER_FILE}")
    return replace(result, wrote=wrote, kept_earlier=kept)
