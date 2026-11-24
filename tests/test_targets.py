"""Resolve the user's own target companies to public boards, and scan them.

Purpose: prove a company the user names in config/targets.json is resolved to a Greenhouse,
Lever or Ashby board (or reported plainly as not found), and that resolved boards join the next
scan and rank.
Logic: tests drive cli.main the way a user does. fetch._default_transport is replaced by a
scripted transport that serves fixture bodies by URL and counts the calls; directory.DIRECTORY_PATH
points at a fixture directory that does not hold the target boards; clock.now_utc is frozen. Unit
tests call targets.py directly for slug guesses.
Guardrails: fictional data only (Acme Robotics, Beta Works, example.com). No test opens a socket,
and the per-host pause is set to zero so the suite stays fast.
QA checklist: a new resolution rule gets a named case here before it changes targets.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import run_cli, write_directory

from jobsearch import cli, fetch, scan, targets
from jobsearch.netguard import board_url

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "ats"
GH_LIST = (FIXTURES / "greenhouse_list.json").read_bytes()

DIRECTORY_BOARD = {
    "company": "Gamma Foods", "ats": "greenhouse", "slug": "gamma-foods",
    "industry": "consumer", "size": "51-200", "stage": "growth",
    "last_verified": "2026-11-17", "identity": "company_name", "postings_at_verify": 4,
}
GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme-robotics/jobs"


@pytest.fixture
def env(scan_env):
    """The scan world with only Gamma Foods in the directory; unscripted URLs get a 404."""
    write_directory(scan_env.board_file, [DIRECTORY_BOARD])
    scan_env.transport.body = None
    return scan_env


def write_targets(env, companies: list[dict]) -> None:
    path = env.folder / "config" / "targets.json"
    path.write_text(json.dumps({"schema_version": 1, "companies": companies}), encoding="utf-8")


def local_boards(env) -> dict:
    path = env.folder / "config" / "boards.local.json"
    return json.loads(path.read_text(encoding="utf-8"))["boards"]


# --- Task 1: the tracer ----------------------------------------------------------------------


def test_explicit_greenhouse_target_is_fetched_once_and_resolved(env):
    env.transport.serve(GH_URL, GH_LIST)
    write_targets(env, [{"name": "Acme Robotics", "ats": "greenhouse", "slug": "acme-robotics"}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [GH_URL]  # one request, the lean list URL
    assert out["counts"]["resolved"] == 1
    assert out["wrote"] == ["config/boards.local.json"]
    entry = local_boards(env)["acme robotics"]
    assert entry["status"] == "resolved" and entry["identity"] == "company_name"
    assert entry["company"] == "Acme Robotics"
    assert entry["ats"] == "greenhouse" and entry["slug"] == "acme-robotics"
    assert entry["checked_on"] == "2026-11-17"


def test_resolved_target_roles_are_scanned_and_ranked_with_apply_links(env):
    env.transport.serve(GH_URL, GH_LIST)
    env.transport.serve(GH_URL + "?content=true", GH_LIST)  # the scan asks for full content
    write_targets(env, [{"name": "Acme Robotics", "ats": "greenhouse", "slug": "acme-robotics"}])
    run_cli(env, "targets-resolve")
    assert [e.slug for e in scan.boards_for(env.folder)] == ["gamma-foods", "acme-robotics"]
    run_cli(env, "scan")
    run_cli(env, "rank")
    ranked = json.loads(
        (env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))
    assert ranked["roles"]
    assert all(r["role_id"].startswith("greenhouse:acme-robotics:") for r in ranked["roles"])
    assert all(r["url_apply"].startswith("https://") for r in ranked["roles"])


def test_board_answering_with_another_company_needs_confirmation_and_is_not_scanned(env):
    env.transport.serve(GH_URL, GH_LIST)
    write_targets(env, [{"name": "Beta Works", "ats": "greenhouse", "slug": "acme-robotics"}])
    out = run_cli(env, "targets-resolve")
    assert out["counts"]["needs_confirmation"] == 1 and out["counts"]["resolved"] == 0
    assert out["data"]["needs_confirmation"] == ["Beta Works"]
    entry = local_boards(env)["beta works"]
    assert entry["status"] == "needs_confirmation" and entry["identity"] is None
    assert [e.slug for e in scan.boards_for(env.folder)] == ["gamma-foods"]


# --- Task 2: guesses, confirmation, unresolved reasons and the edges -------------------


def lean(ats: str, slug: str) -> str:
    return board_url(ats, slug, lean=True)


LEVER_US = (FIXTURES / "lever_us_list.json").read_bytes()
LEVER_EU = (FIXTURES / "lever_eu_list.json").read_bytes()
ASHBY = (FIXTURES / "ashby_list.json").read_bytes()
SYSTEMS = ("greenhouse", "lever", "lever_eu", "ashby")


def test_slug_candidates_make_at_most_three_distinct_names():
    assert targets.slug_candidates("Acme Robotics, Inc.") == [
        "acmerobotics", "acme-robotics", "acme"]
    assert targets.slug_candidates("Acme") == ["acme"]
    assert targets.slug_candidates("Acme Acme") == ["acmeacme", "acme-acme", "acme"]
    assert targets.slug_candidates("Gamma Delta Epsilon Zeta") == [
        "gammadeltaepsilonzeta", "gamma-delta-epsilon-zeta", "gamma"]


def test_slug_candidates_are_empty_without_letters_or_digits():
    assert targets.slug_candidates("") == []
    assert targets.slug_candidates("!!! ...") == []


def test_slug_candidates_keep_only_names_a_board_url_allows():
    assert targets.slug_candidates("Caf\u00e9 Noir") == ["cafenoir", "cafe-noir", "cafe"]
    assert targets.slug_candidates("\u682a\u5f0f\u4f1a\u793e") == []


def test_every_candidate_is_probed_on_the_four_systems_in_fixed_order(env):
    write_targets(env, [{"name": "Acme Robotics, Inc."}])
    out = run_cli(env, "targets-resolve")
    expected = [lean(ats, slug) for slug in ("acmerobotics", "acme-robotics", "acme")
                for ats in SYSTEMS]
    assert env.transport.calls == expected
    assert out["counts"]["unresolved"] == 1
    reason = ("No public board found on Greenhouse, Lever or Ashby under the names tried: "
              "acmerobotics, acme-robotics, acme.")
    assert out["data"]["unresolved"] == [f"Acme Robotics, Inc.: {reason}"]
    entry = local_boards(env)["acme robotics"]
    assert entry["status"] == "unresolved" and entry["ats"] is None and entry["slug"] is None
    assert entry["reason"] == reason


def test_probing_stops_at_the_first_board_that_answers(env):
    env.transport.serve(lean("lever", "acme-robotics"), LEVER_US)
    write_targets(env, [{"name": "Acme Robotics"}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [
        lean("greenhouse", "acmerobotics"), lean("lever", "acmerobotics"),
        lean("lever_eu", "acmerobotics"), lean("ashby", "acmerobotics"),
        lean("greenhouse", "acme-robotics"), lean("lever", "acme-robotics")]
    assert out["counts"]["resolved"] == 1
    entry = local_boards(env)["acme robotics"]
    assert entry["status"] == "resolved" and entry["identity"] == "description_mentions"
    assert entry["ats"] == "lever" and entry["slug"] == "acme-robotics"


@pytest.mark.parametrize("ats,body", [("lever_eu", LEVER_EU), ("ashby", ASHBY)])
def test_a_guessed_board_on_each_other_system_resolves_by_description(env, ats, body):
    env.transport.serve(lean(ats, "acmerobotics"), body)
    write_targets(env, [{"name": "Acme Robotics"}])
    run_cli(env, "targets-resolve")
    entry = local_boards(env)["acme robotics"]
    assert (entry["status"], entry["ats"], entry["identity"]) == (
        "resolved", ats, "description_mentions")


def test_a_guessed_greenhouse_board_with_a_matching_company_name_resolves(env):
    env.transport.serve(lean("greenhouse", "acmerobotics"), GH_LIST)
    write_targets(env, [{"name": "Acme Robotics"}])
    run_cli(env, "targets-resolve")
    entry = local_boards(env)["acme robotics"]
    assert (entry["status"], entry["identity"]) == ("resolved", "company_name")


def test_a_guessed_board_that_names_another_company_is_never_accepted(env):
    env.transport.serve(lean("greenhouse", "betaworks"), GH_LIST)
    env.transport.serve(lean("lever", "zetafoods"), LEVER_US)
    write_targets(env, [{"name": "Beta Works"}, {"name": "Zeta Foods"}])
    out = run_cli(env, "targets-resolve")
    assert out["counts"]["needs_confirmation"] == 2 and out["counts"]["resolved"] == 0
    assert out["data"]["needs_confirmation"] == ["Beta Works", "Zeta Foods"]
    beta = local_boards(env)["beta works"]
    assert (beta["status"], beta["ats"], beta["slug"]) == (
        "needs_confirmation", "greenhouse", "betaworks")
    assert "https://boards.greenhouse.io/betaworks" in beta["reason"]
    assert [e.slug for e in scan.boards_for(env.folder)] == ["gamma-foods"]


def test_a_board_with_no_postings_cannot_be_checked_so_it_waits_for_a_yes(env):
    env.transport.serve(lean("ashby", "acmerobotics"), b'{"jobs": []}')
    write_targets(env, [{"name": "Acme Robotics"}])
    run_cli(env, "targets-resolve")
    assert local_boards(env)["acme robotics"]["status"] == "needs_confirmation"


def test_a_yes_in_targets_json_makes_the_board_found_last_run_resolved(env):
    env.transport.serve(lean("greenhouse", "betaworks"), GH_LIST)
    write_targets(env, [{"name": "Beta Works"}])
    run_cli(env, "targets-resolve")
    before = list(env.transport.calls)
    write_targets(env, [{"name": "Beta Works", "confirmed": True}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == before  # a yes needs no new request
    assert out["counts"]["resolved"] == 1
    entry = local_boards(env)["beta works"]
    assert entry["status"] == "resolved" and entry["identity"] is None
    assert entry["reason"] == "Confirmed by you."
    assert (entry["ats"], entry["slug"]) == ("greenhouse", "betaworks")
    assert [e.slug for e in scan.boards_for(env.folder)] == ["gamma-foods", "betaworks"]


def test_a_yes_beside_an_explicit_board_resolves_it_without_a_request(env):
    write_targets(env, [{"name": "Beta Works", "ats": "ashby", "slug": "beta-works",
                         "confirmed": True}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [] and out["counts"]["resolved"] == 1
    assert local_boards(env)["beta works"]["reason"] == "Confirmed by you."


def test_a_yes_with_no_board_ever_shown_is_not_accepted(env):
    env.transport.serve(lean("greenhouse", "betaworks"), GH_LIST)
    write_targets(env, [{"name": "Beta Works", "confirmed": True}])
    out = run_cli(env, "targets-resolve")
    assert out["counts"]["needs_confirmation"] == 1 and out["counts"]["resolved"] == 0
    assert any("Beta Works" in w for w in out["warnings"])


def test_a_no_stops_all_further_guessing_for_that_company(env):
    env.transport.serve(lean("greenhouse", "betaworks"), GH_LIST)
    write_targets(env, [{"name": "Beta Works"}])
    run_cli(env, "targets-resolve")
    before = len(env.transport.calls)
    write_targets(env, [{"name": "Beta Works", "confirmed": False}])
    out = run_cli(env, "targets-resolve")
    assert len(env.transport.calls) == before  # zero requests after the rejection
    assert out["counts"]["rejected"] == 1
    entry = local_boards(env)["beta works"]
    assert entry["status"] == "rejected" and entry["slug"] == "betaworks"
    assert scan.boards_for(env.folder)[-1].slug == "gamma-foods"
    run_cli(env, "targets-resolve")
    assert len(env.transport.calls) == before  # and none on later runs


def test_a_no_with_no_board_known_probes_nothing(env):
    write_targets(env, [{"name": "Beta Works", "confirmed": False}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [] and out["counts"]["rejected"] == 1
    assert local_boards(env)["beta works"]["ats"] is None


def test_a_board_that_cannot_be_reached_is_reported_and_retried_next_run(env, monkeypatch):
    def refuse(url, headers, clock=None):
        env.transport.calls.append(url)
        raise fetch.TransportError("offline")

    monkeypatch.setattr(fetch, "_default_transport", refuse)
    write_targets(env, [{"name": "Acme Robotics"}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [lean("greenhouse", "acmerobotics")]  # probing stopped
    reason = local_boards(env)["acme robotics"]["reason"]
    assert reason == "Could not reach boards-api.greenhouse.io; try again later."
    assert out["counts"]["unresolved"] == 1
    monkeypatch.setattr(fetch, "_default_transport", env.transport)
    env.transport.serve(lean("greenhouse", "acmerobotics"), GH_LIST)
    out = run_cli(env, "targets-resolve")
    assert out["counts"]["resolved"] == 1


def test_a_lever_outage_is_a_failed_probe_not_no_board(env):
    env.transport.serve(lean("lever", "acmerobotics"), b"", 503, {"retry-after": "0"})
    write_targets(env, [{"name": "Acme Robotics"}])
    out = run_cli(env, "targets-resolve")
    lever_calls = [u for u in env.transport.calls if "lever" in u]
    assert len(lever_calls) == 3  # the fetch layer retried, then gave up
    assert env.transport.calls[-1] == lever_calls[-1]  # nothing probed after the outage
    entry = local_boards(env)["acme robotics"]
    assert entry["status"] == "unresolved"
    assert entry["reason"] == "Could not reach api.lever.co; try again later."
    assert "No public board found" not in entry["reason"]
    assert out["counts"]["unresolved"] == 1


def test_a_board_that_answers_with_unreadable_data_is_a_failed_probe(env):
    env.transport.serve(lean("greenhouse", "acmerobotics"), b"<html>not json</html>")
    write_targets(env, [{"name": "Acme Robotics"}])
    run_cli(env, "targets-resolve")
    entry = local_boards(env)["acme robotics"]
    assert entry["status"] == "unresolved" and "try again later" in entry["reason"]


def test_an_explicit_board_that_does_not_exist_is_unresolved_and_not_guessed_around(env):
    write_targets(env, [{"name": "Acme Robotics", "ats": "lever", "slug": "nope"}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [lean("lever", "nope")]
    assert out["counts"]["unresolved"] == 1
    assert local_boards(env)["acme robotics"]["reason"] == "That board was not found on Lever."


@pytest.mark.parametrize("ats,slug", [("workday", "acme"), ("lever", "bad slug!")])
def test_an_unusable_explicit_board_is_reported_without_a_request(env, ats, slug):
    write_targets(env, [{"name": "Acme Robotics", "ats": ats, "slug": slug}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [] and out["counts"]["unresolved"] == 1
    assert "config/targets.json" in local_boards(env)["acme robotics"]["reason"]


def test_two_names_for_one_company_are_resolved_once_and_reported_as_one(env):
    env.transport.serve(GH_URL, GH_LIST)
    write_targets(env, [
        {"name": "Acme Robotics"},
        {"name": "ACME Robotics, Inc.", "ats": "greenhouse", "slug": "acme-robotics"}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [GH_URL]  # one request for the one company
    assert sum(out["counts"].values()) == 1 and out["counts"]["resolved"] == 1
    assert list(local_boards(env)) == ["acme robotics"]
    assert local_boards(env)["acme robotics"]["company"] == "Acme Robotics"
    assert any("more than once" in w for w in out["warnings"])


def test_an_empty_targets_list_is_ok_with_a_next_step(env):
    write_targets(env, [])
    out = run_cli(env, "targets-resolve")
    assert out["counts"] == {"resolved": 0, "needs_confirmation": 0, "unresolved": 0,
                             "rejected": 0}
    assert env.transport.calls == [] and "targets.json" in out["next"]
    assert local_boards(env) == {}


def test_a_missing_targets_file_is_ok_with_a_next_step_and_writes_nothing(env):
    out = run_cli(env, "targets-resolve")
    assert out["ok"] and out["wrote"] == [] and "targets.json" in out["next"]
    assert not (env.folder / "config" / "boards.local.json").exists()


def test_a_name_with_no_letters_or_digits_is_unresolved_with_a_reason(env):
    write_targets(env, [{"name": "!!! ..."}, {"name": "Acme Robotics", "ats": "greenhouse",
                                                "slug": "acme-robotics"}])
    env.transport.serve(GH_URL, GH_LIST)
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [GH_URL]
    assert out["counts"]["unresolved"] == 1 and out["counts"]["resolved"] == 1
    assert out["data"]["unresolved"] == [
        "!!! ...: That name has no letters or numbers to look up."]
    assert list(local_boards(env)) == ["acme robotics"]  # nothing to key the empty name by


def test_a_name_with_no_board_name_to_try_says_how_to_add_the_board(env):
    write_targets(env, [{"name": "\u682a\u5f0f\u4f1a\u793e"}])
    out = run_cli(env, "targets-resolve")
    assert env.transport.calls == [] and out["counts"]["unresolved"] == 1
    assert "ats and slug" in out["data"]["unresolved"][0]


def test_boards_are_probed_and_written_in_normalized_name_order(env):
    write_targets(env, [{"name": "Zeta Foods"}, {"name": "acme robotics"}, {"name": "Beta Works"}])
    run_cli(env, "targets-resolve")
    firsts = [u for u in env.transport.calls if "greenhouse" in u and u.endswith("/jobs")]
    assert [u.split("/")[-2] for u in firsts][::3] == ["acmerobotics", "betaworks", "zetafoods"]
    path = env.folder / "config" / "boards.local.json"
    assert list(json.loads(path.read_text(encoding="utf-8"))["boards"]) == [
        "acme robotics", "beta works", "zeta foods"]


def test_more_than_the_cap_resolves_the_first_25_by_name_with_a_warning(env):
    assert targets.MAX_TARGETS == 25
    write_targets(env, [{"name": f"Company {n:02d}", "ats": "greenhouse", "slug": f"c{n:02d}"}
                        for n in range(27, 0, -1)])
    out = run_cli(env, "targets-resolve")
    assert len(env.transport.calls) == 25 and sum(out["counts"].values()) == 25
    assert lean("greenhouse", "c25") in env.transport.calls
    assert lean("greenhouse", "c26") not in env.transport.calls
    assert any("25" in w and "Company 26" not in w for w in out["warnings"])
    assert "company 26" not in local_boards(env)


def test_only_resolved_boards_join_the_scan(env):
    env.transport.serve(GH_URL, GH_LIST)
    env.transport.serve(lean("greenhouse", "betaworks"), GH_LIST)
    write_targets(env, [
        {"name": "Acme Robotics", "ats": "greenhouse", "slug": "acme-robotics"},
        {"name": "Beta Works"},
        {"name": "Delta Textiles"},
        {"name": "Epsilon Group", "ats": "ashby", "slug": "epsilon", "confirmed": False}])
    out = run_cli(env, "targets-resolve")
    assert out["counts"] == {"resolved": 1, "needs_confirmation": 1, "unresolved": 1,
                             "rejected": 1}
    assert [e.slug for e in scan.boards_for(env.folder)] == ["gamma-foods", "acme-robotics"]


def test_industry_and_size_are_carried_to_the_scan_and_bad_tags_are_dropped(env):
    env.transport.serve(GH_URL, GH_LIST)
    write_targets(env, [{"name": "Acme Robotics", "ats": "greenhouse", "slug": "acme-robotics",
                         "industry": "manufacturing-hardware", "size": "51-200"},
                        {"name": "Zeta Foods", "ats": "greenhouse", "slug": "zeta",
                         "industry": "snacks"}])
    out = run_cli(env, "targets-resolve")
    acme = [e for e in scan.boards_for(env.folder) if e.slug == "acme-robotics"][0]
    assert (acme.industry, acme.size) == ("manufacturing-hardware", "51-200")
    assert local_boards(env)["zeta foods"]["industry"] is None
    assert any("snacks" in w for w in out["warnings"])


def test_a_second_run_keeps_the_found_board_and_changes_nothing(env):
    env.transport.serve(lean("lever", "acmerobotics"), LEVER_US)
    write_targets(env, [{"name": "Acme Robotics"}])
    run_cli(env, "targets-resolve")
    path = env.folder / "config" / "boards.local.json"
    first, calls = path.read_bytes(), len(env.transport.calls)
    run_cli(env, "targets-resolve")
    assert path.read_bytes() == first and len(env.transport.calls) == calls


def test_output_stays_short_when_many_boards_need_a_yes(env):
    env.transport.serve(GH_URL, GH_LIST)
    write_targets(env, [{"name": f"Other {n:02d}", "ats": "greenhouse", "slug": "acme-robotics"}
                        for n in range(12)])
    code = cli.main(["targets-resolve", "--data-dir", str(env.folder)])
    text = env.capsys.readouterr().out
    assert code == 0 and len(text.splitlines()) < 40
    assert len(json.loads(text)["data"]["needs_confirmation"]) == 10


def test_a_broken_targets_file_is_a_plain_error(env):
    (env.folder / "config" / "targets.json").write_text("{not json", encoding="utf-8")
    out = run_cli(env, "targets-resolve", ok=False)
    assert "targets.json" in out["error"]


def test_boards_local_is_removed_when_targets_json_is_gone(env):
    local = env.folder / "config" / "boards.local.json"
    local.write_text(json.dumps({"schema_version": 1, "boards": []}), encoding="utf-8")
    run_cli(env, "targets-resolve")
    assert not local.exists()
    run_cli(env, "targets-resolve")  # neither file present: nothing to remove, nothing raised
