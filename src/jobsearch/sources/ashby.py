"""Ashby job-board list adapter.

Purpose: turn one Ashby posting-api response into normalized Posting values, using the live
shape recorded in docs/ats-live-vs-docs.md (compensation is per job, workplaceType is
capitalized, intervals are words such as "1 YEAR").
Logic: the response is an object with a "jobs" list. Every key is read by name with a default.
Locations are location followed by secondaryLocations[].location, without duplicates. The work
mode is workplaceType lowercased (Remote, Hybrid, OnSite and Onsite are known); isRemote alone is
never enough, because it is true for hybrid roles too. Pay comes from the Salary components
across every compensation tier: one component gives its own range, several in the first stated
currency give the lowest minimum and the highest maximum plus one warning naming the role, and
components in another currency are left out with a second warning. The interval word maps to the
shared vocabulary ("1 YEAR" to year, "NONE" to none). Bonus, commission and equity components
are not salary and are ignored.
Guardrails: a job with isListed false, or without an id, a title or an apply link, is skipped. A
response without a jobs list raises FetchError with the shared sentence. The raw response body
is never stored on a Posting.
QA checklist: tests/fixtures/ats/ashby_list.json is the shape under test; a new live key goes
into docs/ats-live-vs-docs.md first.
"""

from __future__ import annotations

import re

from jobsearch.directory import BoardEntry
from jobsearch.fetch import UNKNOWN_SHAPE, FetchError
from jobsearch.models import POSTING_SCHEMA_VERSION, Posting, make_role_id
from jobsearch.sources.text import (
    NO_PAY,
    clean_text,
    finite_number,
    iso_to_utc_z,
    pick_ranges,
)

_WORK_MODES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}
_INTERVALS = {"year": "year", "month": "month", "week": "week", "day": "day", "hour": "hour"}
_INTERVAL_WORD = re.compile(r"\s*\d*\s*([A-Za-z]+)\s*")


def _locations(job: dict) -> list[str]:
    found: list[str] = []
    main = clean_text(job.get("location"))
    if main is not None:
        found.append(main)
    secondary = job.get("secondaryLocations")
    for item in secondary if isinstance(secondary, list) else []:
        name = clean_text(item.get("location")) if isinstance(item, dict) else None
        if name is not None and name not in found:
            found.append(name)
    return found


def _interval(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    word = _INTERVAL_WORD.fullmatch(value)
    if word is None:
        return "other"
    name = word.group(1).lower()
    if name == "none":
        return None
    return _INTERVALS.get(name, "other")


def _salary_components(compensation: object) -> list[dict]:
    if not isinstance(compensation, dict):
        return []
    found: list[dict] = []
    tiers = compensation.get("compensationTiers")
    for tier in tiers if isinstance(tiers, list) else []:
        components = tier.get("components") if isinstance(tier, dict) else None
        for component in components if isinstance(components, list) else []:
            if not isinstance(component, dict) or component.get("compensationType") != "Salary":
                continue
            if finite_number(component.get("minValue")) is None \
                    and finite_number(component.get("maxValue")) is None:
                continue
            found.append(component)
    return found


def _pay(job: dict, title: str, warnings: list[str] | None) -> dict[str, object]:
    components = _salary_components(job.get("compensation"))
    if not components:
        return dict(NO_PAY)
    components, currency = pick_ranges(
        components, lambda c: clean_text(c.get("currencyCode")),
        lambda c: _interval(c.get("interval")), title, "salary range", warnings)
    lows = [v for v in (finite_number(c.get("minValue")) for c in components) if v is not None]
    highs = [v for v in (finite_number(c.get("maxValue")) for c in components) if v is not None]
    intervals = {_interval(c.get("interval")) for c in components}
    return {
        "comp_min": min(lows) if lows else None,
        "comp_max": max(highs) if highs else None,
        "comp_currency": currency,
        "comp_interval": intervals.pop() if len(intervals) == 1 else None,
        "comp_source": "structured",
    }


def parse_list(entry: BoardEntry, payload: object, fetched_at: str,
               warnings: list[str] | None = None) -> list[Posting]:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise FetchError(UNKNOWN_SHAPE)
    postings: list[Posting] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("isListed") is False:
            continue
        job_id, title = clean_text(job.get("id")), clean_text(job.get("title"))
        apply_url, job_url = clean_text(job.get("applyUrl")), clean_text(job.get("jobUrl"))
        if job_id is None or ":" in job_id or title is None or (apply_url or job_url) is None:
            continue
        mode_text = job.get("workplaceType")
        description = job.get("descriptionPlain")
        postings.append(Posting.from_dict({
            "schema_version": POSTING_SCHEMA_VERSION,
            "role_id": make_role_id("ashby", entry.slug, job_id),
            "source": "ats",
            "ats": "ashby",
            "board_slug": entry.slug,
            "posting_id": job_id,
            "company": entry.company,
            "title": title,
            "team": clean_text(job.get("team")),
            "locations": _locations(job),
            "work_mode_text": mode_text if isinstance(mode_text, str) else None,
            "work_mode": _WORK_MODES.get(mode_text.strip().lower(), "unknown")
            if isinstance(mode_text, str) else "unknown",
            "posted": iso_to_utc_z(job.get("publishedAt")),
            "url_apply": apply_url or job_url,
            "url_posting": job_url,
            "description_text": description.strip() if isinstance(description, str) else "",
            **_pay(job, title, warnings),
            "fetched_at": fetched_at,
        }))
    return postings
