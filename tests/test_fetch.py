"""The single fetch layer is polite, bounded, cached and plain-English on failure.

Purpose: prove pacing, retries, the size cap, the gzip cache and the offline fallback without
touching the network. Every test injects a scripted transport, a fake clock and a fixed "now".
Logic: the Script object pops one prepared response (or exception) per transport call and
records each URL, header set and clock reading. The fake clock's sleep advances the clock, so
the tests can assert both what was slept and how far apart requests landed.
Guardrails: conftest resets pacing and zeroes the interval for the suite; this file's autouse
fixture puts the real interval back, so the pacing assertions test real spacing and injected
clocks never leak between cases. No test opens a socket; the default transport is exercised through a monkeypatched
requests.Session.get.
QA checklist: new fetch behavior needs a scripted test here before it lands in fetch.py.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests

from jobsearch import fetch
from jobsearch.fetch import FetchError, TransportError, TransportResponse
from jobsearch.netguard import Purpose, UrlRefused, board_url

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
GH = board_url("greenhouse", "acme")  # https://boards-api.greenhouse.io/...?content=true
GH_OTHER = board_url("greenhouse", "other")
LEVER = board_url("lever", "acme")


class Clock:
    """A fake monotonic clock whose sleep advances it. Safe to share between threads."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.t += seconds


class Script:
    """A transport that returns prepared items in order and records every call."""

    def __init__(self, items, clock: Clock | None = None) -> None:
        self.items = list(items)
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.times: list[float] = []
        self._clock = clock
        self._lock = threading.Lock()

    def __call__(self, url, headers):
        with self._lock:
            self.calls.append(url)
            self.headers.append(dict(headers))
            if self._clock is not None:
                self.times.append(self._clock())
            item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(body: bytes = b'{"jobs": []}', **headers: str) -> TransportResponse:
    return TransportResponse(200, {"content-type": "application/json", **headers}, body)


def resp(status: int, body: bytes = b"", **headers: str) -> TransportResponse:
    return TransportResponse(status, dict(headers), body)


# Captured at import: collection runs before any fixture, so this is the real interval.
REAL_INTERVAL = fetch.MIN_INTERVAL_SECONDS


@pytest.fixture(autouse=True)
def _real_interval(_fresh_pacing, monkeypatch) -> None:
    # Taking _fresh_pacing by name makes this run after the conftest zeroing.
    monkeypatch.setattr(fetch, "MIN_INTERVAL_SECONDS", REAL_INTERVAL)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def run(tmp_path: Path, clock: Clock):
    state = {"now": NOW}

    def go(url: str, script, *, purpose: Purpose = Purpose.ATS_API, **kw):
        return fetch.fetch(
            url,
            purpose=purpose,
            cache_dir=tmp_path / "cache",
            transport=script,
            clock=clock,
            sleep=clock.sleep,
            now=lambda: state["now"],
            **kw,
        )

    go.state = state  # type: ignore[attr-defined]
    go.cache_dir = tmp_path / "cache"  # type: ignore[attr-defined]
    return go


# ---------------------------------------------------------------- cache


def test_cache_hit_second_fetch_makes_no_transport_call(run) -> None:
    script = Script([ok(), ok()])
    first = run(GH, script)
    second = run(GH, script)
    assert (first.from_cache, second.from_cache) == (False, True)
    assert len(script.calls) == 1
    assert second.body == first.body
    assert second.status == 200 and second.stale is False


def test_only_content_type_is_kept_live_and_from_cache(run) -> None:
    headers = {"etag": 'W/"1"', "last-modified": "Mon, 23 Nov 2026 10:00:00 GMT"}
    script = Script([ok(**headers), ok(**headers)])
    first, second = run(GH, script), run(GH, script)
    assert first.headers == second.headers == {"content-type": "application/json"}


