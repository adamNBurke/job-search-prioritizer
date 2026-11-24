"""The starter directory of public job boards, with industry, size and stage tags.

Purpose: the scan needs a list of company boards to read, and the user needs to narrow it by
industry and size without knowing any board names.
Logic: load_directory reads src/jobsearch/data/boards.json (schema_version 1) and validates
every entry strictly against the three vocabularies below. select_boards keeps file order and
applies the user's industry and size choices; count_matching gives the number a skill can quote
before a scan. board_key is the identity of a board across files ("ats:slug").
Guardrails: a bad entry raises UserError naming its index, so a bad edit never silently drops
boards. The shipped file's last_verified, identity and postings_at_verify are validated here and
not loaded; the data-quality tests read them from the JSON. Entries are sorted by casefolded
company then ats, so diffs and scans are stable. The shipped file holds only boards verified
against the live endpoints; local boards the user adds live in config/boards.local.json and may
lack industry, size and stage.
QA checklist: the vocabularies below are copied exactly into docs/data-files.md and
drift-tested. A new board is probed live first (found, postings above zero, company name
matches) and gets today's date in last_verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jobsearch.errors import UserError

INDUSTRIES = (
    "software", "fintech", "healthcare", "biotech", "education", "ecommerce-retail",
    "consumer", "media-entertainment", "marketing-adtech", "logistics-transportation",
    "manufacturing-hardware", "energy-climate", "security", "hr-workplace",
    "real-estate-construction", "travel-hospitality", "professional-services",
    "nonprofit-public",
)
SIZES = ("1-50", "51-200", "201-1000", "1001-5000", "5001+")
STAGES = ("early", "growth", "public", "nonprofit")
ATS_CHOICES = ("greenhouse", "lever", "lever_eu", "ashby")
IDENTITIES = ("company_name", "description_mentions")

DIRECTORY_PATH = Path(__file__).resolve().parent / "data" / "boards.json"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class BoardEntry:
    company: str
    ats: str
    slug: str
    industry: str | None
    size: str | None
    stage: str | None


def board_key(entry: BoardEntry) -> str:
    return f"{entry.ats}:{entry.slug}"


def _bad(index: int, message: str) -> UserError:
    return UserError(f"The board directory has a problem at entry {index}: {message}")


def _text(raw: dict, index: int, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _bad(index, f"{key} must be non-empty text")
    return value


def _choice(raw: dict, index: int, key: str, allowed: tuple[str, ...]) -> str:
    value = raw.get(key)
    if value not in allowed:
        raise _bad(index, f"{key} must be one of {', '.join(allowed)}")
    return value


def _entry(raw: object, index: int) -> BoardEntry:
    if not isinstance(raw, dict):
        raise _bad(index, "it is not an object")
    expected = {"company", "ats", "slug", "industry", "size", "stage", "last_verified",
                "identity", "postings_at_verify"}
    extra = sorted(set(raw) - expected)
    if extra:
        raise _bad(index, f"unknown field(s) {', '.join(extra)}")
    if not _DATE.fullmatch(_text(raw, index, "last_verified")):
        raise _bad(index, "last_verified must look like 2026-11-16")
    count = raw.get("postings_at_verify")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _bad(index, "postings_at_verify must be a whole number, zero or more")
    entry = BoardEntry(
        company=_text(raw, index, "company"),
        ats=_choice(raw, index, "ats", ATS_CHOICES),
        slug=_text(raw, index, "slug"),
        industry=_choice(raw, index, "industry", INDUSTRIES),
        size=_choice(raw, index, "size", SIZES),
        stage=_choice(raw, index, "stage", STAGES),
    )
    _choice(raw, index, "identity", IDENTITIES)
    return entry


def load_directory(path: Path | None = None) -> list[BoardEntry]:
    source = path if path is not None else DIRECTORY_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UserError("The board directory could not be read.") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise UserError("The board directory is from a different version of this tool.")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise UserError("The board directory has no entries list.")
    entries = [_entry(raw, index) for index, raw in enumerate(raw_entries)]
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        key = board_key(entry)
        if key in seen:
            raise _bad(index, f"{key} appears twice")
        seen.add(key)
    return entries


def select_boards(
    entries: list[BoardEntry],
    industries: list[str],
    sizes: list[str],
    exclude_industries: tuple[str, ...] | list[str] = (),
) -> list[BoardEntry]:
    """File order is kept. An empty industries or sizes list means no restriction."""
    return [
        e for e in entries
        if (not industries or e.industry in industries)
        and (not sizes or e.size in sizes)
        and e.industry not in exclude_industries
    ]


def count_matching(
    entries: list[BoardEntry],
    industries: list[str],
    sizes: list[str],
    exclude_industries: tuple[str, ...] | list[str] = (),
) -> int:
    return len(select_boards(entries, industries, sizes, exclude_industries))
