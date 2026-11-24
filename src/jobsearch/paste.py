"""Paste a posting: turn text the user supplies into a Posting stored in roles/pasted.jsonl.

Purpose: the starter directory leans toward tech and mid-market companies, so a user needs a way to
bring any other role into the same ranking. A pasted role is scored exactly like a scanned one.
Logic: parse_paste_file reads a small text file inside the data folder: header lines Company,
Title, Location and Link (Company and Title required), a blank line, then the posting text.
posting_from_text cleans the text (HTML becomes plain text, entities are decoded, hidden and
control characters are removed, line endings become LF), cuts it at PASTE_TEXT_LIMIT with a
warning, reads the work mode (the Location line first, then the first 2,000 characters of the
text) and any stated pay, and builds the Posting through Posting.from_dict. The role_id comes
from the link when there is one, else from the cleaned text, so pasting the same thing twice
gives the same id. save_pasted upserts roles/pasted.jsonl by role_id and keeps the first
pasted_on date.
Links: classify_url maps a posting link on a Greenhouse, Lever or Ashby board to (system, board,
posting id), a bare board link to ("board", system, board) and anything else to ("page", None,
None). paste_url reads a Greenhouse posting through the job API (one job, pay in cents) and a Lever
or Ashby posting from the cached board list, so the role gets the same role_id and fields as a
scanned role. Any other https page is fetched once with Purpose.USER_URL (https only, private and
local addresses refused, every redirect hop re-checked, the name resolved and vetted before every
request) and turned into text; its title comes from og:title, else the title tag, else the first
h1, and its company from og:site_name, else the host without "www.".
Pay: each short clause of the text is read with parse_pay_text, and a clause counts only when it
states a currency and a plausible amount (1,000 or more, or an hourly rate), so "3-5 years" is
never pay. Ranges in the first stated currency are merged (lowest minimum, highest maximum);
ranges in another currency are left out with a warning, never blended.
Guardrails: posting text is untrusted data. It is capped, stored as a Posting field, never printed
by the paste command and never acted on. The paste file must resolve inside the data folder.
LinkedIn is never contacted: a LinkedIn link, short link or image-host link is refused before any
request, and a page that redirects there is stopped at the redirect hop by the fetch layer; both
ask the user to paste the posting text instead. A residual risk applies: a
host could change its address between the vetting lookup and the connection; the impact is one
GET that carries no user data, no cookies and a size cap.
QA checklist: tests/test_paste.py pins every rule above; a new header line or a changed cleaning
rule needs a case there first.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from jobsearch import directory, fetch, fsio, paths, roles_store, scan
from jobsearch.directory import BoardEntry, board_key
from jobsearch.errors import UserError
from jobsearch.fetch import Resolver, Transport
from jobsearch.models import POSTING_SCHEMA_VERSION, Posting, make_pasted_role_id
from jobsearch.netguard import Purpose, UrlRefused, board_url, host_of, is_denied
from jobsearch.output import clean_text
from jobsearch.sources import greenhouse, parse_board
from jobsearch.sources.text import (
    NO_PAY,
    PayParse,
    html_to_text,
    parse_pay_text,
    pay_fields,
    split_locations,
)

PASTE_TEXT_LIMIT = 50000

_MAX_FILE_BYTES = 1_000_000
_MAX_NAME_CHARS = 200
_MODE_SCAN_CHARS = 2000
_HEADER = re.compile(r"(company|title|location|link)[ \t]*:[ \t]*(.*)", re.IGNORECASE)
_TAG = re.compile(r"<\s*/?[A-Za-z][^>]*>")
_BLANK_RUNS = re.compile(r"\n{3,}")
_CLAUSE = re.compile(r"\n|;\s|,\s|\.\s|[()]")
_PAY_WORDS = re.compile(r"\b(salary|pay|compensation|base|wage|wages|rate)\b", re.IGNORECASE)
_NOT_PAY = re.compile(
    r"\b(bonus|equity|stock|grant|budget|funding|raised|revenue|valuation|reimbursement)\b",
    re.IGNORECASE)
_MODES = (
    ("remote", re.compile(r"\bremote\b", re.IGNORECASE)),
    ("hybrid", re.compile(r"\bhybrid\b", re.IGNORECASE)),
    ("onsite", re.compile(r"\bon[- ]?site\b|\bin[- ]office\b", re.IGNORECASE)),
)
_MAX_PAGE_BYTES = 2_000_000
_GREENHOUSE_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io"})
_BOARD_HOSTS = {"jobs.lever.co": "lever", "jobs.eu.lever.co": "lever_eu",
                "jobs.ashbyhq.com": "ashby"}
_POSTING_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
_CHARSET = re.compile(r"charset=([\w.:-]+)", re.IGNORECASE)
_LINKEDIN = "LinkedIn is never contacted by this tool. Paste the posting text instead."
_UNREADABLE_LINK = "That web address could not be read, so it was not contacted."
_GONE = "That posting is no longer listed on the company's board."
_WHOLE_BOARD = "That is a whole job board. Add the company as a target instead."
_NOT_A_PAGE = "That link is not a web page this tool can read. Paste the posting text instead."
_NO_TITLE = "No job title could be found on that page. Paste the posting text instead."
_NEED_TEXT = "Paste the posting text below the header lines."
_NEED_HEADERS = "The pasted file needs Company: and Title: lines at the top."


@dataclass(frozen=True)
class PasteFile:
    company: str
    title: str
    location: str | None
    link: str | None
    text: str


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_paste_file(path: Path, folder: Path) -> PasteFile:
    """Read one paste file from inside the data folder; raise UserError with the fix otherwise."""
    target = Path(path).expanduser().resolve()
    if not target.is_relative_to(folder.resolve()):
        raise UserError("The pasted file must be inside your data folder.")
    try:
        if not target.is_file():
            raise UserError("That pasted file could not be found.")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise UserError("That file is too large to be a job posting.")
        raw = target.read_bytes()
    except OSError:
        raise UserError("That pasted file could not be read.") from None
    try:
        text = _newlines(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise UserError("The pasted file is not UTF-8 text. Save it as UTF-8 and try again."
                        ) from None
    if not text.strip():
        raise UserError(_NEED_TEXT)

    lines = text.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    headers: dict[str, str] = {}
    index = start
    while index < len(lines):
        match = _HEADER.fullmatch(lines[index].strip())
        if match is None or match.group(1).lower() in headers:
            break
        headers[match.group(1).lower()] = match.group(2).strip()
        index += 1
    body = "\n".join(lines[index:]).strip()
    if not headers.get("company") or not headers.get("title"):
        raise UserError(_NEED_HEADERS)
    if not body:
        raise UserError(_NEED_TEXT)
    return PasteFile(headers["company"], headers["title"], headers.get("location") or None,
                     headers.get("link") or None, body)


def _one_line(value: str) -> str:
    return " ".join(clean_text(value).split())[:_MAX_NAME_CHARS]


def _tidy(text: str) -> str:
    lines = [line.strip() for line in clean_text(text).split("\n")]
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def _body(text: str) -> str:
    text = _newlines(text.lstrip("﻿"))
    if _TAG.search(text):
        return _tidy(html_to_text(text, unescape_first=False))
    return _tidy(html.unescape(text))


def _work_mode(location: str | None, text: str) -> str:
    for source in (location or "", text[:_MODE_SCAN_CHARS]):
        found = [(m.start(), name) for name, pattern in _MODES
                 for m in [pattern.search(source)] if m is not None]
        if found:
            return min(found)[1]
    return "unknown"


def _plausible(parsed: PayParse, clause: str) -> bool:
    if parsed.currency is None or parsed.min <= 0 or _NOT_PAY.search(clause):
        return False
    if parsed.interval == "hour":
        return parsed.max >= 5
    if parsed.max < 1000:
        return False
    return parsed.min < parsed.max or parsed.interval is not None or bool(_PAY_WORDS.search(clause))


def _pay(text: str, title: str, notes: list[str]) -> dict[str, object]:
    found: list[PayParse] = []
    for clause in _CLAUSE.split(text):
        parsed = parse_pay_text(clause)
        if parsed is not None and _plausible(parsed, clause) and parsed not in found:
            found.append(parsed)
    return pay_fields(found, "parsed_html", title, notes) if found else dict(NO_PAY)


def posting_from_text(company: str, title: str, location: str | None, link: str | None,
                      text: str, today: str) -> tuple[Posting, list[str]]:
    """Build the Posting and the plain-English notes for the user. Never prints or fetches."""
    company, title = _one_line(company), _one_line(title)
    if not company or not title:
        raise UserError(_NEED_HEADERS)
    description = _body(text)
    notes: list[str] = []
    if len(description) > PASTE_TEXT_LIMIT:
        description = description[:PASTE_TEXT_LIMIT].rstrip()
        notes.append(f"The posting text was longer than {PASTE_TEXT_LIMIT:,} characters, "
                     "so it was cut there.")
    if not description:
        raise UserError(_NEED_TEXT)

    link = " ".join((link or "").split())
    https = link if link.startswith("https://") else ""
    if link and not https:
        notes.append("The link is not an https address, so it was not kept as the apply link.")
    role_id = make_pasted_role_id(link if link else description)
    place = _one_line(location) if location else ""
    posting = Posting.from_dict({
        "schema_version": POSTING_SCHEMA_VERSION,
        "role_id": role_id,
        "source": "pasted",
        "ats": None,
        "board_slug": None,
        "posting_id": role_id.rsplit(":", 1)[1],
        "company": company,
        "title": title,
        "team": None,
        "locations": split_locations(place),
        "work_mode_text": None,
        "work_mode": _work_mode(place, description),
        "posted": None,
        "url_apply": https,
        "url_posting": https or None,
        "description_text": description,
        **_pay(description, title, notes),
        "fetched_at": f"{today}T00:00:00Z",
    })
    return posting, notes


def save_pasted(folder: Path, posting: Posting, today: str) -> None:
    """Upsert one role into roles/pasted.jsonl by role_id; the first pasted_on date is kept."""
    records = {p.role_id: (on, p) for on, p in roles_store.load_pasted(folder)}
    first = records[posting.role_id][0] if posting.role_id in records else today
    records[posting.role_id] = (first, posting)
    fsio.write_jsonl_atomic(roles_store.pasted_path(folder), [
        {"pasted_on": records[role_id][0], "posting": records[role_id][1].to_dict()}
        for role_id in sorted(records)
    ])


def classify_url(url: str) -> tuple[str, str | None, str | None]:
    """("greenhouse"|"lever"|"lever_eu"|"ashby", board, posting id) for a posting link,
    ("board", system, board) for a whole board and ("page", None, None) for anything else."""
    page: tuple[str, str | None, str | None] = ("page", None, None)
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return page
    if parts.scheme.lower() != "https" or not host:
        return page
    segments = [part for part in parts.path.split("/") if part]
    if host in _GREENHOUSE_HOSTS and segments:
        slug, rest = segments[0], segments[1:]
        if len(rest) == 2 and rest[0] == "jobs" and rest[1].isascii() and rest[1].isdigit():
            return ("greenhouse", slug, rest[1])
        if rest in ([], ["jobs"]):
            return ("board", "greenhouse", slug)
    elif host in _BOARD_HOSTS and segments:
        ats, slug = _BOARD_HOSTS[host], segments[0]
        if len(segments) >= 2 and _POSTING_ID.fullmatch(segments[1]):
            return (ats, slug, segments[1])
        if len(segments) == 1:
            return ("board", ats, slug)
    return page


class _PageMeta(HTMLParser):
    """The og:title, og:site_name, first title tag and first h1 of a page; nothing is run."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title: str | None = None
        self.og_site: str | None = None
        self.text: dict[str, list[str]] = {"title": [], "h1": []}
        self._open: str | None = None
        self._done: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "meta":
            values = dict(attrs)
            key = str(values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if isinstance(content, str) and content.strip():
                if key == "og:title" and self.og_title is None:
                    self.og_title = content
                elif key == "og:site_name" and self.og_site is None:
                    self.og_site = content
        elif tag in self.text and self._open is None and tag not in self._done:
            self._open = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._open:
            self._done.add(tag)
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None and sum(map(len, self.text[self._open])) < _MAX_NAME_CHARS * 2:
            self.text[self._open].append(data)

    def pick(self, key: str) -> str:
        return " ".join("".join(self.text[key]).split())


def _known_company(folder: Path, ats: str, slug: str) -> str | None:
    wanted = f"{ats}:{slug}"
    for entry in [*directory.load_directory(), *scan.boards_for(folder)]:
        if board_key(entry) == wanted:
            return entry.company
    return None


def _board_entry(folder: Path, ats: str, slug: str) -> tuple[BoardEntry, bool]:
    known = _known_company(folder, ats, slug)
    company = known or " ".join(slug.replace("_", " ").replace("-", " ").split()).title()
    entry = BoardEntry(company=company, ats=ats, slug=slug, industry=None, size=None, stage=None)
    return entry, known is not None


def _get(url: str, folder: Path, purpose: Purpose, transport: Transport | None,
         resolver: Resolver | None) -> fetch.FetchResult:
    result = fetch.fetch(url, purpose=purpose, cache_dir=paths.cache_http_dir(folder),
                         transport=transport, resolver=resolver)
    if result.stale:
        raise UserError("That site could not be reached, and only an out-of-date copy was saved. "
                        "Try again later.")
    return result


def _ats_posting(folder: Path, ats: str, slug: str, posting_id: str,
                 transport: Transport | None) -> tuple[Posting, list[str]]:
    entry, known = _board_entry(folder, ats, slug)
    notes: list[str] = []
    if ats == "greenhouse":
        # board_url validates the slug; its lean list URL ends in /jobs, so one job is /jobs/<id>.
        url = f"{board_url('greenhouse', slug, lean=True)}/{posting_id}?pay_transparency=true"
        result = _get(url, folder, Purpose.ATS_API, transport, None)
        if result.status == 404:
            raise UserError(_GONE)
        if result.status != 200:
            raise UserError(f"The board returned HTTP {result.status}.")
        posting = greenhouse.parse_job(entry, fetch.decode_json(result), result.fetched_at, notes)
    else:
        result = _get(board_url(ats, slug), folder, Purpose.ATS_API, transport, None)
        if result.status == 404:
            raise UserError("No job board was found at that address.")
        if result.status != 200:
            raise UserError(f"The board returned HTTP {result.status}.")
        listed = parse_board(entry, fetch.decode_json(result), result.fetched_at, warnings=notes)
        found = next((p for p in listed if p.posting_id == posting_id), None)
        if found is None:
            raise UserError(_GONE)
        posting = found
    if not known and posting.company == entry.company:
        notes.append("Company name taken from the board address; correct it if wrong.")
    return posting, notes


def _decode_page(result: fetch.FetchResult) -> str:
    mime, _, params = result.headers.get("content-type", "").partition(";")
    mime = mime.strip().lower()
    if not (mime.startswith("text/") or mime == "application/xhtml+xml"):
        raise UserError(_NOT_A_PAGE)
    body = result.body[:_MAX_PAGE_BYTES]
    label = _CHARSET.search(params)
    if label is not None:
        try:
            decoded = body.decode(label.group(1), errors="replace")
        except (LookupError, ValueError, TypeError):
            decoded = None
        if isinstance(decoded, str):
            return decoded
    return body.decode("utf-8", errors="replace")


def _page_posting(folder: Path, url: str, today: str, transport: Transport | None,
                  resolver: Resolver | None) -> tuple[Posting, list[str]]:
    result = _get(url, folder, Purpose.USER_URL, transport, resolver)
    if result.status != 200:
        raise UserError("That page was not found." if result.status == 404
                        else f"The page returned HTTP {result.status}.")
    page = _decode_page(result)
    meta = _PageMeta()
    meta.feed(page)
    meta.close()
    title = meta.og_title or meta.pick("title") or meta.pick("h1")
    if not title.strip():
        raise UserError(_NO_TITLE)
    host = host_of(url) or ""
    company = meta.og_site or (host[4:] if host.startswith("www.") else host)
    posting, notes = posting_from_text(company, title, None, url, page, today)
    notes.append("The title and company were read from the page; check them.")
    return posting, notes


def paste_url(folder: Path, url: str, today: str, *, transport: Transport | None = None,
              resolver: Resolver | None = None) -> tuple[Posting, list[str]]:
    """Turn a posting link into a Posting. Raises UserError (or UrlRefused) with the fix."""
    url = url.strip()
    if host_of(url) is None:
        raise UserError(_UNREADABLE_LINK)
    if is_denied(url):
        raise UserError(_LINKEDIN)
    first, second, third = classify_url(url)
    if first == "board":
        raise UserError(_WHOLE_BOARD)
    try:
        if first == "page":
            return _page_posting(folder, url, today, transport, resolver)
        assert second is not None and third is not None
        return _ats_posting(folder, first, second, third, transport)
    except UrlRefused as refusal:
        if str(refusal).startswith("LinkedIn"):  # a redirect hop led to a denied host
            raise UserError(_LINKEDIN) from None
        raise
