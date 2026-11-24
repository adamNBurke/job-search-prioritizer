"""Stranger test: from answers to a ranked list, over fixture boards.

Purpose: prove the Core Value on every OS without the network. A fictional stranger finishes the
interview (their answers file), runs the skills' command order and gets a ranked file with at
least one A or B role, a repeat pass gives the same bytes, and the funnel counts add up.
Logic: every test drives cli.main the way a user does (Env.rank_from_answers is the start
skill's first-time order). directory.DIRECTORY_PATH points at the
three fixture boards (one Greenhouse, one Lever, one Ashby, all fictional), fetch._default_transport
is a recording transport that serves tests/fixtures/pipeline/http by URL, and clock.now_utc is
frozen. No socket is opened. The one live test at the bottom is marked live, so the default run
deselects it; it is the same sequence against the shipped directory and is never run in CI.
Guardrails: fixtures hold fictional data only (Northwind Example, Harbor Example, Meridian
Example, example.com). Every test builds its own data folder under tmp_path.
QA checklist: a new persona needs a row in OWN_FILTER and passing roles on the fixture boards; a
changed ranked file shape needs docs/data-files.md and tests/test_output.py updated first.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import PERSONA_NAMES

from jobsearch import clock, cli, directory, fetch
from jobsearch.commands import rank as rank_command
from jobsearch.fetch import TransportResponse
from jobsearch.netguard import ATS_HOSTS, board_url

HERE = Path(__file__).resolve().parent
PIPELINE = HERE / "fixtures" / "pipeline"
PERSONAS = HERE / "fixtures" / "personas"
TODAY = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
SCAN_DATE = "2026-11-17"
RANKED = f"ranked/ranked-{SCAN_DATE}"

BOARD_FILES = {
    ("greenhouse", "northwind-example"): "greenhouse_northwind.json",
    ("lever", "harbor-example"): "lever_harbor.json",
    ("ashby", "meridian-example"): "ashby_meridian.json",
    ("greenhouse", "injection-example"): "greenhouse_injection.json",
}
# The filter each persona's own answers create, which must show up near the top of the table.
OWN_FILTER = {"casey-ops": "work_mode", "devon-analyst": "sponsorship", "rowan-designer": "pay_floor"}


class Recorder:
    """A transport that serves fixture bodies by board URL and records every request."""

    def __init__(self, bodies: dict[str, bytes] | None = None) -> None:
        self.bodies = bodies if bodies is not None else default_bodies()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers, clock=None):
        self.calls.append((url, dict(headers)))
        body = self.bodies.get(url)
        if body is None:
            return TransportResponse(404, {"content-type": "application/json"}, b"{}")
        return TransportResponse(200, {"content-type": "application/json"}, body)

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


def default_bodies() -> dict[str, bytes]:
    return {board_url(ats, slug): (PIPELINE / "http" / name).read_bytes()
            for (ats, slug), name in BOARD_FILES.items()}


class Env:
    def __init__(self, folder: Path, transport: Recorder, capsys, tmp: Path) -> None:
        self.folder, self.transport, self.capsys, self.tmp = folder, transport, capsys, tmp
        self.last_stdout = ""

    def cli(self, *argv: str, expect_ok: bool = True) -> dict:
        code = cli.main([*argv, "--data-dir", str(self.folder)])
        self.last_stdout = self.capsys.readouterr().out
        out = json.loads(self.last_stdout)
        assert code == (0 if out["ok"] else 1), out
        if expect_ok:
            assert out["ok"], out
        return out

    def rank_from_answers(self) -> dict:
        """The start skill's first-time order, without the title and function-review steps."""
        if not (self.folder / "config" / "criteria.json").is_file():
            self.cli("criteria-build")
        self.cli("scan")
        proposed = self.cli("criteria-test")["data"]
        if proposed.get("proposed_a_min") is not None and proposed.get("proposed_b_min") is not None:
            self.cli("calibrate", "--a-min", str(proposed["proposed_a_min"]),
                     "--b-min", str(proposed["proposed_b_min"]))
        return self.cli("rank")

    def ranked_path(self, suffix: str) -> Path:
        return self.folder / f"{RANKED}.{suffix}"

    def ranked_json(self) -> dict:
        return json.loads(self.ranked_path("json").read_text(encoding="utf-8"))

    def ranked_bytes(self) -> dict[str, bytes]:
        return {s: self.ranked_path(s).read_bytes() for s in ("md", "json", "csv")}


