"""Tracer: scan a directory, rank the scan, read a reason-first file.

Purpose: prove the whole path from a criteria file to a ranked file once, on the real modules,
so the parallel plans that extend them start from a spine that works.
Logic: every test drives cli.main the way a user does. directory.DIRECTORY_PATH points at a
one-entry fixture directory, fetch._default_transport is replaced by a scripted transport that
serves tests/fixtures/ats/greenhouse_list.json, and clock.now_utc is frozen. No socket is
opened. The live proof (real Greenhouse boards) is the command sequence in the plan's verify
step, not a test, because the suite never touches the network.
Guardrails: fixtures hold fictional data only (Acme Robotics, example.com). Each test builds
its own data folder under tmp_path, so tests never share state.
QA checklist: a new behavior of scan, rank, the command registry, the filter contract or the
scorer gets a case here until its own plan moves it to a dedicated test file.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys

import pytest
from helpers import LIST_FIXTURE, TODAY, TRACER, board, run_cli

from jobsearch import cli, commands, criteria, directory, fetch, families, filters
from jobsearch import roles_store, scoring
from jobsearch.errors import UserError
from jobsearch.sources import parse_board
from jobsearch.sources.text import html_to_text


@pytest.fixture
def env(scan_env):
    return scan_env


def set_criteria(env, mutate) -> None:
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    mutate(data)
    (env.folder / "config" / "criteria.json").write_text(json.dumps(data), encoding="utf-8")


def ranked_json(env) -> dict:
    return json.loads((env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))


def test_scan_then_rank_writes_files_with_reason_before_apply(env):
    set_criteria(env, lambda d: d.update(one_per_company={"enabled": False, "keep": "best"}))
    scan = run_cli(env, "scan")
    assert scan["ok"] and scan["counts"]["kept"] == 3
    d = env.folder / "scans" / "2026-11-17"
    assert (d / "postings.jsonl").is_file() and (d / "scan-meta.json").is_file()
    assert (env.folder / "roles" / "roles.jsonl").is_file()

    ranked = run_cli(env, "rank")
    # The funnel file is written beside the three ranked files and listed with them;
    # So is the rejected list, and the invite stamp.
    assert sorted(ranked["wrote"]) == [
        "config/notices.json", "ranked/funnel-2026-11-17.md", "ranked/ranked-2026-11-17.csv",
        "ranked/ranked-2026-11-17.json", "ranked/ranked-2026-11-17.md",
        "ranked/rejected-2026-11-17.json"]
    text = (env.folder / "ranked" / "ranked-2026-11-17.md").read_text(encoding="utf-8")
    blocks = text.split("### ")[1:]
    tiers = [r["tier"] for r in ranked_json(env)["roles"]]
    assert len(tiers) == 3 and len(blocks) == tiers.count("A") + tiers.count("B")
    assert blocks
    for block in blocks:
        lines = block.splitlines()
        apply_at = next(i for i, line in enumerate(lines) if line.startswith("Apply: <https://"))
        assert lines[2] and not lines[2].startswith(("Tier", "Apply"))  # reason line is first
        assert 2 < apply_at
    assert "Tier A, score" in text or "Tier B, score" in text or "Tier C, score" in text
    assert "—" not in text


def test_title_gate_drops_off_family_titles_and_funnel_balances(env):
    run_cli(env, "scan")
    d = env.folder / "scans" / "2026-11-17"
    lines = (d / "postings.jsonl").read_text(encoding="utf-8").splitlines()
    titles = [json.loads(line)["title"] for line in lines]
    assert "Technical Writer" not in titles and len(titles) == 3
    totals = json.loads((d / "scan-meta.json").read_text(encoding="utf-8"))["totals"]
    assert totals["title_gate_dropped"] == 1
    assert totals["fetched"] == (totals["title_gate_dropped"] + totals["duplicates_merged"]
                                 + totals["kept"])
    rank_input = roles_store.load_rankable(env.folder)
    assert rank_input.fetched_total == (rank_input.title_gate_dropped
                                        + rank_input.duplicates_merged + len(rank_input.facts))
    assert rank_input.pasted_total == 0


def test_missing_posted_date_is_a_gap_not_a_zero(env, monkeypatch):
    data = json.loads(LIST_FIXTURE.read_text(encoding="utf-8"))
    del data["jobs"][0]["first_published"]
    env.transport.body = json.dumps(data).encode("utf-8")
    run_cli(env, "scan")
    run_cli(env, "rank")
    role = next(r for r in ranked_json(env)["roles"] if r["role_id"].endswith(":7000001"))
    assert role["gaps"] == ["Posting age"]
    assert role["score"] == 100  # only the title criterion was evaluable
    assert role["coverage"] == 70 and role["provisional"] is False
    # Was ending in "Could not check Posting age."; the fact is said once, in place.
    assert "Posted: not stated." in role["reason_text"]
    assert "Could not check" not in role["reason_text"]


def test_second_scan_and_rank_are_cached_and_byte_identical(env):
    run_cli(env, "scan")
    run_cli(env, "rank")
    first_calls = len(env.transport.calls)
    paths = [env.folder / "ranked" / "ranked-2026-11-17.md",
             env.folder / "ranked" / "ranked-2026-11-17.json"]
    before = [p.read_bytes() for p in paths]
    run_cli(env, "scan")
    run_cli(env, "rank")
    assert first_calls == 1 and len(env.transport.calls) == 1
    assert [p.read_bytes() for p in paths] == before


def _with_work_mode_filter(policy: str):
    def mutate(data):
        data["hard_filters"] = [{
            "id": "work_mode", "type": "enum_allow", "field": "work_mode", "allow": ["remote"],
            "unknown": policy, "label": "Work arrangement",
        }]
    return mutate


def test_unknown_policy_gap_passes_and_lists_the_gap(env):
    run_cli(env, "scan")
    run_cli(env, "rank")
    plain = {r["role_id"]: r["score"] for r in ranked_json(env)["roles"]}
    set_criteria(env, _with_work_mode_filter("gap"))
    run_cli(env, "rank")
    data = ranked_json(env)
    rejected = json.loads((env.folder / "ranked" / "rejected-2026-11-17.json").read_text(
        encoding="utf-8"))["rejected"]
    assert rejected == []
    by_id = {r["role_id"]: r for r in data["roles"]}
    assert {k: v["score"] for k, v in by_id.items()} == plain  # score unchanged
    remote = by_id["greenhouse:acme-robotics:7000001"]
    assert "Work arrangement" not in remote["gaps"]  # work_mode is "remote" there
    other = by_id["greenhouse:acme-robotics:7000002"]
    assert "Work arrangement" in other["gaps"]


def test_unknown_policy_reject_moves_role_to_rejected_list(env):
    run_cli(env, "scan")
    set_criteria(env, _with_work_mode_filter("reject"))
    run_cli(env, "rank")
    data = ranked_json(env)
    assert [r["role_id"] for r in data["roles"]] == ["greenhouse:acme-robotics:7000001"]
    rejected = json.loads((env.folder / "ranked" / "rejected-2026-11-17.json").read_text(
        encoding="utf-8"))["rejected"]
    assert len(rejected) == 2
    assert all("is not stated in the posting" in r["reason"] for r in rejected)
    assert all(r["filter_id"] == "work_mode" for r in rejected)


def test_unregistered_filter_type_is_a_plain_error(env, monkeypatch):
    monkeypatch.delitem(filters.FILTER_TYPES, "min_number")

    def mutate(data):
        data["hard_filters"] = [{"id": "pay", "type": "min_number", "field": "comp_max",
                                 "value": 1, "unknown": "gap"}]
    run_cli(env, "scan")
    set_criteria(env, mutate)
    out = cli.main(["rank", "--data-dir", str(env.folder)])
    result = json.loads(env.capsys.readouterr().out)
    assert out == 1 and "cannot run yet" in result["error"]


def test_rank_without_a_scan_says_run_scan_first(env):
    code = cli.main(["rank", "--data-dir", str(env.folder)])
    assert code == 1
    assert "Run scan first" in json.loads(env.capsys.readouterr().out)["error"]


def test_scan_without_criteria_says_run_the_interview(env):
    (env.folder / "config" / "criteria.json").unlink()
    code = cli.main(["scan", "--data-dir", str(env.folder)])
    assert code == 1
    assert "Run the interview first" in json.loads(env.capsys.readouterr().out)["error"]
    assert env.transport.calls == []


def test_scan_with_every_board_unreachable_writes_nothing(env, monkeypatch):
    def down(url, headers, clock=None):
        raise fetch.TransportError("down")
    monkeypatch.setattr(fetch, "_default_transport", down)
    code = cli.main(["scan", "--data-dir", str(env.folder)])
    assert code == 1
    assert not (env.folder / "scans").exists()


# Command registry --------------------------------------------------------------------------

def test_registry_finds_scan_and_rank_and_cli_uses_it():
    names = [m.NAME for m in commands.discover()]
    assert "scan" in names and "rank" in names
    assert {"init", "status"} <= set(cli._HANDLERS)
    assert not {"probe", "run"} & set(cli._HANDLERS)
    assert set(names) <= set(cli._HANDLERS)


def test_registry_refuses_reserved_and_duplicate_names(tmp_path, monkeypatch):
    package = tmp_path / "fakecmds"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    body = "NAME = {name!r}\nHELP = 'x'\ndef add_arguments(parser): pass\ndef run(args): pass\n"
    (package / "a.py").write_text(body.format(name="status"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    fake = importlib.import_module("fakecmds")
    monkeypatch.setattr(commands, "__path__", fake.__path__)
    monkeypatch.setattr(commands, "__name__", "fakecmds")
    with pytest.raises(RuntimeError, match="reserved"):
        commands.discover()
    (package / "a.py").write_text(body.format(name="same"), encoding="utf-8")
    (package / "b.py").write_text(body.format(name="same"), encoding="utf-8")
    importlib.invalidate_caches()
    for name in ("fakecmds.a", "fakecmds.b"):
        sys.modules.pop(name, None)
    with pytest.raises(RuntimeError, match="used by both"):
        commands.discover()


# Spine units -------------------------------------------------------------------------------

def test_directory_validation_names_the_bad_entry(tmp_path):
    bad = dict(board(), industry="made-up")
    path = tmp_path / "boards.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": [board(), bad]}), encoding="utf-8")
    with pytest.raises(UserError, match="entry 1"):
        directory.load_directory(path)


def test_shipped_directory_is_valid_sorted_and_selectable():
    entries = directory.load_directory()
    keys = [(e.company.casefold(), e.ats) for e in entries]
    assert keys == sorted(keys)
    software = sum(1 for e in entries if e.industry == "software")
    assert 0 < software < len(entries)
    assert directory.count_matching(entries, [], []) == len(entries)
    assert directory.count_matching(entries, ["software"], []) == software
    assert directory.count_matching(entries, [], [], ["software"]) == len(entries) - software


def test_title_normalization_and_matching():
    assert families.normalize_title("Senior Robotics Engineer (Remote) - US") == "robotics engineer"
    assert families.normalize_title("Head of Operations") == "operations"
    compiled = families.compile_families(json.loads(TRACER.read_text(encoding="utf-8")))
    assert families.match_family("Sr. Robotics-Engineer II", compiled).family_id == "tf-robotics"
    assert families.match_family("Robotics Engineering Manager", compiled) is None
    assert families.excluded_term("Technical Recruiter", compiled) == "recruiter"


def test_long_or_broken_regex_is_refused():
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    data["title"]["families"][0]["regex"] = ["(" + "a" * 250 + ")"]
    assert any("longer than 200" in e for e in criteria.validate_criteria(data))
    with pytest.raises(UserError):
        families.compile_families(data)
    data["title"]["families"][0]["regex"] = ["(unclosed"]
    assert any("not a valid pattern" in e for e in criteria.validate_criteria(data))


def test_html_to_text_decodes_twice_and_breaks_blocks():
    raw = "&lt;p&gt;Research &amp;amp; development&amp;nbsp;now&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;"
    text = html_to_text(raw, unescape_first=True)
    assert [line for line in text.split("\n") if line] == [
        "Research & development now", "One", "Two"]
    assert "\n\n\n" not in text


def test_greenhouse_adapter_converts_offsets_and_flags_remote():
    entry = directory._entry(board(), 0)
    payload = json.loads(LIST_FIXTURE.read_text(encoding="utf-8"))
    postings = parse_board(entry, payload, "2026-11-17T12:00:00Z")
    first = next(p for p in postings if p.posting_id == "7000001")
    assert first.posted == "2026-07-21T17:38:40Z"
    assert first.work_mode == "remote" and first.team == "Engineering"
    assert first.role_id == "greenhouse:acme-robotics:7000001"
    assert all(p.work_mode == "unknown" for p in postings if p.posting_id == "7000002")
    with pytest.raises(fetch.FetchError):
        parse_board(entry, {"nope": 1}, "2026-11-17T12:00:00Z")


def test_criteria_validation_reports_plain_problems():
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    assert criteria.validate_criteria(data) == []
    broken = copy.deepcopy(data)
    broken["criteria"][0]["weight"] = 60
    broken["tiers"][1]["min_score"] = 80
    broken["criteria"][1]["evaluator"] = "judged_scale"
    errors = criteria.validate_criteria(broken)
    assert errors == sorted(errors)
    assert any("add up to" in e for e in errors)
    assert any("go down from A to C" in e for e in errors)
    assert any("judged_scale" in e for e in errors)


def test_scoring_rounds_half_up_and_rejects_unregistered_evaluator():
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    assert scoring._half_up(scoring.Fraction(133, 2)) == 67
    assert scoring._half_up(scoring.Fraction(100, 3)) == 33
    data["criteria"][1]["evaluator"] = "judged_scale"
    compiled = families.compile_families(data)
    postings = parse_board(directory._entry(board(), 0), json.loads(
        LIST_FIXTURE.read_text(encoding="utf-8")), "2026-11-17T12:00:00Z")
    facts = roles_store.RoleFacts(postings[0], None, None, None, "x", "x")
    with pytest.raises(UserError, match="cannot run yet"):
        scoring.score_role(facts, data, compiled, TODAY.date())
    assert "enum_allow" in filters.FILTER_TYPES


def test_ranked_file_ends_with_the_career_ops_block_and_next_steps_byte_identically(env):
    # The hand-off to career-ops and the pointer to the letter and review
    # skills close every ranked file, end to end through scan and rank on the fixtures.
    run_cli(env, "scan")
    run_cli(env, "rank")
    path = env.folder / "ranked" / "ranked-2026-11-17.md"
    first = path.read_bytes()
    text = first.decode("utf-8")
    assert text.index("\n## Funnel\n") < text.index("\n## Send to career-ops\n")
    block = text.split("\n## Send to career-ops\n")[1].split("\n## Next steps\n")[0]
    listed = [line for line in block.splitlines() if line.startswith("- [ ] https://")]
    tier_a = [r for r in ranked_json(env)["roles"] if r["tier"] == "A"]
    assert len(listed) == len(tier_a) and listed
    assert all(line.count(" | ") == 2 for line in listed)
    tail = text.split("\n## Next steps\n")[1]
    assert "/job-search-prioritizer:letter" in tail and "/job-search-prioritizer:review" in tail
    run_cli(env, "rank")
    assert path.read_bytes() == first
