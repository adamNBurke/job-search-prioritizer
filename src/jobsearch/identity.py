"""Company identity checks: does this job board really belong to this company?

Purpose: a guessed board name can silently belong to a different company.
The directory builder and target resolution both ask this one question.
Logic: normalize_company folds a company name to a comparable phrase (NFKC, casefold,
punctuation to spaces, legal suffixes dropped). identity_from_payload reads the raw list payload
of one board. Greenhouse lists carry company_name on every posting, so the first name found must
normalize to the company. Lever and Ashby carry no company field, so the company name must
appear as a whole phrase in at least half of the first five postings (sorted by id) description
text. It returns "company_name", "description_mentions", or None.
Guardrails: pure functions, no network, no adapters (it runs before the adapters and does not
depend on them). Payload keys are read by name with safe defaults, so a hostile or odd payload
returns None instead of raising. A mismatch is never auto-accepted.
QA checklist: a new job-board system needs a branch here and a test for match, mismatch and a
malformed payload.
"""

from __future__ import annotations

import re
import unicodedata

SUFFIXES = frozenset(
    {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "plc", "sa", "ag"}
)
SAMPLE_SIZE = 5
_NOT_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NOT_WORD.sub(" ", folded.replace("_", " ")).split())


def normalize_company(name: str) -> str:
    """A comparable form of a company name: "Acme Robotics, Inc." -> "acme robotics"."""
    words = _fold(name).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


def _jobs(payload: object) -> list[dict]:
    items = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _text_of(posting: dict, keys: tuple[str, ...]) -> str:
    parts = [posting.get(key) for key in keys]
    return " ".join(part for part in parts if isinstance(part, str))


def _mentions(phrase: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", _fold(text)) is not None


def identity_from_payload(ats: str, payload: object, company: str) -> str | None:
    target = normalize_company(company)
    if not target:
        return None
    jobs = _jobs(payload)
    if not jobs:
        return None
    if ats == "greenhouse":
        for job in jobs:
            name = job.get("company_name")
            if isinstance(name, str) and name.strip():
                return "company_name" if normalize_company(name) == target else None
        return None
    if ats in ("lever", "lever_eu"):
        keys = ("descriptionPlain", "openingPlain", "additionalPlain")
    elif ats == "ashby":
        keys = ("descriptionPlain",)
    else:
        return None
    sample = sorted(jobs, key=lambda job: str(job.get("id", "")))[:SAMPLE_SIZE]
    hits = sum(1 for job in sample if _mentions(target, _text_of(job, keys)))
    return "description_mentions" if hits * 2 >= len(sample) else None
