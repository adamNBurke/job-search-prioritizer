"""Greenhouse job-board list adapter.

Purpose: turn one Greenhouse board response into normalized Posting values, using the live
shape recorded in docs/ats-live-vs-docs.md (the vendor docs differ).
Logic: read the "jobs" list by key name with defaults; for each job build a Posting through
Posting.from_dict, so a bad value fails loudly instead of leaking into scoring. first_published
carries a local UTC offset that varies within one board, so each value is converted to UTC on
its own. The description is entity-escaped twice, so html_to_text runs with unescape_first.
A location that says "remote" gives work_mode "remote"; Greenhouse states nothing else, so
every other posting is "unknown". Pay is read from the pay-range block inside the description
(the list never carries pay_input_ranges): every element with the class pay-range is parsed with
parse_pay_text; ranges in the first stated currency are merged (lowest minimum, highest maximum)
and one warning names the role, while ranges in another currency are left out with a second
warning, so a USD and a CAD range never blend into one pair. That pay has comp_source
"parsed_html", the lower-trust source. parse_job reads one job fetched with pay_transparency=true:
its pay_input_ranges are in whole cents (divided by 100), merged by the same same-currency rule,
and carry comp_source "structured" with no interval (the API states none).
Guardrails: a job without an id, a title or an apply link is skipped, because a role the
user cannot open is not worth ranking. The raw response body is never stored on a Posting.
QA checklist: tests/fixtures/ats/greenhouse_list.json is the shape under test; a new live key
goes into docs/ats-live-vs-docs.md first.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from html.parser import HTMLParser

from jobsearch.directory import BoardEntry
from jobsearch.fetch import UNKNOWN_SHAPE, FetchError
from jobsearch.models import POSTING_SCHEMA_VERSION, Posting, make_role_id
from jobsearch.sources.text import (
    NO_PAY,
    PayParse,
    finite_number,
    html_to_text,
    iso_to_utc_z,
    parse_pay_text,
    pay_fields,
    split_locations,
)

_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)


class _PayRanges(HTMLParser):
    """Collects the text of every element whose class list contains pay-range."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._tag: str | None = None
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._tag is None:
            classes = (dict(attrs).get("class") or "").split()
            if "pay-range" in classes:
                self._tag, self._depth, self._parts = tag, 1, []
        elif tag == self._tag:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._tag is not None and tag == self._tag:
            self._depth -= 1
            if self._depth == 0:
                self.blocks.append(" ".join(" ".join(self._parts).split()))
                self._tag = None

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._parts.append(data)


def _pay(content: object, title: str, warnings: list[str] | None) -> dict[str, object]:
    if not isinstance(content, str) or "pay-range" not in content:
        return dict(NO_PAY)
    collector = _PayRanges()
    collector.feed(html.unescape(content))
    collector.close()
    parsed = [p for p in map(parse_pay_text, collector.blocks) if p is not None]
    return pay_fields(parsed, "parsed_html", title, warnings)


def _locations(job: dict) -> list[str]:
    location = job.get("location")
    name = location.get("name") if isinstance(location, dict) else None
    if not isinstance(name, str):
        return []
    return split_locations(name)


def _team(job: dict) -> str | None:
    departments = job.get("departments")
    if isinstance(departments, list) and departments and isinstance(departments[0], dict):
        name = departments[0].get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _posting(job: dict, entry: BoardEntry, fetched_at: str,
             pay: Callable[[str], dict[str, object]]) -> Posting | None:
    """One job object as a Posting, or None when it lacks a usable id, a title or an apply link.

    A usable id is non-empty and has no ":" (the role id joins its parts with ":").

    `pay` is called with the stripped title only for a job that is kept, so a skipped job never
    adds a pay warning.
    """
    job_id, title, url = job.get("id"), job.get("title"), job.get("absolute_url")
    if job_id is None or isinstance(job_id, bool):
        return None
    if not str(job_id).strip() or ":" in str(job_id):
        return None  # an id that cannot form a role id skips this job, not the whole board
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    content = job.get("content")
    company = job.get("company_name")
    locations = _locations(job)
    return Posting.from_dict({
        "schema_version": POSTING_SCHEMA_VERSION,
        "role_id": make_role_id("greenhouse", entry.slug, str(job_id)),
        "source": "ats",
        "ats": "greenhouse",
        "board_slug": entry.slug,
        "posting_id": str(job_id),
        "company": company if isinstance(company, str) and company.strip() else entry.company,
        "title": title.strip(),
        "team": _team(job),
        "locations": locations,
        "work_mode_text": None,
        "work_mode": "remote" if any(_REMOTE.search(loc) for loc in locations) else "unknown",
        "posted": iso_to_utc_z(job.get("first_published")),
        "url_apply": url.strip(),
        "url_posting": url.strip(),
        "description_text": (
            html_to_text(content, unescape_first=True) if isinstance(content, str) else ""
        ),
        **pay(title.strip()),
        "fetched_at": fetched_at,
    })


def parse_list(entry: BoardEntry, payload: object, fetched_at: str,
               warnings: list[str] | None = None) -> list[Posting]:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise FetchError(UNKNOWN_SHAPE)
    postings: list[Posting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        posting = _posting(job, entry, fetched_at,
                           lambda title, job=job: _pay(job.get("content"), title, warnings))
        if posting is not None:
            postings.append(posting)
    return postings


def _structured_pay(ranges: object, title: str, warnings: list[str] | None) -> dict[str, object]:
    """Pay from a single job's pay_input_ranges (whole cents), same-currency ranges only."""
    found: list[PayParse] = []
    for item in ranges if isinstance(ranges, list) else []:
        if not isinstance(item, dict):
            continue
        low, high = finite_number(item.get("min_cents")), finite_number(item.get("max_cents"))
        if low is None or high is None:
            continue
        currency = item.get("currency_type")
        found.append(PayParse(low / 100, high / 100,
                              currency.strip() or None if isinstance(currency, str) else None,
                              None))
    return pay_fields(found, "structured", title, warnings)


def parse_job(entry: BoardEntry, payload: object, fetched_at: str,
              warnings: list[str] | None = None) -> Posting:
    """One job from /boards/<slug>/jobs/<id>?pay_transparency=true.

    Same mapping as a list item, except pay: the single job carries pay_input_ranges in cents
    (structured), and a job without that key falls back to the pay-range block in the text.
    """
    if not isinstance(payload, dict):
        raise FetchError(UNKNOWN_SHAPE)
    ranges = payload.get("pay_input_ranges")

    def pay(title: str) -> dict[str, object]:
        if isinstance(ranges, list) and ranges:
            return _structured_pay(ranges, title, warnings)
        return _pay(payload.get("content"), title, warnings)

    posting = _posting(payload, entry, fetched_at, pay)
    if posting is None:
        raise FetchError(UNKNOWN_SHAPE)
    return posting
