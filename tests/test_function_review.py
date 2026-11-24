"""function-review: the worklist of A and B roles and the reject-only verdict file.

Purpose: prove that an agent can hide a role whose description shows another function, and can do
nothing else: no verdict raises a role, sets a score or tier, or changes a role it does not hide.
Logic: every test drives cli.main the way a user does, on the scan_env fixture in tests/conftest.py (one Greenhouse
board, frozen clock 2026-11-17, scripted transport, no socket). The verdict file is written by the
test, as the agent would, and function-review and rank read it back.
Guardrails: fixtures are fictional (Acme Robotics, example.com). Each test builds its own data
folder under tmp_path.
QA checklist: a new verdict rule gets a case in the bad-file matrix; a changed worklist field
needs docs/data-files.md updated in the same commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import TRACER, run_cli

ROLE_1 = "greenhouse:acme-robotics:7000001"  # tier A, score 70 in the tracer fixture
ROLE_2 = "greenhouse:acme-robotics:7000002"  # tier B, score 57
ROLE_3 = "greenhouse:acme-robotics:7000003"  # tier C, score 43
RANKED = ("ranked-2026-11-17.md", "ranked-2026-11-17.json", "ranked-2026-11-17.csv")


@pytest.fixture
def env(scan_env):
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    data["one_per_company"] = {"enabled": False, "keep": "best"}
    (scan_env.folder / "config" / "criteria.json").write_text(json.dumps(data), encoding="utf-8")
    return scan_env


def ranked_json(env) -> dict:
    return json.loads((env.folder / "ranked" / "ranked-2026-11-17.json").read_text(
        encoding="utf-8"))


def ranked_bytes(env) -> list[bytes]:
    return [(env.folder / "ranked" / name).read_bytes() for name in RANKED]


def worklist_path(env) -> Path:
    return env.folder / "roles" / "function-worklist.json"


def worklist(env) -> dict:
    return json.loads(worklist_path(env).read_text(encoding="utf-8"))


def write_verdicts(env, verdicts: list[dict]) -> None:
    path = env.folder / "roles" / "function-verdicts.json"
    path.write_text(json.dumps({"schema_version": 1, "verdicts": verdicts}), encoding="utf-8")


def scanned(env) -> None:
    run_cli(env, "scan")


def test_function_review_lists_a_and_b_roles_without_a_verdict(env):
    scanned(env)
    run_cli(env, "rank")
    shown = sorted(
        (r for r in ranked_json(env)["roles"] if r["tier"] in ("A", "B")),
        key=lambda r: (r["tier"], -r["score"], r["role_id"]))
    assert [r["role_id"] for r in shown] == [ROLE_1, ROLE_2]  # the fixture has one A and one B

    out = run_cli(env, "function-review")
    assert out["counts"] == {"to_review": 2, "hidden": 0, "kept": 0}
    assert out["wrote"] == ["roles/function-worklist.json"]
    data = worklist(env)
    assert [r["role_id"] for r in data["roles"]] == [r["role_id"] for r in shown]
    first = data["roles"][0]
    assert set(first) == {"role_id", "company", "title", "team", "family", "tier", "score",
                          "description"}
    assert first["tier"] == "A" and first["score"] == 70 and first["company"] == "Acme Robotics"
    assert all(r["description"] is None or len(r["description"]) <= 3000
               for r in data["roles"])
    assert "Read roles/function-worklist.json" in out["next"]


def test_a_reject_verdict_hides_the_role_at_rank_and_is_counted(env):
    scanned(env)
    reason = "Hardware engineering role, not operations."
    write_verdicts(env, [{"role_id": ROLE_1, "verdict": "reject", "reason": reason}])

    out = run_cli(env, "function-review")
    assert out["counts"] == {"to_review": 1, "hidden": 1, "kept": 0}
    assert [r["role_id"] for r in worklist(env)["roles"]] == [ROLE_2]

    run_cli(env, "rank")
    data = ranked_json(env)
    rejected_file = env.folder / "ranked" / "rejected-2026-11-17.json"
    rejected = [r for r in json.loads(rejected_file.read_text(encoding="utf-8"))["rejected"]
                if r["role_id"] == ROLE_1]
    assert len(rejected) == 1
    assert rejected[0]["filter_id"] == "function_review"
    assert rejected[0]["reason"] == f"Read from the description: {reason}"
    assert ROLE_1 not in [r["role_id"] for r in data["roles"]]
    assert data["funnel"]["rejected_by_filters"] == 1
    assert {"filter_id": "function_review", "label": "Other function, read from the description",
            "count": 1} in data["rejections"]
    markdown = (env.folder / "ranked" / "ranked-2026-11-17.md").read_text(encoding="utf-8")
    assert "- Other function, read from the description: 1" in markdown


def test_a_keep_verdict_changes_nothing_but_ends_the_listing(env):
    scanned(env)
    run_cli(env, "rank")
    before = ranked_bytes(env)

    write_verdicts(env, [{"role_id": ROLE_1, "verdict": "keep", "reason": "Plain operations fit."}])
    out = run_cli(env, "function-review")
    assert out["counts"] == {"to_review": 1, "hidden": 0, "kept": 1}
    assert [r["role_id"] for r in worklist(env)["roles"]] == [ROLE_2]

    run_cli(env, "rank")
    assert ranked_bytes(env) == before


def test_no_verdict_raises_or_changes_another_role(env):
    scanned(env)
    run_cli(env, "rank")
    plain = {r["role_id"]: r for r in ranked_json(env)["roles"]}

    write_verdicts(env, [
        {"role_id": ROLE_1, "verdict": "reject", "reason": "Different function."},
        {"role_id": ROLE_2, "verdict": "keep", "reason": "Fits the target work."},
    ])
    run_cli(env, "rank")
    after = {r["role_id"]: r for r in ranked_json(env)["roles"]}
    assert ROLE_1 not in after and set(after) == set(plain) - {ROLE_1}
    for role_id, role in after.items():
        for key in ("score", "tier", "tier_cap", "cap_reasons", "reason_text"):
            assert role[key] == plain[role_id][key], (role_id, key)


# The verdict rule matrix ------------------------------------------------------

def _entry(**over: object) -> dict:
    entry: dict = {"role_id": ROLE_1, "verdict": "reject", "reason": "Different function."}
    entry.update(over)
    return entry


def _without(key: str) -> dict:
    entry = _entry()
    del entry[key]
    return entry


BAD_FILES = {
    "not-an-object": ["just a string"],
    "a-list-at-the-top": [[]],
    "no-verdicts-list": {"schema_version": 1},
    "verdicts-not-a-list": {"schema_version": 1, "verdicts": {}},
    "schema-version-missing": {"verdicts": []},
    "stray-top-level-key": {"schema_version": 1, "verdicts": [], "tier": "A"},
    "entry-not-an-object": {"schema_version": 1, "verdicts": ["reject"]},
    "stray-tier": {"schema_version": 1, "verdicts": [_entry(tier="A")]},
    "stray-score": {"schema_version": 1, "verdicts": [_entry(score=99)]},
    "stray-rank": {"schema_version": 1, "verdicts": [_entry(rank=1)]},
    "stray-note": {"schema_version": 1, "verdicts": [_entry(note="why")]},
    "unknown-verdict": {"schema_version": 1, "verdicts": [_entry(verdict="promote")]},
    "reason-missing": {"schema_version": 1, "verdicts": [_without("reason")]},
    "reason-empty": {"schema_version": 1, "verdicts": [_entry(reason="   ")]},
    "reason-too-long": {"schema_version": 1, "verdicts": [_entry(reason="x" * 201)]},
    "reason-newline": {"schema_version": 1, "verdicts": [_entry(reason="one\ntwo")]},
    "reason-zero-width": {"schema_version": 1, "verdicts": [_entry(reason="one​two")]},
    "reason-bidi": {"schema_version": 1, "verdicts": [_entry(reason="one‮two")]},
    "role-id-missing": {"schema_version": 1, "verdicts": [_without("role_id")]},
    "role-id-empty": {"schema_version": 1, "verdicts": [_entry(role_id="")]},
    "role-id-repeated": {"schema_version": 1, "verdicts": [_entry(), _entry(verdict="keep")]},
    "too-many-entries": {"schema_version": 1, "verdicts": [
        {"role_id": f"x:y:{i}", "verdict": "keep", "reason": "Fine."} for i in range(5001)]},
}


@pytest.mark.parametrize("bad", BAD_FILES.values(), ids=BAD_FILES.keys())
def test_bad_verdict_files_are_refused_with_the_path(env, bad):
    scanned(env)
    run_cli(env, "rank")
    run_cli(env, "function-review")
    ranked_before, worklist_before = ranked_bytes(env), worklist_path(env).read_bytes()

    (env.folder / "roles" / "function-verdicts.json").write_text(json.dumps(bad), encoding="utf-8")
    review = run_cli(env, "function-review", ok=False)
    assert review["data"]["errors"] and review["counts"]["errors"] >= 1
    assert "function-verdicts.json" in review["error"] and "function-review" in review["next"]
    assert worklist_path(env).read_bytes() == worklist_before

    for command in ("rank", "criteria-test"):
        refused = run_cli(env, command, ok=False)
        assert "function-review" in refused["error"], command
    assert ranked_bytes(env) == ranked_before  # a refused file hides no role


@pytest.mark.parametrize("key, text", [
    ("stray-tier", "verdicts[0].tier: a verdict can only hide a role; it cannot set a tier, "
                   "score or rank."),
    ("stray-score", "verdicts[0].score: a verdict can only hide a role"),
    ("stray-rank", "verdicts[0].rank: a verdict can only hide a role"),
    ("reason-too-long", "verdicts[0].reason: is 201 characters; the limit is 200."),
    ("role-id-repeated", "verdicts[1].role_id: repeats the role_id of verdicts[0]."),
])
def test_the_refusal_names_the_path_and_the_rule(key, text):
    from jobsearch import verdicts
    assert any(problem.startswith(text) for problem in verdicts.validate_verdicts(BAD_FILES[key]))


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_a_unicode_line_or_paragraph_separator_in_a_reason_is_a_line_break(separator):
    from jobsearch import verdicts
    bad = {"schema_version": 1, "verdicts": [_entry(reason=f"one{separator}two")]}
    assert any(p.startswith("verdicts[0].reason: must be one line")
               for p in verdicts.validate_verdicts(bad))


def test_a_malformed_json_file_is_refused_too(env):
    scanned(env)
    (env.folder / "roles" / "function-verdicts.json").write_text("{not json", encoding="utf-8")
    assert "not readable JSON" in run_cli(env, "function-review", ok=False)["error"]
    assert "function-review" in run_cli(env, "rank", ok=False)["error"]


def test_a_verdict_for_a_role_not_in_the_scan_is_ignored(env):
    scanned(env)
    run_cli(env, "rank")
    before = ranked_bytes(env)
    write_verdicts(env, [{"role_id": "greenhouse:acme-robotics:9999999", "verdict": "reject",
                          "reason": "A role that is not open now."}])
    out = run_cli(env, "function-review")
    assert out["counts"] == {"to_review": 2, "hidden": 0, "kept": 0}
    run_cli(env, "rank")
    assert ranked_bytes(env) == before


def test_criteria_test_and_rank_apply_the_same_verdicts(env):
    scanned(env)
    write_verdicts(env, [{"role_id": ROLE_1, "verdict": "reject", "reason": "Other function."}])
    ranked = run_cli(env, "rank")["counts"]
    funnel = ranked_json(env)["funnel"]
    assert ranked["rejected"] == funnel["rejected_by_filters"] == 1
    preview = run_cli(env, "criteria-test")["counts"]
    assert preview == {"scanned": ranked["scanned"], "passed": ranked["passed"], "rejected": 1}
    assert ROLE_1 not in [r["role_id"] for r in ranked_json(env)["roles"]]


def test_a_reviewed_role_is_never_listed_again_and_a_c_role_never_listed(env):
    scanned(env)
    write_verdicts(env, [{"role_id": ROLE_2, "verdict": "keep", "reason": "Plain fit."}])
    run_cli(env, "function-review")
    listed = [r["role_id"] for r in worklist(env)["roles"]]
    assert listed == [ROLE_1]
    assert ROLE_2 not in listed and ROLE_3 not in listed  # reviewed, and a C role
    write_verdicts(env, [{"role_id": ROLE_2, "verdict": "keep", "reason": "Plain fit."},
                         {"role_id": ROLE_1, "verdict": "keep", "reason": "Plain fit."}])
    out = run_cli(env, "function-review")
    assert out["counts"]["to_review"] == 0 and worklist(env)["roles"] == []
    assert out["next"] == "Run rank."


def test_the_worklist_text_is_cleaned_and_bounded(env):
    scanned(env)
    postings = env.folder / "scans" / "2026-11-17" / "postings.jsonl"
    rows = [json.loads(line) for line in postings.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["role_id"] == ROLE_1:
            row["description_text"] = (
                "Build\x07 things — now​.\n\n\n   Then   ship.‮ " + "word " * 3000)
            row["title"] = row["title"] + "\x00"
        elif row["role_id"] == ROLE_2:
            row["description_text"] = "  \n\t "
    postings.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
    run_cli(env, "function-review")
    items = {r["role_id"]: r for r in worklist(env)["roles"]}
    text = items[ROLE_1]["description"]
    assert text.startswith("Build things - now. Then ship. word word")
    assert len(text) <= 3000 and "\n" not in text and "  " not in text
    assert not any(ch in text for ch in "\x07​‮—")
    assert "\x00" not in items[ROLE_1]["title"]
    assert items[ROLE_2]["description"] is None


def test_ranking_twice_with_verdicts_is_byte_identical(env):
    scanned(env)
    write_verdicts(env, [
        {"role_id": ROLE_1, "verdict": "reject", "reason": "Hardware role."},
        {"role_id": ROLE_2, "verdict": "keep", "reason": "Operations role."},
    ])
    run_cli(env, "rank")
    first = ranked_bytes(env)
    run_cli(env, "function-review")
    worklist_first = worklist_path(env).read_bytes()
    run_cli(env, "rank")
    run_cli(env, "function-review")
    assert ranked_bytes(env) == first and worklist_path(env).read_bytes() == worklist_first