def stage(tmp_path, monkeypatch, capsys, persona: str, *, boards: Path | None = None,
          bodies: dict[str, bytes] | None = None, name: str = "ws") -> Env:
    """A set-up data folder holding the persona's resume and answers, with fixtures wired in."""
    monkeypatch.setattr(directory, "DIRECTORY_PATH", boards or PIPELINE / "boards.json")
    monkeypatch.setattr(clock, "now_utc", lambda: TODAY)
    transport = Recorder(bodies)
    monkeypatch.setattr(fetch, "_default_transport", transport)
    folder = tmp_path / name
    env = Env(folder, transport, capsys, tmp_path)
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    capsys.readouterr()
    env.cli("profile-import", "--resume", str(PERSONAS / persona / "resume.md"))
    shutil.copyfile(PERSONAS / persona / "answers.json", folder / "profile" / "answers.json")
    return env


@pytest.fixture
def casey(tmp_path, monkeypatch, capsys) -> Env:
    return stage(tmp_path, monkeypatch, capsys, "casey-ops")


def tier_roles(data: dict, *tiers: str) -> list[dict]:
    return [r for r in data["roles"] if r["tier"] in tiers]


# Task 1: the skills' command order for the demo persona -------------------------------------

def test_the_skill_path_goes_from_answers_to_a_ranked_list_with_an_a_or_b_role(casey):
    out = casey.rank_from_answers()
    assert out["command"] == "rank" and out["counts"]["scanned"] == 23
    for suffix in ("md", "json", "csv"):
        assert casey.ranked_path(suffix).is_file()
        assert f"{RANKED}.{suffix}" in out["wrote"]
    counts = out["counts"]
    assert counts["passed"] == counts["tier_a"] + counts["tier_b"] + counts["tier_c"] > 0
    assert out["data"]["funnel_line"].startswith("23 scanned, ")
    assert tier_roles(casey.ranked_json(), "A", "B")
    assert out["next"].startswith(f"Open {RANKED}.md")


def test_scan_and_rank_again_leave_the_config_and_cutoffs_alone(casey):
    first = casey.rank_from_answers()
    calibration = (casey.folder / "config" / "calibration.json").read_bytes()
    criteria = (casey.folder / "config" / "criteria.json").read_bytes()
    history = sorted(p.name for p in (casey.folder / "config" / "criteria-history").iterdir())
    casey.cli("scan")
    again = casey.cli("rank")
    assert rank_command.UNCALIBRATED not in first["warnings"] + again["warnings"]
    assert (casey.folder / "config" / "calibration.json").read_bytes() == calibration
    assert (casey.folder / "config" / "criteria.json").read_bytes() == criteria
    assert sorted(p.name for p in (casey.folder / "config" / "criteria-history").iterdir()) == history


def test_with_no_passing_roles_no_cutoffs_are_set(tmp_path, monkeypatch, capsys):
    # A directory with one board that holds only off-family titles: nothing passes.
    board = json.loads((PIPELINE / "boards.json").read_text(encoding="utf-8"))
    board["entries"] = [e for e in board["entries"] if e["slug"] == "northwind-example"]
    only = tmp_path / "one-board.json"
    only.write_text(json.dumps(board), encoding="utf-8")
    jobs = json.loads((PIPELINE / "http" / "greenhouse_northwind.json").read_text(encoding="utf-8"))
    jobs["jobs"] = [j for j in jobs["jobs"] if j["title"] == "Operations Manager"][:1]
    jobs["jobs"][0]["location"]["name"] = "Paris, France"  # outside US: Casey's region filter
    bodies = {board_url("greenhouse", "northwind-example"): json.dumps(jobs).encode("utf-8")}
    env = stage(tmp_path, monkeypatch, capsys, "casey-ops", boards=only, bodies=bodies)
    out = env.rank_from_answers()
    assert out["counts"]["passed"] == 0
    assert rank_command.UNCALIBRATED in out["warnings"]
    assert not (env.folder / "config" / "calibration.json").exists()