def test_cache_expires_at_the_ok_ttl(run) -> None:
    script = Script([ok(b'{"n": 1}'), ok(b'{"n": 2}')])
    run(GH, script)
    run.state["now"] = NOW + timedelta(seconds=fetch.TTL_OK_SECONDS - 1)
    assert run(GH, script).from_cache is True
    run.state["now"] = NOW + timedelta(seconds=fetch.TTL_OK_SECONDS)
    again = run(GH, script)
    assert again.from_cache is False and again.body == b'{"n": 2}'
    assert len(script.calls) == 2


def test_not_found_is_returned_not_raised_and_cached_for_one_hour(run) -> None:
    not_found = TransportResponse(404, {"content-type": "text/plain"}, b"Not Found")
    script = Script([not_found, not_found])
    first = run(GH, script)
    assert first.status == 404 and first.body == b"Not Found"
    second = run(GH, script)
    assert second.from_cache is True and second.status == 404
    assert len(script.calls) == 1
    run.state["now"] = NOW + timedelta(seconds=fetch.TTL_NOT_FOUND_SECONDS)
    assert run(GH, script).from_cache is False
    assert len(script.calls) == 2


def test_server_error_is_never_cached(run) -> None:
    script = Script([resp(500)] * 3)
    with pytest.raises(FetchError):
        run(GH, script)
    assert list(run.cache_dir.rglob("*.json.gz")) == []


def test_other_client_errors_are_returned_once_and_not_cached(run) -> None:
    script = Script([resp(403, b"no"), resp(403, b"no")])
    result = run(GH, script)
    assert result.status == 403 and len(script.calls) == 1
    assert list(run.cache_dir.rglob("*.json.gz")) == []


def test_refresh_calls_transport_and_rewrites_the_entry(run) -> None:
    script = Script([ok(b'{"v": 1}'), ok(b'{"v": 2}')])
    run(GH, script)
    refreshed = run(GH, script, refresh=True)
    assert refreshed.from_cache is False and refreshed.body == b'{"v": 2}'
    assert run(GH, script).body == b'{"v": 2}'  # served from the rewritten entry
    assert len(script.calls) == 2


def test_cache_files_are_gzip_under_two_hex_directories(run) -> None:
    run(GH, Script([ok()]))
    files = list(run.cache_dir.rglob("*"))
    files = [f for f in files if f.is_file()]
    assert len(files) == 1
    rel = files[0].relative_to(run.cache_dir).as_posix()
    assert re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{64}\.json\.gz", rel)
    assert rel.split("/")[0] == rel.split("/")[1][:2]
    entry = json.loads(gzip.decompress(files[0].read_bytes()).decode("utf-8"))
    assert entry["status"] == 200 and entry["url"] == GH


@pytest.mark.parametrize(
    "garbage",
    [
        b"this is not gzip at all",
        gzip.compress(b"{not json")[:12],  # truncated gzip
        gzip.compress(b"{not json"),  # valid gzip, invalid JSON
        gzip.compress(b'["a list, not an entry"]'),
        gzip.compress(json.dumps({"schema_version": 1, "status": 200, "fetched_at": "x",
                                  "body_b64": "!!!", "headers": {}}).encode("utf-8")),
        gzip.compress(b"{}")[:-6] + b"\x00" * 6,  # corrupt trailer
        b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\xff" * 40,  # gzip header, invalid deflate data
    ],
    ids=["not_gzip", "truncated", "bad_json", "wrong_shape", "bad_fields", "corrupt_trailer",
         "bad_deflate"],
)
def test_corrupt_cache_file_is_a_miss_and_is_overwritten(run, garbage: bytes) -> None:
    script = Script([ok(), ok(b'{"fresh": true}')])
    run(GH, script)
    (path,) = [f for f in run.cache_dir.rglob("*.json.gz")]
    path.write_bytes(garbage)
    result = run(GH, script)
    assert result.from_cache is False and result.body == b'{"fresh": true}'
    assert run(GH, script).from_cache is True  # the bad file was replaced by a good one


