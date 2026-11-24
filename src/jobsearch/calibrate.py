"""Tier cutoffs proposed from the user's own results.

Purpose: fixed cutoffs tuned to one person put a stranger's whole list in tier C,
so the cutoffs come from the shape of the user's own passing roles.
Logic: passing_with_caps lists (score, held at B by a cap) for every role that passed the hard
filters, provisional ones included (counted, and held at B), sorted
high to low. propose picks the B cutoff so about A_SHARE
plus B_SHARE of all passing roles are A or B, and the A cutoff from the roles no cap holds at B,
so about A_SHARE of all passing roles can still be A when caps hold high scorers at B. The B
cutoff is never dragged down for that: the A cutoff has a floor one above it, so tier A may be
small or empty, and Proposal.caps_dominate says so (criteria-test prints CAPS_DOMINATE).
With no capped roles that is the old all-roles pick. The indexes use exact fractions, so no float
rounding moves a cutoff. A score equal to a cutoff lands in that tier, so ties at a cutoff all go
to the higher tier. counts_at counts the roles in each tier at any pair of cutoffs and agrees
with output.role_tier: a capped role at or above the A cutoff counts as B. Both take plain
scores (read as not capped) or (score, capped) pairs. accept writes the user's chosen integers into the tiers through
criteria_store.save_new_version (so criteria_version bumps and the old config is kept) and
records them in config/calibration.json.
Guardrails: all arithmetic is Python, never the model. A proposal is None with no passing roles.
accept refuses a non-integer, a value outside 1 to 100, and an A cutoff not above the B cutoff,
before anything is written. The tier C cutoff stays 0 and the tier meanings are kept.
QA checklist: a changed share needs the unit cases in tests/test_calibrate.py; a changed
calibration.json shape needs docs/data-files.md updated in the same commit.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from math import ceil
from pathlib import Path

from jobsearch import criteria_store, fsio, paths, roles_store, scoring
from jobsearch.errors import UserError
from jobsearch.roles_store import RankInput
from jobsearch.scoring import ScoredRole

A_SHARE = Fraction(1, 10)
B_SHARE = Fraction(1, 4)
SCHEMA_VERSION = 1
LOWEST_CUTOFF = 1
HIGHEST_CUTOFF = 100
CAPS_DOMINATE = ("Many of your top-scoring roles are held at tier B because they name none of your "
                 "skills, are off your level, were posted a long time ago or have too little "
                 "detail to score fully, so tier A is small.")


class CalibrationError(UserError):
    """The cutoffs cannot be calibrated or accepted. Message is plain English."""


@dataclass(frozen=True)
class Proposal:
    a_min: int
    b_min: int
    counts: dict[str, int]
    passing: int
    caps_dominate: bool = False  # caps held so many top scorers at B that tier A is small or empty


def score_latest(folder: Path) -> tuple[dict, RankInput, list[ScoredRole]]:
    """The config, the latest scan and every role scored against it."""
    criteria = criteria_store.load_criteria(folder)
    rank_input = roles_store.load_rankable(folder)
    scored = scoring.score_all(
        rank_input.facts, criteria, date.fromisoformat(rank_input.scan_date))
    return criteria, rank_input, scored


def passing_with_caps(scored: list[ScoredRole]) -> list[tuple[int, bool]]:
    """(score, held at tier B by a cap) for every passing role, high score first, open before
    capped at equal scores, so equal inputs always give equal lists."""
    pairs = [(s.score, s.tier_cap is not None) for s in scored if s.rejected is None]
    return sorted(pairs, key=lambda pair: (-pair[0], pair[1]))


def _pairs(items: Sequence[int | tuple[int, bool]]) -> list[tuple[int, bool]]:
    """Plain scores read as not capped; (score, capped) pairs pass through. High score first,
    open before capped."""
    pairs = [(item, False) if isinstance(item, int) else (item[0], bool(item[1]))
             for item in items]
    return sorted(pairs, key=lambda pair: (-pair[0], pair[1]))


def counts_at(items: Sequence[int | tuple[int, bool]], a_min: int, b_min: int) -> dict[str, int]:
    """Roles per tier. A capped role is never A: at or above the A cutoff it counts as B."""
    pairs = _pairs(items)
    a = sum(1 for score, capped in pairs if score >= a_min and not capped)
    ab = sum(1 for score, _ in pairs if score >= b_min)
    return {"A": a, "B": ab - a, "C": len(pairs) - ab}


def propose(items: Sequence[int | tuple[int, bool]]) -> Proposal | None:
    if not items:
        return None
    pairs = _pairs(items)
    n = len(pairs)
    a_index = max(1, ceil(A_SHARE * n))
    b_index = min(n, max(a_index + 1, ceil((A_SHARE + B_SHARE) * n)))
    # The B cutoff is picked over every passing role and is kept. The A cutoff is picked
    # among roles no cap holds at B, with a floor one above the B cutoff: when caps held that pick
    # down to or below the B cutoff, tier A shrinks (it may be empty) rather than ranking below
    # tier B or dragging B down.
    a_all = pairs[a_index - 1][0]
    b_min = pairs[b_index - 1][0]
    open_scores = [score for score, capped in pairs if not capped]
    a_open = open_scores[min(a_index, len(open_scores)) - 1] if open_scores else a_all
    floored = False
    if a_open > b_min:
        a_min = a_open
    elif a_open == a_all:
        a_min, b_min = a_open, a_open - 1  # a tie at the cutoff, not a cap: B moves down one
    else:
        a_min, b_min = b_min + 1, b_min
        floored = True
    if a_min > HIGHEST_CUTOFF:
        a_min, b_min = HIGHEST_CUTOFF, HIGHEST_CUTOFF - 1
    if b_min < LOWEST_CUTOFF:
        a_min, b_min = max(a_min, LOWEST_CUTOFF + 1), LOWEST_CUTOFF
    counts = counts_at(pairs, a_min, b_min)
    held_a = counts["A"] == 0 and any(capped and score >= a_min for score, capped in pairs)
    return Proposal(a_min, b_min, counts, n, caps_dominate=floored or held_a)


def _check_cutoff(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationError(f"The {name} cutoff must be a whole number.")
    if not LOWEST_CUTOFF <= value <= HIGHEST_CUTOFF:
        raise CalibrationError(
            f"The {name} cutoff must be a whole number from {LOWEST_CUTOFF} to {HIGHEST_CUTOFF}.")
    return value


def accept(folder: Path, criteria: dict, a_min: int, b_min: int, *, scan_date: str,
           proposal: Proposal | None) -> str:
    """Write the cutoffs as the next criteria_version and record them. Returns that version."""
    a = _check_cutoff("A", a_min)
    b = _check_cutoff("B", b_min)
    if a <= b:
        raise CalibrationError("The A cutoff must be higher than the B cutoff.")
    if proposal is None:
        raise CalibrationError("No roles passed your filters in the latest scan, so there is "
                               "nothing to calibrate against yet.")
    first = (criteria.get("created_from") == "intake"
             and not (folder / paths.LAYOUT["calibration"]).exists())
    updated = copy.deepcopy(criteria)
    for tier in updated["tiers"]:
        if tier["id"] == "A":
            tier["min_score"] = a
        elif tier["id"] == "B":
            tier["min_score"] = b
        else:
            tier["min_score"] = 0
    version = criteria_store.save_new_version(
        folder, updated, "intake" if first else "retune")
    fsio.write_json_atomic(folder / paths.LAYOUT["calibration"], {
        "schema_version": SCHEMA_VERSION,
        "criteria_version": version,
        "scan_date": scan_date,
        "passing_roles": proposal.passing,
        "proposed": {"a_min": proposal.a_min, "b_min": proposal.b_min,
                     "counts": dict(proposal.counts)},
        "accepted": {"a_min": a, "b_min": b},
    })
    return version