def test_the_skill_path_only_asks_job_board_hosts_with_plain_gets(casey):
    casey.rank_from_answers()
    hosts = set(ATS_HOSTS.values())
    assert len(casey.transport.calls) == 3
    for url, headers in casey.transport.calls:
        assert url.startswith("https://") and url.split("/")[2] in hosts
        assert "casey" not in url.casefold() and "casey" not in json.dumps(headers).casefold()


# Task 2: three personas, byte-identical repeats, funnel identities -------------------------------


def persona_secrets(persona: str) -> list[str]:
    """Strings from the persona's own files that must never travel in a request."""
    resume = (PERSONAS / persona / "resume.md").read_text(encoding="utf-8")
    lines = [line.strip().lstrip("#-* ").strip() for line in resume.splitlines()]
    secrets = [line for line in lines if len(line) >= 12]
    secrets += re.findall(r"[\w.]+@[\w.]+", resume)
    secrets.append(persona.split("-")[0])
    answers = json.loads((PERSONAS / persona / "answers.json").read_text(encoding="utf-8"))
    secrets += [t for t in answers["target_titles"]] + [s["term"] for s in answers["skills"]]
    return secrets


@pytest.mark.parametrize("persona", PERSONA_NAMES)
def test_every_persona_gets_a_ranked_list_with_an_a_or_b_role(tmp_path, monkeypatch, capsys, persona):
    env = stage(tmp_path, monkeypatch, capsys, persona)
    out = env.rank_from_answers()
    assert out["ok"]
    data = env.ranked_json()
    assert tier_roles(data, "A", "B"), data["funnel"]
    assert all(r["url_apply"].startswith("https://") for r in tier_roles(data, "A", "B"))
    funnel = data["funnel"]
    assert funnel["scanned"] + funnel["pasted"] == (
        funnel["duplicates_merged"] + funnel["title_gate_dropped"]
        + funnel["rejected_by_filters"] + funnel["passed"])
    assert sum(funnel["tiers"].values()) == funnel["passed"]


@pytest.mark.parametrize("persona", PERSONA_NAMES)
def test_each_personas_own_filter_is_a_top_two_rejection_reason(tmp_path, monkeypatch, capsys, persona):
    env = stage(tmp_path, monkeypatch, capsys, persona)
    env.rank_from_answers()
    top_two = [r["filter_id"] for r in env.ranked_json()["rejections"][:2]]
    assert OWN_FILTER[persona] in top_two


def test_the_three_personas_have_different_rejection_tables(tmp_path, monkeypatch, capsys):
    tables = {}
    for persona in PERSONA_NAMES:
        env = stage(tmp_path, monkeypatch, capsys, persona, name=f"ws-{persona}")
        env.rank_from_answers()
        tables[persona] = [(r["filter_id"], r["count"]) for r in env.ranked_json()["rejections"]]
    assert len({json.dumps(t) for t in tables.values()}) == 3


@pytest.mark.parametrize("persona", PERSONA_NAMES)
def test_a_second_run_writes_byte_identical_ranked_files(tmp_path, monkeypatch, capsys, persona):
    env = stage(tmp_path, monkeypatch, capsys, persona)
    env.rank_from_answers()
    first = env.ranked_bytes()
    assert all(first.values())
    env.cli("scan")
    env.cli("rank")
    assert env.ranked_bytes() == first
    assert len(env.transport.calls) <= 3  # the repeat read the cache, not the boards


