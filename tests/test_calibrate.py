"""Calibration: cutoffs proposed from the user's own results, previewed, accepted and ranked.

Purpose: fixed tier cutoffs tuned to one person put a stranger's whole list in tier C, so the
cutoffs come from the shape of the user's own passing roles.
Logic: the unit tests drive calibrate.propose, counts_at and accept on plain score lists. The
integration tests drive cli.main the way a user does: scan a fixture board, criteria-test,
calibrate with the proposed values, rank. The scan transport is scripted, so no socket opens.
Guardrails: fixtures hold fictional data only (Acme Robotics, example.com). Every test builds its
own data folder under tmp_path. The frozen clock is the only source of "today".
QA checklist: a changed share or tie rule needs a unit case here; a changed file shape needs
docs/data-files.md updated in the same commit.
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction

import pytest
from helpers import TRACER, run_cli

from jobsearch import calibrate, criteria_store, output
from jobsearch.scoring import ScoredRole
from jobsearch.filters import FilterFailure


@pytest.fixture
def env(scan_env):
    return scan_env


def scored(score: int, rejected: bool = False, provisional: bool = False,
           capped: bool = False) -> ScoredRole:
    return ScoredRole(
        role_id=f"r{score}", source="greenhouse", company="Acme", title="Engineer",
        locations=[], url_apply="https://example.com/a", first_seen="2026-11-17",
        last_seen="2026-11-17", family_id="f", family_label="F",
        rejected=FilterFailure("work_mode", "Work arrangement", "no") if rejected else None,
        score=score, coverage=100, provisional=provisional,
        tier_cap="B" if capped else None,
    )


# ------------------------------------------------------------------------------ unit tests

def test_constants_are_the_named_shares():
    assert calibrate.A_SHARE == Fraction(1, 10) and calibrate.B_SHARE == Fraction(1, 4)


def test_propose_over_twenty_scores_puts_two_in_a_and_five_in_b():
    scores = list(range(100, 80, -1))  # 20 distinct, descending
    proposal = calibrate.propose(scores)
    assert proposal.a_min == scores[1] and proposal.b_min == scores[6]
    assert proposal.counts == {"A": 2, "B": 5, "C": 13}
    assert proposal.passing == 20
    assert calibrate.counts_at(scores, proposal.a_min, proposal.b_min) == proposal.counts


def test_ties_at_the_cutoff_land_in_the_same_tier_and_counts_are_real():
    scores = [95, 90, 90] + list(range(80, 63, -1))  # 20 scores, 2nd and 3rd tie
    proposal = calibrate.propose(scores)
    assert proposal.a_min == 90
    counts = calibrate.counts_at(scores, proposal.a_min, proposal.b_min)
    assert counts["A"] == 3 and sum(counts.values()) == 20
    assert proposal.counts == counts


def test_one_score_sets_b_one_lower():
    proposal = calibrate.propose([60])
    assert (proposal.a_min, proposal.b_min) == (60, 59)
    assert proposal.counts == {"A": 1, "B": 0, "C": 0}


def test_all_scores_equal_to_one_gives_two_and_one():
    proposal = calibrate.propose([1, 1, 1])
    assert (proposal.a_min, proposal.b_min) == (2, 1)
    assert proposal.counts == {"A": 0, "B": 3, "C": 0}


def test_a_b_cutoff_clamped_to_one_keeps_a_real_a_cutoff():
    # One strong role over nine zeros must not collapse the A cutoff from 50 to 2.
    proposal = calibrate.propose([50] + [0] * 9)
    assert (proposal.a_min, proposal.b_min) == (50, 1)


def test_cutoffs_stay_strictly_ordered_and_in_range_for_every_small_list():
    for n in range(1, 40):
        for flat in (0, 1, 55, 100):
            proposal = calibrate.propose([flat] * n)
            assert 100 >= proposal.a_min > proposal.b_min >= 1


def test_no_scores_gives_no_proposal():
    assert calibrate.propose([]) is None


def _twenty_with_two_capped() -> list[ScoredRole]:
    return [scored(score, capped=score >= 99) for score in range(100, 80, -1)]


def _tiers(a_min: int, b_min: int) -> dict:
    return {"tiers": [{"id": "A", "min_score": a_min}, {"id": "B", "min_score": b_min},
                      {"id": "C", "min_score": 0}]}


def _ranked_counts(roles: list[ScoredRole], a_min: int, b_min: int) -> dict[str, int]:
    shown = [output.role_tier(r, _tiers(a_min, b_min)) for r in roles if r.rejected is None]
    return {tier: shown.count(tier) for tier in ("A", "B", "C")}


def test_passing_with_caps_skips_rejected_keeps_provisional_and_orders_open_first():
    roles = [scored(40), scored(90, provisional=True), scored(70, rejected=True),
             scored(90, capped=True), scored(60, capped=True), scored(60)]
    assert calibrate.passing_with_caps(roles) == [
        (90, False), (90, True), (60, False), (60, True), (40, False)]


def test_the_a_cutoff_comes_from_open_roles_when_caps_hold_high_scorers_at_b():
    proposal = calibrate.propose(calibrate.passing_with_caps(_twenty_with_two_capped()))
    assert (proposal.a_min, proposal.b_min) == (97, 94)
    assert proposal.counts == {"A": 2, "B": 5, "C": 13}
    assert proposal.passing == 20


def test_counts_with_caps_equal_the_tiers_the_ranked_files_show():
    roles = _twenty_with_two_capped()
    pairs = calibrate.passing_with_caps(roles)
    for a_min, b_min in [(97, 94), (100, 90), (99, 81), (90, 85), (82, 81), (100, 1)]:
        assert calibrate.counts_at(pairs, a_min, b_min) == _ranked_counts(roles, a_min, b_min)


def test_a_capped_role_at_or_above_the_a_cutoff_counts_as_b():
    pairs = [(95, True), (90, False), (50, False)]
    assert calibrate.counts_at(pairs, 90, 50) == {"A": 1, "B": 2, "C": 0}


def test_every_role_capped_leaves_no_a_and_keeps_the_cutoffs_in_order():
    roles = [scored(score, capped=True) for score in range(100, 80, -1)]
    pairs = calibrate.passing_with_caps(roles)
    proposal = calibrate.propose(pairs)
    assert proposal.counts["A"] == 0
    assert 100 >= proposal.a_min > proposal.b_min >= 1
    assert proposal.counts == _ranked_counts(roles, proposal.a_min, proposal.b_min)


def test_cutoffs_stay_in_order_and_in_range_for_every_small_capped_list():
    for n in range(1, 25):
        for flat in (1, 55, 100):
            for capped in range(n + 1):
                pairs = [(flat, i < capped) for i in range(n)]
                proposal = calibrate.propose(pairs)
                assert 100 >= proposal.a_min > proposal.b_min >= 1
                assert sum(proposal.counts.values()) == n


def test_pairs_with_no_caps_propose_exactly_what_plain_scores_do():
    for scores in ([100, 90, 90, 80], list(range(100, 80, -1)), [60], [1, 1, 1], [5, 5, 4, 1]):
        plain = calibrate.propose(scores)
        assert calibrate.propose([(s, False) for s in scores]) == plain
        assert calibrate.counts_at([(s, False) for s in scores], plain.a_min, plain.b_min) \
            == calibrate.counts_at(scores, plain.a_min, plain.b_min)


def test_fewer_open_roles_than_the_a_index_never_puts_tier_a_below_tier_b():
    pairs = [(99, True), (98, True), (97, True), (96, True), (95, True), (94, True),
             (93, False)] + [(s, True) for s in range(92, 80, -1)]
    proposal = calibrate.propose(pairs)
    assert proposal.b_min == 93  # the all-roles B pick is kept, not dragged down to 92
    assert proposal.a_min == 94 and proposal.counts["A"] == 0
    assert proposal.caps_dominate is True


def _capped_high_open_low() -> list[tuple[int, bool]]:
    """The review probe: 30 capped roles scoring 66 to 95 and 40 open roles scoring 19 to 58."""
    return [(s, True) for s in range(66, 96)] + [(s, False) for s in range(19, 59)]


def test_many_capped_high_scorers_never_put_tier_a_below_the_all_roles_b_cutoff():
    pairs = _capped_high_open_low()
    plain = calibrate.propose([score for score, _ in pairs])  # every role read as open
    proposal = calibrate.propose(calibrate.passing_with_caps(
        [scored(score, capped=capped) for score, capped in pairs]))
    assert proposal.b_min == plain.b_min == 71  # the B share holds
    assert proposal.a_min > proposal.b_min
    assert proposal.counts["A"] + proposal.counts["B"] == plain.counts["A"] + plain.counts["B"]
    assert proposal.counts == calibrate.counts_at(pairs, proposal.a_min, proposal.b_min)
    assert proposal.counts["A"] == 0  # no open role reaches the B cutoff, so tier A is empty
    assert proposal.caps_dominate is True


def test_a_floor_above_the_top_score_keeps_the_cutoffs_in_range():
    pairs = [(100, True)] * 10 + [(50, False)] * 3
    proposal = calibrate.propose(pairs)
    assert 100 >= proposal.a_min > proposal.b_min >= 1
    assert proposal.caps_dominate is True


def test_caps_that_do_not_move_the_picks_do_not_raise_the_flag():
    assert calibrate.propose(calibrate.passing_with_caps(_twenty_with_two_capped())) \
        .caps_dominate is False
    for scores in ([100, 90, 90, 80], list(range(100, 80, -1)), [60], [1, 1, 1], [5, 5, 4, 1]):
        assert calibrate.propose(scores).caps_dominate is False
        assert calibrate.propose([(s, False) for s in scores]).caps_dominate is False


def test_the_caps_dominate_sentence_is_one_plain_sentence():
    text = calibrate.CAPS_DOMINATE
    assert text.endswith(".") and text.count(". ") == 0
    assert "tier A is small" in text and "\u2014" not in text


def test_criteria_test_says_so_when_caps_dominate(env, monkeypatch):
    real = calibrate.propose
    monkeypatch.setattr(calibrate, "propose",
                        lambda items: dataclasses.replace(real(items), caps_dominate=True))
    run_cli(env, "scan")
    out = run_cli(env, "criteria-test")
    assert calibrate.CAPS_DOMINATE in out["warnings"]
    preview = (env.folder / out["wrote"][0]).read_text(encoding="utf-8")
    assert calibrate.CAPS_DOMINATE in preview


def test_criteria_test_stays_quiet_when_caps_do_not_dominate(env):
    run_cli(env, "scan")
    out = run_cli(env, "criteria-test")
    assert calibrate.CAPS_DOMINATE not in out["warnings"]


def test_accept_refuses_bad_values_and_changes_nothing(env):
    criteria = criteria_store.load_criteria(env.folder)
    before = (env.folder / "config" / "criteria.json").read_bytes()
    for a_min, b_min in [(50, 50), (40, 50), (101, 50), (50, 0), (0, 0), ("70", 50), (70.5, 50)]:
        with pytest.raises(calibrate.CalibrationError):
            calibrate.accept(env.folder, criteria, a_min, b_min,
                             scan_date="2026-11-17", proposal=None)
    assert (env.folder / "config" / "criteria.json").read_bytes() == before
    assert not (env.folder / "config" / "calibration.json").exists()


# ------------------------------------------------------------------------ through cli.main

def test_preview_accept_and_rank_use_the_calibrated_cutoffs(env):
    run_cli(env, "scan")
    preview = run_cli(env, "criteria-test")
    assert preview["counts"]["passed"] == 3
    a_min, b_min = preview["data"]["proposed_a_min"], preview["data"]["proposed_b_min"]
    assert a_min > b_min >= 1
    assert sum(preview["data"]["proposed_counts"].values()) == 3
    assert "ranked/preview-2026-11-17.md" in preview["wrote"]
    text = (env.folder / "ranked" / "preview-2026-11-17.md").read_text(encoding="utf-8")
    assert "—" not in text and "Proposed" in text

    accepted = run_cli(env, "calibrate", "--a-min", str(a_min), "--b-min", str(b_min))
    assert accepted["data"]["criteria_version"] == "1.1"
    assert "Run rank" in accepted["next"]
    config = json.loads((env.folder / "config" / "criteria.json").read_text(encoding="utf-8"))
    assert config["criteria_version"] == "1.1" and config["created_from"] == "intake"
    assert [t["min_score"] for t in config["tiers"]] == [a_min, b_min, 0]
    calibration = json.loads(
        (env.folder / "config" / "calibration.json").read_text(encoding="utf-8"))
    assert calibration["accepted"] == {"a_min": a_min, "b_min": b_min}
    assert sorted(calibration) == [
        "accepted", "criteria_version", "passing_roles", "proposed", "scan_date",
        "schema_version"]
    assert calibration["passing_roles"] == 3
    assert calibration["proposed"]["counts"] == preview["data"]["proposed_counts"]
    assert calibration["criteria_version"] == "1.1" and calibration["schema_version"] == 1

    run_cli(env, "rank")
    ranked = json.loads(
        (env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))
    for role in ranked["roles"]:
        want = "A" if role["score"] >= a_min else "B" if role["score"] >= b_min else "C"
        assert role["tier"] == want


def test_second_calibration_is_a_retune(env):
    run_cli(env, "scan")
    run_cli(env, "calibrate", "--a-min", "80", "--b-min", "60")
    run_cli(env, "calibrate", "--a-min", "75", "--b-min", "55")
    config = json.loads((env.folder / "config" / "criteria.json").read_text(encoding="utf-8"))
    assert config["criteria_version"] == "1.2" and config["created_from"] == "retune"
    assert (env.folder / "config" / "criteria-history" / "criteria-v1.1.json").is_file()


def test_calibrate_refuses_bad_cutoffs_with_a_plain_error(env):
    run_cli(env, "scan")
    path = env.folder / "config" / "criteria.json"
    before = path.read_bytes()
    for argv in (["--a-min", "40", "--b-min", "50"], ["--a-min", "101", "--b-min", "50"],
                 ["--a-min", "70", "--b-min", "0"], ["--a-min", "seventy", "--b-min", "50"],
                 ["--a-min", "70.5", "--b-min", "50"]):
        out = run_cli(env, "calibrate", *argv, ok=False)
        assert out["error"] and "Traceback" not in out["error"]
    assert path.read_bytes() == before
    assert not (env.folder / "config" / "calibration.json").exists()


def test_criteria_test_with_no_passing_roles_says_cutoffs_cannot_be_proposed(env):
    data = json.loads(TRACER.read_text(encoding="utf-8"))
    data["hard_filters"] = [{
        "id": "work_mode", "type": "enum_allow", "field": "work_mode", "allow": ["hybrid"],
        "unknown": "reject", "label": "Work arrangement",
    }]
    (env.folder / "config" / "criteria.json").write_text(json.dumps(data), encoding="utf-8")
    run_cli(env, "scan")
    out = run_cli(env, "criteria-test")
    assert out["counts"]["passed"] == 0
    assert "cutoffs cannot be proposed yet" in " ".join(out["warnings"])
    assert out["next"]
    run_cli(env, "calibrate", "--a-min", "70", "--b-min", "50", ok=False)


def _cap_off_level_config(env) -> None:
    """The tracer config plus a level criterion that holds an off-level title at tier B.

    The target is manager, which no fixture title names, so "Senior Robotics Engineer" reads as
    off level while the titles with no level word are never capped.
    """
    path = env.folder / "config" / "criteria.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["criteria"][0]["weight"] = 60
    data["criteria"].append({
        "id": "level", "label": "Level", "weight": 10, "evaluator": "level_match",
        "params": {"target": "manager", "years": 5, "cap_off_level": True},
    })
    path.write_text(json.dumps(data), encoding="utf-8")


def test_preview_calibrate_and_ranked_counts_agree_with_a_level_cap_in_play(env):
    _cap_off_level_config(env)
    run_cli(env, "scan")
    _, _, roles = calibrate.score_latest(env.folder)
    capped = [r for r in roles if r.rejected is None and r.tier_cap is not None]
    assert capped, "the fixture needs one passing role the level cap holds at B"
    a_min = min(r.score for r in capped)
    assert a_min > 1  # the capped role scores at or above the A cutoff, so the cap matters
    b_min = a_min - 1

    preview = run_cli(env, "criteria-test")
    tiers = {t["id"]: t["min_score"] for t in json.loads(
        (env.folder / "config" / "criteria.json").read_text(encoding="utf-8"))["tiers"]}
    pairs = calibrate.passing_with_caps(roles)
    assert preview["data"]["current_counts"] == calibrate.counts_at(pairs, tiers["A"], tiers["B"])
    proposed = preview["data"]["proposed_counts"]
    want = _ranked_counts(roles, preview["data"]["proposed_a_min"],
                          preview["data"]["proposed_b_min"])
    assert proposed == want

    accepted = run_cli(env, "calibrate", "--a-min", str(a_min), "--b-min", str(b_min))
    run_cli(env, "rank")
    ranked = json.loads(
        (env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))
    shown = ranked["funnel"]["tiers"]
    assert shown == _ranked_counts(roles, a_min, b_min)
    assert (accepted["counts"]["tier_a"], accepted["counts"]["tier_b"],
            accepted["counts"]["tier_c"]) == (shown["A"], shown["B"], shown["C"])
    assert shown["B"] >= 1
