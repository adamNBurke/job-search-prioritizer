"""Scan hardening: dedupe, closed and reopened roles, failed boards, parallel hosts, re-runs.

Purpose: prove the scan never shows a duplicate, never keeps a closed role in the rankable set,
never punishes a board that was briefly unreachable, and repeats exactly inside the cache window.
Logic: a scripted web transport serves small invented board payloads by slug, records every call
with its host and thread, and can sleep a seeded random time so thread completion order is
shuffled. A mutable clock moves the scan date between runs. fetch.MIN_INTERVAL_SECONDS is set to
zero so per-host pacing never waits in a test. No socket is opened.
Guardrails: payloads hold fictional data only (Acme Robotics, example.com). Each test builds its
own data folder under tmp_path, so tests never share state.
QA checklist: a new scan behavior gets a named test here; the funnel identity is asserted on every
scan that has a title-gate drop or a merge.
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from helpers import TRACER, board

from jobsearch import cli, clock, directory, fetch, roles_store, scan
from jobsearch.fetch import TransportError, TransportResponse

ROOT = Path(__file__).resolve().parent.parent
DAY1 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
DAY3 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------ payloads


def gh(*jobs: tuple[int, str, str]) -> dict:
    return {"jobs": [
        {"id": i, "title": title, "absolute_url": f"https://job-boards.example.com/{i}",
         "location": {"name": place}, "content": "", "company_name": "Acme Robotics",
         "first_published": "2026-11-01T09:00:00-05:00"}
        for i, title, place in jobs], "meta": {"total": len(jobs)}}


def lever_list(*jobs: tuple[str, str, str]) -> list:
    return [{"id": i, "text": title, "applyUrl": f"https://jobs.example.com/{i}/apply",
             "hostedUrl": f"https://jobs.example.com/{i}", "workplaceType": "remote",
             "categories": {"location": place, "allLocations": [place]},
             "createdAt": 1788199000001, "descriptionPlain": ""}
            for i, title, place in jobs]


def ashby_jobs(*jobs: tuple[str, str, str]) -> dict:
    return {"apiVersion": "1", "jobs": [
        {"id": i, "title": title, "applyUrl": f"https://jobs.example.com/{i}/application",
         "jobUrl": f"https://jobs.example.com/{i}", "location": place, "secondaryLocations": [],
         "workplaceType": "Remote", "isListed": True, "isRemote": True, "team": "Platform",
         "publishedAt": "2026-11-01T09:00:00.000+00:00", "descriptionPlain": "",
         "compensation": {"compensationTiers": []}}
        for i, title, place in jobs]}


class Web:
    """Serves a payload per board slug; records (host, slug, thread) for every call."""

    def __init__(self, routes: dict[str, object], seed: int | None = None,
                 meet: int = 0) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, int]] = []
        self.rng = random.Random(seed) if seed is not None else None
        self._lock = threading.Lock()
        # With meet=N the first call to each host waits until N hosts are calling at once, so a
        # sequential scan times out (BrokenBarrierError) instead of passing by luck of timing.
        self._barrier = threading.Barrier(meet, timeout=10) if meet else None
        self._arrived: set[str] = set()

    def __call__(self, url, headers, clock=None):
        parts = urlsplit(url)
        slug = next(p for p in parts.path.split("/") if p in self.routes)
        if self._barrier is not None:
            with self._lock:
                first = parts.hostname not in self._arrived
                self._arrived.add(parts.hostname)
            if first:
                self._barrier.wait()
        if self.rng is not None:
            with self._lock:
                pause = self.rng.random() * 0.03
            time.sleep(pause)
        with self._lock:
            self.calls.append((parts.hostname, slug, threading.get_ident()))
        route = self.routes[slug]
        if isinstance(route, Exception):
            raise route
        if isinstance(route, int):
            return TransportResponse(route, {"content-type": "application/json"},
                                     b'{"status":404}')
        return TransportResponse(200, {"content-type": "application/json"},
                                 json.dumps(route).encode("utf-8"))


def family(fid: str, term: str, rung: int = 2) -> dict:
    return {"id": fid, "label": term, "rung": rung, "terms": [term], "regex": [],
            "origin": "stated"}


class Env:
    def __init__(self, tmp_path, monkeypatch, capsys, entries, terms=("data analyst",)):
        self.tmp_path, self.monkeypatch, self.capsys = tmp_path, monkeypatch, capsys
        self.directory_file = tmp_path / "boards.json"
        self.set_entries(entries)
        monkeypatch.setattr(directory, "DIRECTORY_PATH", self.directory_file)
        self.now = DAY1
        monkeypatch.setattr(clock, "now_utc", lambda: self.now)
        self.folder = tmp_path / "ws"
        assert cli.main(["init", "--data-dir", str(self.folder)]) == 0
        capsys.readouterr()
        (self.folder / "config").mkdir()
        self.set_terms(terms)

    def set_entries(self, entries: list[dict]) -> None:
        self.directory_file.write_text(json.dumps(
            {"schema_version": 1, "generated_on": "2026-11-17", "entries": entries}),
            encoding="utf-8")

    def set_terms(self, terms) -> None:
        data = json.loads(TRACER.read_text(encoding="utf-8"))
        data["title"]["families"] = [family(f"tf-{n}", t) for n, t in enumerate(terms)]
        self.criteria = data
        (self.folder / "config" / "criteria.json").write_text(json.dumps(data), encoding="utf-8")

    def scan(self, web: Web, *, day=None, refresh: bool = True):
        if day is not None:
            self.now = day
        return scan.run_scan(self.folder, self.criteria, refresh=refresh, transport=web)

    def meta(self, date: str) -> dict:
        path = self.folder / "scans" / date / "scan-meta.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def roles(self) -> dict:
        return roles_store.load_roles(self.folder)

    def read(self, rel: str) -> bytes:
        return (self.folder / rel).read_bytes()

    def cli(self, *argv: str) -> dict:
        code = cli.main([*argv, "--data-dir", str(self.folder)])
        out = json.loads(self.capsys.readouterr().out)
        assert code == (0 if out["ok"] else 1), out
        return out


@pytest.fixture
def make_env(tmp_path, monkeypatch, capsys):
    def build(entries, **kwargs):
        return Env(tmp_path, monkeypatch, capsys, entries, **kwargs)
    return build


ACME = "greenhouse:acme-robotics:"


def ids(postings_text: bytes) -> list[str]:
    return [json.loads(line)["role_id"] for line in postings_text.decode("utf-8").splitlines()]


# ------------------------------------------------------------------------------- dedupe


def test_duplicates_merge_to_the_lowest_role_id_and_the_senior_title_stays(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    web = Web({"acme-robotics": gh(
        (101, "Data Analyst", "New York, NY"), (102, "data   ANALYST ", "New York, NY"),
        (103, "Senior Data Analyst", "New York, NY"), (104, "Technical Writer", "New York, NY"))})
    outcome = env.scan(web)
    assert ids(env.read("scans/2026-11-17/postings.jsonl")) == [ACME + "101", ACME + "103"]
    assert outcome.duplicates_merged == 1
    roles = env.roles()
    assert roles[ACME + "101"].duplicates == [ACME + "102"]
    assert roles[ACME + "101"].dedupe_key
    assert ACME + "102" not in roles


def test_funnel_identity_holds_in_scan_meta_and_in_the_rank_input(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    web = Web({"acme-robotics": gh(
        (101, "Data Analyst", "Austin, TX"), (102, "data analyst", "Austin, TX"),
        (103, "Technical Writer", "Austin, TX"))})
    env.scan(web)
    meta = env.meta("2026-11-17")
    totals = meta["totals"]
    assert totals["duplicates_merged"] == 1 and totals["title_gate_dropped"] == 1
    assert totals["fetched"] == (totals["title_gate_dropped"] + totals["duplicates_merged"]
                                 + totals["kept"])
    kept_lines = env.read("scans/2026-11-17/postings.jsonl").decode("utf-8").splitlines()
    assert totals["kept"] == len(kept_lines)
    assert meta["boards"][0]["kept"] == len(kept_lines)
    rank_input = roles_store.load_rankable(env.folder)
    assert rank_input.duplicates_merged == 1
    assert rank_input.fetched_total == (rank_input.title_gate_dropped
                                        + rank_input.duplicates_merged + len(rank_input.facts))


def test_same_title_in_different_locations_is_not_a_duplicate(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    web = Web({"acme-robotics": gh((101, "Data Analyst", "Austin, TX"),
                                   (102, "Data Analyst", "Boston, MA"))})
    assert env.scan(web).duplicates_merged == 0


def test_dedupe_key_and_title_normalization():
    from jobsearch import dedupe
    from jobsearch.models import Posting

    assert dedupe.dedupe_title("  Data   Analyst, II ") == "data analyst ii"
    assert dedupe.dedupe_title("Data Analyst") == dedupe.dedupe_title("DATA ANALYST")
    assert dedupe.dedupe_title("Senior Data Analyst") != dedupe.dedupe_title("Data Analyst")

    def posting(title: str, places: list[str], company: str = "Acme Robotics") -> Posting:
        return Posting.from_dict({
            "schema_version": 1, "role_id": "greenhouse:a:1", "source": "ats",
            "ats": "greenhouse", "board_slug": "a", "posting_id": "1", "company": company,
            "title": title, "team": None, "locations": places, "work_mode_text": None,
            "work_mode": "unknown", "posted": None, "url_apply": "https://x.example.com",
            "url_posting": None, "description_text": "", "comp_min": None, "comp_max": None,
            "comp_currency": None, "comp_interval": None, "comp_source": None,
            "fetched_at": "2026-11-17T12:00:00Z"})

    one = dedupe.dedupe_key(posting("Data Analyst", ["Boston, MA", "Austin, TX"]))
    two = dedupe.dedupe_key(posting("data analyst", [" austin, tx", "BOSTON, MA"], "ACME robotics"))
    assert one == two
    assert one != dedupe.dedupe_key(posting("Data Analyst", ["Austin, TX"]))


@pytest.mark.parametrize("order", list(itertools.permutations(
    ["jobsearch.dedupe", "jobsearch.scan", "jobsearch.roles_store"])))
def test_modules_import_in_every_order_without_a_cycle(order):
    code = "; ".join(f"import {name}" for name in order)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=ROOT, check=False)
    assert done.returncode == 0, done.stderr


def test_dedupe_module_imports_neither_scan_nor_roles_store():
    source = (ROOT / "src" / "jobsearch" / "dedupe.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    assert not {n for n in imported if n.endswith(("scan", "roles_store"))}


# --------------------------------------------------------------------- closed and reopened


def two_roles() -> Web:
    return Web({"acme-robotics": gh((1, "Data Analyst", "Austin, TX"),
                                    (2, "Data Analyst II", "Austin, TX"))})


def test_a_role_gone_on_the_next_scan_is_closed_and_listed(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    env.scan(two_roles())
    outcome = env.scan(Web({"acme-robotics": gh((1, "Data Analyst", "Austin, TX"))}), day=DAY2)
    closed = env.roles()[ACME + "2"]
    assert (closed.status, closed.closed_on, closed.last_seen) == (
        "closed", "2026-11-18", "2026-11-17")
    assert [r["role_id"] for r in env.meta("2026-11-18")["closed_roles"]] == [ACME + "2"]
    assert [r["role_id"] for r in outcome.closed_roles] == [ACME + "2"]
    rankable = roles_store.load_rankable(env.folder)
    assert [f.posting.role_id for f in rankable.facts] == [ACME + "1"]


def test_a_closed_role_that_returns_reopens_with_its_first_seen_kept(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    env.scan(two_roles())
    env.scan(Web({"acme-robotics": gh((1, "Data Analyst", "Austin, TX"))}), day=DAY2)
    env.scan(two_roles(), day=DAY3)
    role = env.roles()[ACME + "2"]
    assert (role.status, role.first_seen, role.closed_on) == ("open", "2026-11-17", None)
    assert role.last_seen == "2026-11-19"
    meta = env.meta("2026-11-19")
    assert [r["role_id"] for r in meta["reopened_roles"]] == [ACME + "2"]
    assert meta["closed_roles"] == []


def test_a_board_that_failed_keeps_its_roles_open_and_the_user_is_told(make_env):
    env = make_env([board("greenhouse", "acme-robotics"), board("lever", "acme-lever")])
    both = {"acme-robotics": gh((1, "Data Analyst", "Austin, TX")),
            "acme-lever": lever_list(("l-1", "Data Analyst", "Remote"))}
    env.scan(Web(both))
    down = Web({**both, "acme-lever": TransportError("offline")})
    outcome = env.scan(down, day=DAY2)
    assert {r.status for r in env.roles().values()} == {"open"}
    lever_meta = next(b for b in env.meta("2026-11-18")["boards"] if b["slug"] == "acme-lever")
    assert lever_meta["status"] == "failed" and lever_meta["error"]
    assert env.meta("2026-11-18")["closed_roles"] == []
    assert any("acme-lever" in w or "Acme Robotics" in w for w in outcome.warnings)


def test_a_board_that_failed_with_no_saved_copy_keeps_its_roles_open(make_env):
    env = make_env([board("greenhouse", "acme-robotics"), board("lever", "acme-lever")])
    both = {"acme-robotics": gh((1, "Data Analyst", "Austin, TX")),
            "acme-lever": lever_list(("l-1", "Data Analyst", "Remote"))}
    env.scan(Web(both))
    cache = env.folder / "cache" / "http"
    for path in cache.rglob("*"):
        if path.is_file():
            path.unlink()
    env.scan(Web({**both, "acme-lever": TransportError("offline")}), day=DAY2)
    assert {r.status for r in env.roles().values()} == {"open"}


def test_the_command_result_says_how_many_boards_could_not_be_reached(make_env):
    env = make_env([board("greenhouse", "acme-robotics"), board("lever", "acme-lever")])
    both = {"acme-robotics": gh((1, "Data Analyst", "Austin, TX")),
            "acme-lever": lever_list(("l-1", "Data Analyst", "Remote"))}
    env.monkeypatch.setattr(fetch, "_default_transport", Web(both))
    env.set_terms(("data analyst",))
    env.cli("scan")
    env.monkeypatch.setattr(fetch, "_default_transport",
                            Web({**both, "acme-lever": TransportError("offline")}))
    env.now = DAY2
    result = env.cli("scan", "--refresh")
    assert "Could not reach 1 board; its roles keep their last state." in result["warnings"]
    counts = result["counts"]
    assert counts["boards_ok"] == 1 and counts["boards_failed"] == 1
    assert counts["boards_not_found"] == 0


def test_an_empty_board_answer_closes_all_of_that_boards_roles(make_env):
    env = make_env([board("greenhouse", "acme-robotics"), board("lever", "acme-lever")])
    both = {"acme-robotics": gh((1, "Data Analyst", "Austin, TX")),
            "acme-lever": lever_list(("l-1", "Data Analyst", "Remote"))}
    env.scan(Web(both))
    env.scan(Web({**both, "acme-lever": []}), day=DAY2)
    roles = env.roles()
    assert roles["lever:acme-lever:l-1"].status == "closed"
    assert roles[ACME + "1"].status == "open"
    lever_meta = next(b for b in env.meta("2026-11-18")["boards"] if b["slug"] == "acme-lever")
    assert lever_meta["status"] == "ok"


def test_a_board_that_now_answers_404_closes_all_of_that_boards_roles(make_env):
    env = make_env([board("greenhouse", "acme-robotics"), board("lever", "acme-lever")])
    both = {"acme-robotics": gh((1, "Data Analyst", "Austin, TX")),
            "acme-lever": lever_list(("l-1", "Data Analyst", "Remote"))}
    env.scan(Web(both))
    env.scan(Web({**both, "acme-lever": 404}), day=DAY2)
    assert env.roles()["lever:acme-lever:l-1"].status == "closed"
    lever_meta = next(b for b in env.meta("2026-11-18")["boards"] if b["slug"] == "acme-lever")
    assert lever_meta["status"] == "no_board"


def test_a_role_dropped_by_the_title_gate_is_not_closed(make_env):
    env = make_env([board("greenhouse", "acme-robotics")], terms=("data analyst", "ops analyst"))
    web = Web({"acme-robotics": gh((1, "Data Analyst", "Austin, TX"),
                                   (2, "Ops Analyst", "Austin, TX"))})
    env.scan(web)
    env.set_terms(("data analyst",))  # the ops family is gone; the role is still on the board
    env.scan(web, day=DAY2)
    assert env.roles()[ACME + "2"].status == "open"
    assert env.meta("2026-11-18")["closed_roles"] == []
    assert ids(env.read("scans/2026-11-18/postings.jsonl")) == [ACME + "1"]


def test_closed_roles_are_sorted_by_company_title_then_role_id(make_env):
    env = make_env([board("greenhouse", "zeta-co", "Zeta Co"),
                    board("greenhouse", "alpha-co", "Alpha Co")])
    full = {"zeta-co": gh((9, "Data Analyst", "Austin, TX")),
            "alpha-co": gh((8, "Data Analyst II", "Austin, TX"), (7, "Data Analyst", "Boston, MA"),
                           (6, "Data Analyst", "Austin, TX"))}
    for name in full:  # company_name in the payload is Acme Robotics; the directory names win
        for job in full[name]["jobs"]:
            job["company_name"] = "Zeta Co" if name == "zeta-co" else "Alpha Co"
    env.scan(Web(full))
    env.scan(Web({"zeta-co": gh(), "alpha-co": gh()}), day=DAY2)
    closed = env.meta("2026-11-18")["closed_roles"]
    keys = [(r["company"], r["title"], r["role_id"]) for r in closed]
    assert len(keys) == 4 and keys == sorted(keys)
    assert keys[0][0] == "Alpha Co" and keys[-1][0] == "Zeta Co"


# ------------------------------------------------------------------- parallel and repeatable


def three_hosts() -> tuple[list[dict], dict[str, object]]:
    entries = [
        board("greenhouse", "gh-one"), board("lever", "lv-one"), board("ashby", "as-one"),
        board("greenhouse", "gh-two"), board("lever", "lv-two"), board("ashby", "as-two"),
        board("greenhouse", "gh-three"),
    ]
    routes: dict[str, object] = {
        "gh-one": gh((1, "Data Analyst", "Austin, TX")),
        "gh-two": gh((2, "Data Analyst", "Boston, MA")),
        "gh-three": gh((3, "Data Analyst", "Denver, CO")),
        "lv-one": lever_list(("a", "Data Analyst", "Remote")),
        "lv-two": lever_list(("b", "Data Analyst", "Paris")),
        "as-one": ashby_jobs(("c", "Data Analyst", "Remote - US")),
        "as-two": ashby_jobs(("d", "Data Analyst", "Remote - UK")),
    }
    return entries, routes


def test_hosts_run_on_parallel_threads_and_each_host_keeps_directory_order(make_env):
    entries, routes = three_hosts()
    env = make_env(entries)
    web = Web(routes, seed=1, meet=3)  # three hosts must be in flight together
    env.scan(web)
    by_host: dict[str, list[str]] = {}
    for host, slug, _thread in web.calls:
        by_host.setdefault(host, []).append(slug)
    assert by_host == {
        "boards-api.greenhouse.io": ["gh-one", "gh-two", "gh-three"],
        "api.lever.co": ["lv-one", "lv-two"],
        "api.ashbyhq.com": ["as-one", "as-two"],
    }


def test_shuffled_thread_completion_writes_byte_identical_files(tmp_path, monkeypatch, capsys):
    entries, routes = three_hosts()
    outputs = []
    for seed in (3, 11):
        sub = tmp_path / f"run{seed}"
        sub.mkdir()
        env = Env(sub, monkeypatch, capsys, entries)
        env.scan(Web(routes, seed=seed))
        outputs.append((env.read("scans/2026-11-17/postings.jsonl"),
                        env.read("roles/roles.jsonl")))
    assert outputs[0] == outputs[1]
    assert len(outputs[0][0].splitlines()) == 7


def test_fetch_boards_returns_results_in_input_order(make_env):
    entries, routes = three_hosts()
    env = make_env(entries)
    loaded = scan.boards_for(env.folder)
    fetched = scan.fetch_boards(env.folder, loaded, refresh=True, transport=Web(routes, seed=5))
    assert [f.entry.slug for f in fetched] == [e["slug"] for e in entries]


def test_a_second_scan_inside_the_cache_window_is_silent_and_identical(make_env):
    entries, routes = three_hosts()
    env = make_env(entries)
    web = Web(routes, seed=2)
    env.scan(web, refresh=False)
    env.cli("rank")
    first = {rel: env.read(rel) for rel in (
        "scans/2026-11-17/postings.jsonl", "roles/roles.jsonl",
        "ranked/ranked-2026-11-17.md", "ranked/ranked-2026-11-17.json")}
    calls_after_first = len(web.calls)
    env.scan(web, refresh=False)
    env.cli("rank")
    assert len(web.calls) == calls_after_first
    assert {rel: env.read(rel) for rel in first} == first


def test_the_same_day_rerun_keeps_the_closed_and_reopened_lists(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    env.scan(two_roles())
    one_role = Web({"acme-robotics": gh((1, "Data Analyst", "Austin, TX"))})
    env.scan(one_role, day=DAY2)
    first = env.meta("2026-11-18")
    env.scan(one_role, day=DAY2, refresh=False)
    again = env.meta("2026-11-18")
    assert again["closed_roles"] == first["closed_roles"] != []
    assert env.roles()[ACME + "2"].closed_on == "2026-11-18"


# ---------------------------------------------------------------------------- the command


def test_scan_result_counts_and_next_step(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    env.monkeypatch.setattr(fetch, "_default_transport", Web({"acme-robotics": gh(
        (101, "Data Analyst", "Austin, TX"), (102, "Data Analyst", "Austin, TX"),
        (103, "Technical Writer", "Austin, TX"))}))
    result = env.cli("scan")
    counts = result["counts"]
    assert counts["boards_ok"] == 1 and counts["boards_failed"] == 0
    assert counts["boards_not_found"] == 0
    assert (counts["fetched"], counts["kept"], counts["duplicates_merged"]) == (3, 1, 1)
    assert counts["new_roles"] == 1
    assert counts["closed_roles"] == 0 and counts["reopened_roles"] == 0
    assert counts["from_cache"] == 0
    assert "rank" in result["next"]
    env.now = DAY1
    again = env.cli("scan")
    assert again["counts"]["from_cache"] == 1


def test_scan_refresh_adds_the_courtesy_line(make_env):
    env = make_env([board("greenhouse", "acme-robotics")])
    env.monkeypatch.setattr(fetch, "_default_transport", Web({"acme-robotics": gh(
        (101, "Data Analyst", "Austin, TX"))}))
    result = env.cli("scan", "--refresh")
    assert any("Refresh skips the cache" in w for w in result["warnings"])
