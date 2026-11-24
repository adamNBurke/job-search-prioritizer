"""Board adapters: one module per job-board system, one dispatcher.

Purpose: scan.py reads a board response without knowing which system produced it.
Logic: parse_board picks the adapter by entry.ats and returns normalized Posting values. An
optional warnings list collects plain-English notes an adapter wants the user to see.
Guardrails: an unsupported system or an unrecognized shape raises FetchError with a plain
sentence; adapters never touch the network (only fetch.py does).
QA checklist: a new adapter adds one module here and one branch below, plus a fixture test.
"""

from __future__ import annotations

from jobsearch.directory import BoardEntry
from jobsearch.fetch import UNKNOWN_SHAPE, FetchError
from jobsearch.models import Posting
from jobsearch.sources import ashby, greenhouse, lever


def parse_board(entry: BoardEntry, payload: object, fetched_at: str, *,
                warnings: list[str] | None = None) -> list[Posting]:
    if entry.ats == "greenhouse":
        return greenhouse.parse_list(entry, payload, fetched_at, warnings)
    if entry.ats in ("lever", "lever_eu"):
        return lever.parse_list(entry, payload, fetched_at, warnings)
    if entry.ats == "ashby":
        return ashby.parse_list(entry, payload, fetched_at, warnings)
    raise FetchError(UNKNOWN_SHAPE)
