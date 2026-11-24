"""Tone gate: name machine-sounding patterns in a draft, by line and kind.

Purpose: a cover letter in the user's own words should not read like a template. The gate finds
the patterns that give a draft away so the user can reword them. It advises; it never blocks a
save and never rewrites a word.
Logic: check() NFC-normalizes the text, turns CRLF and CR into LF, and scans each line for the
four kinds in KINDS. em_dash is U+2014, U+2015, a spaced double hyphen, or U+2013 with a space on
both sides. stock_phrase is a whole-word, case-insensitive match of a phrase from
data/tone_phrases.json (curly apostrophes count as straight); when two phrases overlap only the
longest is kept. colon_preface is one to four words then a colon and more text, at a line start
or after . ! ? and a space ("The result: we cut costs"), except a greeting start from the data
file ("Dear Sam: ..."). The colon must be followed by whitespace, so a clock time such as 9:30
never matches. contrast_frame is "not just|only|merely|simply ... but" in one sentence, or "it's
not X, it's Y" with a comma or semicolon. Every
hit carries its 1-based line, its 1-based column (code points of the NFC line), its kind, a plain
label and a short excerpt. Hits are sorted by line, then column, then kind, so the same draft
always gives the same bytes. render_hits() turns hits into Markdown list lines.
Guardrails: pure functions; the only file read is the shipped phrase list, loaded once. The
phrase list is literal text, escaped before matching, never compiled as a pattern from data. The
excerpt is the user's own draft text and is escaped with output.md_escape before it reaches a
report. Nothing here edits the draft.
QA checklist: tests/test_tone_gate.py has a positive and a negative control for every kind, an
ordering test, a byte-stability test and a shape test for the phrase file.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from jobsearch import fsio, output

EM_DASH = "em_dash"
STOCK_PHRASE = "stock_phrase"
COLON_PREFACE = "colon_preface"
CONTRAST_FRAME = "contrast_frame"
KINDS = (EM_DASH, STOCK_PHRASE, COLON_PREFACE, CONTRAST_FRAME)
_EXCERPT_CHARS = 80
_LEAD_CHARS = 30
_QUOTES = {"\u2018": "'", "\u2019": "'"}
_DASHES = re.compile(r"[\u2014\u2015]|(?<=\s)(?:--|\u2013)(?=\s)")
_WORD = r"\w+(?:['.-]\w+)*"
_PREFACE = re.compile(rf"({_WORD}(?:\s+{_WORD}){{0,3}}):\s+(?=\S)")
_LIST_LEAD = re.compile(r"[\s>#*+-]*")
_SENTENCE_START = re.compile(r"[.!?]\s+")
_CONTRAST = (
    re.compile(r"(?<!\w)not\s+(?:just|only|merely|simply)(?!\w)[^.!?]*?(?<!\w)but(?!\w)"),
    re.compile(r"(?<!\w)(?:it|that|this)(?:'s|\s+is)\s+not(?!\w)[^.!?;,]*[,;]\s*"
               r"(?:it|that|this)(?:'s|\s+is)(?!\w)"),
)


@dataclass(frozen=True)
class Hit:
    line: int
    column: int
    kind: str
    label: str
    excerpt: str


def normalize(text: str) -> str:
    """NFC, with CRLF and CR turned into LF, so columns and lines mean the same on every OS."""
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    """The shipped phrase list: stock phrases and the greeting starts a colon may follow."""
    path = Path(__file__).resolve().parent / "data" / "tone_phrases.json"
    return fsio.read_json(path)  # type: ignore[return-value]


@functools.lru_cache(maxsize=8)
def _phrase_patterns(phrases: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Each phrase as a whole-word pattern; spaces match any run of white space."""
    compiled = []
    for phrase in phrases:
        body = r"\s+".join(re.escape(word) for word in phrase.split())
        compiled.append((phrase, re.compile(rf"(?<!\w){body}(?!\w)")))
    return tuple(compiled)


def _align(line: str) -> str:
    """Lowercase with curly apostrophes made straight, one output character per input
    character, so a match position in the result is the same column in the line."""
    out = []
    for char in line:
        char = _QUOTES.get(char, char)
        lowered = char.lower()
        out.append(lowered if len(lowered) == 1 else char)
    return "".join(out)


def _excerpt(line: str, start: int) -> str:
    """A window of the line around the hit, at most _EXCERPT_CHARS characters."""
    low = max(0, start - _LEAD_CHARS)
    return line[low:low + _EXCERPT_CHARS].strip()


def _dash_hits(number: int, line: str) -> list[Hit]:
    return [Hit(number, m.start() + 1, EM_DASH, "em dash", _excerpt(line, m.start()))
            for m in _DASHES.finditer(line)]


def _phrase_hits(number: int, line: str) -> list[Hit]:
    aligned = _align(line)
    found: list[tuple[int, int, str]] = []
    for phrase, pattern in _phrase_patterns(tuple(_data()["phrases"])):
        found += [(m.start(), m.end(), phrase) for m in pattern.finditer(aligned)]
    kept: list[tuple[int, int, str]] = []
    for start, end, phrase in sorted(found, key=lambda f: (-(f[1] - f[0]), f[0], f[2])):
        if all(end <= k_start or start >= k_end for k_start, k_end, _ in kept):
            kept.append((start, end, phrase))
    return [Hit(number, start + 1, STOCK_PHRASE, f'stock phrase "{phrase}"',
                _excerpt(line, start)) for start, _, phrase in kept]


def _preface_hits(number: int, line: str) -> list[Hit]:
    exempt = {s.casefold() for s in _data()["colon_preface_exempt_starts"]}
    starts = {_LIST_LEAD.match(line).end()}  # type: ignore[union-attr]
    starts |= {m.end() for m in _SENTENCE_START.finditer(line)}
    hits = []
    for start in sorted(starts):
        found = _PREFACE.match(line, start)
        if found is None:
            continue
        prefix = found.group(1)
        if prefix.split()[0].casefold() in exempt:
            continue
        hits.append(Hit(number, start + 1, COLON_PREFACE, "colon-preface setup",
                        _excerpt(line, start)))
    return hits


def _contrast_hits(number: int, line: str) -> list[Hit]:
    aligned = _align(line)
    return [Hit(number, m.start() + 1, CONTRAST_FRAME, "contrast framing",
                _excerpt(line, m.start()))
            for pattern in _CONTRAST for m in pattern.finditer(aligned)]


def check(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for number, line in enumerate(normalize(text).split("\n"), start=1):
        if not line.strip():
            continue
        hits += _dash_hits(number, line)
        hits += _phrase_hits(number, line)
        hits += _preface_hits(number, line)
        hits += _contrast_hits(number, line)
    return sorted(hits, key=lambda h: (h.line, h.column, h.kind))


def render_hits(hits: list[Hit]) -> list[str]:
    """One Markdown list line per hit: - Line N: <label> in "<excerpt>"."""
    rows = []
    for hit in hits:
        excerpt = output.md_escape(hit.excerpt[:_EXCERPT_CHARS])  # cut first, then escape
        rows.append(f'- Line {hit.line}: {hit.label} in "{excerpt}"')
    return rows
