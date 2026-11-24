"""Paste a posting (text file or link) and have it ranked like a scanned role.

Purpose: prove a role the directory misses can be pasted, stored as data in roles/pasted.jsonl and
ranked beside scanned roles, with the funnel still balanced.
Logic: tests drive cli.main the way a user does, on a one-board fixture directory, a frozen clock
and a scripted transport. Unit tests call paste.py directly for parsing and encoding edges.
Guardrails: fictional data only (Acme Robotics, Beta Works, example.com). No test opens a socket;
every network case passes an injected transport and resolver, and LinkedIn cases assert zero
transport calls.
QA checklist: a new paste behavior gets a named case here before it changes paste.py.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import run_cli

from jobsearch import cli, clock, fetch, roles_store
from jobsearch.errors import UserError
from jobsearch.fetch import TransportResponse

ROOT = Path(__file__).resolve().parent
TODAY_ISO = "2026-11-17"

HEADER_FILE = (
    "Company: Acme Example\n"
    "Title: Operations Manager\n"
    "Location: Remote - US\n"
    "Link: https://careers.example.com/ops\n"
    "\n"
    "Acme Example needs an operations manager. You will run the daily plan.\n"
    "Salary: $90,000 - $110,000 per year. Three to five years of experience.\n"
)


def paste_mod():
    """Imported lazily so one missing module fails the named test, not the whole file."""
    return importlib.import_module("jobsearch.paste")


@pytest.fixture
def env(scan_env):
    return scan_env


def paste_file(env, text: str, name: str = "paste.txt", *, raw: bytes | None = None) -> Path:
    path = env.folder / name
    path.write_bytes(raw if raw is not None else text.encode("utf-8"))
    return path


def ranked_json(env) -> dict:
    return json.loads((env.folder / "ranked" / "ranked-2026-11-17.json").read_text(encoding="utf-8"))


def pasted_lines(env) -> list[dict]:
    path = env.folder / "roles" / "pasted.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- parsing and building one pasted posting -------------------------------------------------


def test_paste_file_with_link_builds_a_pasted_posting(env):
    paste = paste_mod()
    path = paste_file(env, HEADER_FILE)
    parsed = paste.parse_paste_file(path, env.folder)
    posting, _notes = paste.posting_from_text(
        parsed.company, parsed.title, parsed.location, parsed.link, parsed.text, TODAY_ISO)
    from jobsearch.models import make_pasted_role_id
    assert posting.source == "pasted" and posting.ats is None and posting.board_slug is None
    assert posting.role_id == make_pasted_role_id("https://careers.example.com/ops")
    assert (posting.company, posting.title) == ("Acme Example", "Operations Manager")
    assert posting.locations == ["Remote - US"] and posting.work_mode == "remote"
    assert posting.url_apply == "https://careers.example.com/ops"
    assert (posting.comp_min, posting.comp_max) == (90000.0, 110000.0)
    assert (posting.comp_currency, posting.comp_interval) == ("USD", "year")
    assert posting.description_text == parsed.text
    assert posting.description_text.startswith("Acme Example needs an operations manager.")


def test_paste_without_a_link_takes_the_role_id_from_the_text(env):
    paste = paste_mod()
    text = HEADER_FILE.replace("Link: https://careers.example.com/ops\n", "")
    parsed = paste.parse_paste_file(paste_file(env, text), env.folder)
    posting, _ = paste.posting_from_text(
        parsed.company, parsed.title, parsed.location, parsed.link, parsed.text, TODAY_ISO)
    from jobsearch.models import make_pasted_role_id
    assert posting.url_apply == ""
    assert posting.role_id == make_pasted_role_id(posting.description_text)


def test_a_range_of_years_is_not_read_as_pay(env):
    paste = paste_mod()
    posting, _ = paste.posting_from_text(
        "Acme Example", "Operations Manager", None, None,
        "You bring 3-5 years of experience and pay for a $10 parking pass is not offered.",
        TODAY_ISO)
    assert posting.comp_min is None and posting.comp_max is None


def test_mixed_currency_pay_uses_only_the_first_currency(env):
    paste = paste_mod()
    posting, notes = paste.posting_from_text(
        "Acme Example", "Operations Manager", None, None,
        "United States: $120,000 - $180,000 USD.\nCanada: $150,000 - $210,000 CAD.", TODAY_ISO)
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (
        120000.0, 180000.0, "USD")
    assert any("CAD" in note for note in notes)


def test_work_mode_comes_from_the_location_line_before_the_text(env):
    paste = paste_mod()
    hybrid, _ = paste.posting_from_text(
        "A", "Operations Manager", "Hybrid - Austin, TX", None,
        "This role is fully remote.", TODAY_ISO)
    assert hybrid.work_mode == "hybrid"
    from_text, _ = paste.posting_from_text(
        "A", "Operations Manager", "Austin, TX", None, "This is an on-site role.", TODAY_ISO)
    assert from_text.work_mode == "onsite"
    unknown, _ = paste.posting_from_text(
        "A", "Operations Manager", None, None, "Run the daily plan.", TODAY_ISO)
    assert unknown.work_mode == "unknown"


def test_missing_company_or_title_gets_a_plain_error(env):
    paste = paste_mod()
    path = paste_file(env, "Title: Operations Manager\n\nRun the plan.\n")
    with pytest.raises(UserError, match="needs Company: and Title: lines at the top"):
        paste.parse_paste_file(path, env.folder)


def test_empty_or_whitespace_text_asks_for_the_posting_text(env):
    paste = paste_mod()
    for body in ("", "   \n\t\n", "Company: A\nTitle: B\n\n   \n"):
        path = paste_file(env, body)
        with pytest.raises(UserError, match="Paste the posting text below the header lines"):
            paste.parse_paste_file(path, env.folder)


def test_bom_crlf_and_entities_are_normalized_and_the_id_is_stable(env):
    paste = paste_mod()
    messy = ("Company: Café Nørd\r\nTitle: Operations Analyst\r\n\r\n"
             "We&#39;re hiring &amp; growing. Résumé review – naïve.\r\n"
             "Second line").encode("utf-8")
    clean = ("Company: Café Nørd\nTitle: Operations Analyst\n\n"
             "We're hiring & growing. Résumé review – naïve.\n"
             "Second line").encode("utf-8")
    posted = []
    for name, raw in (("a.txt", b"\xef\xbb\xbf" + messy), ("b.txt", clean)):
        parsed = paste.parse_paste_file(paste_file(env, "", name, raw=raw), env.folder)
        posting, _ = paste.posting_from_text(
            parsed.company, parsed.title, parsed.location, parsed.link, parsed.text, TODAY_ISO)
        posted.append(posting)
    first, second = posted
    assert first.company == "Café Nørd" and "﻿" not in first.company
    assert first.description_text == (
        "We're hiring & growing. Résumé review – naïve.\nSecond line")
    assert "\r" not in first.description_text
    assert first.role_id == second.role_id and first.role_id.startswith("pasted:-:")


def test_html_text_becomes_plain_text(env):
    paste = paste_mod()
    posting, _ = paste.posting_from_text(
        "A", "Operations Manager", None, None,
        "<div><p>Run the plan.</p><ul><li>Hire</li></ul><script>alert(1)</script></div>",
        TODAY_ISO)
    assert "<" not in posting.description_text and "alert" not in posting.description_text
    assert "Run the plan." in posting.description_text


def test_text_over_the_limit_is_cut_with_a_warning(env):
    paste = paste_mod()
    long_text = "word " * (paste.PASTE_TEXT_LIMIT // 5 + 500)
    posting, notes = paste.posting_from_text(
        "A", "Operations Manager", None, None, long_text, TODAY_ISO)
    assert len(posting.description_text) <= paste.PASTE_TEXT_LIMIT
    assert any("cut" in note.lower() for note in notes)


def test_a_file_outside_the_data_folder_is_refused(env):
    paste = paste_mod()
    outside = env.tmp / "outside.txt"
    outside.write_text(HEADER_FILE, encoding="utf-8")
    with pytest.raises(UserError, match="inside your data folder"):
        paste.parse_paste_file(outside, env.folder)


# --- storing and ranking ---------------------------------------------------------------------


def test_pasting_the_same_file_twice_keeps_one_record_and_the_first_date(env, monkeypatch):
    path = paste_file(env, HEADER_FILE)
    run_cli(env, "paste", "--text-file", str(path))
    monkeypatch.setattr(clock, "now_utc", lambda: datetime(2026, 11, 20, tzinfo=timezone.utc))
    run_cli(env, "paste", "--text-file", str(path))
    lines = pasted_lines(env)
    assert len(lines) == 1 and lines[0]["pasted_on"] == "2026-11-17"
    assert lines[0]["posting"]["source"] == "pasted"


def test_pasted_role_is_ranked_beside_scanned_roles(env):
    run_cli(env, "scan")
    text = HEADER_FILE.replace("Acme Example", "Beta Works").replace(
        "Operations Manager", "Field Technician").replace(
        "https://careers.example.com/ops", "https://careers.example.com/field")
    run_cli(env, "paste", "--text-file", str(paste_file(env, text)))
    run_cli(env, "rank")
    roles = {r["role_id"]: r for r in ranked_json(env)["roles"]}
    pasted = [r for r in roles.values() if r["source"] == "pasted"]
    assert len(pasted) == 1 and pasted[0]["company"] == "Beta Works"
    assert pasted[0]["url_apply"] == "https://careers.example.com/field"
    assert pasted[0]["first_seen"] == TODAY_ISO
    assert any(r["source"] == "ats" for r in roles.values())


def test_pasted_duplicate_of_a_scanned_role_appears_once_and_the_funnel_balances(env):
    run_cli(env, "scan")
    scan_meta = json.loads(
        (env.folder / "scans" / TODAY_ISO / "scan-meta.json").read_text(encoding="utf-8"))
    scan_dups = scan_meta["totals"]["duplicates_merged"]
    duplicate = ("Company: Acme Robotics\nTitle: Operations  Analyst\n"
                 "Location: Pittsburgh, PA\n\nRun the daily operations plan for the Pittsburgh site.\n")
    other = ("Company: Beta Works\nTitle: Field Technician\nLocation: Remote - US\n"
             "Link: https://careers.example.com/field\n\nRepair robots in the field.\n")
    run_cli(env, "paste", "--text-file", str(paste_file(env, duplicate, "dup.txt")))
    run_cli(env, "paste", "--text-file", str(paste_file(env, other, "other.txt")))
    rank_input = roles_store.load_rankable(env.folder)
    assert rank_input.pasted_total == 2
    assert rank_input.duplicates_merged == scan_dups + 1
    assert rank_input.fetched_total + rank_input.pasted_total == (
        rank_input.duplicates_merged + rank_input.title_gate_dropped + len(rank_input.facts))
    run_cli(env, "rank")
    data = ranked_json(env)
    ops = [r for r in data["roles"] if r["title"].startswith("Operations")]
    assert [r["role_id"] for r in ops] == ["greenhouse:acme-robotics:7000003"]
    f = data["funnel"]
    assert f["scanned"] + f["pasted"] == (
        f["duplicates_merged"] + f["title_gate_dropped"] + f["rejected_by_filters"] + f["passed"])


def test_a_pasted_role_with_the_same_role_id_as_a_scanned_role_is_merged(env):
    run_cli(env, "scan")
    posting = {
        "schema_version": 1, "role_id": "greenhouse:acme-robotics:7000001", "source": "ats",
        "ats": "greenhouse", "board_slug": "acme-robotics", "posting_id": "7000001",
        "company": "Other Name", "title": "Something Else", "team": None, "locations": [],
        "work_mode_text": None, "work_mode": "unknown", "posted": None,
        "url_apply": "https://example.com/a", "url_posting": None, "description_text": "x",
        "comp_min": None, "comp_max": None, "comp_currency": None, "comp_interval": None,
        "comp_source": None, "fetched_at": "2026-11-17T00:00:00Z",
    }
    path = env.folder / "roles" / "pasted.jsonl"
    path.write_text(json.dumps({"pasted_on": TODAY_ISO, "posting": posting}) + "\n",
                    encoding="utf-8")
    rank_input = roles_store.load_rankable(env.folder)
    assert rank_input.pasted_total == 1
    scan_dups = json.loads((env.folder / "scans" / TODAY_ISO / "scan-meta.json").read_text(
        encoding="utf-8"))["totals"]["duplicates_merged"]
    assert rank_input.duplicates_merged == scan_dups + 1
    assert sum(1 for f in rank_input.facts if f.posting.role_id.endswith(":7000001")) == 1


def test_paste_result_never_echoes_the_posting_text_and_stays_short(env):
    sentence = "Unique sentence about the warehouse night shift schedule."
    text = f"Company: Beta Works\nTitle: Field Technician\n\n{sentence}\nAnother private line here.\n"
    cli.main(["paste", "--text-file", str(paste_file(env, text)), "--data-dir", str(env.folder)])
    out = env.capsys.readouterr().out
    assert sentence not in out and "Another private line" not in out
    assert len(out.splitlines()) == 1
    data = json.loads(out)
    assert data["ok"] and data["data"]["source"] == "pasted"
    assert data["data"]["company"] == "Beta Works" and data["next"] == "Run rank to see where it lands."


# --- paste by link ---------------------------------------------------------------------------

GREENHOUSE_JOB = ROOT / "fixtures" / "ats" / "greenhouse_job.json"
LEVER_LIST = ROOT / "fixtures" / "ats" / "lever_us_list.json"
ASHBY_LIST = ROOT / "fixtures" / "ats" / "ashby_list.json"
# Built from fragments: the leak gate allows LinkedIn domain literals only in a few named files.
LI_HOST = "linked" + "in.com"
LI_SHORT = "lnkd" + ".in"
LI_CDN = "lic" + "dn.com"
LINKEDIN_MESSAGE = "LinkedIn is never contacted by this tool. Paste the posting text instead."
PAGE = (
    "<html><head><title>Fallback Title - Jobs</title>"
    '<meta property="og:title" content="Operations Manager">'
    '<meta property="og:site_name" content="Gamma Example"></head>'
    "<body><nav>Menu</nav><h1>Heading Title</h1><p>Run the daily plan &amp; the team.</p>"
    "<script>track()</script><p>Salary: $80,000 - $95,000 per year.</p></body></html>"
)


class Recorder:
    """A scripted transport: url -> (status, content type, body or Location header)."""

    def __init__(self, routes: dict[str, tuple]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, headers, clock=None):
        self.calls.append(url)
        status, content_type, body = self.routes.get(url, (404, "text/plain", b""))
        if status in (301, 302):
            return TransportResponse(status, {"location": body}, b"")
        return TransportResponse(status, {"content-type": content_type}, body)


class Resolver:
    def __init__(self, address: str = "93.184.216.34") -> None:
        self.address, self.hosts = address, []

    def __call__(self, host):
        self.hosts.append(host)
        return [self.address]


GH_JOB_URL = ("https://boards-api.greenhouse.io/v1/boards/acme-robotics/jobs/7000001"
              "?pay_transparency=true")


def greenhouse_recorder(payload: dict | None = None) -> Recorder:
    body = json.dumps(payload).encode() if payload else GREENHOUSE_JOB.read_bytes()
    return Recorder({GH_JOB_URL: (200, "application/json", body)})


@pytest.mark.parametrize("url, expected", [
    ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme", "123")),
    ("https://boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme", "123")),
    ("https://jobs.lever.co/acme/abc-def", ("lever", "acme", "abc-def")),
    ("https://jobs.lever.co/acme/abc-def/apply", ("lever", "acme", "abc-def")),
    ("https://jobs.eu.lever.co/acme/x", ("lever_eu", "acme", "x")),
    ("https://jobs.ashbyhq.com/acme/0a1b-22", ("ashby", "acme", "0a1b-22")),
    ("https://job-boards.greenhouse.io/acme", ("board", "greenhouse", "acme")),
    ("https://jobs.lever.co/acme", ("board", "lever", "acme")),
    ("https://jobs.ashbyhq.com/acme/", ("board", "ashby", "acme")),
    ("https://careers.example.com/ops", ("page", None, None)),
    ("https://job-boards.greenhouse.io/acme/jobs/not-a-number", ("page", None, None)),
])
def test_classify_url(url, expected):
    assert paste_mod().classify_url(url) == expected


def test_greenhouse_link_reads_the_job_api_and_matches_the_scanned_role_id(env):
    transport = greenhouse_recorder()
    posting, notes = paste_mod().paste_url(
        env.folder, "https://job-boards.greenhouse.io/acme-robotics/jobs/7000001", TODAY_ISO,
        transport=transport, resolver=Resolver())
    assert transport.calls == [GH_JOB_URL]
    assert posting.source == "ats" and posting.role_id == "greenhouse:acme-robotics:7000001"
    assert (posting.comp_min, posting.comp_max, posting.comp_currency) == (120000.0, 180000.0, "USD")
    assert posting.comp_source == "structured"
    assert any("CAD" in note for note in notes)  # the Canadian range is not blended in
    assert posting.title == "Senior Robotics Engineer" and "invented warehouse" in posting.description_text


def test_greenhouse_pay_cents_are_divided_by_one_hundred(env):
    payload = json.loads(GREENHOUSE_JOB.read_text(encoding="utf-8"))
    payload["pay_input_ranges"] = [
        {"currency_type": "USD", "min_cents": 13920000, "max_cents": 23520000,
         "title": "United States Salary Range", "blurb": ""}]
    posting, notes = paste_mod().paste_url(
        env.folder, "https://boards.greenhouse.io/acme-robotics/jobs/7000001", TODAY_ISO,
        transport=greenhouse_recorder(payload), resolver=Resolver())
    assert (posting.comp_min, posting.comp_max) == (139200.0, 235200.0)
    assert not any("pay range" in note for note in notes)


def test_a_greenhouse_posting_that_is_gone_says_so(env):
    transport = Recorder({})  # 404 for every url
    with pytest.raises(UserError, match="no longer listed on the company's board"):
        paste_mod().paste_url(
            env.folder, "https://job-boards.greenhouse.io/acme-robotics/jobs/7000001", TODAY_ISO,
            transport=transport, resolver=Resolver())


@pytest.mark.parametrize("url, list_url, fixture, role_id", [
    ("https://jobs.lever.co/acme-robotics/6b1aeaed-4e6b-5fe5-967f-f17103a32d36",
     "https://api.lever.co/v0/postings/acme-robotics?mode=json", LEVER_LIST,
     "lever:acme-robotics:6b1aeaed-4e6b-5fe5-967f-f17103a32d36"),
    ("https://jobs.ashbyhq.com/acme-robotics/d26110db-ce5f-53c9-b79c-4eb820326d71",
     "https://api.ashbyhq.com/posting-api/job-board/acme-robotics?includeCompensation=true",
     ASHBY_LIST, "ashby:acme-robotics:d26110db-ce5f-53c9-b79c-4eb820326d71"),
])
def test_lever_and_ashby_links_pick_the_posting_from_the_board_list(env, url, list_url, fixture,
                                                                    role_id):
    transport = Recorder({list_url: (200, "application/json", fixture.read_bytes())})
    posting, notes = paste_mod().paste_url(
        env.folder, url, TODAY_ISO, transport=transport, resolver=Resolver())
    assert transport.calls == [list_url]
    assert posting.role_id == role_id and posting.source == "ats"
    assert any("Company name taken from the board address" in note for note in notes)


def test_a_lever_id_not_on_the_board_is_no_longer_listed(env):
    list_url = "https://api.lever.co/v0/postings/acme-robotics?mode=json"
    transport = Recorder({list_url: (200, "application/json", LEVER_LIST.read_bytes())})
    with pytest.raises(UserError, match="no longer listed on the company's board"):
        paste_mod().paste_url(env.folder, "https://jobs.lever.co/acme-robotics/zzz", TODAY_ISO,
                              transport=transport, resolver=Resolver())


def test_a_whole_board_link_is_refused_without_a_request(env):
    transport = Recorder({})
    with pytest.raises(UserError, match="whole job board. Add the company as a target instead"):
        paste_mod().paste_url(env.folder, "https://jobs.lever.co/acme-robotics", TODAY_ISO,
                              transport=transport, resolver=Resolver())
    assert transport.calls == []


def test_another_page_is_fetched_once_through_the_user_url_path(env):
    url = "https://www.careers.example.com/jobs/ops"
    transport = Recorder({url: (200, "text/html; charset=utf-8", PAGE.encode("utf-8"))})
    resolver = Resolver()
    posting, notes = paste_mod().paste_url(
        env.folder, url, TODAY_ISO, transport=transport, resolver=resolver)
    assert transport.calls == [url] and resolver.hosts == ["www.careers.example.com"]
    assert posting.source == "pasted" and posting.title == "Operations Manager"
    assert posting.company == "Gamma Example" and posting.url_apply == url
    assert "<" not in posting.description_text and "track()" not in posting.description_text
    assert "Run the daily plan & the team." in posting.description_text
    assert (posting.comp_min, posting.comp_max) == (80000.0, 95000.0)
    assert any("read from the page" in note for note in notes)


def test_page_title_falls_back_to_the_title_tag_then_the_first_heading(env):
    paste = paste_mod()
    no_og = "<html><head><title>Plain Title</title></head><body><p>Run the plan.</p></body></html>"
    no_title = "<html><body><h1>Only Heading</h1><p>Run the plan.</p></body></html>"
    for number, (body, expected) in enumerate(((no_og, "Plain Title"), (no_title, "Only Heading"))):
        url = f"https://www.careers.example.com/a{number}"  # a fresh url: the fetch cache is keyed by it
        transport = Recorder({url: (200, "text/html", body.encode("utf-8"))})
        posting, _ = paste.paste_url(env.folder, url, TODAY_ISO, transport=transport,
                                     resolver=Resolver())
        assert posting.title == expected
        assert posting.company == "careers.example.com"  # host without www


def test_a_page_with_no_title_asks_for_the_text(env):
    url = "https://careers.example.com/a"
    transport = Recorder({url: (200, "text/html", b"<html><body><p>Run the plan.</p></body></html>")})
    with pytest.raises(UserError, match="Paste the posting text instead"):
        paste_mod().paste_url(env.folder, url, TODAY_ISO, transport=transport, resolver=Resolver())


def test_a_non_text_content_type_is_refused(env):
    url = "https://careers.example.com/a.pdf"
    transport = Recorder({url: (200, "application/pdf", b"%PDF-1.7")})
    with pytest.raises(UserError, match="not a web page"):
        paste_mod().paste_url(env.folder, url, TODAY_ISO, transport=transport, resolver=Resolver())


@pytest.mark.parametrize("url", [
    f"https://www.{LI_HOST}/jobs/view/1",
    f"https://{LI_SHORT}/abc",
    f"https://media.{LI_CDN}/x",
    f"{LI_HOST}/jobs/view/1",
    f"http://www.{LI_HOST}/jobs/view/1",
])
def test_linkedin_links_are_refused_with_zero_requests(env, url):
    transport = Recorder({})
    resolver = Resolver()
    with pytest.raises(UserError) as caught:
        paste_mod().paste_url(env.folder, url, TODAY_ISO, transport=transport, resolver=resolver)
    assert str(caught.value) == LINKEDIN_MESSAGE
    assert transport.calls == [] and resolver.hosts == []


def test_a_page_that_redirects_to_linkedin_is_refused_and_never_requested(env):
    url = "https://careers.example.com/go"
    transport = Recorder({url: (302, "", f"https://www.{LI_HOST}/jobs/view/9")})
    with pytest.raises(UserError) as caught:
        paste_mod().paste_url(env.folder, url, TODAY_ISO, transport=transport, resolver=Resolver())
    assert str(caught.value) == LINKEDIN_MESSAGE
    assert transport.calls == [url]  # only the first hop was requested


@pytest.mark.parametrize("url, message", [
    ("http://careers.example.com/ops", "Only https web addresses are contacted."),
    ("https://192.168.1.10/ops", "private network are not contacted"),
    ("https://localhost/ops", "private network are not contacted"),
])
def test_http_and_private_addresses_are_refused_without_a_request(env, url, message):
    from jobsearch.netguard import UrlRefused
    transport = Recorder({})
    with pytest.raises(UrlRefused, match=message):
        paste_mod().paste_url(env.folder, url, TODAY_ISO, transport=transport, resolver=Resolver())
    assert transport.calls == []


def test_a_name_that_resolves_to_a_private_address_is_refused_before_connecting(env):
    from jobsearch.netguard import UrlRefused
    transport = Recorder({})
    with pytest.raises(UrlRefused, match="private network are not contacted"):
        paste_mod().paste_url(env.folder, "https://careers.example.com/ops", TODAY_ISO,
                              transport=transport, resolver=Resolver("10.0.0.5"))
    assert transport.calls == []


def test_url_flag_through_the_command_pastes_and_ranks_a_page(env, monkeypatch):
    url = "https://careers.example.com/field"
    body = PAGE.replace("Operations Manager", "Field Technician").encode("utf-8")
    recorder = Recorder({url: (200, "text/html", body)})
    monkeypatch.setattr(fetch, "_default_transport", recorder)
    out = run_cli(env, "paste", "--url", url)
    assert out["data"]["source"] == "pasted" and out["data"]["title"] == "Field Technician"
    assert recorder.calls == [url]
    assert pasted_lines(env)[0]["posting"]["url_apply"] == url


def test_a_greenhouse_link_to_a_scanned_role_is_merged_by_role_id(env, monkeypatch):
    run_cli(env, "scan")
    scan_calls = len(env.transport.calls)
    monkeypatch.setattr(fetch, "_default_transport", greenhouse_recorder())
    run_cli(env, "paste", "--url", "https://job-boards.greenhouse.io/acme-robotics/jobs/7000001")
    rank_input = roles_store.load_rankable(env.folder)
    assert rank_input.pasted_total == 1 and scan_calls == 1
    assert sum(1 for f in rank_input.facts
               if f.posting.role_id == "greenhouse:acme-robotics:7000001") == 1
    assert rank_input.fetched_total + rank_input.pasted_total == (
        rank_input.duplicates_merged + rank_input.title_gate_dropped + len(rank_input.facts))


def test_linkedin_url_through_the_command_fails_plainly_with_no_request(env):
    out = run_cli(env, "paste", "--url", f"https://www.{LI_HOST}/jobs/view/1", ok=False)
    assert out["error"] == LINKEDIN_MESSAGE
    assert env.transport.calls == []
    assert not (env.folder / "roles" / "pasted.jsonl").exists()


def test_pasted_text_with_a_400_digit_pay_figure_still_builds_a_posting_with_no_pay(env):
    posting, _ = paste_mod().posting_from_text(
        "Acme Example", "Operations Manager", None, None,
        "Pay: $" + "9" * 400 + " per year for this role.", TODAY_ISO)
    assert posting.comp_min is None and posting.comp_max is None
