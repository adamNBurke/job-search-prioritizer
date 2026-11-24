"""Target companies: resolve each company the user names to a public job board.

Purpose: the starter directory leans toward tech and mid-size companies, so the user names the
employers they care about in config/targets.json and this module finds each one's board on
Greenhouse, Lever or Ashby. Resolved boards are written to config/boards.local.json, which the
scan reads beside the directory.
Logic: resolve_targets reads config/targets.json and folds each name with
identity.normalize_company. Names that fold to the same company are merged and resolved once;
at most MAX_TARGETS companies are resolved per run, in normalized-name order. Per company:
confirmed false writes "rejected" and sends no request; confirmed true on a board already known
(explicit ats and slug, or the board found last run) writes "resolved" on the user's word and
sends no request; a known board is fetched once and checked; with no known board, up to three
slug_candidates are probed on greenhouse, lever, lever_eu, ashby in that order and probing stops
at the first board that answers. identity.identity_from_payload decides each found board: a
decisive match is resolved, anything else is needs_confirmation. The whole of
config/boards.local.json is rebuilt from the targets on every run, sorted by normalized name,
so a company removed from targets.json leaves the scan.
Guardrails: every request is an anonymous GET through fetch.fetch with Purpose.ATS_API, so the
per-host pause, the cache and the host allow-list apply, and MAX_TARGETS times three names times
four systems bounds a run. A board that names another company, or cannot be matched, is never
resolved without the user's own yes, so it is never scanned. A failure to reach a board (offline,
timeout, 429, 5xx, unreadable data) is reported as that and stops probing the company for this
run; it is never recorded as "no board" and, because only 200 and 404 answers are cached, is
tried again on the next run. A yes with no board ever shown is not accepted.
QA checklist: a new resolution rule gets a named case in tests/test_targets.py first; the output
file must stay inside the docs/data-files.md contract for boards.local.json (identity is null
for a board the user confirmed, because the contract allows only company_name and
description_mentions).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from jobsearch import clock, directory, fetch, fsio, identity, paths
from jobsearch.errors import UserError
from jobsearch.fetch import Transport
from jobsearch.netguard import ATS_HOSTS, Purpose, UrlRefused, board_url

MAX_TARGETS = 25
SCHEMA_VERSION = 1
SYSTEM_ORDER = ("greenhouse", "lever", "lever_eu", "ashby")

_SYSTEM_NAME = {"greenhouse": "Greenhouse", "lever": "Lever", "lever_eu": "Lever (EU)",
                "ashby": "Ashby"}
_PUBLIC_PAGE = {
    "greenhouse": "https://boards.greenhouse.io/{}", "lever": "https://jobs.lever.co/{}",
    "lever_eu": "https://jobs.eu.lever.co/{}", "ashby": "https://jobs.ashbyhq.com/{}",
}
_WORDS = re.compile(r"[a-z0-9]+")
_CONFIRMED = "Confirmed by you."
_REJECTED = "You said this was not the right board, so it is not scanned or searched for again."
_NO_LETTERS = "That name has no letters or numbers to look up."
_NO_NAME = "That entry in config/targets.json has no company name."
_NO_CANDIDATE = ("No board name could be made from that company name. Add its ats and slug "
                 "to config/targets.json.")
_EXPLICIT_HELP = "Use greenhouse, lever, lever_eu or ashby and the board name from its address."


@dataclass
class Resolution:
    """What one targets-resolve run found. Counts cover every company resolved."""

    resolved: int = 0
    needs_confirmation: int = 0
    unresolved: int = 0
    rejected: int = 0
    confirm_names: list[str] = field(default_factory=list)
    unresolved_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    wrote: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.resolved + self.needs_confirmation + self.unresolved + self.rejected


@dataclass(frozen=True)
class _Probe:
    kind: str  # "found", "absent" or "failed"
    payload: object = None
    reason: str = ""


def slug_candidates(name: str) -> list[str]:
    """Up to three board names to try for a company: joined, hyphenated, first word.

    "Acme Robotics, Inc." -> ["acmerobotics", "acme-robotics", "acme"]. Only ASCII letters and
    digits survive (a board address allows no more), so a name with none gives [].
    """
    folded = unicodedata.normalize("NFKD", identity.normalize_company(name))
    words = _WORDS.findall(folded.encode("ascii", "ignore").decode("ascii"))
    if not words:
        return []
    found = ["".join(words), "-".join(words)]
    if len(words) > 1:
        found.append(words[0])
    return list(dict.fromkeys(found))


def _read_companies(path: Path) -> list[dict]:
    try:
        data = fsio.read_json(path)
    except (ValueError, UnicodeDecodeError, OSError):
        raise UserError("config/targets.json could not be read as JSON.") from None
    companies = data.get("companies") if isinstance(data, dict) else None
    if not isinstance(companies, list):
        raise UserError("config/targets.json needs a companies list.")
    return [item for item in companies if isinstance(item, dict)]


def _read_previous(path: Path) -> dict[str, dict]:
    """The boards written last run, by key; anything unreadable counts as nothing found yet."""
    try:
        data = fsio.read_json(path)
    except (ValueError, UnicodeDecodeError, OSError):
        return {}
    boards = data.get("boards") if isinstance(data, dict) else None
    if not isinstance(boards, dict):
        return {}
    return {key: item for key, item in boards.items() if isinstance(item, dict)}


def _board_of(item: dict) -> tuple[str, str] | None:
    ats, slug = item.get("ats"), item.get("slug")
    return (ats, slug) if isinstance(ats, str) and isinstance(slug, str) and slug else None


def _merge(group: list[dict]) -> dict:
    """One target from several that name the same company: first value present wins."""
    merged = dict(group[0])
    for other in group[1:]:
        if _board_of(merged) is None and _board_of(other) is not None:
            merged["ats"], merged["slug"] = other["ats"], other["slug"]
        for key in ("industry", "size", "confirmed"):
            if merged.get(key) is None and other.get(key) is not None:
                merged[key] = other[key]
    return merged


def _entry(company: str, status: str, target: dict, *, board: tuple[str, str] | None = None,
           identity_kind: str | None = None, checked_on: str | None = None,
           reason: str | None = None) -> dict:
    industry, size = target.get("industry"), target.get("size")
    return {
        "company": company, "status": status,
        "ats": board[0] if board else None, "slug": board[1] if board else None,
        "identity": identity_kind, "checked_on": checked_on, "reason": reason,
        "industry": industry if industry in directory.INDUSTRIES else None,
        "size": size if size in directory.SIZES else None,
    }


def _probe(folder: Path, ats: str, slug: str, transport: Transport | None) -> _Probe:
    """One anonymous GET of a board's lean list URL, sorted into found, absent or failed."""
    system = _SYSTEM_NAME[ats]
    unreachable = _Probe("failed", reason=f"Could not reach {ATS_HOSTS[ats]}; try again later.")
    try:
        result = fetch.fetch(
            board_url(ats, slug, lean=True), purpose=Purpose.ATS_API,
            cache_dir=paths.cache_http_dir(folder), transport=transport)
    except fetch.FetchError:
        return unreachable
    if result.stale:  # the network was down and only an old copy was left: not an answer
        return unreachable
    if result.status == 404:
        return _Probe("absent")
    if result.status != 200:
        return _Probe("failed", reason=(
            f"{system} answered with an unexpected response (HTTP {result.status}); "
            "try again later."))
    try:
        return _Probe("found", fetch.decode_json(result))
    except fetch.FetchError:
        return _Probe("failed", reason=f"{system} sent data this tool could not read; "
                                       "try again later.")


