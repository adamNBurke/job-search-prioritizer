# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Leak gate: fail the build when a personal identifier or LinkedIn fetch code is about to ship.

Purpose
    The public tree is exported fresh from the development repo. This gate runs on that
    export, in the pre-commit hook, and in CI. It looks for two things: shapes that are never
    acceptable in a public tree (pattern rules), and the maintainer's own identifiers (the
    personal denylist, which exists only on the maintainer's machine).

Logic
    1. Load the generic policy (leak_policy.json beside this file).
    2. Load the personal denylist when one exists. Each non-comment line is auto-classified as
       a phone, a date, a street address or a plain term. An optional prefix (term:, phone:,
       date:, address:) sets the kind. Lines are numbered 1..N in file order, ignoring blank
       lines and comments; findings quote that number, never the text.
    3. A sentinel self-test renders every loaded term in a canonical variant into a temp file
       outside the scan root and requires the matcher to flag it. A broken normalizer exits 2
       instead of silently passing.
    4. Walk the root (skipping .git), scan every file name and every text file line by line,
       and print one `FAIL <path>:<line> <rule-id>` per finding, sorted by path then line.
    5. Exit 0 clean, 1 findings, 2 configuration or self-test error.

Matching
    Denylist matching runs on normalized text: NFKC, then NFKD with accent marks dropped,
    casefolded, every dash variant and underscore mapped to a space, whitespace collapsed.
    Invisible characters (Unicode categories Mn, Me and Cf, plus the braille blank and the
    Hangul fillers) are handled in two readings of each line, and a term hits on either: deleted,
    so a zero-width mark inside a word cannot split it, and read as a space, so one standing in
    for the space between two words cannot hide a multi-word term. Joined forms typed with no
    separator at all are out of scope for v1.
    The pattern rules (phone, email, domain and the rest) run on the raw line and on a light
    copy (NFKC, invisible characters deleted, dash variants to a hyphen, every Zs space to a
    plain space; no casefold), and the rule ids are united.
    Terms match whole words only. Phones compare digits only (a leading country digit 1 is
    optional). Dates match ISO, US numeric, day-first numeric and spelled-month forms with
    ordinals, plus year-less spelled forms. Street addresses compare token by token with
    suffix, unit and direction abbreviations folded together.

Denylist location
    --denylist PATH, else the JSP_LEAK_DENYLIST environment value, else a per-user config
    folder (a dot-config folder in the home directory on macOS and Linux, the roaming
    application-data folder on Windows). The file must live outside the scan root and must not
    be tracked by git; either fault exits 2.

Guardrails
    The gate never prints, logs or writes a denylist term, a matched string or a line excerpt,
    on pass or on fail. A file or folder name that matches a term is shown as
    [name-redacted]. Every pattern below is written with a character class inside the literal
    so that it cannot match this file's own source: tools/ ships in the export and is scanned.
    --require-denylist turns a missing, empty, short, in-tree or tracked denylist into exit 2.
    Without the flag a missing denylist prints a SKIPPED line and a qualified PASS, never a
    bare PASS.

QA checklist
    - A new pattern rule gets a planted case in tests/test_leak_gate.py and a safe-shape case.
    - A new denylist kind gets a variant test and a sentinel render.
    - The self-scan test must stay green: run `uv run --script tools/leak_check.py --root tools`.
    - Nothing in the output may contain text taken from a scanned file or the denylist.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_CONFIG = 2

MESSAGE_PATH = "<commit-message>"
# Untracked tool caches. Skipped only by command-line scans of a working tree, never by the
# export or --staged paths, so a tracked cache folder would still fail as a binary file.
CACHE_DIR_NAMES = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
REDACTED_NAME = "[name-redacted]"
POLICY_KEYS = (
    "allowed_email_domains",
    "phone_fiction_exchange",
    "phone_fiction_line_prefix",
    "linkedin_domain_allowed_paths",
    "home_path_placeholders",
    "forbidden_paths",
    "binary_allowlist",
    "min_denylist_terms",
)