def test_cache_write_failure_does_not_break_the_fetch(run, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise PermissionError("read-only cache")

    monkeypatch.setattr(fetch.fsio, "write_bytes_atomic", boom)
    result = run(GH, Script([ok()]))
    assert result.status == 200


# ---------------------------------------------------------------- pacing


def slow_transport(script, clock: Clock, ends: list[float]):
    """A transport whose every request, failed or not, takes three seconds on the injected clock."""
    def slow(url, headers):
        try:
            return script(url, headers)
        finally:
            clock.t += 3.0
            ends.append(clock())
    return slow


def test_two_sequential_fetches_to_one_host_are_spaced(run, clock: Clock) -> None:
    script = Script([ok(), ok()], clock)
    run(GH, script)
    run(GH_OTHER, script)
    assert clock.sleeps == [fetch.MIN_INTERVAL_SECONDS]
    assert script.times[1] - script.times[0] >= fetch.MIN_INTERVAL_SECONDS


def test_pacing_counts_from_the_end_of_the_previous_request(run, clock: Clock) -> None:
    # A request that takes 3 s must still be followed by a 1 s pause, not an instant start.
    script = Script([ok(), ok()], clock)
    ends: list[float] = []
    slow = slow_transport(script, clock, ends)
    run(GH, slow)
    run(GH_OTHER, slow)
    assert script.times[1] - ends[0] >= fetch.MIN_INTERVAL_SECONDS
    assert clock.sleeps == [fetch.MIN_INTERVAL_SECONDS]


def test_pacing_also_counts_from_a_request_that_failed(run, clock: Clock) -> None:
    script = Script([TransportError("down"), ok()], clock)
    ends: list[float] = []
    slow = slow_transport(script, clock, ends)
    with pytest.raises(FetchError):
        run(GH, slow)
    run(GH_OTHER, slow)
    assert script.times[1] - ends[0] >= fetch.MIN_INTERVAL_SECONDS


def test_different_hosts_do_not_wait_on_each_other(run, clock: Clock) -> None:
    script = Script([ok(), ok()], clock)
    run(GH, script)
    run(LEVER, script)
    assert clock.sleeps == []


def test_same_host_threads_are_spaced_on_the_injected_clock(run, clock: Clock) -> None:
    script = Script([ok(), ok(), ok()], clock)
    errors: list[BaseException] = []

    def worker(url: str) -> None:
        try:
            run(url, script)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    urls = [board_url("greenhouse", name) for name in ("a1", "a2", "a3")]
    threads = [threading.Thread(target=worker, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    times = sorted(script.times)
    assert len(times) == 3
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= fetch.MIN_INTERVAL_SECONDS for g in gaps)


# ---------------------------------------------------------------- retries


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried(run, clock: Clock, status: int) -> None:
    script = Script([resp(status), ok()])
    assert run(GH, script).status == 200
    assert len(script.calls) == 2
    assert clock.sleeps == [1]


def test_backoff_is_one_then_two_seconds(run, clock: Clock) -> None:
    script = Script([resp(503), resp(503), ok()])
    result = run(GH, script)
    assert result.status == 200 and len(script.calls) == 3
    assert clock.sleeps == [1, 2]


@pytest.mark.parametrize("status, header, value, sleeps", [
    (429, "retry-after", "3", [3]),
    (429, "Retry-After", "4", [4]),  # the header name is case-insensitive
    (503, "retry-after", format_datetime(NOW + timedelta(seconds=5), usegmt=True), [5]),  # an HTTP date
    (429, "retry-after", "30", [30]),  # exactly thirty seconds is still honored
    (503, "retry-after", "soon", [1]),  # unreadable falls back to the backoff
], ids=["seconds", "header-name-case", "http-date", "exactly-thirty", "unreadable"])
def test_retry_after_is_honored_or_falls_back_to_backoff(run, clock: Clock, status, header, value, sleeps) -> None:
    run(GH, Script([resp(status, **{header: value}), ok()]))
    assert clock.sleeps == sleeps


def test_retry_after_over_thirty_seconds_stops_with_a_plain_message(run, clock: Clock) -> None:
    script = Script([resp(429, **{"retry-after": "31"}), ok()])
    with pytest.raises(FetchError, match="The board asked us to wait; try again later."):
        run(GH, script)
    assert len(script.calls) == 1 and clock.sleeps == []


def test_retry_after_date_over_thirty_seconds_stops(run) -> None:
    when = format_datetime(NOW + timedelta(seconds=300), usegmt=True)
    with pytest.raises(FetchError, match="asked us to wait"):
        run(GH, Script([resp(503, **{"retry-after": when})]))


@pytest.mark.parametrize("digits", [400, 5000])
def test_retry_after_of_enormous_digits_stops_with_the_plain_message(run, clock: Clock, digits: int) -> None:
    # float(int(text)) used to raise OverflowError or ValueError out of fetch().
    script = Script([resp(429, **{"retry-after": "9" * digits})])
    with pytest.raises(FetchError, match="The board asked us to wait; try again later."):
        run(GH, script)
    assert len(script.calls) == 1 and clock.sleeps == []


def test_retry_after_http_date_with_an_extreme_offset_falls_back_to_backoff(
    run, clock: Clock, monkeypatch
) -> None:
    # Some Python builds raise OverflowError (others ValueError) for an absurd UTC offset; the
    # parser is stubbed so the test does not depend on the interpreter's date handling.
    def overflow(_text: str):
        raise OverflowError("date value out of range")

    monkeypatch.setattr(fetch, "parsedate_to_datetime", overflow)
    run(GH, Script([resp(503, **{"retry-after": "Thu, 12 Nov 2026 12:00:00 +9999"}), ok()]))
    assert clock.sleeps == [1]


def test_retry_cap_is_three_attempts_then_an_error_naming_the_status(run) -> None:
    script = Script([resp(500), resp(500), resp(500), resp(500)])
    with pytest.raises(FetchError, match="500"):
        run(GH, script)
    assert len(script.calls) == 3
    assert len(script.items) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 501])
