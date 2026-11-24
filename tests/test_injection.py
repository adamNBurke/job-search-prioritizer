"""Posting text that gives orders changes nothing.

Purpose: a job posting is attacker-controlled text. The pipeline must treat it as data, so a
posting that tells the reader to ignore its instructions, run a command, fetch a link or write a
file causes no request, no file and no change to the user's config, and none of its sentences
reach the ranked files.
Logic: the demo persona runs the one-command pipeline over the fixture boards plus an injection
board (greenhouse_injection.json). Assertions are on the recorded requests, on the sorted file
listing of the data folder and its surroundings, on the config and profile bytes, and on the three
ranked files, never on prose about what the tool "would" do. Ordering and empty-text edges are
here as well.
Guardrails: every string is fictional and the attacker address sits under example.com. Words the
leak gate watches for are never written as literals.
QA checklist: a new output file needs its path added to ALLOWED_TOP; a new field that could carry
description text needs a sentence check here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from test_stranger_pipeline import PIPELINE, SCAN_DATE, Env, default_bodies, stage

from jobsearch.netguard import ATS_HOSTS, board_url
from jobsearch.sources.text import html_to_text

INJECTION_SLUG = "injection-example"
ALLOWED_TOP = {"jobsearch.workspace.json", ".gitignore", "cache", "profile", "config", "scans",
               "roles", "ranked"}
HOSTILE_MARKERS = ("‮", "​")


def boards_with_injection(tmp_path: Path) -> Path:
    data = json.loads((PIPELINE / "boards.json").read_text(encoding="utf-8"))
    data["entries"].append({
        "company": "Injection Example", "ats": "greenhouse", "slug": INJECTION_SLUG,
        "industry": "software", "size": "51-200", "stage": "growth",
        "last_verified": "2026-11-17", "identity": "company_name", "postings_at_verify": 3,
    })
    data["entries"].sort(key=lambda e: (e["company"].casefold(), e["ats"]))
    target = tmp_path / "boards-with-injection.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


def injection_jobs() -> dict:
    return json.loads((PIPELINE / "http" / "greenhouse_injection.json").read_text(encoding="utf-8"))


def hostile_job(jobs: dict) -> dict:
    return next(j for j in jobs["jobs"] if any(m in j["title"] for m in HOSTILE_MARKERS))


def bodies_for(jobs: dict) -> dict[str, bytes]:
    bodies = default_bodies()
    bodies[board_url("greenhouse", INJECTION_SLUG)] = json.dumps(jobs).encode("utf-8")
    return bodies


def hostile_sentences(jobs: dict) -> list[str]:
    text = html_to_text(hostile_job(jobs)["content"], unescape_first=True)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]
    assert len(sentences) >= 4
    return sentences


def files_under(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def injected_env(tmp_path, monkeypatch, capsys, jobs: dict | None = None, name: str = "ws") -> Env:
    env = stage(tmp_path, monkeypatch, capsys, "casey-ops",
                boards=boards_with_injection(tmp_path),
                bodies=bodies_for(jobs if jobs is not None else injection_jobs()), name=name)
    env.cli("criteria-build")
    config = env.folder / "config" / "criteria.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["one_per_company"] = {"enabled": False, "keep": "best"}  # show every passing role
    config.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return env


@pytest.fixture
def injected(tmp_path, monkeypatch, capsys) -> Env:
    before = set(os.listdir(Path.cwd()))
    env = injected_env(tmp_path, monkeypatch, capsys)
    env.before_cwd = before
    env.profile_before = {p: (env.folder / p).read_bytes()
                          for p in ("profile/answers.json", "profile/resume.md")}
    env.criteria_before = json.loads(
        (env.folder / "config" / "criteria.json").read_text(encoding="utf-8"))
    env.out = env.rank_from_answers()
    return env


def test_injection_posting_causes_no_extra_request(injected):
    expected = sorted(board_url(ats, slug) for ats, slug in (
        ("greenhouse", "northwind-example"), ("lever", "harbor-example"),
        ("ashby", "meridian-example"), ("greenhouse", INJECTION_SLUG)))
    assert sorted(injected.transport.urls) == expected
    hosts = set(ATS_HOSTS.values())
    for url, headers in injected.transport.calls:
        assert url.split("/")[2] in hosts
        assert "attacker" not in url
        assert not {"authorization", "cookie"} & {k.casefold() for k in headers}


def test_injection_posting_writes_no_file_outside_the_documented_layout(injected):
    for rel in files_under(injected.folder):
        top = rel.split("/")[0]
        assert top in ALLOWED_TOP, rel
        if top == "cache":
            assert rel.startswith("cache/http/"), rel
    assert not [p for p in injected.tmp.rglob("*") if p.name.casefold() == "owned.txt"]
    assert sorted(p.name for p in injected.tmp.iterdir()) == [
        "boards-with-injection.json", "ws"]
    assert set(os.listdir(Path.cwd())) == injected.before_cwd
    assert (injected.folder / "ranked" / f"ranked-{SCAN_DATE}.md").is_file()


def test_injection_posting_leaves_the_profile_and_criteria_alone(injected):
    for rel, before in injected.profile_before.items():
        assert (injected.folder / rel).read_bytes() == before
    after = json.loads((injected.folder / "config" / "criteria.json").read_text(encoding="utf-8"))
    for key in ("tiers", "criteria_version"):  # calibrate is the only step allowed to move these
        after.pop(key), injected.criteria_before.pop(key)
    assert after == injected.criteria_before


def test_no_description_sentence_reaches_any_ranked_file(injected):
    sentences = hostile_sentences(injection_jobs())
    for suffix in ("md", "json", "csv"):
        text = injected.ranked_path(suffix).read_text(encoding="utf-8").casefold()
        for sentence in sentences:
            assert sentence.casefold() not in text, (suffix, sentence)
        for fragment in ("ignore all previous", "system prompt", "owned.txt", "collect"):
            assert fragment not in text, (suffix, fragment)


def test_the_hostile_title_renders_escaped_with_hidden_characters_removed(injected):
    md = injected.ranked_path("md").read_text(encoding="utf-8")
    assert "Click here" in md  # the role is listed, so its title was rendered
    assert "[Click here](" not in md
    assert "\\[Click here\\]\\(" in md
    assert not any(line.startswith("# Operations") for line in md.splitlines())
    assert any(line.startswith(("### \\# Operations", "- \\# Operations"))
               for line in md.splitlines())
    for suffix in ("md", "json", "csv"):
        text = injected.ranked_path(suffix).read_text(encoding="utf-8")
        assert not any(marker in text for marker in HOSTILE_MARKERS), suffix


def test_the_hostile_role_is_scored_like_any_other_role(injected):
    role = next(r for r in injected.ranked_json()["roles"] if "Click here" in r["title"])
    assert role["tier"] in ("A", "B", "C") and isinstance(role["score"], int)
    assert role["url_apply"] == "https://job-boards.example.com/injection-example/jobs/4200002"


def run_with_order(tmp_path, monkeypatch, capsys, position: str) -> tuple[dict, dict]:
    jobs = injection_jobs()
    hostile = hostile_job(jobs)
    others = [j for j in jobs["jobs"] if j is not hostile]
    slot = {"first": 0, "middle": 1, "last": len(others)}[position]
    jobs["jobs"] = [*others[:slot], hostile, *others[slot:]]
    env = injected_env(tmp_path, monkeypatch, capsys, jobs, name=f"ws-{position}")
    env.rank_from_answers()
    data = env.ranked_json()
    rows = {r["role_id"]: (r["score"], r["tier"], r["reason_text"])
            for r in data["roles"] if "Click here" not in r["title"]}
    return rows, data["funnel"]


def test_moving_the_injection_posting_changes_no_other_role(tmp_path, monkeypatch, capsys):
    results = {pos: run_with_order(tmp_path, monkeypatch, capsys, pos)
               for pos in ("first", "middle", "last")}
    rows = [rows for rows, _ in results.values()]
    assert len(rows[0]) >= 5
    assert rows[0] == rows[1] == rows[2]
    assert results["first"][1] == results["middle"][1] == results["last"][1]


def test_a_hostile_posting_with_an_empty_description_still_passes_through(tmp_path, monkeypatch, capsys):
    jobs = injection_jobs()
    hostile_job(jobs)["content"] = ""
    env = injected_env(tmp_path, monkeypatch, capsys, jobs)
    env.rank_from_answers()
    role = next(r for r in env.ranked_json()["roles"] if "Click here" in r["title"])
    assert "Your skills in the description" in role["gaps"]
