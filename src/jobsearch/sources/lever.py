"""Lever job-board list adapter, for the US host and the EU host.

Purpose: turn one Lever postings response into normalized Posting values, using the live shape
recorded in docs/ats-live-vs-docs.md (the vendor docs differ: "onsite" has no hyphen live and
createdAt is epoch milliseconds).
Logic: the response is a bare top-level list. Every key is read by name with a default, because
Lever's categories keys are optional. The ats value is "lever" for an entry on the US host and
"lever_eu" for an entry on the EU host, and the role_id carries it, so the same site name on the
two hosts never collides. Locations are categories.location followed by categories.allLocations,
without duplicates, in first-seen order. Pay comes from the top-level salaryRange when Lever sends
one (integers, not cents); Lever's own interval words map to the shared vocabulary. The
description joins descriptionPlain, each lists[] item (its heading and its html_to_text content)
and additionalPlain, separated by blank lines.
Guardrails: a posting without an id, a title or an apply link is skipped (a role the user cannot
open is not worth ranking). A response that is not a list raises FetchError with the shared
sentence. The raw response body is never stored on a Posting.
QA checklist: tests/fixtures/ats/lever_us_list.json and lever_eu_list.json are the shapes under
test; a new live key goes into docs/ats-live-vs-docs.md first.
"""

from __future__ import annotations

from datetime import datetime, timezone

from jobsearch.directory import BoardEntry
from jobsearch.fetch import UNKNOWN_SHAPE, FetchError
from jobsearch.models import POSTING_SCHEMA_VERSION, Posting, make_role_id
from jobsearch.sources.text import NO_PAY, clean_text, finite_number, html_to_text

_WORK_MODES = {"onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}
_INTERVALS = {
    "per-year-salary": "year",
    "per-month-salary": "month",
    "per-week-salary": "week",
    "per-day-salary": "day",
    "per-hour-wage": "hour",
}


def _epoch_ms_to_utc_z(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _locations(categories: object) -> list[str]:
    if not isinstance(categories, dict):
        return []
    found: list[str] = []
    primary = clean_text(categories.get("location"))
    if primary is not None:
        found.append(primary)
    extra = categories.get("allLocations")
    for item in extra if isinstance(extra, list) else []:
        name = clean_text(item)
        if name is not None and name not in found:
            found.append(name)
    return found


def _description(job: dict) -> str:
    parts: list[str] = []
    plain = clean_text(job.get("descriptionPlain"))
    if plain is not None:
        parts.append(plain)
    lists = job.get("lists")
    for item in lists if isinstance(lists, list) else []:
        if not isinstance(item, dict):
            continue
        heading = clean_text(item.get("text"))
        content = item.get("content")
        body = html_to_text(content, unescape_first=False) if isinstance(content, str) else ""
        block = "\n".join(piece for piece in (heading, body) if piece)
        if block:
            parts.append(block)
    additional = clean_text(job.get("additionalPlain"))
    if additional is not None:
        parts.append(additional)
    return "\n\n".join(parts)


def _pay(job: dict) -> dict[str, object]:
    salary = job.get("salaryRange")
    if not isinstance(salary, dict):
        return dict(NO_PAY)
    low, high = finite_number(salary.get("min")), finite_number(salary.get("max"))
    if low is None and high is None:
        return dict(NO_PAY)
    interval = salary.get("interval")
    return {
        "comp_min": low,
        "comp_max": high,
        "comp_currency": clean_text(salary.get("currency")),
        "comp_interval": _INTERVALS.get(interval, "other") if isinstance(interval, str) else None,
        "comp_source": "structured",
    }


def parse_list(entry: BoardEntry, payload: object, fetched_at: str,
               warnings: list[str] | None = None) -> list[Posting]:
    if not isinstance(payload, list):
        raise FetchError(UNKNOWN_SHAPE)
    ats = "lever_eu" if entry.ats == "lever_eu" else "lever"
    postings: list[Posting] = []
    for job in payload:
        if not isinstance(job, dict):
            continue
        job_id, title = job.get("id"), clean_text(job.get("text"))
        apply_url, hosted = clean_text(job.get("applyUrl")), clean_text(job.get("hostedUrl"))
        if not isinstance(job_id, str) or not job_id.strip() or ":" in job_id:
            continue
        if title is None or (apply_url or hosted) is None:
            continue
        categories = job.get("categories")
        team = clean_text(categories.get("team")) if isinstance(categories, dict) else None
        mode_text = job.get("workplaceType")
        postings.append(Posting.from_dict({
            "schema_version": POSTING_SCHEMA_VERSION,
            "role_id": make_role_id(ats, entry.slug, job_id.strip()),
            "source": "ats",
            "ats": ats,
            "board_slug": entry.slug,
            "posting_id": job_id.strip(),
            "company": entry.company,
            "title": title,
            "team": team,
            "locations": _locations(categories),
            "work_mode_text": mode_text if isinstance(mode_text, str) else None,
            "work_mode": _WORK_MODES.get(mode_text, "unknown") if isinstance(mode_text, str)
            else "unknown",
            "posted": _epoch_ms_to_utc_z(job.get("createdAt")),
            "url_apply": apply_url or hosted,
            "url_posting": hosted,
            "description_text": _description(job),
            **_pay(job),
            "fetched_at": fetched_at,
        }))
    return postings
