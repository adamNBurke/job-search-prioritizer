"""Title families: turn the user's approved job titles into safe matchers.

Purpose: the scan keeps only roles whose title is in a family the user approved, and the scorer
reads the same families, so the scanner and the scorer can never disagree about what a target
role looks like.
Logic: normalize_title lowercases, NFKC-normalizes, drops text in parentheses and after " - " or
"|", removes seniority words and collapses spaces. compile_families turns every family term into
a case-insensitive pattern with word boundaries where spaces, hyphens and slashes are
interchangeable. match_family tests every family. When a title matches more than one, the family
whose matching term has the most words wins, then the higher rung, then the lower id,
so config order never changes the result and "Business Operations Manager" is scored as that
family, not as "Operations Manager". A regex entry counts as zero words, so a broad pattern never
outranks a specific term. A term is tried against both the normalized title and the plain
lowercased title, so "head of operations" still matches although normalizing removes "head of".
excluded_term checks the plain lowercased title, because normalizing would remove the very words
(intern, junior) an exclusion names.
function_excluded runs match_family and then tests the words the user approved for that
one family (title.families[].exclude) on the same plain title, so "Operations Manager, Legal" is
caught; the scan's title gate never reads these words, only rank does.
Guardrails: terms are escaped before compiling, so a model-written term can never be a pattern.
Family exclude words go through the same escaping, are whole words, and apply only to the family
match_family picks for the title.
Optional regex entries are capped at 200 characters and a pattern that does not compile becomes
a plain-English UserError. No config value is ever evaluated as code.
QA checklist: a new rule is covered in tests/test_pipeline_tracer.py or the families test
that replaces it; the overlap rule (most words, rung, id, regex as zero words, order-free) is
covered in tests/test_criteria_build.py. Seniority words live in two lists for two jobs:
SENIORITY_TOKENS are the words normalize_title strips from a title, and data/seniority.json holds
the per-level words scoring reads to name a title's level.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from jobsearch.errors import UserError

REGEX_MAX_LENGTH = 200
EXCLUDE_WORDS_MAX = 20  # words one family may rule out
TERM_MAX = 80  # characters in one term or exclude word
LEVELS = ("entry", "mid", "senior", "lead", "manager", "director", "executive")

SENIORITY_TOKENS = frozenset({
    "intern", "junior", "jr", "associate", "senior", "sr", "lead", "staff", "principal",
    "director", "vp", "chief", "i", "ii", "iii", "iv",
})
_SENIORITY_PHRASES = ("head of", "vice president")
_PARENS = re.compile(r"\([^)]*\)")
_TAIL = re.compile(r"\s+-\s+|\|")
_SEPARATORS = re.compile(r"[\s\-/]+")
_PHRASE_PATTERNS = tuple(
    re.compile(rf"(?<!\w){re.escape(p)}(?!\w)") for p in _SENIORITY_PHRASES
)


def _plain(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", title).lower().split())


def normalize_title(title: str) -> str:
    text = _plain(title)
    text = _PARENS.sub(" ", text)
    text = _TAIL.split(text)[0]
    for pattern in _PHRASE_PATTERNS:
        text = pattern.sub(" ", text)
    words = [w for w in text.split() if w.strip(",.;:") not in SENIORITY_TOKENS]
    return " ".join(words)


@dataclass(frozen=True)
class FamilyMatch:
    family_id: str
    label: str
    rung: int


@dataclass(frozen=True)
class CompiledFamily:
    family_id: str
    label: str
    rung: int
    patterns: tuple[re.Pattern[str], ...]
    term_words: tuple[int, ...]  # words in each pattern's term; 0 for a regex entry
    excludes: tuple[tuple[str, re.Pattern[str]], ...] = ()  # the user's words for this family


@dataclass(frozen=True)
class CompiledFamilies:
    families: tuple[CompiledFamily, ...]
    excludes: tuple[tuple[str, re.Pattern[str]], ...]


def _term_words(term: str) -> list[str]:
    return [w for w in _SEPARATORS.split(_plain(term)) if w]


def _term_pattern(term: str) -> re.Pattern[str] | None:
    words = _term_words(term)
    if not words:
        return None
    body = r"[\s\-/]+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _regex_pattern(source: object, family_label: str) -> re.Pattern[str]:
    if not isinstance(source, str) or len(source) > REGEX_MAX_LENGTH:
        raise UserError(
            f'A pattern in the title family "{family_label}" is too long or is not text. '
            f"Patterns are limited to {REGEX_MAX_LENGTH} characters."
        )
    try:
        return re.compile(source, re.IGNORECASE)
    except re.error:
        raise UserError(
            f'A pattern in the title family "{family_label}" is not a valid pattern.'
        ) from None


def compile_families(criteria: dict) -> CompiledFamilies:
    title = criteria.get("title", {})
    families: list[CompiledFamily] = []
    for family in title.get("families", []):
        label = str(family.get("label", family.get("id", "")))
        patterns: list[re.Pattern[str]] = []
        words: list[int] = []
        for term in family.get("terms", []):
            pattern = _term_pattern(str(term))
            if pattern is not None:
                patterns.append(pattern)
                words.append(len(_term_words(str(term))))
        for source in family.get("regex", []):
            patterns.append(_regex_pattern(source, label))
            words.append(0)  # a pattern never outranks a specific term
        family_excludes: list[tuple[str, re.Pattern[str]]] = []
        for word in family.get("exclude", []):
            word_pattern = _term_pattern(str(word))
            if word_pattern is not None:
                family_excludes.append((str(word), word_pattern))
        families.append(CompiledFamily(
            family_id=str(family["id"]), label=label, rung=int(family["rung"]),
            patterns=tuple(patterns), term_words=tuple(words),
            excludes=tuple(family_excludes),
        ))
    excludes: list[tuple[str, re.Pattern[str]]] = []
    for group in title.get("exclude", []):
        for term in group.get("terms", []):
            pattern = _term_pattern(str(term))
            if pattern is not None:
                excludes.append((str(term), pattern))
    return CompiledFamilies(families=tuple(families), excludes=tuple(excludes))


def match_family(title: str, compiled: CompiledFamilies) -> FamilyMatch | None:
    """The family a title belongs to: most words in the matching term, then rung, then lower id."""
    candidates = (normalize_title(title), _plain(title))
    best: tuple[tuple[int, int], str, CompiledFamily] | None = None
    for family in compiled.families:
        words = max((count for pattern, count in zip(family.patterns, family.term_words)
                     if any(pattern.search(text) for text in candidates)), default=None)
        if words is None:
            continue
        # Larger words and rung win; the lower id wins a tie, so config order never matters.
        if best is None or (words, family.rung) > best[0] or (
                (words, family.rung) == best[0] and family.family_id < best[1]):
            best = ((words, family.rung), family.family_id, family)
    if best is None:
        return None
    winner = best[2]
    return FamilyMatch(winner.family_id, winner.label, winner.rung)


def excluded_term(title: str, compiled: CompiledFamilies) -> str | None:
    text = _plain(title)
    for term, pattern in compiled.excludes:
        if pattern.search(text):
            return term
    return None


def function_excluded(title: str, compiled: CompiledFamilies) -> tuple[str, str] | None:
    """(word, family label) when the title holds a word its own family rules out, else None."""
    match = match_family(title, compiled)
    if match is None:
        return None
    text = _plain(title)
    for family in compiled.families:
        if family.family_id != match.family_id:
            continue
        for word, pattern in family.excludes:
            if pattern.search(text):
                return word, family.label
    return None


def word_is_matchable(word: str) -> bool:
    """True when the word holds a letter or digit the matcher can find in a title.

    A word such as "-" or "/" has none: the compiler would drop it without a message, so the
    validators refuse it instead.
    """
    return _term_pattern(word) is not None


def rules_out_own_terms(word: str, terms: list[str]) -> bool:
    """True when the word matches inside one of the family's own terms: it would then reject
    every title the family can match, so it is never accepted."""
    pattern = _term_pattern(word)
    return pattern is not None and any(pattern.search(_plain(term)) for term in terms)


def exclude_word_errors(where: str, words: object, family: dict) -> list[str]:
    """Plain-English problems with a family's exclude words, each starting with its path.

    Shared by criteria.validate_criteria (the saved config) and titles.apply_exclusions (the
    agent-written titles/exclusions.json), so the two can never disagree.
    """
    if not isinstance(words, list):
        return [f"{where}: must be a list of words."]
    errors: list[str] = []
    if len(words) > EXCLUDE_WORDS_MAX:
        errors.append(f"{where}: has more than {EXCLUDE_WORDS_MAX} words.")
    own = [t for t in family.get("terms", []) if isinstance(t, str)]
    label = family.get("label", family.get("id", ""))
    seen: set[str] = set()
    for index, word in enumerate(words):
        here = f"{where}[{index}]"
        if not isinstance(word, str) or not word.strip():
            errors.append(f"{here}: must be a word, not empty and not a number.")
            continue
        text = word.strip()
        if len(text) > TERM_MAX:
            errors.append(f"{here}: is longer than {TERM_MAX} characters.")
        elif text.casefold() in seen:
            errors.append(f'{here}: "{text}" is listed twice.')
        elif not word_is_matchable(text):
            errors.append(f'{here}: "{text}" has no letters or digits to match.')
        else:
            seen.add(text.casefold())
            if rules_out_own_terms(text, own):
                errors.append(f'{here}: "{text}" would rule out every title in "{label}".')
    return errors
