"""tools/build_directory.py: verify seed boards through a scripted transport, write only good rows.

Purpose: prove the maintainer tool keeps a bad or unreachable board out of the directory and
says why, and that what it writes loads with directory.load_directory and filters by industry.
Logic: a scripted transport answers by URL; MIN_INTERVAL_SECONDS is zero so no test sleeps.
Guardrails: offline; the companies are fictional; every verification URL is lean.
QA checklist: a new rejection reason gets a seed row, a scripted answer and an assertion here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import build_directory
from jobsearch import clock, directory, fetch
from jobsearch.fetch import TransportError, TransportResponse

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ats"
TODAY = "2026-11-17"


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(clock, "today_iso", lambda: TODAY)


def seed_row(company: str, ats: str, slug: str, industry: str = "manufacturing-hardware",
             size: str = "51-200", stage: str = "growth") -> dict:
    return {"company": company, "ats": ats, "slug": slug, "industry": industry,
            "size": size, "stage": stage}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Scripted:
    """Answers by the last path segment of the URL; records every URL requested."""

    def __init__(self, routes: dict) -> None:
        self.routes, self.urls = routes, []

    def __call__(self, url: str, headers) -> TransportResponse:
        self.urls.append(url)
        slug = url.split("?")[0].rstrip("/").split("/")[-1]
        if slug == "jobs":  # the Greenhouse list ends in /jobs
            slug = url.split("/boards/")[1].split("/")[0]
        route = self.routes[slug]
        if isinstance(route, Exception):
            raise route
        if isinstance(route, tuple):
            return TransportResponse(route[0], {}, route[1])
        return TransportResponse(200, {"content-type": "application/json"}, route)


GOOD_ROUTES = {
    "acme": fixture_bytes("greenhouse_list.json"),
    "acme-lever": fixture_bytes("lever_us_list.json"),
    "acme-eu": fixture_bytes("lever_eu_list.json"),
    "acme-ashby": fixture_bytes("ashby_list.json"),
}


def write_seed(tmp_path: Path, rows: list) -> Path:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def run(tmp_path: Path, rows: list, routes: dict, capsys, extra: list[str] | None = None):
    out = tmp_path / "out" / "boards.json"
    transport = Scripted(routes)
    code = build_directory.main(
        ["--seed", str(write_seed(tmp_path, rows)), "--out", str(out),
         "--cache-dir", str(tmp_path / "cache"), *(extra or [])],
        transport=transport,
    )
    return code, out, transport, capsys.readouterr().out


def test_all_four_systems_verify_and_the_file_loads_and_filters(tmp_path, capsys) -> None:
    rows = [
        seed_row("Acme Robotics", "ashby", "acme-ashby", "software"),
        seed_row("Acme Robotics", "lever", "acme-lever"),
        seed_row("Acme Robotics", "lever_eu", "acme-eu"),
        seed_row("Acme Robotics", "greenhouse", "acme"),
    ]
    code, out, transport, text = run(tmp_path, rows, GOOD_ROUTES, capsys)
    assert code == 0
    assert "verified 4 of 4 rows" in text
    entries = directory.load_directory(out)
    assert [e.ats for e in entries] == ["ashby", "greenhouse", "lever", "lever_eu"]
    written = json.loads(out.read_text(encoding="utf-8"))["entries"]
    assert {r["identity"] for r in written} == {"company_name", "description_mentions"}
    assert all(r["last_verified"] == TODAY and r["postings_at_verify"] > 0 for r in written)
    assert [e.ats for e in directory.select_boards(entries, ["software"], [])] == ["ashby"]
    assert len(directory.select_boards(entries, ["manufacturing-hardware"], [])) == 3


def test_verification_requests_are_lean(tmp_path, capsys) -> None:
    rows = [seed_row("Acme Robotics", "greenhouse", "acme"),
            seed_row("Acme Robotics", "ashby", "acme-ashby")]
    _, _, transport, _ = run(tmp_path, rows, GOOD_ROUTES, capsys)
    assert sorted(transport.urls) == [
        "https://api.ashbyhq.com/posting-api/job-board/acme-ashby",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
    ]


def test_rejected_rows_are_reported_with_a_reason_and_never_written(tmp_path, capsys) -> None:
    empty = json.dumps({"jobs": [], "meta": {"total": 0}}).encode()
    other = json.dumps({"jobs": [{"id": 1, "company_name": "Someone Else"}]}).encode()
    routes = {
        **GOOD_ROUTES,
        "gone": (404, b'{"status":404,"error":"Job not found"}'),
        "empty": empty,
        "wrong": other,
        "down": TransportError("no route"),
        "teapot": (403, b"forbidden"),
        "garbled": b"<html>not json</html>",
        "shape": json.dumps({"unexpected": True}).encode(),
    }
    rows = [
        seed_row("Acme Robotics", "greenhouse", "acme"),
        seed_row("Gone Co", "greenhouse", "gone"),
        seed_row("Empty Co", "greenhouse", "empty"),
        seed_row("Acme Robotics", "greenhouse", "wrong"),
        seed_row("Down Co", "greenhouse", "down"),
        seed_row("Teapot Co", "greenhouse", "teapot"),
        seed_row("Garbled Co", "greenhouse", "garbled"),
        seed_row("Shape Co", "greenhouse", "shape"),
        seed_row("Acme Robotics", "greenhouse", "acme"),
        seed_row("Bad Slug", "greenhouse", "a/b"),
        {"company": "Bad Vocab", "ats": "greenhouse", "slug": "x", "industry": "nope",
         "size": "51-200", "stage": "growth"},
    ]
    code, out, _, text = run(tmp_path, rows, routes, capsys)
    assert code == 0
    lines = [line for line in text.splitlines() if line.startswith("rejected")]
    def reason_of(line: str) -> str:
        return line.split(": ", 1)[1].split(" - ")[0]

    reasons = {line.split(" (")[0].removeprefix("rejected "): reason_of(line)
               for line in lines if "(" in line}
    assert reasons["Gone Co"] == "no_board"
    assert reasons["Empty Co"] == "empty_board"
    assert reasons["Down Co"] == "fetch_failed"
    assert reasons["Teapot Co"] == "fetch_failed"
    assert reasons["Garbled Co"] == "fetch_failed"
    assert reasons["Shape Co"] == "fetch_failed"
    assert any("greenhouse:wrong" in line and reason_of(line) == "identity_mismatch"
               for line in lines)
    assert any(reason_of(line) == "duplicate_row" for line in lines)
    assert any("Bad Slug" in line and reason_of(line) == "invalid_row" for line in lines)
    assert any("row 11" in line and reason_of(line) == "invalid_row" for line in lines)
    assert "verified 1 of 11 rows" in text
    assert [e.slug for e in directory.load_directory(out)] == ["acme"]


def test_a_stale_cached_copy_is_a_failed_fetch_not_a_verification(tmp_path, monkeypatch) -> None:
    rows = [seed_row("Acme Robotics", "greenhouse", "acme")]
    entry, reason, _ = build_directory.verify_row(
        rows[0], cache_dir=tmp_path / "c", today=TODAY, transport=Scripted(GOOD_ROUTES))
    assert reason == "verified" and entry is not None
    stale = fetch.FetchResult(200, {}, GOOD_ROUTES["acme"], True, "2026-01-01T00:00:00Z",
                              stale=True)
    monkeypatch.setattr(fetch, "fetch", lambda *a, **k: stale)
    assert build_directory.verify_row(
        rows[0], cache_dir=tmp_path / "c", today=TODAY)[1:] == (
        "fetch_failed", "only a stale cached copy was available")


def test_output_is_sorted_by_company_then_ats_and_is_stable(tmp_path, capsys) -> None:
    rows = [
        seed_row("zeta Robotics", "lever", "acme-lever"),
        seed_row("Acme Robotics", "lever_eu", "acme-eu"),
        seed_row("Acme Robotics", "greenhouse", "acme"),
    ]
    _, out, _, _ = run(tmp_path, rows, GOOD_ROUTES, capsys)
    first = out.read_text(encoding="utf-8")
    keys = [(e.company, e.ats) for e in directory.load_directory(out)]
    assert keys == [("Acme Robotics", "greenhouse"), ("Acme Robotics", "lever_eu")]
    # The Lever fixture names Acme Robotics only, so the "zeta Robotics" row fails identity.
    assert "zeta" not in first


def test_only_limits_the_run_and_the_output(tmp_path, capsys) -> None:
    rows = [seed_row("Acme Robotics", "greenhouse", "acme"),
            seed_row("Acme Robotics", "lever", "acme-lever"),
            seed_row("Other Co", "greenhouse", "other")]
    code, out, transport, text = run(tmp_path, rows, GOOD_ROUTES, capsys,
                                     ["--only", "acme robotics"])
    assert code == 0 and "verified 2 of 2 rows" in text
    assert len(transport.urls) == 2


def test_a_bad_seed_exits_two_and_writes_nothing(tmp_path, capsys) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text("{not json", encoding="utf-8")
    out = tmp_path / "out.json"
    code = build_directory.main(["--seed", str(seed), "--out", str(out),
                                 "--cache-dir", str(tmp_path / "c")])
    assert code == 2 and not out.exists()
    seed.write_text('{"rows": 5}', encoding="utf-8")
    assert build_directory.main(["--seed", str(seed), "--out", str(out),
                                 "--cache-dir", str(tmp_path / "c")]) == 2


def test_the_cache_dir_is_required() -> None:
    with pytest.raises(SystemExit):
        build_directory.main(["--seed", "x"])


def test_the_tool_has_no_direct_clock_or_network_import() -> None:
    text = Path(build_directory.__file__).read_text(encoding="utf-8")
    assert "datetime.now" not in text and "import requests" not in text
    assert "fetch.fetch(" in text
