"""The duplicate key: one role listed twice is one role.

Purpose: a company often posts the same job under two ids (one per office, or a re-post). The
ranked list must show it once, so scan.py groups postings by this key and keeps the lowest
role_id of each group.
Logic: dedupe_title lowercases (casefold, after NFKC), turns every run of characters that is not a
letter or digit into one space and trims, so "Data  Analyst, II" and "data analyst ii" match.
Seniority words stay in the title, so "Senior Data Analyst" is a different role from "Data
Analyst". dedupe_key joins the casefolded company, the normalized title and the sorted,
casefolded, stripped locations (joined by ";") with "|".
Guardrails: this module imports only the standard library and the Posting type, never scan or
roles_store. scan.py imports roles_store, and roles_store.load_rankable calls dedupe_key for
pasted roles, so keeping the key here keeps the module graph acyclic. Locations are
compared as the board sent them: "NYC" and "New York, NY" are different places to this key, on
purpose (a flagged assumption; revisit if users report near-duplicates).
QA checklist: changing the key changes which roles merge, so a test in tests/test_scan.py pins
each rule above.
"""

from __future__ import annotations

import re
import unicodedata

from jobsearch.models import Posting

_NOT_ALPHANUMERIC = re.compile(r"[\W_]+")
_SEPARATOR = "|"


def dedupe_title(title: str) -> str:
    folded = unicodedata.normalize("NFKC", title).casefold()
    return _NOT_ALPHANUMERIC.sub(" ", folded).strip()


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def dedupe_key(posting: Posting) -> str:
    places = sorted({_fold(place) for place in posting.locations if place.strip()})
    return _SEPARATOR.join((_fold(posting.company), dedupe_title(posting.title), ";".join(places)))