def _judge(name: str, target: dict, board: tuple[str, str], payload: object, today: str) -> dict:
    """A board that answered: resolved on a decisive identity match, else waits for the user."""
    ats, slug = board
    kind = identity.identity_from_payload(ats, payload, name)
    if kind is not None:
        return _entry(name, "resolved", target, board=board, identity_kind=kind, checked_on=today)
    return _entry(name, "needs_confirmation", target, board=board, checked_on=today, reason=(
        f"Found a {_SYSTEM_NAME[ats]} board ({_PUBLIC_PAGE[ats].format(slug)}) but could not "
        f"match it to {name} from its postings. Open it and confirm it is the right company."))


def _resolve_known(folder: Path, name: str, target: dict, board: tuple[str, str], today: str,
                   transport: Transport | None) -> dict:
    ats, slug = board
    if ats not in SYSTEM_ORDER:
        return _entry(name, "unresolved", target, board=board, reason=(
            f"That job-board system is not supported. {_EXPLICIT_HELP} Edit config/targets.json."))
    try:
        board_url(ats, slug, lean=True)
    except UrlRefused as exc:
        return _entry(name, "unresolved", target, board=board, reason=(
            f"{exc} Fix the slug in config/targets.json."))
    probe = _probe(folder, ats, slug, transport)
    if probe.kind == "failed":
        return _entry(name, "unresolved", target, board=board, reason=probe.reason)
    if probe.kind == "absent":
        return _entry(name, "unresolved", target, board=board,
                      reason=f"That board was not found on {_SYSTEM_NAME[ats]}.")
    return _judge(name, target, board, probe.payload, today)