def test_other_statuses_are_never_retried(run, clock: Clock, status: int) -> None:
    script = Script([resp(status, b"x"), ok()])
    result = run(GH, script)
    assert result.status == status and len(script.calls) == 1
    assert clock.sleeps == []


# ---------------------------------------------------------------- size cap


def test_size_cap_constant_is_twenty_five_megabytes() -> None:
    assert fetch.MAX_BODY_BYTES == 25 * 1024 * 1024


def test_injected_transport_oversize_body_is_rejected(run, monkeypatch) -> None:
    monkeypatch.setattr(fetch, "MAX_BODY_BYTES", 100)
    with pytest.raises(FetchError, match="The response was too large to accept."):
        run(GH, Script([ok(b"x" * 101)]))
    assert list(run.cache_dir.rglob("*.json.gz")) == []


def test_body_exactly_at_the_cap_is_accepted(run, monkeypatch) -> None:
    monkeypatch.setattr(fetch, "MAX_BODY_BYTES", 100)
    assert run(GH, Script([ok(b"x" * 100)])).status == 200


class FakeResponse:
    def __init__(self, status: int = 200, headers=None, chunks=(b"{}",)) -> None:
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self._chunks = chunks
        self.closed = False
        self.chunk_sizes: list[int] = []
        self.consumed = 0

    def iter_content(self, chunk_size: int = 1):
        self.chunk_sizes.append(chunk_size)
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk

    @property
    def content(self):  # pragma: no cover - the default transport must stream instead
        raise AssertionError("the default transport must stream with iter_content")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def session_get(monkeypatch):
    seen: list[dict] = []
    box: dict = {"response": FakeResponse(), "raises": None}

    def fake_get(self, url, **kwargs):
        seen.append({"url": url, **kwargs})
        if box["raises"] is not None:
            raise box["raises"]
        return box["response"]

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return seen, box


