"""The scan spine: read the chosen job boards politely and keep the roles in the user's families.

Purpose: turn "scan my boards" into dated files the ranker reads, using only the shared fetch
layer so every board request is cached, paced and refused when it should be.
Logic: boards_for picks the boards (the starter directory narrowed by config/scan-scope.json,
plus resolved boards from config/boards.local.json). fetch_boards reads each board through
fetch.fetch with Purpose.ATS_API and the data folder cache. Boards on different hosts run on
parallel worker threads (at most four, one per host); one worker takes its host's boards in
directory order, and results are put back by original index, so completion order never shows in
any output. HTTP 404 is "no_board", a FetchError, an unreadable answer or a stale cached copy
(the network was down) is "failed" with its message, HTTP 200 is "ok" with parsed postings.
run_scan applies the title gate (a family match and no excluded term), folds duplicates by
dedupe.dedupe_key (the lowest role_id of a group is kept, the rest are recorded on it), writes
scans/<date>/postings.jsonl sorted by role_id and scans/<date>/scan-meta.json, and upserts
roles/roles.jsonl. The funnel always balances: fetched = title_gate_dropped + duplicates_merged
+ kept. Closing compares each readable board's open roles with EVERY posting the board returned
(not only the kept ones), so a role the title gate now drops stays open; a role missing from an
ok or no_board answer is closed with closed_on set, and one that comes back is reopened with its
first_seen kept. closed_roles lists the roles closed on the scan date, so a same-day re-run
reports the same list.
Guardrails: a board that failed to answer never closes its roles: it is named in
scan-meta with status failed and in the warnings. Workers only fetch and parse; every file write
happens on the main thread after results are assembled. If no board could be read at
all (offline, every board failed) nothing is written and a UserError says so, so an empty scan
never replaces the last good one. A re-run on the same day rewrites the same files; "new" roles
are those whose first_seen is today. Only fetch.py touches the network.
QA checklist: new scan-meta fields are documented in docs/data-files.md in the same change;
board entries stay in directory order.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from jobsearch import clock, dedupe, directory, families, fetch, fsio, paths, roles_store, sources
from jobsearch.directory import BoardEntry, board_key
from jobsearch.errors import UserError
from jobsearch.fetch import Transport
from jobsearch.filters import title_gate
from jobsearch.models import Posting
from jobsearch.netguard import ATS_HOSTS, Purpose, UrlRefused, board_url

_OTHER_STATUS = "The board returned HTTP {status}."
_STALE = "could not be reached, and the saved copy is out of date, so this board was skipped."
_UNREADABLE = "The board sent data this tool could not read."
_MAX_FAILURE_LINES = 5


@dataclass
class BoardFetch:
    entry: BoardEntry
    status: str  # "ok", "no_board" or "failed"
    http_status: int | None
    from_cache: bool
    fetched_at: str | None
    postings: list[Posting] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ScanOutcome:
    scan_date: str
    fetches: list[BoardFetch]
    fetched_total: int
    kept: int
    title_gate_dropped: int
    duplicates_merged: int
    new_roles: list[dict[str, str]]
    reopened_roles: list[dict[str, str]]
    closed_roles: list[dict[str, str]]
    warnings: list[str]
    wrote: list[str]
    notes: list[str] = field(default_factory=list)


def _read_json_file(path: Path, what: str) -> object:
    try:
        return fsio.read_json(path)
    except (OSError, ValueError):
        raise UserError(f"The {what} file could not be read. Fix it or remove it.") from None


def _string_list(data: dict, key: str, what: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise UserError(f"The {what} file needs {key} to be a list of text values.")
    return value


def _local_entries(folder: Path) -> list[BoardEntry]:
    path = folder / paths.LAYOUT["boards_local"]
    if not path.is_file():
        return []
    data = _read_json_file(path, "local boards")
    boards = data.get("boards") if isinstance(data, dict) else None
    if isinstance(boards, dict):
        items = [boards[key] for key in sorted(boards)]
    elif isinstance(boards, list):
        items = boards
    else:
        raise UserError("The local boards file has no boards list.")
    entries: list[BoardEntry] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "resolved":
            continue
        ats, slug, company = item.get("ats"), item.get("slug"), item.get("company")
        if ats not in directory.ATS_CHOICES or not isinstance(slug, str) or not isinstance(
                company, str):
            continue
        entries.append(BoardEntry(
            company=company, ats=ats, slug=slug,
            industry=item.get("industry") if item.get("industry") in directory.INDUSTRIES else None,
            size=item.get("size") if item.get("size") in directory.SIZES else None,
            stage=None,
        ))
    return entries


def boards_for(folder: Path) -> list[BoardEntry]:
    entries = directory.load_directory()
    scope_path = folder / paths.LAYOUT["scan_scope"]
    if scope_path.is_file():
        scope = _read_json_file(scope_path, "scan scope")
        if not isinstance(scope, dict):
            raise UserError("The scan scope file must be a JSON object.")
        entries = directory.select_boards(
            entries,
            _string_list(scope, "industries", "scan scope"),
            _string_list(scope, "sizes", "scan scope"),
            _string_list(scope, "exclude_industries", "scan scope"),
        )
    seen = {board_key(e) for e in entries}
    for local in _local_entries(folder):
        if board_key(local) not in seen:
            seen.add(board_key(local))
            entries.append(local)
    return entries


def _fetch_one(folder: Path, entry: BoardEntry, refresh: bool,
               transport: Transport | None) -> BoardFetch:
    try:
        result = fetch.fetch(
            board_url(entry.ats, entry.slug), purpose=Purpose.ATS_API,
            cache_dir=paths.cache_http_dir(folder), refresh=refresh, transport=transport,
        )
        if result.stale:  # the network was down and only an old copy was left: not an answer
            return BoardFetch(entry, "failed", None, False, None, error=_STALE)
        if result.status == 404:
            return BoardFetch(entry, "no_board", 404, result.from_cache, result.fetched_at)
        if result.status != 200:
            return BoardFetch(entry, "failed", result.status, result.from_cache,
                              result.fetched_at, error=_OTHER_STATUS.format(status=result.status))
        notes: list[str] = []
        postings = sources.parse_board(entry, fetch.decode_json(result), result.fetched_at,
                                       warnings=notes)
        return BoardFetch(entry, "ok", 200, result.from_cache, result.fetched_at, postings,
                          notes=notes)
    except (fetch.FetchError, UrlRefused) as exc:
        return BoardFetch(entry, "failed", None, False, None, error=str(exc))
    except ValueError:
        return BoardFetch(entry, "failed", None, False, None, error=_UNREADABLE)


def fetch_boards(folder: Path, entries: list[BoardEntry], *, refresh: bool = False,
                 transport: Transport | None = None) -> list[BoardFetch]:
    """Read every board; one worker per host, results in the order of `entries`."""
    if not entries:
        return []
    groups: dict[str, list[tuple[int, BoardEntry]]] = {}
    for index, entry in enumerate(entries):
        groups.setdefault(ATS_HOSTS.get(entry.ats, entry.ats), []).append((index, entry))

    def work(items: list[tuple[int, BoardEntry]]) -> list[tuple[int, BoardFetch]]:
        return [(index, _fetch_one(folder, entry, refresh, transport)) for index, entry in items]

    results: list[BoardFetch | None] = [None] * len(entries)
    with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
        futures = [pool.submit(work, items) for items in groups.values()]
        for future in futures:
            for index, fetched in future.result():
                results[index] = fetched
    return [fetched for fetched in results if fetched is not None]


def _board_meta(fetched: BoardFetch, kept: int) -> dict:
    entry = fetched.entry
    return {
        "key": board_key(entry), "company": entry.company, "ats": entry.ats, "slug": entry.slug,
        "industry": entry.industry, "size": entry.size, "stage": entry.stage,
        "status": fetched.status, "http_status": fetched.http_status,
        "from_cache": fetched.from_cache, "fetched": len(fetched.postings), "kept": kept,
        "error": fetched.error,
    }


def _summary(role: roles_store.RoleRecord) -> dict[str, str]:
    return {"role_id": role.role_id, "company": role.company, "title": role.title}


def _sorted_summaries(roles: list[roles_store.RoleRecord]) -> list[dict[str, str]]:
    return sorted((_summary(r) for r in roles),
                  key=lambda s: (s["company"], s["title"], s["role_id"]))


def _failure_warnings(fetches: list[BoardFetch]) -> list[str]:
    failed = [f for f in fetches if f.status == "failed"]
    lines: list[str] = []
    if failed:
        lines.append(
            "Could not reach 1 board; its roles keep their last state." if len(failed) == 1
            else f"Could not reach {len(failed)} boards; their roles keep their last state.")
        lines += [f"{f.entry.company}: {f.error}" for f in failed[:_MAX_FAILURE_LINES]]
        if len(failed) > _MAX_FAILURE_LINES:
            lines.append(f"And {len(failed) - _MAX_FAILURE_LINES} more boards could not be read.")
    lines += [f"{f.entry.company}: no board was found at that address."
              for f in fetches if f.status == "no_board"]
    return lines


def _fold_duplicates(gate_kept: list[Posting]) -> tuple[list[Posting], dict[str, list[str]]]:
    """Keep the lowest role_id of each dedupe group; return the kept postings and what merged."""
    groups: dict[str, list[Posting]] = {}
    for posting in sorted(gate_kept, key=lambda p: p.role_id):
        groups.setdefault(dedupe.dedupe_key(posting), []).append(posting)
    kept: list[Posting] = []
    merged: dict[str, list[str]] = {}
    for members in groups.values():
        keeper = members[0]
        kept.append(keeper)
        merged[keeper.role_id] = sorted(
            {m.role_id for m in members[1:] if m.role_id != keeper.role_id})
    kept.sort(key=lambda p: p.role_id)
    return kept, merged


def _carried_reopened(scan_folder: Path, roles: dict[str, roles_store.RoleRecord],
                      scan_date: str) -> list[roles_store.RoleRecord]:
    """Roles an earlier run of this same date reopened and that are still open."""
    try:
        meta = fsio.read_json(scan_folder / "scan-meta.json")
    except (OSError, ValueError):
        return []
    listed = meta.get("reopened_roles") if isinstance(meta, dict) else None
    found: list[roles_store.RoleRecord] = []
    for item in listed if isinstance(listed, list) else []:
        role = roles.get(item.get("role_id")) if isinstance(item, dict) else None
        if role is not None and role.status == "open" and role.last_seen == scan_date:
            found.append(role)
    return found


def run_scan(folder: Path, criteria: dict, *, refresh: bool = False,
             transport: Transport | None = None) -> ScanOutcome:
    scan_date = clock.today_iso()
    compiled = families.compile_families(criteria)
    entries = boards_for(folder)
    if not entries:
        raise UserError("No job boards are selected. Widen your industry or size choices.")
    fetches = fetch_boards(folder, entries, refresh=refresh, transport=transport)
    readable = [f for f in fetches if f.status == "ok"]
    if not readable:
        raise UserError("No job board could be read. Check your internet connection and "
                        "try again; nothing was changed.")
    warnings = _failure_warnings(fetches)
    notes = sorted(note for f in fetches for note in f.notes)

    fetched_total = sum(len(board.postings) for board in readable)
    gate_kept = [p for board in readable for p in board.postings
                 if title_gate(p.title, compiled) is None]
    kept_postings, merged = _fold_duplicates(gate_kept)
    dropped = fetched_total - len(gate_kept)
    merged_total = len(gate_kept) - len(kept_postings)
    per_board_kept: dict[str, int] = {}
    for posting in kept_postings:
        key = f"{posting.ats}:{posting.board_slug}"
        per_board_kept[key] = per_board_kept.get(key, 0) + 1

    roles = roles_store.load_roles(folder)
    scan_folder = folder / paths.LAYOUT["scans"] / scan_date
    reopened = {r.role_id: r for r in _carried_reopened(scan_folder, roles, scan_date)}
    for posting in kept_postings:
        folded = merged[posting.role_id]
        first_seen = scan_date
        for old_id in folded:  # an earlier keeper that now folds into this role
            old = roles.pop(old_id, None)
            if old is not None:
                first_seen = min(first_seen, old.first_seen)
        known = roles.get(posting.role_id)
        if known is None:
            known = roles[posting.role_id] = roles_store.RoleRecord(
                role_id=posting.role_id, board_key=f"{posting.ats}:{posting.board_slug}",
                company=posting.company, title=posting.title, dedupe_key=None,
                first_seen=first_seen, last_seen=scan_date, status="open",
            )
        else:
            if known.status == "closed":
                reopened[known.role_id] = known
                known.status, known.closed_on = "open", None
            known.first_seen = min(known.first_seen, first_seen)
            known.last_seen = scan_date
            known.company, known.title = posting.company, posting.title
        known.dedupe_key = dedupe.dedupe_key(posting)
        known.duplicates = folded

    seen_on_board = {board_key(f.entry): {p.role_id for p in f.postings}
                     for f in fetches if f.status == "ok"}
    seen_on_board.update({board_key(f.entry): set() for f in fetches if f.status == "no_board"})
    for role in roles.values():
        seen = seen_on_board.get(role.board_key)
        if seen is not None and role.status == "open" and role.role_id not in seen:
            role.status, role.closed_on = "closed", scan_date

    kept_ids = {p.role_id for p in kept_postings}
    new_roles = _sorted_summaries(
        [r for r in roles.values() if r.first_seen == scan_date and r.role_id in kept_ids])
    closed_roles = _sorted_summaries(
        [r for r in roles.values() if r.status == "closed" and r.closed_on == scan_date])
    reopened_roles = _sorted_summaries(list(reopened.values()))

    meta = {
        "schema_version": 1,
        "scan_date": scan_date,
        "criteria_version": criteria["criteria_version"],
        "boards": [_board_meta(f, per_board_kept.get(board_key(f.entry), 0)) for f in fetches],
        "totals": {"fetched": fetched_total, "kept": len(kept_postings),
                   "title_gate_dropped": dropped, "duplicates_merged": merged_total},
        "new_roles": new_roles,
        "reopened_roles": reopened_roles,
        "closed_roles": closed_roles,
    }
    fsio.write_jsonl_atomic(scan_folder / "postings.jsonl", [p.to_dict() for p in kept_postings])
    fsio.write_json_atomic(scan_folder / "scan-meta.json", meta)
    roles_store.save_roles(folder, roles)
    return ScanOutcome(
        scan_date=scan_date, fetches=fetches, fetched_total=fetched_total,
        kept=len(kept_postings), title_gate_dropped=dropped, duplicates_merged=merged_total,
        new_roles=new_roles, reopened_roles=reopened_roles, closed_roles=closed_roles,
        warnings=warnings, notes=notes,
        wrote=[paths.LAYOUT["roles_file"], f"{paths.LAYOUT['scans']}/{scan_date}/postings.jsonl",
               f"{paths.LAYOUT['scans']}/{scan_date}/scan-meta.json"],
    )
