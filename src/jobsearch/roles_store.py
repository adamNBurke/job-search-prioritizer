"""Role history and the input to ranking.

Purpose: scan.py remembers every role it has seen (roles/roles.jsonl), and rank needs the
latest scan's postings joined with the company tags and seen-dates, plus the funnel counts that
explain how many roles were dropped before scoring.
Logic: roles.jsonl holds one RoleRecord per line sorted by role_id. load_rankable reads the
newest scan folder (scans/<date>/postings.jsonl and scan-meta.json), joins each posting with its
board's industry, size and stage, adds roles/pasted.jsonl entries when that file exists, and
returns a RankInput. fetched_total and title_gate_dropped are copied from scan-meta totals, and
pasted_total counts every record in pasted.jsonl. A pasted record is dropped, and counted in
duplicates_merged on top of the scan's own count, when its role_id or dedupe key matches a scanned
role or an earlier pasted record (pasted records are read in role_id order). So scanned plus
pasted always equals duplicates_merged plus title-gate drops plus the facts handed to scoring.
Every fact also carries function_verdict: the one-line reason from a reject entry in
roles/function-verdicts.json, read through verdicts.load_verdicts, or None. It is an input like
the scan, so rank, criteria-test and function-review all apply the same verdicts, and
filters.apply_filters turns it into a counted rejection. It can only hide a role.
Guardrails: every file is read as UTF-8 through fsio and every Posting through Posting.from_dict,
so a hand-edited or truncated line stops with a plain message instead of scoring garbage. A
damaged verdict file stops with a message naming function-review and hides no role. A
missing scan is a UserError that names the next step. Listings are sorted, never trusted to
directory order.
QA checklist: a new field on RoleRecord needs a default so older roles.jsonl lines still load.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jobsearch import fsio, paths, verdicts
from jobsearch.dedupe import dedupe_key
from jobsearch.errors import UserError
from jobsearch.models import Posting

SCHEMA_VERSION = 1


@dataclass
class RoleRecord:
    role_id: str
    board_key: str
    company: str
    title: str
    dedupe_key: str | None
    first_seen: str
    last_seen: str
    status: str
    closed_on: str | None = None
    duplicates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoleFacts:
    posting: Posting
    company_industry: str | None
    company_size: str | None
    company_stage: str | None
    first_seen: str
    last_seen: str
    function_verdict: str | None = None  # the reject reason from roles/function-verdicts.json


@dataclass(frozen=True)
class RankInput:
    scan_date: str
    fetched_total: int
    title_gate_dropped: int
    duplicates_merged: int
    pasted_total: int
    facts: list[RoleFacts]


def roles_path(folder: Path) -> Path:
    return folder / paths.LAYOUT["roles_file"]


def pasted_path(folder: Path) -> Path:
    return folder / paths.LAYOUT["pasted"]


def _read_lines(path: Path, what: str) -> list[dict]:
    try:
        text = fsio.read_text(path)
    except OSError:
        raise UserError(f"The {what} file could not be read.") from None
    records: list[dict] = []
    # Split on "\n" only: the writers keep U+2028, U+2029 and U+0085 raw inside JSON strings, and
    # str.splitlines() would cut a record at each of them. A trailing "\r" (Windows editors) is
    # whitespace to json.loads, so it needs no special handling.
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            raise UserError(f"The {what} file has a damaged line ({number}).") from None
        if not isinstance(record, dict):
            raise UserError(f"The {what} file has a damaged line ({number}).")
        records.append(record)
    return records


def load_roles(folder: Path) -> dict[str, RoleRecord]:
    path = roles_path(folder)
    if not path.is_file():
        return {}
    roles: dict[str, RoleRecord] = {}
    for record in _read_lines(path, "role history"):
        try:
            role = RoleRecord(**record)
        except TypeError:
            raise UserError("The role history file has a line this version cannot read.") from None
        roles[role.role_id] = role
    return roles


def save_roles(folder: Path, roles: dict[str, RoleRecord]) -> None:
    fsio.write_jsonl_atomic(roles_path(folder), [asdict(roles[role_id]) for role_id in sorted(roles)])


def latest_scan_dir(folder: Path) -> Path | None:
    scans = folder / paths.LAYOUT["scans"]
    if not scans.is_dir():
        return None
    candidates = sorted(
        (p for p in scans.iterdir() if p.is_dir() and (p / "scan-meta.json").is_file()),
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def _count(totals: object, key: str) -> int:
    value = totals.get(key) if isinstance(totals, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UserError("The latest scan summary is damaged. Run scan again.")
    return value


def load_pasted(folder: Path) -> list[tuple[str, Posting]]:
    """Every record in roles/pasted.jsonl as (pasted_on, posting), in file order."""
    path = pasted_path(folder)
    if not path.is_file():
        return []
    found: list[tuple[str, Posting]] = []
    for record in _read_lines(path, "pasted roles"):
        try:
            found.append((str(record["pasted_on"]), Posting.from_dict(record["posting"])))
        except (KeyError, ValueError, TypeError):
            raise UserError("The pasted roles file has a record this version cannot read."
                            ) from None
    return found


def load_rankable(folder: Path) -> RankInput:
    scan_dir = latest_scan_dir(folder)
    if scan_dir is None:
        raise UserError("No scan yet. Run scan first.")
    try:
        meta = fsio.read_json(scan_dir / "scan-meta.json")
    except (OSError, ValueError):
        raise UserError("The latest scan summary is damaged. Run scan again.") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("boards"), list):
        raise UserError("The latest scan summary is damaged. Run scan again.")
    tags: dict[str, tuple[str | None, str | None, str | None]] = {}
    for board in meta["boards"]:
        if isinstance(board, dict):
            tags[f"{board.get('ats')}:{board.get('slug')}"] = (
                board.get("industry"), board.get("size"), board.get("stage"))

    roles = load_roles(folder)
    rejected_by_reading, _reviewed = verdicts.load_verdicts(folder)
    scan_date = str(meta.get("scan_date", scan_dir.name))
    facts: list[RoleFacts] = []

    def add(posting: Posting, first_seen: str, last_seen: str) -> None:
        industry, size, stage = tags.get(f"{posting.ats}:{posting.board_slug}", (None, None, None))
        known = roles.get(posting.role_id)
        facts.append(RoleFacts(
            posting=posting, company_industry=industry, company_size=size, company_stage=stage,
            first_seen=known.first_seen if known else first_seen,
            last_seen=known.last_seen if known else last_seen,
            function_verdict=rejected_by_reading.get(posting.role_id),
        ))

    postings_file = scan_dir / "postings.jsonl"
    for record in _read_lines(postings_file, "scan postings") if postings_file.is_file() else []:
        try:
            add(Posting.from_dict(record), scan_date, scan_date)
        except ValueError:
            raise UserError("The latest scan has a posting this version cannot read. "
                            "Run scan again.") from None

    seen_ids = {fact.posting.role_id for fact in facts}
    seen_keys = {dedupe_key(fact.posting) for fact in facts}
    pasted_total = pasted_merged = 0
    for pasted_on, posting in sorted(load_pasted(folder), key=lambda item: item[1].role_id):
        pasted_total += 1
        key = dedupe_key(posting)
        if posting.role_id in seen_ids or key in seen_keys:
            pasted_merged += 1
            continue
        seen_ids.add(posting.role_id)
        seen_keys.add(key)
        facts.append(RoleFacts(
            posting=posting, company_industry=None, company_size=None, company_stage=None,
            first_seen=pasted_on, last_seen=pasted_on,
            function_verdict=rejected_by_reading.get(posting.role_id)))

    facts.sort(key=lambda f: f.posting.role_id)
    totals = meta.get("totals")
    return RankInput(
        scan_date=scan_date,
        fetched_total=_count(totals, "fetched"),
        title_gate_dropped=_count(totals, "title_gate_dropped"),
        duplicates_merged=_count(totals, "duplicates_merged") + pasted_merged,
        pasted_total=pasted_total,
        facts=facts,
    )