def _live_fetch(tmp_path: Path, clock: Clock, url: str = GH):
    return fetch.fetch(
        url, purpose=Purpose.ATS_API, cache_dir=tmp_path / "cache", clock=clock,
        sleep=clock.sleep, now=lambda: NOW,
    )


def test_default_transport_aborts_a_streamed_body_over_the_cap(
    session_get, tmp_path: Path, clock: Clock, monkeypatch
) -> None:
    seen, box = session_get
    monkeypatch.setattr(fetch, "MAX_BODY_BYTES", 1000)
    box["response"] = FakeResponse(chunks=[b"x" * 400] * 10)
    with pytest.raises(FetchError, match="The response was too large to accept."):
        _live_fetch(tmp_path, clock)
    assert box["response"].closed is True
    assert box["response"].consumed < 10  # it stopped reading, it did not drain the stream


class DripResponse(FakeResponse):
    """A slow server: every chunk arrives `gap` seconds after the last, on the fake clock."""

    def __init__(self, clock: Clock, gap: float, count: int) -> None:
        super().__init__(chunks=[b"x"] * count)
        self._clock, self._gap = clock, gap

    def iter_content(self, chunk_size: int = 1):
        for chunk in super().iter_content(chunk_size):
            self._clock.sleep(self._gap)  # advances the shared fake clock
            yield chunk


def test_default_transport_gives_up_on_a_drip_fed_body_after_the_total_deadline(
    session_get, tmp_path: Path, clock: Clock
) -> None:
    # A per-read timeout alone lets a server send one byte every 29 seconds forever.
    _seen, box = session_get
    box["response"] = DripResponse(clock, gap=29.0, count=50)
    with pytest.raises(FetchError, match="took too long"):
        _live_fetch(tmp_path, clock)
    assert box["response"].closed is True
    assert box["response"].consumed == 3  # 29, 58, 87 seconds: stopped at the first chunk past 60 s


def test_default_transport_accepts_a_slow_body_that_finishes_inside_the_deadline(
    session_get, tmp_path: Path, clock: Clock
) -> None:
    _seen, box = session_get
    box["response"] = DripResponse(clock, gap=10.0, count=5)
    assert _live_fetch(tmp_path, clock).body == b"xxxxx"


def test_default_transport_streams_with_a_64k_chunk_size(session_get, tmp_path: Path,
                                                         clock: Clock) -> None:
    _seen, box = session_get
    box["response"] = FakeResponse(chunks=[b'{"a":', b' 1}'])
    result = _live_fetch(tmp_path, clock)
    assert result.body == b'{"a": 1}' and result.status == 200
    assert box["response"].chunk_sizes == [65536]
    assert box["response"].closed is True


def test_default_transport_request_shape(session_get, tmp_path: Path, clock: Clock) -> None:
    seen, _box = session_get
    _live_fetch(tmp_path, clock)
    (call,) = seen
    assert call["url"] == GH
    assert call["allow_redirects"] is False
    assert call["stream"] is True
    assert call["timeout"] == (5, 30)
    assert set(call["headers"]) == {"User-Agent", "Accept"}
    assert call["headers"]["User-Agent"] == fetch.USER_AGENT


def test_default_transport_session_ignores_the_environment(
    session_get, tmp_path: Path, clock: Clock
) -> None:
    # trust_env=True would read ~/.netrc, proxy variables and a CA bundle variable.
    seen, _box = session_get
    sessions: list[requests.Session] = []
    original = requests.Session.get

    def spy(self, url, **kwargs):
        sessions.append(self)
        return original(self, url, **kwargs)

    requests.Session.get = spy  # type: ignore[method-assign]
    try:
        _live_fetch(tmp_path, clock)
    finally:
        requests.Session.get = original  # type: ignore[method-assign]
    assert [s.trust_env for s in sessions] == [False]