class GateError(Exception):
    """A configuration or self-test failure. The message must never carry denylist text."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    shown: str | None = None

    def render(self) -> str:
        return f"FAIL {self.shown if self.shown is not None else self.path}:{self.line} {self.rule}"


@dataclass(frozen=True)
class Term:
    index: int
    kind: str
    text: str


# ---------------------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------------------

_DASH_VARIANTS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\ufe58\ufe63\uff0d"
_DASH_TO_SPACE = dict.fromkeys([ord(ch) for ch in _DASH_VARIANTS + "-_"], " ")
_DASH_TO_HYPHEN = dict.fromkeys([ord(ch) for ch in _DASH_VARIANTS], "-")

# Characters a reader cannot see: Unicode categories Mn (nonspacing marks, which include variation
# selectors and the combining grapheme joiner), Me (enclosing marks) and Cf (format characters:
# zero-width space and joiners, soft hyphen, byte-order mark, bidi controls). Some blank glyphs
# fall in other categories: the braille blank and the Hangul fillers. NFKC maps the Hangul filler
# and its halfwidth form to U+1160, so both the mapped and the original code points are listed.
_INVISIBLE_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
_INVISIBLE_EXTRA = frozenset("\u2800\u115f\u1160\u3164\uffa0")


def _strip_invisible(text: str, replacement: str = "") -> str:
    """Replace every invisible character with `replacement` (default: delete it)."""
    return "".join(
        replacement
        if unicodedata.category(ch) in _INVISIBLE_CATEGORIES or ch in _INVISIBLE_EXTRA
        else ch
        for ch in text
    )


def _fold(text: str, invisible_as: str) -> str:
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    # Accent marks are dropped outright; the other invisible characters become `invisible_as`.
    text = _strip_invisible("".join(ch for ch in text if not unicodedata.combining(ch)), invisible_as)
    text = text.casefold().translate(_DASH_TO_SPACE)
    return " ".join(text.split())


def normalize(text: str) -> str:
    """One normal form for matching: NFKC, NFKD minus accents, invisible characters deleted, casefold, dashes to spaces."""
    return _fold(text, "")


def normalize_spaced(text: str) -> str:
    """Like normalize, but an invisible character stands for a space, so Jane<ZWSP>Doe reads as two words."""
    return _fold(text, " ")


def light_normalize(text: str) -> str:
    """A copy of a line for the pattern rules: NFKC, invisible characters deleted, dashes to hyphens.

    No casefold and no whitespace collapse, so shapes and column positions stay recognizable.
    Every space-like character (NBSP and the other Zs spaces) becomes a plain space.
    """
    text = _strip_invisible(unicodedata.normalize("NFKC", text))
    text = "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in text)
    return text.translate(_DASH_TO_HYPHEN)


# ---------------------------------------------------------------------------------------
# Denylist parsing and classification
# ---------------------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_ABBREVIATIONS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_LOOKUP = {**_MONTHS, **_MONTH_ABBREVIATIONS}

# canonical short form -> spelling used when the sentinel renders an address in full
_SUFFIX_FULL = {
    "st": "street", "ave": "avenue", "rd": "road", "blvd": "boulevard", "dr": "drive",
    "ln": "lane", "ct": "court", "pl": "place", "ter": "terrace", "cir": "circle",
    "pkwy": "parkway", "hwy": "highway", "sq": "square", "trl": "trail",
    "apt": "apartment", "ste": "suite", "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}
_SUFFIX_CANON = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave", "road": "rd",
    "rd": "rd", "boulevard": "blvd", "blvd": "blvd", "drive": "dr", "dr": "dr", "lane": "ln",
    "ln": "ln", "court": "ct", "ct": "ct", "place": "pl", "pl": "pl", "terrace": "ter",
    "ter": "ter", "circle": "cir", "cir": "cir", "parkway": "pkwy", "pkwy": "pkwy",
    "highway": "hwy", "hwy": "hwy", "square": "sq", "sq": "sq", "trail": "trl", "trl": "trl",
    "apartment": "apt", "apt": "apt", "suite": "ste", "ste": "ste",
    "north": "n", "n": "n", "south": "s", "s": "s", "east": "e", "e": "e", "west": "w", "w": "w",
    "northeast": "ne", "ne": "ne", "northwest": "nw", "nw": "nw",
    "southeast": "se", "se": "se", "southwest": "sw", "sw": "sw",
}
_STREET_SUFFIXES = {
    "street", "st", "avenue", "ave", "av", "road", "rd", "boulevard", "blvd", "drive", "dr",
    "lane", "ln", "court", "ct", "place", "pl", "terrace", "ter", "circle", "cir",
    "parkway", "pkwy", "highway", "hwy", "square", "sq", "trail", "trl",
}

_PREFIX = re.compile(r"^(term|phone|date|address)\s*:\s*(.*)$", re.IGNORECASE)
_PHONE_TERM = re.compile(r"^[\d\s+().-]+$")
_ISO_DATE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_US_DATE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_MONTH_FIRST_DATE = re.compile(
    r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", re.IGNORECASE
)
_DAY_FIRST_DATE = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\.?,?\s+(\d{4})$", re.IGNORECASE
)
_HOUSE_NUMBER = re.compile(r"^\d+[A-Za-z]?\s+\S")
_WORDS = re.compile(r"[^\W_]+")


def _valid_date(year: int, month: int, day: int) -> tuple[int, int, int] | None:
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return year, month, day


def parse_date(text: str) -> tuple[int, int, int] | None:
    """Return (year, month, day) for the date shapes the denylist accepts, else None."""
    text = text.strip()
    found = _ISO_DATE.match(text)
    if found:
        return _valid_date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    found = _US_DATE.match(text)
    if found:
        return _valid_date(int(found.group(3)), int(found.group(1)), int(found.group(2)))
    found = _MONTH_FIRST_DATE.match(text)
    if found and found.group(1).lower() in _MONTH_LOOKUP:
        month = _MONTH_LOOKUP[found.group(1).lower()]
        return _valid_date(int(found.group(3)), month, int(found.group(2)))
    found = _DAY_FIRST_DATE.match(text)
    if found and found.group(2).lower() in _MONTH_LOOKUP:
        month = _MONTH_LOOKUP[found.group(2).lower()]
        return _valid_date(int(found.group(3)), month, int(found.group(1)))
    return None


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _looks_like_phone(text: str) -> bool:
    return bool(_PHONE_TERM.match(text)) and 7 <= len(_digits(text)) <= 15


def _looks_like_address(text: str) -> bool:
    if not _HOUSE_NUMBER.match(text):
        return False
    return any(word in _STREET_SUFFIXES for word in _WORDS.findall(text.lower()))


def classify(text: str) -> str:
    """Auto-classify a bare denylist line. A well-formed date wins over the phone shape."""
    if parse_date(text) is not None:
        return "date"
    if _looks_like_phone(text):
        return "phone"
    if _looks_like_address(text):
        return "address"
    return "term"


def _confirm_kind(kind: str, text: str) -> str:
    """An explicit prefix that the value cannot satisfy falls back to a plain term match."""
    if kind == "date" and parse_date(text) is None:
        return "term"
    if kind == "phone" and not _digits(text):
        return "term"
    return kind


def parse_denylist(lines: Iterable[str]) -> list[Term]:
    terms: list[Term] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        prefixed = _PREFIX.match(line)
        if prefixed and prefixed.group(2).strip():
            text = prefixed.group(2).strip()
            kind = _confirm_kind(prefixed.group(1).lower(), text)
        else:
            text = line
            kind = classify(text)
        terms.append(Term(len(terms) + 1, kind, text))
    return terms


def summary_line(terms: list[Term]) -> str:
    counts = {kind: 0 for kind in ("term", "phone", "date", "address")}
    for term in terms:
        counts[term.kind] += 1
    inner = ", ".join(f"{kind} {count}" for kind, count in counts.items())
    return f"denylist: {len(terms)} terms ({inner})"


# ---------------------------------------------------------------------------------------
# Denylist matchers. Each takes a normalized line and returns a label or None.
# ---------------------------------------------------------------------------------------

Matcher = Callable[[str], "str | None"]


def _term_matcher(term: Term) -> Matcher:
    needle = normalize(term.text)
    if not needle:
        return lambda line: None
    pattern = re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)")
    return lambda line: "term" if pattern.search(line) else None


_PHONE_CANDIDATE = re.compile(r"\+?\d[\d\s().-]{5,}\d")


def _phone_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) == 11 and left.startswith("1"):
        left = left[1:]
    if len(right) == 11 and right.startswith("1"):
        right = right[1:]
    return left == right


def _phone_matcher(term: Term) -> Matcher:
    wanted = _digits(term.text)

    def match(line: str) -> str | None:
        if not wanted:
            return None
        for found in _PHONE_CANDIDATE.finditer(line):
            # The candidate is the whole run of digits and separators, so the number may sit
            # next to a date or an id. Try every window of whole digit groups.
            groups = re.findall(r"\d+", found.group(0))
            for start in range(len(groups)):
                joined = ""
                for group in groups[start:]:
                    joined += group
                    if len(joined) > len(wanted) + 1:
                        break
                    if _phone_equal(joined, wanted):
                        return "phone"
        return None

    return match


def _month_alternation(month: int) -> str:
    names = [name for name, number in _MONTH_LOOKUP.items() if number == month]
    names.sort(key=len, reverse=True)
    return r"(?:" + "|".join(re.escape(name) for name in names) + r")\.?"


def _numeric_date_variants(year: int, month: int, day: int) -> set[str]:
    yyyy, yy = f"{year:04d}", f"{year % 100:02d}"
    pairs = {(f"{month:02d}", f"{day:02d}"), (str(month), str(day)),
             (f"{month:02d}", str(day)), (str(month), f"{day:02d}")}
    variants = {f"{yyyy}-{mm}-{dd}" for mm, dd in pairs}
    variants |= {f"{yyyy}/{mm}/{dd}" for mm, dd in pairs}
    variants.add(f"{yyyy}{month:02d}{day:02d}")
    variants.add(f"{yyyy}.{month:02d}.{day:02d}")
    for sep in ("/", "-", "."):
        variants |= {f"{mm}{sep}{dd}{sep}{yyyy}" for mm, dd in pairs}
        variants |= {f"{dd}{sep}{mm}{sep}{yyyy}" for mm, dd in pairs}
    variants |= {f"{mm}/{dd}/{yy}" for mm, dd in pairs}
    return variants


def _date_matcher(term: Term) -> Matcher:
    parsed = parse_date(term.text)
    if parsed is None:
        return lambda line: None
    year, month, day = parsed
    numeric = sorted(
        {normalize(variant) for variant in _numeric_date_variants(year, month, day)},
        key=len,
        reverse=True,
    )
    numeric = [variant for variant in numeric if variant]
    mon = _month_alternation(month)
    dayp = rf"0?{day}(?:st|nd|rd|th)?"
    yearp = str(year)
    guard_start, guard_end = r"(?<!\w)", r"(?!\w)"
    full_parts = [re.escape(variant) for variant in numeric]
    full_parts.append(rf"{mon}\s+{dayp},?\s+{yearp}")
    full_parts.append(rf"{dayp}\s+(?:of\s+)?{mon},?\s+{yearp}")
    full = re.compile(guard_start + "(?:" + "|".join(full_parts) + ")" + guard_end)
    yearless = re.compile(
        guard_start + "(?:" + rf"{mon}\s+{dayp}|{dayp}\s+(?:of\s+)?{mon}" + ")" + guard_end
    )

    def match(line: str) -> str | None:
        if full.search(line):
            return "date"
        if yearless.search(line):
            return "date, year-less form"
        return None

    return match


def _address_tokens(text: str) -> list[str]:
    return [_SUFFIX_CANON.get(word, word) for word in _WORDS.findall(normalize(text))]


def _address_matcher(term: Term) -> Matcher:
    wanted = _address_tokens(term.text)
    if not wanted:
        return lambda line: None
    first, size = wanted[0], len(wanted)

    def match(line: str) -> str | None:
        tokens = [_SUFFIX_CANON.get(word, word) for word in _WORDS.findall(line)]
        for start, token in enumerate(tokens):
            if token == first and tokens[start:start + size] == wanted:
                return "address"
        return None

    return match


_MATCHER_BUILDERS: dict[str, Callable[[Term], Matcher]] = {
    "term": _term_matcher,
    "phone": _phone_matcher,
    "date": _date_matcher,
    "address": _address_matcher,
}


def build_matchers(terms: Iterable[Term]) -> list[tuple[Term, Matcher]]:
    return [(term, _MATCHER_BUILDERS[term.kind](term)) for term in terms]


def denylist_hits(text: str, matchers: list[tuple[Term, Matcher]]) -> list[tuple[Term, str]]:
    """Return (term, label) pairs for one raw line. Never returns matched text."""
    if not matchers:
        return []
    # Two readings of the line: invisible characters deleted, and invisible characters read as a
    # space (a zero-width space is often typed where a space belongs). A term hits on either.
    deleted, spaced = normalize(text), normalize_spaced(text)
    lines = [form for form in dict.fromkeys((deleted, spaced)) if form]
    hits = []
    for term, matcher in matchers:
        for line in lines:
            label = matcher(line)
            if label:
                hits.append((term, label))
                break
    return hits


def _denylist_rule(term: Term, label: str) -> str:
    return f"denylist term #{term.index} ({label})"


# ---------------------------------------------------------------------------------------
# Pattern rules. Written so that none can match this file's own source.
# ---------------------------------------------------------------------------------------

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])"
)
# An extension may follow a number directly: x23, ext23, ext.23, or a bare ext.
_EXT_FOLLOWS = r"(?=(?i:x|ext\.?)\s?\d|(?i:ext)\.?(?!\w))"
_NANP = re.compile(
    r"(?<![\d.])(?:\+?1[ .-]?)?\(?(\d{3})\)?[ .-](\d{3})[ .-](\d{4})(?:(?!\w)|" + _EXT_FOLLOWS + ")"
)
# Compact forms: parenthesized with no space, or no separators at all. The separators are optional, so the area code and
# exchange must start with 2-9 (a real NANP number) to keep 10-digit ids and epoch seconds out,
# and the digit guards on both sides refuse a longer run of digits.
_NANP_COMPACT = re.compile(
    r"(?<![\w.])(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-]?([2-9]\d{2})[ .-]?(\d{4})(?:(?!\w)|"
    + _EXT_FOLLOWS + ")"
)
_INTL = re.compile(r"(?<![\w+])\+\d[\d ().-]{6,}\d")
_SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
_CARD_GROUPED = re.compile(r"(?<![\w.])\d{4}([ -])\d{4}\1\d{4}\1\d{1,4}(?!\w)")
_CARD_BARE = re.compile(
    r"(?<![\w.])(?:3[47]\d{13}|4\d{15}|5[1-5]\d{14}|2[2-7]\d{14}|6011\d{12}|65\d{14})(?!\w|\.\d)"
)
_DOT = r"(?:\.|%2e|\[\.\])"
_LINKEDIN_DOMAIN = re.compile(
    rf"(?:[l]inkedin{_DOT}com|[l]icdn{_DOT}com|[l]nkd{_DOT}in)", re.IGNORECASE
)
_LINKEDIN_FETCH = re.compile(
    r"(?<!\w)(?:[v]oyager|[j]obs-guest|l[i]_at|[J]SESSIONID)(?!\w)", re.IGNORECASE
)
_AUTOMATION = re.compile(
    r"^\s*(?:import|from)\s+(?:[p]laywright|[s]elenium|[p]yppeteer|[u]ndetected_chromedriver"
    r"|[l]inkedin_api)\b",
    re.IGNORECASE,
)
_HOME_PATH = re.compile(
    r"(?:(?<![\w.~])/(?:[U]sers|[h]ome)/|(?<!\w)(?i:[A-Za-z]:[\\/]+[U]sers[\\/]+))"
    r"(<[^<>/\\\s]*>|[^/\\\s\"'`<>()\[\]]+)"
)
_SESSION_URL = re.compile(r"[c]laude\.ai/code/session", re.IGNORECASE)
_TRAILER = re.compile(r"^\s*(?:[C]laude-Session|[C]o-Authored-By)\s*:", re.IGNORECASE)
_SECRET = re.compile(
    r"(?<![\w])(?:gh[pousr]_[A-Za-z0-9]{20,}|github[_]pat[_]\w{20,}|AK[I]A[0-9A-Z]{16}(?![0-9A-Z])"
    r"|sk-an[t]-[A-Za-z0-9_-]{20,}|-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----)"
)
_PLACEHOLDER_START = "$%{*<[~"


def luhn_valid(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_fiction_phone(digits: str, policy: dict) -> bool:
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    return digits[3:6] == policy["phone_fiction_exchange"] and digits[6:].startswith(
        policy["phone_fiction_line_prefix"]
    )


def _path_allowed(rel: str, allowed: list[str]) -> bool:
    for entry in allowed:
        if entry.endswith("/"):
            if rel.startswith(entry):
                return True
        elif rel == entry:
            return True
    return False


def _email_rule(line: str, policy: dict) -> bool:
    allowed = [domain.lower() for domain in policy["allowed_email_domains"]]
    for found in _EMAIL.finditer(line):
        domain = found.group(1).lower()
        if not any(domain == ok or domain.endswith("." + ok) for ok in allowed):
            return True
    return False


def _phone_rule(line: str, policy: dict) -> bool:
    for pattern in (_NANP, _NANP_COMPACT):
        for found in pattern.finditer(line):
            digits = found.group(1) + found.group(2) + found.group(3)
            if not _is_fiction_phone(digits, policy):
                return True
    for found in _INTL.finditer(line):
        digits = _digits(found.group(0))
        if 8 <= len(digits) <= 15 and not _is_fiction_phone(digits, policy):
            return True
    return False


def _card_rule(line: str) -> bool:
    for pattern in (_CARD_GROUPED, _CARD_BARE):
        for found in pattern.finditer(line):
            if luhn_valid(_digits(found.group(0))):
                return True
    return False


def _home_rule(line: str, policy: dict) -> bool:
    placeholders = {name.lower() for name in policy["home_path_placeholders"]}
    for found in _HOME_PATH.finditer(line):
        name = found.group(1).lower()
        if name in placeholders or name.strip("<>") in placeholders:
            continue
        if name[0] in _PLACEHOLDER_START:
            continue
        return True
    return False


def _pattern_rules(rel: str, line: str, policy: dict) -> set[str]:
    rules: set[str] = set()
    if "@" in line and _email_rule(line, policy):
        rules.add("email-domain")
    if _phone_rule(line, policy):
        rules.add("phone-shape")
    if _SSN.search(line):
        rules.add("ssn-shape")
    if _card_rule(line):
        rules.add("card-shape")
    if _LINKEDIN_DOMAIN.search(line) and not _path_allowed(rel, policy["linkedin_domain_allowed_paths"]):
        rules.add("linkedin-domain")
    if _LINKEDIN_FETCH.search(line):
        rules.add("linkedin-fetch")
    if _AUTOMATION.search(line):
        rules.add("automation-import")
    if _home_rule(line, policy):
        rules.add("home-path")
    rules.update(trailer_rules_for_line(line))
    if _SECRET.search(line):
        rules.add("secret-shape")
    return rules


def pattern_rules_for_line(rel: str, line: str, policy: dict) -> list[str]:
    """Return the rule ids that one text line trips. Matched text is never returned.

    The rules run on the raw line and on a lightly normalized copy; the ids are united.
    """
    rules = _pattern_rules(rel, line, policy)
    light = light_normalize(line)
    if light != line:
        rules |= _pattern_rules(rel, light, policy)
    return sorted(rules)


def trailer_rules_for_line(line: str) -> list[str]:
    return ["session-trailer"] if (_SESSION_URL.search(line) or _TRAILER.search(line)) else []


# ---------------------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------------------


def _forbidden_dir(name: str, policy: dict) -> bool:
    lowered = name.lower()
    return any(
        entry.endswith("/") and lowered == entry[:-1].lower() for entry in policy["forbidden_paths"]
    )


def _forbidden_file(rel: str, policy: dict) -> bool:
    base = rel.rsplit("/", 1)[-1].lower()
    for entry in policy["forbidden_paths"]:
        if entry.endswith("/"):
            continue
        if entry.startswith("/"):
            if rel == entry[1:]:
                return True
        elif fnmatchcase(base, entry.lower()):
            return True
    return False


class _NameMasker:
    """Shows a path with any component that hits the denylist replaced by a fixed marker."""

    def __init__(self, matchers: list[tuple[Term, Matcher]]):
        self._matchers = matchers
        self._cache: dict[str, str] = {}

    def hits(self, component: str) -> list[tuple[Term, str]]:
        return denylist_hits(component, self._matchers)

    def shown(self, rel: str) -> str:
        if not self._matchers:
            return rel
        parts = []
        for part in rel.split("/"):
            if part not in self._cache:
                self._cache[part] = REDACTED_NAME if self.hits(part) else part
            parts.append(self._cache[part])
        return "/".join(parts)


def _scan_file(
    root: Path,
    rel: str,
    matchers: list[tuple[Term, Matcher]],
    policy: dict,
    patterns: bool,
    shown: str,
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return [Finding(rel, 0, "unreadable-file", shown)]
    if patterns and rel in policy["binary_allowlist"]:
        return findings
    try:
        if b"\x00" in data:
            raise ValueError("binary")
        text = data.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        if patterns:
            findings.append(Finding(rel, 0, "binary-file", shown))
        return findings
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        if not line:
            continue
        if patterns:
            for rule in pattern_rules_for_line(rel, line, policy):
                findings.append(Finding(rel, number, rule, shown))
        for term, label in denylist_hits(line, matchers):
            findings.append(Finding(rel, number, _denylist_rule(term, label), shown))
    return findings


def _scan_tree(
    root: Path,
    matchers: list[tuple[Term, Matcher]],
    policy: dict,
    patterns: bool,
    skip_dirs: tuple[str, ...] = (),
) -> tuple[list[Finding], int, int]:
    """Return (findings, files scanned, entries seen)."""
    findings: list[Finding] = []
    masker = _NameMasker(matchers)
    scanned = 0
    seen = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        rel_dir = base.relative_to(root).as_posix()
        dirs[:] = sorted(name for name in dirs if name != ".git" and name not in skip_dirs)
        keep = []
        for name in dirs:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            seen += 1
            shown = masker.shown(rel)
            for term, label in masker.hits(name):
                findings.append(Finding(rel, 0, _denylist_rule(term, label), shown))
            if patterns and _forbidden_dir(name, policy):
                findings.append(Finding(rel, 0, "forbidden-path", shown))
                continue
            keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            if name == ".git":
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            seen += 1
            shown = masker.shown(rel)
            for term, label in masker.hits(name):
                findings.append(Finding(rel, 0, _denylist_rule(term, label), shown))
            if patterns and _forbidden_file(rel, policy):
                findings.append(Finding(rel, 0, "forbidden-path", shown))
            if (base / name).is_symlink():
                continue
            scanned += 1
            findings.extend(_scan_file(root, rel, matchers, policy, patterns, shown))
    return sorted(set(findings), key=lambda f: (f.path, f.line, f.rule)), scanned, seen


def check_message(text: str, terms: list[Term], trailers: bool = False) -> list[Finding]:
    """Denylist rules on a commit message; with trailers=True the trailer rule applies too."""
    matchers = build_matchers(terms)
    findings: list[Finding] = []
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        for term, label in denylist_hits(line, matchers):
            findings.append(Finding(MESSAGE_PATH, number, _denylist_rule(term, label)))
        if trailers:
            for rule in trailer_rules_for_line(line):
                findings.append(Finding(MESSAGE_PATH, number, rule))
    return sorted(set(findings), key=lambda f: (f.path, f.line, f.rule))


# ---------------------------------------------------------------------------------------
# Sentinel self-test
# ---------------------------------------------------------------------------------------


def _render_phone(text: str) -> str:
    digits = _digits(text)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return " ".join(digits[i:i + 3] for i in range(0, len(digits), 3))


def _render_date(text: str) -> str:
    parsed = parse_date(text)
    if parsed is None:
        return text
    year, month, day = parsed
    name = next(name for name, number in _MONTHS.items() if number == month)
    return f"{name.capitalize()} {day}, {year}"


def _render_address(text: str) -> str:
    out = []
    for word in _WORDS.findall(text.lower()):
        canon = _SUFFIX_CANON.get(word, word)
        out.append(_SUFFIX_FULL.get(canon, word))
    return " ".join(out)


def render_sentinel(term: Term) -> str:
    if term.kind == "phone":
        return _render_phone(term.text)
    if term.kind == "date":
        return _render_date(term.text)
    if term.kind == "address":
        return _render_address(term.text)
    return term.text


def sentinel_variants(term: Term) -> list[str]:
    """The canonical render plus renders with invisible characters planted in it."""
    base = render_sentinel(term)
    variants = [base]
    if term.kind in ("term", "address"):
        if " " in base:
            # An invisible character typed where a space belongs, across both blank-glyph families.
            variants.append(base.replace(" ", "\u200b"))
            variants.append(base.replace(" ", "\u2800"))
        # A variation selector (category Mn) inside the first word.
        variants.append(base[:1] + "\ufe0f" + base[1:])
    elif term.kind == "phone" and len(base) > 1:
        variants.append(base[:1] + "\u200b" + base[1:])
    return variants


def sentinel_self_test(terms: list[Term], policy: dict) -> None:
    """Raise GateError unless every loaded term is flagged when rendered in each sentinel form."""
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "sentinel.txt"
        for term in terms:
            for variant in sentinel_variants(term):
                probe.write_text(variant + "\n", encoding="utf-8")
                findings, _, _ = _scan_tree(
                    Path(tmp), build_matchers([term]), policy, patterns=False
                )
                if not any(f.rule.startswith("denylist term") for f in findings):
                    raise GateError(
                        f"sentinel self-test failed for kind {term.kind} (term #{term.index}); "
                        "the matcher or normalizer is broken, so a clean result cannot be trusted"
                    )


# ---------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------


def default_denylist_path() -> Path:
    override = os.environ.get("JSP_LEAK_DENYLIST")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "job-search-prioritizer" / "leak-denylist.txt"
    return Path.home() / ".config" / "job-search-prioritizer" / "leak-denylist.txt"


def load_policy(path: Path | str | None = None) -> dict:
    target = Path(path) if path else Path(__file__).resolve().parent / "leak_policy.json"
    try:
        policy = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"cannot read the leak policy ({type(exc).__name__})") from None
    missing = [key for key in POLICY_KEYS if key not in policy]
    if missing:
        raise GateError("the leak policy is missing keys: " + ", ".join(missing))
    return policy


def _git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _is_tracked(path: Path) -> bool:
    try:
        done = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", "--", path.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_git_env(),
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _check_denylist_location(path: Path, roots: list[Path]) -> None:
    resolved = path.resolve()
    for root in roots:
        if resolved.is_relative_to(root.resolve()):
            raise GateError("the personal denylist sits inside the tree being scanned")
    if _is_tracked(resolved):
        raise GateError("the personal denylist is tracked by git")


def load_denylist(
    path: Path | None, require: bool, policy: dict, roots: list[Path]
) -> tuple[list[Term], str | None, str | None]:
    """Return (terms, skipped reason, warning). Raises GateError on a configuration fault."""
    target = path if path is not None else default_denylist_path()
    if not target.is_file():
        if require:
            raise GateError("a personal denylist is required but none was found")
        return [], "no denylist found; pass --require-denylist to make this an error", None
    _check_denylist_location(target, roots)
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise GateError("the personal denylist could not be read as UTF-8 text") from None
    terms = parse_denylist(text.split("\n"))
    minimum = int(policy["min_denylist_terms"])
    if not terms:
        if require:
            raise GateError("the personal denylist has no terms")
        return [], "the denylist file holds no terms", None
    if len(terms) < minimum:
        if require:
            raise GateError(f"the personal denylist has fewer than {minimum} terms")
        return terms, None, f"denylist has fewer than {minimum} terms; coverage is thin"
    return terms, None, None


# ---------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------

PASS_QUALIFIED = "leak gate: PASS (pattern rules only; personal denylist not checked)"


def run(
    root: Path | str,
    denylist_path: Path | str | None = None,
    require_denylist: bool = False,
    policy_path: Path | str | None = None,
    message: str | None = None,
    extra_roots: Iterable[Path | str] = (),
    skip_caches: bool = False,
) -> int:
    try:
        policy = load_policy(policy_path)
        root = Path(root)
        if not root.is_dir():
            raise GateError("the scan root is not a directory")
        roots = [root, *[Path(extra) for extra in extra_roots]]
        given = Path(denylist_path) if denylist_path is not None else None
        terms, skipped, warning = load_denylist(given, require_denylist, policy, roots)
        if terms:
            sentinel_self_test(terms, policy)
        findings, scanned, seen = _scan_tree(
            root.resolve(),
            build_matchers(terms),
            policy,
            patterns=True,
            skip_dirs=CACHE_DIR_NAMES if skip_caches else (),
        )
        if (seen == 0 or scanned == 0) and not findings:
            # An empty scan is never a pass, with or without a commit message.
            raise GateError("nothing to scan: the root holds no files")
    except GateError as exc:
        print(f"leak gate: ERROR {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if message is not None:
        findings = sorted(
            set(findings) | set(check_message(message, terms, trailers=True)),
            key=lambda f: (f.path, f.line, f.rule),
        )
    print(f"leak gate: scanned {scanned} files")
    if terms:
        print(summary_line(terms))
    else:
        print(f"personal denylist check SKIPPED ({skipped})")
    if warning:
        print(f"leak gate: WARNING {warning}")
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"leak gate: FAIL ({len(findings)} findings)")
        return EXIT_FINDINGS
    print("leak gate: PASS" if terms else PASS_QUALIFIED)
    return EXIT_OK


def run_message_file(
    message_path: Path | str,
    denylist_path: Path | str | None = None,
    require_denylist: bool = False,
    policy_path: Path | str | None = None,
) -> int:
    """Apply only the personal-denylist rules to a commit message file (trailers are allowed)."""
    try:
        policy = load_policy(policy_path)
        try:
            text = Path(message_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise GateError("the message file could not be read as UTF-8 text") from None
        given = Path(denylist_path) if denylist_path is not None else None
        terms, skipped, warning = load_denylist(given, require_denylist, policy, [])
        if terms:
            sentinel_self_test(terms, policy)
    except GateError as exc:
        print(f"leak gate: ERROR {exc}", file=sys.stderr)
        return EXIT_CONFIG
    findings = check_message(text, terms, trailers=False)
    print("leak gate: checked 1 message")
    print(summary_line(terms) if terms else f"personal denylist check SKIPPED ({skipped})")
    if warning:
        print(f"leak gate: WARNING {warning}")
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"leak gate: FAIL ({len(findings)} findings)")
        return EXIT_FINDINGS
    print("leak gate: PASS" if terms else "leak gate: PASS (personal denylist not checked; nothing else applies to a message)")
    return EXIT_OK


def run_staged(
    repo: Path | str,
    denylist_path: Path | str | None = None,
    require_denylist: bool = False,
    policy_path: Path | str | None = None,
) -> int:
    """Scan the allowlisted index tree of `repo`, so unstaged edits can neither hide nor fake a leak."""
    import export_public

    with tempfile.TemporaryDirectory() as tmp:
        export_dir = Path(tmp) / "export"
        try:
            export_public.export_tree(repo, export_dir)
            top = export_public.repo_toplevel(repo)
        except export_public.ExportError as exc:
            print(f"leak gate: ERROR {exc}", file=sys.stderr)
            return EXIT_CONFIG
        return run(export_dir, denylist_path, require_denylist, policy_path, extra_roots=[top])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leak_check.py",
        description="Fail when a personal identifier or LinkedIn fetch code is about to ship.",
    )
    parser.add_argument("--root", default=".", help="tree to scan (default: current directory)")
    parser.add_argument("--denylist", help="personal denylist file (default: per-user config)")
    parser.add_argument(
        "--require-denylist",
        action="store_true",
        help="exit 2 when the personal denylist is missing, empty, short, in-tree or tracked",
    )
    parser.add_argument("--policy", help="policy JSON (default: leak_policy.json beside this file)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--staged",
        action="store_true",
        help="scan the allowlisted index tree of the repository at --root (default: current directory)",
    )
    modes.add_argument(
        "--message-file",
        help="apply only the personal-denylist rules to this commit message file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.message_file:
        return run_message_file(args.message_file, args.denylist, args.require_denylist, args.policy)
    if args.staged:
        return run_staged(args.root, args.denylist, args.require_denylist, args.policy)
    return run(args.root, args.denylist, args.require_denylist, args.policy, skip_caches=True)


if __name__ == "__main__":
    raise SystemExit(main())