def _resolve_guessing(folder: Path, name: str, target: dict, today: str,
                      transport: Transport | None) -> dict:
    candidates = slug_candidates(name)
    if not candidates:
        return _entry(name, "unresolved", target, reason=_NO_CANDIDATE)
    for slug in candidates:
        for ats in SYSTEM_ORDER:
            probe = _probe(folder, ats, slug, transport)
            if probe.kind == "failed":
                return _entry(name, "unresolved", target, reason=probe.reason)
            if probe.kind == "found":
                return _judge(name, target, (ats, slug), probe.payload, today)
    return _entry(name, "unresolved", target, reason=(
        "No public board found on Greenhouse, Lever or Ashby under the names tried: "
        f"{', '.join(candidates)}."))


def _resolve_one(folder: Path, name: str, target: dict, previous: dict | None, today: str,
                 transport: Transport | None, warnings: list[str]) -> dict:
    board = _board_of(target)
    if board is None and previous is not None and previous.get("status") in (
            "resolved", "needs_confirmation"):
        board = _board_of(previous)  # the board found last run, so a run never re-guesses
    confirmed = target.get("confirmed")
    if confirmed is False:
        return _entry(name, "rejected", target, board=board, reason=_REJECTED)
    if confirmed is True:
        if board is not None:
            return _entry(name, "resolved", target, board=board, reason=_CONFIRMED)
        warnings.append(
            f"{name} is marked confirmed, but no board has been found for it yet, so the yes "
            "was not used.")
    if board is not None:
        return _resolve_known(folder, name, target, board, today, transport)
    return _resolve_guessing(folder, name, target, today, transport)


def _check_tags(name: str, target: dict, warnings: list[str]) -> None:
    for key, allowed in (("industry", directory.INDUSTRIES), ("size", directory.SIZES)):
        value = target.get(key)
        if value is not None and value not in allowed:
            warnings.append(f"The {key} {value!r} for {name} is not a known tag, so it was ignored.")


def resolve_targets(folder: Path, *, transport: Transport | None = None) -> Resolution:
    """Resolve every target in config/targets.json and rewrite config/boards.local.json."""
    outcome = Resolution()
    targets_path = folder / paths.LAYOUT["targets"]
    local_path = folder / paths.LAYOUT["boards_local"]
    if not targets_path.is_file():
        local_path.unlink(missing_ok=True)  # derived from targets.json, so it goes with it
        return outcome
    previous = _read_previous(local_path)
    today = clock.today_iso()

    groups: dict[str, list[dict]] = {}
    for target in _read_companies(targets_path):
        raw = target.get("name")
        name = raw.strip() if isinstance(raw, str) else ""
        key = identity.normalize_company(name)
        if not key:
            outcome.unresolved += 1
            outcome.unresolved_lines.append(f"{name or '(no name)'}: "
                                            f"{_NO_LETTERS if name else _NO_NAME}")
            continue
        groups.setdefault(key, []).append(target)

    keys = sorted(groups)
    if len(keys) > MAX_TARGETS:
        outcome.warnings.append(
            f"config/targets.json names {len(keys)} companies; only the first {MAX_TARGETS} by "
            "name were resolved. Remove some to resolve the rest.")
        keys = keys[:MAX_TARGETS]

    boards: dict[str, dict] = {}
    for key in keys:
        group = groups[key]
        target = _merge(group)
        name = str(group[0].get("name")).strip()
        if len(group) > 1:
            outcome.warnings.append(
                f"{name} is listed more than once in config/targets.json; it is resolved once.")
        _check_tags(name, target, outcome.warnings)
        boards[key] = _resolve_one(folder, name, target, previous.get(key), today, transport,
                                   outcome.warnings)

    fsio.write_json_atomic(local_path, {"schema_version": SCHEMA_VERSION, "boards": boards})
    outcome.wrote.append(paths.LAYOUT["boards_local"])
    for entry in boards.values():
        setattr(outcome, entry["status"], getattr(outcome, entry["status"]) + 1)
        if entry["status"] == "needs_confirmation":
            outcome.confirm_names.append(entry["company"])
        elif entry["status"] == "unresolved":
            outcome.unresolved_lines.append(f"{entry['company']}: {entry['reason']}")
    return outcome