def test_default_transport_prepared_request_has_no_credentials_proxy_or_env_ca_bundle(
    tmp_path: Path, clock: Clock, monkeypatch
) -> None:
    # Check what the adapter finally receives, not only the headers handed in.
    netrc = tmp_path / "netrc"
    netrc.write_text("machine boards-api.greenhouse.io login fakeuser password fakepass\n",
                     encoding="utf-8")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "ca.pem"))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "ca.pem"))
    sent: list[tuple[requests.PreparedRequest, dict]] = []

    def fake_send(self, request, **kwargs):
        sent.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = b"{}"
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    _live_fetch(tmp_path, clock)
    (request, kwargs), = sent
    names = {name.lower() for name in request.headers}
    assert not names & {"authorization", "proxy-authorization", "cookie"}
    assert request.headers["User-Agent"] == fetch.USER_AGENT
    assert not kwargs.get("proxies")
    assert kwargs.get("verify") is True  # not the path from REQUESTS_CA_BUNDLE


def test_default_transport_failure_never_leaks_the_underlying_message(
    session_get, tmp_path: Path, clock: Clock
) -> None:
    _seen, box = session_get
    box["raises"] = requests.ConnectionError("HTTPSConnectionPool(host='secret', port=443)")
    with pytest.raises(FetchError) as info:
        _live_fetch(tmp_path, clock)
    assert str(info.value).startswith("could not reach boards-api.greenhouse.io")
    assert "secret" not in str(info.value) and "Traceback" not in str(info.value)


def test_user_agent_shape_has_no_identity() -> None:
    pattern = r"job-search-prioritizer/\d+\.\d+\.\d+ \(\+https://github\.com/adamNBurke/job-search-prioritizer\)"
    assert re.fullmatch(pattern, fetch.USER_AGENT)
    assert "@" not in fetch.USER_AGENT


def test_request_headers_carry_only_user_agent_and_accept(run) -> None:
    script = Script([ok()])
    run(GH, script)
    assert set(script.headers[0]) == {"User-Agent", "Accept"}
    assert script.headers[0]["User-Agent"] == fetch.USER_AGENT


# ---------------------------------------------------------------- private-address vetting

USER_URL = "https://careers.example.com/job/1"
PRIVATE_MESSAGE = "this computer or a private network"


def resolves_to(*addresses: str, seen: list[str] | None = None):
    def resolver(host: str) -> list[str]:
        if seen is not None:
            seen.append(host)
        return list(addresses)

    return resolver


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "127.0.0.1", "192.168.1.20", "172.16.0.9", "169.254.169.254", "100.64.0.1",
     "0.0.0.0", "::1", "fe80::1", "fe80::1%en0", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.5"],
)
def test_user_url_that_resolves_to_a_non_global_address_is_refused_before_any_request(
    run, address: str
) -> None:
    script = Script([ok()])
    with pytest.raises(UrlRefused, match=PRIVATE_MESSAGE):
        run(USER_URL, script, purpose=Purpose.USER_URL, resolver=resolves_to(address))
    assert script.calls == []


def test_one_private_answer_among_public_ones_refuses_the_host(run) -> None:
    script = Script([ok()])
    with pytest.raises(UrlRefused, match=PRIVATE_MESSAGE):
        run(USER_URL, script, purpose=Purpose.USER_URL,
            resolver=resolves_to("93.184.216.34", "10.0.0.5"))
    assert script.calls == []


def test_user_url_that_resolves_to_public_addresses_is_fetched(run) -> None:
    seen: list[str] = []
    script = Script([ok()])
    result = run(USER_URL, script, purpose=Purpose.USER_URL,
                 resolver=resolves_to("93.184.216.34", "2606:2800:220:1::1", seen=seen))
    assert result.status == 200 and script.calls == [USER_URL]
    assert seen == ["careers.example.com"]


def test_every_redirect_hop_is_resolved_and_vetted(run) -> None:
    answers = {"careers.example.com": ["93.184.216.34"], "rebind.example.net": ["10.0.0.5"]}
    asked: list[str] = []

    def resolver(host: str) -> list[str]:
        asked.append(host)
        return answers[host]

    script = Script([resp(302, location="https://rebind.example.net/x"), ok()])
    with pytest.raises(UrlRefused, match=PRIVATE_MESSAGE):
        run(USER_URL, script, purpose=Purpose.USER_URL, resolver=resolver)
    assert asked == ["careers.example.com", "rebind.example.net"]
    assert script.calls == [USER_URL]  # the private hop was never requested


