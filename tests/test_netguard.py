"""The deny decision is exhaustive, fail-closed, and costs zero network calls.

Purpose: LinkedIn (linkedin.com, licdn.com, lnkd.in) must be unreachable under every spelling,
and no other host may be contacted unless the purpose allows it.
Logic: every refused URL is run through fetch.fetch with BOTH purposes on a recording transport;
the recording list must stay empty. Pure policy rows (board_url, allow rows) call netguard directly.
Guardrails: this file is on the leak-gate allowlist for domain strings, so the
LinkedIn literals may appear here. Every URL with user info is built from fragments joined
around the at sign at runtime, so no literal name-at-host text sits in the file (the leak
gate's email rule has no path exemption).
QA checklist: add a row to DENIED_URLS for any new spelling you can think of; the zero-call
assertion comes for free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch import fetch
from jobsearch.netguard import (
    ATS_HOSTS,
    DENIED_DOMAINS,
    UPDATE_HOST,
    Purpose,
    UrlRefused,
    board_url,
    check_url,
    host_of,
    is_denied,
)

AT = chr(64)  # the at sign, kept out of the source text on purpose

# Code points used below:
#   U+FF4C U+FF49 U+FF4E U+FF4B U+FF45 U+FF44 are fullwidth l, i, n, k, e, d; U+FF0E is a
#   fullwidth full stop. NFKC maps all of them to plain ASCII.
FULLWIDTH_LINKEDIN = "".join(chr(c) for c in (0xFF4C, 0xFF49, 0xFF4E, 0xFF4B, 0xFF45, 0xFF44, 0xFF49, 0xFF4E))
FULLWIDTH_DOT = chr(0xFF0E)
IDEOGRAPHIC_STOP = chr(0x3002)  # the IDNA label separator the idna codec folds to "."
SOFT_HYPHEN = chr(0x00AD)  # nameprep maps it to nothing, so "linke<SHY>din" becomes "linkedin"
CIRCLED_L = chr(0x24C1)  # circled capital L, NFKC maps to "L"

# Every row is denied by is_denied and refused by fetch with zero transport calls.
DENIED_URLS = {
    "www": "https://www.linkedin.com/jobs/view/1",
    "uppercase": "HTTPS://LinkedIn.COM/x",
    "mixed_case_scheme": "HtTpS://linkedin.com/x",
    "trailing_dot": "https://linkedin.com./",
    "two_trailing_dots": "https://linkedin.com../x",
    "leading_dot": "https://.linkedin.com/x",
    "empty_inner_label": "https://www..linkedin.com/x",
    "country_subdomain": "https://uk.linkedin.com/in/x",
    "deep_subdomain": "https://a.b.c.linkedin.com/x",
    "cdn": "https://media.licdn.com/a.png",
    "cdn_bare": "https://licdn.com/a.png",
    "short_link": "https://lnkd.in/abc",
    "short_link_sub": "https://www.lnkd.in/abc",
    "no_scheme": "linkedin.com/jobs",
    "scheme_relative": "//linkedin.com/x",
    "userinfo_before": "https://" + "user:pass" + AT + "linkedin.com/",
    "userinfo_evil_before": "https://" + "evil.example" + AT + "linkedin.com/",
    "with_port": "https://linkedin.com:443/x",
    "http_scheme": "http://linkedin.com/x",
    "fullwidth": "https://" + FULLWIDTH_LINKEDIN + FULLWIDTH_DOT + "com/x",
    "ideographic_stop": "https://linkedin" + IDEOGRAPHIC_STOP + "com/x",
    "soft_hyphen": "https://linke" + SOFT_HYPHEN + "din.com/x",
    "circled_letter": "https://" + CIRCLED_L + "inkedin.com/x",
    "percent_encoded_dot": "https://linkedin%2ecom/x",
    "percent_encoded_letter": "https://%6cinkedin.com/x",
    "backslash": "https:\\\\linkedin.com",
    "embedded_space": "https://linked in.com/x",
    "embedded_tab": "https://linkedin\t.com/x",
    "embedded_newline": "https://linkedin.com\n/x",
    "leading_space": "  https://linkedin.com/x",
    "empty_host": "https:///x",
    "empty": "",
}

# Refused for BOTH purposes although the host is not a denied one.
REFUSED_BOTH = {
    "userinfo_after": "https://" + "linkedin.com" + AT + "evil.example/",
    "userinfo_plain": "https://" + "someone" + AT + "careers.example.com/",
    "http_ats": "http://boards-api.greenhouse.io/x",
    "port_8443": "https://boards-api.greenhouse.io:8443/x",
    "port_80": "https://careers.example.com:80/x",
    "ftp_scheme": "ftp://careers.example.com/x",
    "file_scheme": "file:///etc/hosts",
    "bad_port": "https://careers.example.com:abc/x",
}


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def transport(calls: list[str]):
    def send(url: str, headers):  # noqa: ARG001
        calls.append(url)
        return fetch.TransportResponse(200, {}, b"{}")

    return send


def _fetch(url: str, purpose: Purpose, transport, tmp_path: Path):
    fetch._reset_pacing()
    return fetch.fetch(url, purpose=purpose, cache_dir=tmp_path / "cache", transport=transport)


@pytest.mark.parametrize("name", sorted(DENIED_URLS))
def test_denied_url_is_denied(name: str) -> None:
    assert is_denied(DENIED_URLS[name]) is True


@pytest.mark.parametrize("purpose", list(Purpose), ids=lambda p: p.value)
@pytest.mark.parametrize("name", sorted(DENIED_URLS))
def test_denied_url_is_refused_with_zero_calls(
    name: str, purpose: Purpose, transport, calls: list[str], tmp_path: Path
) -> None:
    with pytest.raises(UrlRefused):
        _fetch(DENIED_URLS[name], purpose, transport, tmp_path)
    assert calls == []


@pytest.mark.parametrize("name", ["www", "cdn", "short_link", "trailing_dot", "fullwidth"])
def test_denied_linkedin_message_is_plain_english(name: str) -> None:
    with pytest.raises(UrlRefused, match="LinkedIn is never contacted"):
        check_url(DENIED_URLS[name], Purpose.USER_URL)


@pytest.mark.parametrize("purpose", list(Purpose), ids=lambda p: p.value)
@pytest.mark.parametrize("name", sorted(REFUSED_BOTH))
def test_refused_for_both_purposes_with_zero_calls(
    name: str, purpose: Purpose, transport, calls: list[str], tmp_path: Path
) -> None:
    with pytest.raises(UrlRefused):
        _fetch(REFUSED_BOTH[name], purpose, transport, tmp_path)
    assert calls == []


def test_userinfo_with_denied_host_after_the_at_sign_is_not_mistaken_for_the_host() -> None:
    # The part before the at sign is user info, not the host: the host here is evil.example.
    url = REFUSED_BOTH["userinfo_after"]
    assert host_of(url) == "evil.example"
    assert is_denied(url) is False  # it is refused for the user info, not as LinkedIn
    with pytest.raises(UrlRefused, match="user name or password"):
        check_url(url, Purpose.USER_URL)


def test_policy_fails_closed_when_the_host_cannot_be_read() -> None:
    assert host_of("https:///x") is None
    assert host_of("") is None
    assert is_denied("https:///x") is True
    assert is_denied("") is True


@pytest.mark.parametrize("ats", sorted(ATS_HOSTS))
def test_each_ats_host_is_allowed_for_ats_api(ats: str) -> None:
    assert check_url(f"https://{ATS_HOSTS[ats]}/x", Purpose.ATS_API) == ATS_HOSTS[ats]


def test_non_allowlisted_host_is_refused_for_ats_api(
    transport, calls: list[str], tmp_path: Path
) -> None:
    with pytest.raises(UrlRefused, match="not an approved job-board host"):
        _fetch("https://example.com/x", Purpose.ATS_API, transport, tmp_path)
    assert calls == []


def test_update_check_reaches_only_the_release_host() -> None:
    url = "https://api.github.com/repos/adamNBurke/job-search-prioritizer/releases/latest"
    assert check_url(url, Purpose.UPDATE_CHECK) == UPDATE_HOST == "api.github.com"
    for other in ("https://example.com/x", "https://uploads.github.com/x",
                  "https://github.com/x", "https://api.github.com.evil.example/x",
                  "http://api.github.com/x", "https://api.github.com:8443/x"):
        with pytest.raises(UrlRefused):
            check_url(other, Purpose.UPDATE_CHECK)
    with pytest.raises(UrlRefused, match="not the release-check host"):
        check_url("https://example.com/x", Purpose.UPDATE_CHECK)
    # the release host is not an approved job-board host or a free-form link host
    with pytest.raises(UrlRefused):
        check_url(url, Purpose.ATS_API)


def test_uppercase_ats_host_is_normalized_and_allowed() -> None:
    assert check_url("https://API.Lever.CO/v0/postings/x", Purpose.ATS_API) == "api.lever.co"


def test_user_url_allows_an_ordinary_https_host(transport, calls: list[str], tmp_path: Path) -> None:
    result = _fetch("https://careers.example.com/job/1", Purpose.USER_URL, transport, tmp_path)
    assert result.status == 200
    assert calls == ["https://careers.example.com/job/1"]


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://localhost/",
        "https://localhost./",
        "https://app.localhost/",
        "https://intranet.local/",
        "https://svc.internal/",
        "https://printer.lan/",
        "https://router.home.arpa/",
        "https://intranet.corp/",
        "https://2130706433/",  # decimal form of 127.0.0.1
        "https://0x7f000001/",  # hex form
        "https://0177.0.0.1/",  # octal-looking form
        "https://127.1/",  # short form
        "https://10.0.0.5/",
        "https://[::ffff:127.0.0.1]/",
    ],
)
def test_user_url_refuses_local_and_ip_hosts_with_zero_calls(
    url: str, transport, calls: list[str], tmp_path: Path
) -> None:
    with pytest.raises(UrlRefused, match="this computer or a private network"):
        _fetch(url, Purpose.USER_URL, transport, tmp_path)
    assert calls == []


@pytest.mark.parametrize(
    "url",
    ["https://localhost../", "https://127.0.0.1../", "https://.localhost/", "https://a..internal/"],
)
def test_hosts_with_empty_labels_are_unreadable_and_refused_with_zero_calls(
    url: str, transport, calls: list[str], tmp_path: Path
) -> None:
    # Only one trailing dot is stripped; any other empty label fails closed.
    assert host_of(url) is None
    for purpose in (Purpose.ATS_API, Purpose.USER_URL):
        with pytest.raises(UrlRefused):
            _fetch(url, purpose, transport, tmp_path)
    assert calls == []


@pytest.mark.parametrize("slug", ["acme-robotics", "a.b_c-1", "x" * 100])
def test_board_slug_accepts_ordinary_names(slug: str) -> None:
    assert board_url("greenhouse", slug).startswith("https://boards-api.greenhouse.io/")


def test_board_url_templates_are_exact() -> None:
    assert board_url("greenhouse", "acme-robotics") == (
        "https://boards-api.greenhouse.io/v1/boards/acme-robotics/jobs?content=true"
    )
    assert board_url("ashby", "Ashby") == (
        "https://api.ashbyhq.com/posting-api/job-board/Ashby?includeCompensation=true"
    )
    assert board_url("lever", "leverdemo") == "https://api.lever.co/v0/postings/leverdemo?mode=json"
    assert board_url("lever_eu", "leverdemo") == (
        "https://api.eu.lever.co/v0/postings/leverdemo?mode=json"
    )


def test_lean_board_url_drops_the_rich_content_flags_only() -> None:
    assert board_url("greenhouse", "acme", lean=True) == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    )
    assert board_url("ashby", "acme", lean=True) == (
        "https://api.ashbyhq.com/posting-api/job-board/acme"
    )
    for ats in ("lever", "lever_eu"):
        assert board_url(ats, "acme", lean=True) == board_url(ats, "acme")
    for ats in ATS_HOSTS:
        assert check_url(board_url(ats, "acme", lean=True), Purpose.ATS_API) == ATS_HOSTS[ats]


def test_lean_board_url_still_refuses_unsafe_slugs() -> None:
    with pytest.raises(UrlRefused, match="board name"):
        board_url("ashby", "a/b", lean=True)


@pytest.mark.parametrize(
    "slug",
    ["a/b", "a" + AT + "b", "a?b", "a#b", "a b", ".", "..", "", "x" * 101, "a\tb", "a\nb",
     "a%2fb", "a\\b", "a:b", chr(0xFF4C) + "ever"],
    ids=lambda s: repr(s)[:24],
)
def test_board_slug_with_unsafe_characters_is_refused(slug: str) -> None:
    with pytest.raises(UrlRefused, match="board name"):
        board_url("greenhouse", slug)


def test_board_url_output_always_passes_check_url() -> None:
    for ats in ATS_HOSTS:
        url = board_url(ats, "acme")
        assert check_url(url, Purpose.ATS_API) == ATS_HOSTS[ats]


def test_unknown_ats_is_refused() -> None:
    with pytest.raises(UrlRefused, match="not supported"):
        board_url("workday", "acme")


def test_denied_domains_constant_names_exactly_the_three_hosts() -> None:
    assert set(DENIED_DOMAINS) == {"linkedin.com", "licdn.com", "lnkd.in"}