@pytest.mark.parametrize("persona", PERSONA_NAMES)
def test_no_request_carries_any_profile_text(tmp_path, monkeypatch, capsys, persona):
    env = stage(tmp_path, monkeypatch, capsys, persona)
    env.rank_from_answers()
    assert env.transport.calls
    sent = "\n".join(url + json.dumps(headers, sort_keys=True)
                     for url, headers in env.transport.calls).casefold()
    for secret in persona_secrets(persona):
        assert secret.casefold() not in sent, secret
    hosts = set(ATS_HOSTS.values())
    assert all(url.split("/")[2] in hosts for url in env.transport.urls)


def test_pasted_duplicate_and_board_duplicate_keep_the_funnel_balanced(casey):
    casey.rank_from_answers()
    first = casey.ranked_json()["funnel"]
    assert first["pasted"] == 0 and first["duplicates_merged"] == 1
    postings = (casey.folder / "scans" / SCAN_DATE / "postings.jsonl").read_text(encoding="utf-8")
    scanned = [json.loads(line) for line in postings.splitlines()]
    target = next(p for p in scanned if p["company"] == "Northwind Example"
                  and p["title"] == "Program Manager" and len(p["locations"]) == 1)
    paste_file = casey.folder / "profile" / "pasted-posting.txt"
    paste_file.write_text(
        f"Company: {target['company']}\nTitle: {target['title']}\n"
        f"Location: {target['locations'][0]}\nLink: https://jobs.example.com/pasted/1\n\n"
        "The same invented program role, copied from the careers page.\n", encoding="utf-8")
    casey.cli("paste", "--text-file", str(paste_file))
    casey.cli("rank")
    funnel = casey.ranked_json()["funnel"]
    assert funnel["pasted"] == 1 and funnel["duplicates_merged"] == 2
    assert funnel["scanned"] + funnel["pasted"] == (
        funnel["duplicates_merged"] + funnel["title_gate_dropped"]
        + funnel["rejected_by_filters"] + funnel["passed"])
    tiers = funnel["tiers"]
    assert tiers["A"] + tiers["B"] + tiers["C"] == funnel["passed"]
    line = next(x for x in casey.ranked_path("md").read_text(encoding="utf-8").splitlines()
                if x.endswith(f"{tiers['C']} C"))
    assert line == (f"{funnel['scanned']} scanned, 1 pasted, {funnel['passed']} passed filters, "
                    f"{tiers['A']} A, {tiers['B']} B, {tiers['C']} C")


def test_a_posting_with_no_description_passes_through_with_a_skills_gap(casey):
    casey.rank_from_answers()
    data = casey.ranked_json()
    role = next(r for r in data["roles"] if r["role_id"] == "greenhouse:northwind-example:4100009")
    assert "Your skills in the description" in role["gaps"]
    rows = list(csv.DictReader(io.StringIO(casey.ranked_path("csv").read_text(encoding="utf-8-sig"))))
    row = next(r for r in rows if r["role_id"] == role["role_id"])
    assert "Your skills in the description" in row["gaps"]


# Task 3: the live stranger run (opt-in, never in CI) ---------------------------------------------

@pytest.mark.live
def test_live_demo_persona_gets_an_a_or_b_role_from_the_shipped_directory(tmp_path, capsys):
    """Same sequence as the hand-run: real clock, real network, the shipped board directory.

    Run it with: uv run --frozen pytest -q -m live tests/test_stranger_pipeline.py
    It sends anonymous GETs to public job-board hosts at the fetch layer's one-second pace.
    """
    folder = tmp_path / "ws"
    assert cli.main(["init", "--data-dir", str(folder)]) == 0
    capsys.readouterr()
    resume = str(PERSONAS / "casey-ops" / "resume.md")
    assert cli.main(["profile-import", "--resume", resume, "--data-dir", str(folder)]) == 0
    capsys.readouterr()
    shutil.copyfile(PERSONAS / "casey-ops" / "answers.json", folder / "profile" / "answers.json")
    out = Env(folder, None, capsys, tmp_path).rank_from_answers()
    assert out["ok"] is True
    ranked = sorted((folder / "ranked").glob("ranked-*.json"))[-1]
    roles = json.loads(ranked.read_text(encoding="utf-8"))["roles"]
    assert sum(1 for r in roles if r["tier"] in ("A", "B")) >= 1