def test_ats_api_fetches_never_resolve_names(run) -> None:
    seen: list[str] = []
    run(GH, Script([ok()]), resolver=resolves_to("10.0.0.5", seen=seen))
    assert seen == []


def test_name_lookup_failure_is_a_plain_could_not_reach_error(run) -> None:
    def broken(host: str) -> list[str]:
        raise TransportError("name lookup failed")

    script = Script([ok()])
    with pytest.raises(FetchError, match="could not reach careers.example.com"):
        run(USER_URL, script, purpose=Purpose.USER_URL, resolver=broken)
    assert script.calls == []


def test_empty_lookup_answer_is_treated_as_unreachable(run) -> None:
    with pytest.raises(FetchError, match="could not reach"):
        run(USER_URL, Script([ok()]), purpose=Purpose.USER_URL, resolver=resolves_to())


def test_system_resolver_reads_addresses_from_getaddrinfo(monkeypatch) -> None:
    import socket

    monkeypatch.undo()  # the suite-wide stub is replaced by the real function under test
    calls: list[tuple] = []

    def fake_getaddrinfo(host, port, **kwargs):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", 443, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert fetch._system_resolve("careers.example.com") == ["93.184.216.34", "2606:2800:220:1::1"]
    assert calls == [("careers.example.com", 443)]

    def failing(host, port, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", failing)
    with pytest.raises(TransportError):
        fetch._system_resolve("nowhere.example.com")


# ---------------------------------------------------------------- offline


def test_offline_with_a_cached_copy_returns_it_flagged_stale(run) -> None:
    script = Script([ok(b'{"old": true}'), TransportError("down")])
    run(GH, script)
    run.state["now"] = NOW + timedelta(days=30)  # long past any TTL
    result = run(GH, script)
    assert result.from_cache is True and result.stale is True
    assert result.body == b'{"old": true}' and result.status == 200


def test_offline_with_refresh_still_falls_back_to_the_cached_copy(run) -> None:
    script = Script([ok(b'{"old": true}'), TransportError("down")])
    run(GH, script)
    result = run(GH, script, refresh=True)
    assert result.stale is True and result.from_cache is True


def test_offline_without_a_cached_copy_is_a_plain_english_error(run) -> None:
    with pytest.raises(FetchError) as info:
        run(GH, Script([TransportError("down")]))
    assert str(info.value) == (
        "could not reach boards-api.greenhouse.io; check your internet connection."
    )


def test_offline_with_a_corrupt_cache_file_is_still_a_plain_error(run) -> None:
    script = Script([ok(), TransportError("down")])
    run(GH, script)
    (path,) = list(run.cache_dir.rglob("*.json.gz"))
    path.write_bytes(b"junk")
    run.state["now"] = NOW + timedelta(days=30)
    with pytest.raises(FetchError, match="^could not reach "):
        run(GH, script)


def test_errors_never_contain_the_query_string(run) -> None:
    messages = []
    for script in (
        Script([TransportError("down")]),
        Script([resp(500)] * 3),
        Script([resp(429, **{"retry-after": "99"})]),
    ):
        fetch._reset_pacing()
        with pytest.raises(FetchError) as info:
            run(GH, script)
        messages.append(str(info.value))
    assert all("?" not in m and "content=true" not in m for m in messages)


# ---------------------------------------------------------------- redirects


def test_redirect_to_a_denied_host_is_refused_before_it_is_requested(run) -> None:
    lever_url = "https://api.lever.co/v0/postings/acme"
    script = Script([
        resp(302, location=lever_url),
        resp(302, location="https://www.linkedin.com/jobs/view/1"),
        ok(),
    ])
    with pytest.raises(UrlRefused, match="LinkedIn"):
        run(GH, script)
    assert script.calls == [GH, lever_url]


def test_redirect_to_a_non_allowlisted_host_is_refused_for_ats_api(run) -> None:
    script = Script([resp(301, location="https://example.com/x"), ok()])
    with pytest.raises(UrlRefused, match="approved job-board host"):
        run(GH, script)
    assert script.calls == [GH]


def test_scheme_relative_redirect_to_linkedin_is_refused(run) -> None:
    script = Script([resp(302, location="//linkedin.com/x"), ok()])
    with pytest.raises(UrlRefused):
        run(GH, script)
    assert len(script.calls) == 1


def test_redirect_downgrade_to_http_is_refused(run) -> None:
    script = Script([resp(302, location="http://boards-api.greenhouse.io/x"), ok()])
    with pytest.raises(UrlRefused):
        run(GH, script)
    assert len(script.calls) == 1


def test_four_redirects_is_too_many(run) -> None:
    hop = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    script = Script([resp(302, location=hop)] * 4 + [ok()])
    with pytest.raises(FetchError, match="too many redirects"):
        run(GH, script)


def test_redirect_without_a_location_is_a_plain_error(run) -> None:
    with pytest.raises(FetchError, match="redirected"):
        run(GH, Script([resp(302)]))


def test_a_followed_redirect_returns_the_final_body(run) -> None:
    target = "https://api.lever.co/v0/postings/acme?mode=json"
    script = Script([resp(302, location=target), ok(b'{"moved": true}')])
    result = run(GH, script)
    assert result.body == b'{"moved": true}'
    assert run(GH, script).from_cache is True  # cached under the URL that was asked for


# ---------------------------------------------------------------- decoding


def _result(body: bytes) -> fetch.FetchResult:
    return fetch.FetchResult(200, {}, body, False, "2026-11-12T12:00:00Z")


def test_decode_json_reads_utf8_explicitly() -> None:
    text = "caf" + chr(0xE9) + " " + chr(0x4E2D)
    payload = json.dumps({"t": text}, ensure_ascii=False).encode("utf-8")
    assert fetch.decode_json(_result(payload)) == {"t": text}


@pytest.mark.parametrize(
    "body",
    [b"\xff\xfe\x00bad", b"Not Found", b"", b"{", b"[" * 200000],
    ids=["not_utf8", "plain_text", "empty", "truncated", "deeply_nested"],
)
def test_decode_json_failures_are_fetch_errors(body: bytes) -> None:
    with pytest.raises(FetchError):
        fetch.decode_json(_result(body))


def test_default_now_reads_the_program_clock(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    """Without `now`, fetched_at must come from jobsearch.clock, so a frozen test clock holds.

    Regression: fetch fell back to datetime.now() directly, so two scans a second apart wrote
    different fetched_at bytes and the byte-identical scan test flaked about 1 run in 13.
    """
    from jobsearch import clock as program_clock

    monkeypatch.setattr(program_clock, "now_utc", lambda: NOW)
    result = fetch.fetch(GH, purpose=Purpose.ATS_API, cache_dir=tmp_path / "cache",
                         transport=Script([ok()]), clock=clock, sleep=clock.sleep)
    assert result.fetched_at == "2026-11-12T12:00:00Z"


def test_accept_header_follows_the_purpose(run) -> None:
    """An ATS API asks for JSON; a posting page asks for HTML, never JSON."""
    ats = Script([ok()])
    run(GH, ats)
    assert ats.headers[0]["Accept"] == "application/json"

    page = Script([ok(b"<html></html>")])
    run(USER_URL, page, purpose=Purpose.USER_URL, resolver=resolves_to("93.184.216.34"))
    assert page.headers[0]["Accept"].startswith("text/html")
    assert "application/json" not in page.headers[0]["Accept"]
    assert set(page.headers[0]) == {"User-Agent", "Accept"}
    assert page.headers[0]["User-Agent"] == fetch.USER_AGENT
