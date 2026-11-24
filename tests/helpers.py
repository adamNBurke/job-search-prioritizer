"""Shared test helpers: the one-board scan world, the CLI runners and the posting and ranked-file builders.

Purpose: one copy of the setup the test files used to copy, so a new patch point or a new file field is made once instead of in every file.
Logic: these are plain functions and constants. They include the Markdown doc parsers (md_section,
table_rows, first_column, bullets), the ATS keysets reader, the em-dash check and PERSONA_NAMES. The
fixtures built from them (scan_env, folder) live in tests/conftest.py. tests/ has no __init__.py, so
pytest's default import mode puts this folder on sys.path and a test file imports it as
`from helpers import ...`.
Guardrails: fictional data only (Acme Robotics, example.com). Nothing here opens a socket or reads
the real clock; the doc readers open files under docs/ only. Paths use pathlib and every file is read
and written with encoding="utf-8", so the Windows CI run and tests/test_utf8_ast.py stay green.
QA checklist: a new patch point for the scan world goes into scan_env once; a new field on a posting
goes into BASE_POSTING once; run_cli stays the strictest runner (it asserts ok and the exit code).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jobsearch import cli
from jobsearch.fetch import TransportResponse
from jobsearch.models import Posting
from jobsearch.roles_store import RoleFacts

ROOT = Path(__file__).resolve().parent
LIST_FIXTURE = ROOT / "fixtures" / "ats" / "greenhouse_list.json"
TRACER = ROOT / "fixtures" / "criteria" / "tracer.json"
TODAY = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
PERSONA_NAMES = ["casey-ops", "devon-analyst", "rowan-designer"]  # folders under fixtures/personas
EM_DASH = chr(0x2014)  # built at runtime so no literal em dash sits in a test file


def has_em_dash(text: str) -> bool:
    return EM_DASH in text


def md_section(text: str, heading: str, level: str = "##", doc: str = "the document") -> str:
    """The body of a '<level> heading' section, up to the next heading of the same or higher level."""
    pattern = rf"^{level} {re.escape(heading)}\n(.*?)(?=^#{{1,{len(level)}}} |\Z)"
    match = re.search(pattern, text, re.M | re.S)
    assert match, f"{doc} has no '{level} {heading}' section"
    return match.group(1)


def table_rows(section: str) -> list[list[str]]:
    """The cells of each body row of the first Markdown table in a section (header and rule skipped)."""
    rows = [ln for ln in section.splitlines() if ln.startswith("|")]
    return [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows[2:]]


def first_column(section: str) -> list[str]:
    return [row[0].strip("`") for row in table_rows(section)]


def bullets(section: str) -> list[str]:
    return [ln[2:].strip().strip("`") for ln in section.splitlines() if ln.startswith("- ")]


def ats_keysets() -> dict:
    """The keysets block of docs/ats-live-vs-docs.md, parsed."""
    text = (ROOT.parent / "docs" / "ats-live-vs-docs.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- keysets:begin -->\s*`{3}json\s*(.*?)`{3}\s*<!-- keysets:end -->", text, re.S)
    assert match, "keysets block missing from docs/ats-live-vs-docs.md"
    return json.loads(match.group(1))


def board(ats: str = "greenhouse", slug: str = "acme-robotics", company: str = "Acme Robotics") -> dict:
    """One directory entry, the nine keys directory.load_directory requires."""
    return {
        "company": company, "ats": ats, "slug": slug,
        "industry": "manufacturing-hardware", "size": "51-200", "stage": "growth",
        "last_verified": "2026-11-17", "identity": "company_name", "postings_at_verify": 4,
    }


def write_directory(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(
        {"schema_version": 1, "generated_on": "2026-11-17", "entries": entries}), encoding="utf-8")


class Transport:
    """A scripted fetch transport: records every URL, serves a scripted URL, else the default body.

    A URL passed to serve() gets its own response. Any other URL gets `body` as a 200 when body is
    set, and a 404 with an empty JSON object when it is None.
    """

    def __init__(self, body: bytes | None = None) -> None:
        self.body = body
        self.routes: dict[str, TransportResponse] = {}
        self.calls: list[str] = []

    def serve(self, url: str, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        self.routes[url] = TransportResponse(
            status, {"content-type": "application/json", **(headers or {})}, body)

    def __call__(self, url, headers, clock=None):
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        if self.body is not None:
            return TransportResponse(200, {"content-type": "application/json"}, self.body)
        return TransportResponse(404, {"content-type": "application/json"}, b"{}")


def cli_result(capsys, *argv: str) -> tuple[int, dict]:
    """Run a command through cli.main and return (exit code, parsed Result JSON).

    An argparse refusal prints nothing on stdout, so the Result is built from stderr.
    """
    capsys.readouterr()
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    if not captured.out.strip():
        return code, {"ok": False, "error": captured.err.strip()}
    return code, json.loads(captured.out)


def run_cli(env, *argv: str, ok: bool = True) -> dict:
    """Run a command in a scan_env world; assert the Result's ok flag and the matching exit code."""
    code, out = cli_result(env.capsys, *argv, "--data-dir", str(env.folder))
    assert out["ok"] is ok, out
    assert code == (0 if ok else 1), out
    return out


BASE_POSTING = {
    "schema_version": 1, "role_id": "greenhouse:acme-robotics:1", "source": "ats",
    "ats": "greenhouse", "board_slug": "acme-robotics", "posting_id": "1",
    "company": "Acme Robotics", "title": "Robotics Engineer", "team": None,
    "locations": [], "work_mode_text": None, "work_mode": "unknown", "posted": None,
    "url_apply": "https://example.com/apply", "url_posting": None,
    "description_text": "Build robots.", "comp_min": None, "comp_max": None,
    "comp_currency": None, "comp_interval": None, "comp_source": None,
    "fetched_at": "2026-11-17T12:00:00Z",
}


def facts_for(industry: str | None = None, size: str | None = None, stage: str | None = None,
              **posting: object) -> RoleFacts:
    """RoleFacts for one posting: BASE_POSTING with `posting` fields overridden."""
    data = dict(BASE_POSTING)
    data.update(posting)
    return RoleFacts(Posting.from_dict(data), industry, size, stage, "2026-11-17", "2026-11-17")


def ranked_role(role_id: str, tier: str, score: int, **extra: object) -> dict:
    """One shown entry of a ranked file, with any extra keys after the four."""
    return {"role_id": role_id, "tier": tier, "score": score, "shown": True, **extra}


def write_ranked(folder: Path, scan_date: str, roles: list[dict], version: str = "1.2") -> Path:
    """Write ranked/ranked-{scan_date}.json by hand and return its path."""
    path = folder / "ranked" / f"ranked-{scan_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "criteria_version": version,
                                "scan_date": scan_date, "roles": roles}), encoding="utf-8")
    return path
