"""Maintainer tool: verify seed boards live and write the starter directory.

Purpose
    The shipped directory (src/jobsearch/data/boards.json) must hold only boards that exist,
    have open postings, and really belong to the named company (a guessed
    slug can be a different company). This tool turns the public seed list into that file.
    Industry, size and stage in the seed are approximate maintainer tags from public knowledge,
    not verified facts; only existence, postings and identity are verified.

Logic
    1. Read the seed (default tools/directory_seed.json): {"rows": [{company, ats, slug,
       industry, size, stage}, ...]}.
    2. For each row, check the vocabularies, then GET the lean list URL (netguard.board_url with
       lean=True) through fetch.fetch with Purpose.ATS_API: one anonymous, read-only GET per
       row, paced at one second per host by the fetch layer.
    3. Classify: invalid_row (bad vocabulary or board name), duplicate_row (same ats and slug as
       an earlier row), fetch_failed (transient failure, a status other than 200 or 404, bad
       JSON, an unexpected shape, or a stale cached copy), no_board (HTTP 404), empty_board
       (zero postings), identity_mismatch (the board does not name the company, see
       jobsearch.identity), else verified.
    4. Write the verified rows, sorted by casefolded company then ats, with last_verified set
       to the run date, to --out in the boards.json shape. Print one line per rejected row with
       its reason and detail, and a final "verified N of M rows" line.

Guardrails
    - Read-only, anonymous GETs to the four approved job-board hosts, never LinkedIn.
    - A rejected or transiently failed row is reported and never written, so a flaky network
      shrinks the directory instead of poisoning it. Re-run (the cache lives in --cache-dir,
      which must be a scratch folder outside the repo) or use --refresh to bypass the cache.
    - --out is replaced atomically. --only limits the run to the named companies, so point it
      at a scratch --out, not at the shipped file.
    - Exit 0 when the run finished, even with rejections; exit 2 on a bad seed or arguments.

QA checklist
    - Run it live on one row first (--only), then the whole seed.
    - Check the summary: verified count between 90 and 120 for the shipped file.
    - Run `uv run --frozen pytest -q tests/test_directory.py` and the export leak gate.

Run: uv run --frozen python tools/build_directory.py --seed tools/directory_seed.json
     --out src/jobsearch/data/boards.json --cache-dir <scratch folder>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jobsearch import clock, directory, fetch, fsio, identity  # noqa: E402
from jobsearch.netguard import Purpose, UrlRefused, board_url  # noqa: E402

DEFAULT_SEED = Path(__file__).resolve().parent / "directory_seed.json"
DEFAULT_OUT = directory.DIRECTORY_PATH
SEED_FIELDS = ("company", "ats", "slug", "industry", "size", "stage")


class SeedError(Exception):
    """The seed file or an argument is unusable. The message is plain English."""


def load_seed(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SeedError(f"The seed file {path} could not be read as JSON.") from None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SeedError('The seed file must be a JSON object with a "rows" list.')
    return rows


def _check_row(row: object) -> str | None:
    """None when the row is well formed, else a short plain-English problem."""
    if not isinstance(row, dict):
        return "the row is not an object"
    extra = sorted(set(row) - set(SEED_FIELDS))
    if extra:
        return f"unknown field(s) {', '.join(extra)}"
    for key in ("company", "slug"):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"{key} must be non-empty text"
    for key, allowed in (("ats", directory.ATS_CHOICES), ("industry", directory.INDUSTRIES),
                         ("size", directory.SIZES), ("stage", directory.STAGES)):
        if row.get(key) not in allowed:
            return f"{key} must be one of {', '.join(allowed)}"
    return None


def _postings(ats: str, payload: object) -> int | None:
    """How many open postings the list carries, or None when the shape is not recognized."""
    if ats in ("lever", "lever_eu"):
        return len(payload) if isinstance(payload, list) else None
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return None
    if ats == "ashby":  # an unlisted job is not on the public board
        return sum(1 for job in jobs if not (isinstance(job, dict) and job.get("isListed") is False))
    return len(jobs)


def verify_row(row: dict, *, cache_dir: Path, today: str, refresh: bool = False,
               transport: fetch.Transport | None = None) -> tuple[dict | None, str, str]:
    """(entry, "verified", "") for a good row, else (None, reason, plain-English detail)."""
    ats, slug, company = row["ats"], row["slug"], row["company"]
    try:
        url = board_url(ats, slug, lean=True)
    except UrlRefused as err:
        return None, "invalid_row", str(err)
    try:
        result = fetch.fetch(url, purpose=Purpose.ATS_API, cache_dir=cache_dir, refresh=refresh,
                             transport=transport)
        if result.status == 404:
            return None, "no_board", "HTTP 404"
        if result.status != 200:
            return None, "fetch_failed", f"HTTP {result.status}"
        if result.stale:
            return None, "fetch_failed", "only a stale cached copy was available"
        payload = fetch.decode_json(result)
    except (fetch.FetchError, UrlRefused) as err:
        return None, "fetch_failed", str(err)
    count = _postings(ats, payload)
    if count is None:
        return None, "fetch_failed", "the board answered with a shape this tool does not know"
    if count == 0:
        return None, "empty_board", "no open postings"
    found = identity.identity_from_payload(ats, payload, company)
    if found is None:
        return None, "identity_mismatch", "the board does not name this company"
    return {
        "company": company, "ats": ats, "slug": slug, "industry": row["industry"],
        "size": row["size"], "stage": row["stage"], "last_verified": today,
        "identity": found, "postings_at_verify": count,
    }, "verified", ""


def build(rows: list, *, cache_dir: Path, today: str, only: list[str] | None = None,
          refresh: bool = False, transport: fetch.Transport | None = None,
          ) -> tuple[list[dict], list[tuple[str, str, str]], int]:
    """(verified entries in output order, [(label, reason, detail)] rejections, rows considered)."""
    wanted = {name.casefold() for name in only} if only else None
    entries: list[dict] = []
    rejected: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    considered = 0
    for index, row in enumerate(rows):
        label = f"row {index + 1}"
        problem = _check_row(row)
        if problem is None:
            label = f"{row['company']} ({row['ats']}:{row['slug']})"
            if wanted is not None and row["company"].casefold() not in wanted:
                continue
        elif wanted is not None:
            continue
        considered += 1
        if problem is not None:
            rejected.append((label, "invalid_row", problem))
            continue
        key = f"{row['ats']}:{row['slug']}"
        if key in seen:
            rejected.append((label, "duplicate_row", "the same board appears earlier in the seed"))
            continue
        seen.add(key)
        entry, reason, detail = verify_row(row, cache_dir=cache_dir, today=today, refresh=refresh,
                                   transport=transport)
        if entry is None:
            rejected.append((label, reason, detail))
        else:
            entries.append(entry)
    entries.sort(key=lambda e: (e["company"].casefold(), e["ats"]))
    return entries, rejected, considered


def render(entries: list[dict], today: str) -> str:
    document = {"schema_version": 1, "generated_on": today, "entries": entries}
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None, *, transport: fetch.Transport | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify seed boards live and write the directory.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache-dir", type=Path, required=True,
                        help="a scratch folder outside the repo for the response cache")
    parser.add_argument("--only", action="append", metavar="COMPANY",
                        help="verify only this company (repeatable); writes only those rows")
    parser.add_argument("--refresh", action="store_true", help="bypass the response cache")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        rows = load_seed(args.seed)
    except SeedError as err:
        print(err, file=sys.stderr)
        return 2
    today = clock.today_iso()
    entries, rejected, considered = build(
        rows, cache_dir=args.cache_dir, today=today, only=args.only, refresh=args.refresh,
        transport=transport,
    )
    for label, reason, detail in rejected:
        print(f"rejected {label}: {reason} - {detail}")
    fsio.write_text_atomic(args.out, render(entries, today))
    print(f"verified {len(entries)} of {considered} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
