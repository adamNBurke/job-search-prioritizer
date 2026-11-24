"""A saved config change is the version the next rank ranks with.

Purpose: prove that criteria_store.save_new_version keeps the previous config, bumps
criteria_version, and that rank names the new version in its ranked files.
Logic: unit tests drive save_new_version on a bare data folder; the end-to-end test drives
cli.main the way a user does (init, scan with a scripted transport, save, rank), using the scan_env
fixture in tests/conftest.py (one-board directory, frozen clock, scripted transport).
No socket is opened.
Guardrails: fixtures hold fictional data only. Each test builds its own folder under tmp_path.
QA checklist: every documented behavior has one named test below.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from helpers import TRACER, run_cli

from jobsearch import criteria_store
from jobsearch.criteria_store import CriteriaError


def tracer() -> dict:
    return json.loads(TRACER.read_text(encoding="utf-8"))


def seed(folder: Path, data: dict | None = None) -> bytes:
    """Write a config file by hand (as the interview would) and return its bytes."""
    path = folder / "config" / "criteria.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(tracer() if data is None else data, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return path.read_bytes()


def current(folder: Path) -> dict:
    return json.loads((folder / "config" / "criteria.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(("old", "new"), [("1.0", "1.1"), ("1.9", "1.10"), ("2.10", "2.11")])
def test_next_version_bumps_the_minor_number_numerically(old, new):
    assert criteria_store.next_version(old) == new


@pytest.mark.parametrize("bad", ["1", "1.", "a.b", "1.0.0", "1.x", "", " 1.0", "1.0\n", "-1.0"])
def test_next_version_refuses_a_malformed_version(bad):
    with pytest.raises(CriteriaError):
        criteria_store.next_version(bad)


def test_next_version_refuses_a_non_string():
    with pytest.raises(CriteriaError):
        criteria_store.next_version(1.0)  # type: ignore[arg-type]


def test_save_keeps_the_old_file_byte_identical_and_bumps_the_version(tmp_path):
    original = seed(tmp_path)
    data = tracer()
    data["criteria"][0]["weight"], data["criteria"][1]["weight"] = 60, 40
    result = criteria_store.save_new_version(tmp_path, data, "retune")
    assert result == "1.1"
    snapshot = tmp_path / "config" / "criteria-history" / "criteria-v1.0.json"
    assert snapshot.read_bytes() == original
    saved = current(tmp_path)
    assert saved["criteria_version"] == "1.1" and saved["created_from"] == "retune"
    assert [c["weight"] for c in saved["criteria"]] == [60, 40]
    assert criteria_store.load_criteria(tmp_path)["criteria_version"] == "1.1"
    assert data["criteria_version"] == "1.0"  # the caller's dict is not modified


def test_save_with_no_config_writes_one_point_zero_and_no_history(tmp_path):
    data = tracer()
    data["criteria_version"] = "7.3"  # whatever the draft said, a first save is 1.0
    assert criteria_store.save_new_version(tmp_path, data, "intake") == "1.0"
    assert current(tmp_path)["criteria_version"] == "1.0"
    assert not (tmp_path / "config" / "criteria-history").exists()


def test_save_accepts_a_draft_without_version_fields(tmp_path):
    seed(tmp_path)
    draft = tracer()
    del draft["criteria_version"], draft["created_from"]
    assert criteria_store.save_new_version(tmp_path, draft, "proposal:p-0007") == "1.1"
    assert current(tmp_path)["created_from"] == "proposal:p-0007"


def test_save_refuses_invalid_data_and_leaves_both_files_untouched(tmp_path):
    original = seed(tmp_path)
    bad = tracer()
    bad["criteria"][0]["weight"] = 99  # weights no longer add up to 100
    with pytest.raises(CriteriaError, match="add up to 100"):
        criteria_store.save_new_version(tmp_path, bad, "retune")
    assert (tmp_path / "config" / "criteria.json").read_bytes() == original
    assert not (tmp_path / "config" / "criteria-history").exists()


def test_save_refuses_a_non_object_without_writing(tmp_path):
    original = seed(tmp_path)
    with pytest.raises(CriteriaError):
        criteria_store.save_new_version(tmp_path, ["not", "an", "object"], "retune")  # type: ignore[arg-type]
    assert (tmp_path / "config" / "criteria.json").read_bytes() == original


@pytest.mark.parametrize("bad", ["", "manual", "Retune", "proposal:p-12", "proposal:p-0001 ",
                                 "retune\n", "intake|retune"])
def test_save_refuses_a_created_from_outside_the_fixed_forms(tmp_path, bad):
    original = seed(tmp_path)
    with pytest.raises(CriteriaError, match="created_from"):
        criteria_store.save_new_version(tmp_path, tracer(), bad)
    assert (tmp_path / "config" / "criteria.json").read_bytes() == original


def test_save_refuses_when_the_current_version_cannot_be_read(tmp_path):
    path = tmp_path / "config" / "criteria.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CriteriaError, match="cannot be kept"):
        criteria_store.save_new_version(tmp_path, tracer(), "retune")
    assert path.read_text(encoding="utf-8") == "{not json"
    assert not (tmp_path / "config" / "criteria-history").exists()


def test_an_existing_history_snapshot_is_never_overwritten(tmp_path):
    seed(tmp_path)
    snapshot = tmp_path / "config" / "criteria-history" / "criteria-v1.0.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("kept as the user left it\n", encoding="utf-8")
    assert criteria_store.save_new_version(tmp_path, tracer(), "retune") == "1.1"
    assert snapshot.read_text(encoding="utf-8") == "kept as the user left it\n"


def test_a_save_over_a_name_clash_keeps_the_replaced_config_under_another_name(tmp_path):
    """After a rollback an older criteria-v1.1.json can sit in the history while a newer 1.1 is
    live. Saving again must not lose that live 1.1."""
    original = seed(tmp_path)
    history = tmp_path / "config" / "criteria-history"
    history.mkdir(parents=True)
    stale = history / "criteria-v1.0.json"
    stale.write_text("an older 1.0 the user rolled back from\n", encoding="utf-8")
    assert criteria_store.save_new_version(tmp_path, tracer(), "retune") == "1.1"
    assert stale.read_text(encoding="utf-8") == "an older 1.0 the user rolled back from\n"
    kept = [p for p in history.iterdir() if p != stale]
    assert len(kept) == 1 and kept[0].read_bytes() == original
    # A second clash keeps that config too, under a name that is not taken.
    stale_11 = history / "criteria-v1.1.json"
    stale_11.write_text("another stale snapshot\n", encoding="utf-8")
    live = (tmp_path / "config" / "criteria.json").read_bytes()
    assert criteria_store.save_new_version(tmp_path, tracer(), "retune") == "1.2"
    assert stale_11.read_text(encoding="utf-8") == "another stale snapshot\n"
    assert live in [p.read_bytes() for p in history.iterdir()]


def test_a_snapshot_with_the_same_bytes_is_not_written_twice(tmp_path):
    seed(tmp_path)
    history = tmp_path / "config" / "criteria-history"
    history.mkdir(parents=True)
    (history / "criteria-v1.0.json").write_bytes((tmp_path / "config" / "criteria.json")
                                                 .read_bytes())
    criteria_store.save_new_version(tmp_path, tracer(), "retune")
    assert [p.name for p in history.iterdir()] == ["criteria-v1.0.json"]


def test_repeated_saves_keep_every_earlier_version(tmp_path):
    seed(tmp_path)
    for expected in ("1.1", "1.2", "1.3"):
        assert criteria_store.save_new_version(tmp_path, tracer(), "retune") == expected
    history = tmp_path / "config" / "criteria-history"
    assert sorted(p.name for p in history.iterdir()) == [
        "criteria-v1.0.json", "criteria-v1.1.json", "criteria-v1.2.json"]
    kept = json.loads((tmp_path / "config" / "criteria-history" / "criteria-v1.2.json")
                      .read_text(encoding="utf-8"))
    assert kept["criteria_version"] == "1.2"


def test_save_works_on_a_copy_so_the_callers_dict_is_unchanged(tmp_path):
    seed(tmp_path)
    data = tracer()
    before = copy.deepcopy(data)
    criteria_store.save_new_version(tmp_path, data, "retune")
    assert data == before


def test_a_retune_saved_through_the_store_is_the_version_the_next_rank_uses(scan_env):
    folder = scan_env.folder
    original = seed(folder)
    run_cli(scan_env, "scan")
    run_cli(scan_env, "rank")
    ranked_json = folder / "ranked" / "ranked-2026-11-17.json"
    assert json.loads(ranked_json.read_text(encoding="utf-8"))["criteria_version"] == "1.0"

    retuned = tracer()
    retuned["criteria"][0]["weight"] -= 10  # move ten points from title fit to freshness
    retuned["criteria"][1]["weight"] += 10
    assert criteria_store.save_new_version(folder, retuned, "retune") == "1.1"

    out = run_cli(scan_env, "rank")
    assert out["data"]["criteria_version"] == "1.1"
    assert json.loads(ranked_json.read_text(encoding="utf-8"))["criteria_version"] == "1.1"
    text = (folder / "ranked" / "ranked-2026-11-17.md").read_text(encoding="utf-8")
    assert "Scoring config version 1.1." in text
    kept = folder / "config" / "criteria-history" / "criteria-v1.0.json"
    assert kept.read_bytes() == original


@pytest.mark.parametrize("created_from, expected", [
    ("intake", "intake"), ("retune", "retune"), ("proposal:p-0001", "retune")])
def test_created_from_after_edit_keeps_intake_and_turns_anything_else_into_retune(
        created_from, expected):
    assert criteria_store.created_from_after_edit({"created_from": created_from}) == expected
