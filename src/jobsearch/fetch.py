"""The only network module. Anonymous, read-only GET requests, nothing else.

Purpose: one polite, bounded, auditable path to the network for the whole tool.
Logic: every URL passes netguard.check_url before the cache or the network is touched, and
again on every redirect hop. Requests to one host run one at a time, each starting at least
MIN_INTERVAL_SECONDS after the previous one finished. 429 and 5xx answers are retried up to three attempts in all,
honoring Retry-After up to 30 seconds. A body over MAX_BODY_BYTES is refused. 200 and 404
answers are cached gzip-compressed; when the network is down, the cached copy is returned
flagged stale.
A USER_URL host is also resolved before every request and on every redirect hop, and is
refused when any address it resolves to is not globally routable.
Guardrails: GET only, no cookies, headers limited to User-Agent and Accept (no identity; the
Accept value is JSON for an ATS API and HTML for a user-supplied page address),
and the session ignores the environment (trust_env off): no ~/.netrc credentials, no proxy
variables, no CA bundle variable. Proxy support is not a v1 feature. No full URL with a query
string in any message, no traceback for an expected failure.
QA checklist: tests/test_fetch.py scripts every branch with an injected transport and clock.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import ipaddress
import json
import socket
import threading
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests

from jobsearch import clock as program_clock
from jobsearch import fsio
from jobsearch.netguard import PRIVATE_ADDRESS_MESSAGE, Purpose, UrlRefused, check_url
from jobsearch.paths import plugin_version

USER_AGENT = (
    f"job-search-prioritizer/{plugin_version()} "
    "(+https://github.com/adamNBurke/job-search-prioritizer)"
)
MIN_INTERVAL_SECONDS = 1.0
TTL_OK_SECONDS = 6 * 3600
TTL_NOT_FOUND_SECONDS = 3600

MAX_BODY_BYTES = 25 * 1024 * 1024

_MAX_REDIRECTS = 3
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 30
_CHUNK_BYTES = 65536
_TOO_LARGE = "The response was too large to accept."
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CACHED_HEADERS = ("content-type",)
_ATS_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
_PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
}
_TIMEOUT = (5, 30)
# _TIMEOUT is per read; this bounds the whole request, so a server that drips one byte every
# 29 seconds cannot hold the command (and the per-host lock) forever.
_TOTAL_DEADLINE_SECONDS = 60.0
_TOO_SLOW = "The board took too long to send its answer; try again later."


class FetchError(Exception):
    """Message is plain English and safe to show the user."""


UNKNOWN_SHAPE = "The board returned data in a shape this tool does not recognize."


class TransportError(OSError):
    """The request could not be completed (DNS, connection, timeout)."""


@dataclass
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str]], TransportResponse]
Resolver = Callable[[str], Sequence[str]]  # host name -> address strings; injectable for tests


@dataclass(frozen=True)
class FetchResult:
    status: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool
    fetched_at: str
    stale: bool = False


_pace_guard = threading.Lock()
_pace_locks: dict[str, threading.Lock] = {}
_pace_last: dict[str, float] = {}


def _reset_pacing() -> None:
    """Forget per-host timing. Tests use this so injected clocks do not leak between cases."""
    with _pace_guard:
        _pace_locks.clear()
        _pace_last.clear()


def _host_lock(host: str) -> threading.Lock:
    with _pace_guard:
        return _pace_locks.setdefault(host, threading.Lock())


def _wait_turn(host: str, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    """Sleep until MIN_INTERVAL_SECONDS have passed since the last request to this host ended.

    The caller holds the host lock, so per-host concurrency is one, and records the end time
    in _pace_last when the request finishes.
    """
    last = _pace_last.get(host)
    if last is not None:
        wait = MIN_INTERVAL_SECONDS - (clock() - last)
        if wait > 0:
            sleep(wait)


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
) -> TransportResponse:
    """One anonymous GET on a fresh session: no cookies or credentials survive the call.

    The body is streamed and abandoned the moment it passes MAX_BODY_BYTES (decoded size,
    so a compressed bomb is caught too) or the request passes _TOTAL_DEADLINE_SECONDS.
    """
    deadline = clock() + _TOTAL_DEADLINE_SECONDS
    try:
        with requests.Session() as session:
            session.trust_env = False  # no netrc auth, no env proxies, no env CA bundle
            response = session.get(
                url,
                headers=dict(headers),
                timeout=_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            try:
                body = bytearray()
                for chunk in response.iter_content(_CHUNK_BYTES):
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise FetchError(_TOO_LARGE)
                    body.extend(chunk)
                    if clock() > deadline:
                        raise FetchError(_TOO_SLOW)
            finally:
                response.close()
            return TransportResponse(
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=bytes(body),
            )
    except requests.RequestException as exc:
        raise TransportError(f"request failed: {type(exc).__name__}") from None


def _system_resolve(host: str) -> list[str]:
    """Every address the operating system returns for `host`, as strings."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):  # socket.gaierror is an OSError
        raise TransportError("name lookup failed") from None
    return [str(info[4][0]) for info in infos]


def _require_public_addresses(host: str, resolve: Resolver) -> None:
    """Refuse a name unless every address it resolves to is globally routable.

    Residual risk, not closed here: this lookup and the one the HTTP library makes when it
    connects are separate, so a name whose DNS answer changes between the two (DNS rebinding)
    can still slip through. Closing that needs the connection pinned to the vetted address,
    which is deferred.
    """
    addresses = list(resolve(host))
    if not addresses:
        raise TransportError("name lookup returned no address")
    for text in addresses:
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone id
        except ValueError:
            raise UrlRefused(PRIVATE_ADDRESS_MESSAGE) from None  # unreadable: fail closed
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            raise UrlRefused(PRIVATE_ADDRESS_MESSAGE)


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / digest[:2] / f"{digest}.json.gz"


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _read_cache(path: Path) -> dict[str, object] | None:
    try:
        entry = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, EOFError, zlib.error):
        return None
    if not isinstance(entry, dict) or entry.get("schema_version") != 1:
        return None
    return entry


def _write_cache(path: Path, url: str, status: int, headers: dict[str, str],
                 fetched_at: str, body: bytes) -> None:
    entry = {
        "schema_version": 1,
        "url": url,
        "status": status,
        "headers": {k: v for k, v in headers.items() if k in _CACHED_HEADERS},
        "fetched_at": fetched_at,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }
    payload = json.dumps(entry, ensure_ascii=True, sort_keys=True).encode("utf-8")
    fsio.write_bytes_atomic(path, gzip.compress(payload))


def _hit(
    entry: dict[str, object], now: datetime | None, *, stale: bool = False
) -> FetchResult | None:
    """Build a cached FetchResult. With `now` given the entry must still be inside its TTL."""
    status = entry.get("status")
    fetched_at = entry.get("fetched_at")
    body_b64 = entry.get("body_b64")
    headers = entry.get("headers")
    if (
        not isinstance(status, int)
        or not isinstance(fetched_at, str)
        or not isinstance(body_b64, str)
        or not isinstance(headers, dict)
    ):
        return None
    ttl = {200: TTL_OK_SECONDS, 404: TTL_NOT_FOUND_SECONDS}.get(status)
    if ttl is None:
        return None
    try:
        age = (now - _parse_time(fetched_at)).total_seconds() if now is not None else 0.0
        body = base64.b64decode(body_b64)
    except ValueError:
        return None
    if now is not None and (age < 0 or age >= ttl):
        return None
    return FetchResult(
        status=status,
        headers={str(k): str(v) for k, v in headers.items()},
        body=body,
        from_cache=True,
        fetched_at=fetched_at,
        stale=stale,
    )


def _parse_retry_after(raw: str, now: datetime) -> float | None:
    """Seconds to wait from a Retry-After value (delay seconds or HTTP-date), or None."""
    text = raw.strip()
    if text.isascii() and text.isdigit():
        # Anything over six digits is far past the cap; never feed a huge string to int().
        return float(_MAX_RETRY_AFTER_SECONDS + 1) if len(text) > 6 else float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - now).total_seconds())


def _retry_delay(headers: Mapping[str, str], attempt: int, now: datetime) -> float:
    raw = headers.get("retry-after")
    if raw is not None:
        seconds = _parse_retry_after(raw, now)
        if seconds is not None:
            if seconds > _MAX_RETRY_AFTER_SECONDS:
                raise FetchError("The board asked us to wait; try again later.")
            return seconds
    return float(1 << (attempt - 1))  # 1 s, then 2 s


def _request(
    send: Transport,
    target: str,
    host: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
    vet: Callable[[str], None] | None = None,
    headers: Mapping[str, str] = _ATS_HEADERS,
) -> TransportResponse:
    """One request with bounded retries, paced and serialized per host."""
    with _host_lock(host):
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if vet is not None:
                vet(host)  # before every attempt, not only the first
            _wait_turn(host, clock, sleep)
            try:
                raw = send(target, headers)
            finally:
                _pace_last[host] = clock()  # spacing runs from completion, even on failure
            if len(raw.body) > MAX_BODY_BYTES:
                raise FetchError(_TOO_LARGE)
            response = TransportResponse(
                raw.status, {k.lower(): v for k, v in raw.headers.items()}, raw.body
            )
            if response.status not in _RETRYABLE_STATUSES:
                return response
            delay = _retry_delay(response.headers, attempt, now())
            if attempt == _MAX_ATTEMPTS:
                raise FetchError(
                    f"{host} kept answering with HTTP {response.status}; try again later."
                )
            if delay > 0:
                sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def fetch(
    url: str,
    *,
    purpose: Purpose,
    cache_dir: Path,
    refresh: bool = False,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
    resolver: Resolver | None = None,
) -> FetchResult:
    host = check_url(url, purpose)  # before the cache and the network: a refusal costs nothing
    current_time = now if now is not None else program_clock.now_utc
    path = _cache_path(cache_dir, url)

    if not refresh:
        entry = _read_cache(path)
        if entry is not None:
            hit = _hit(entry, current_time())
            if hit is not None:
                return hit

    send = transport if transport is not None else partial(_default_transport, clock=clock)
    vet: Callable[[str], None] | None = None
    if purpose is Purpose.USER_URL:  # ATS hosts are fixed names, so only free-form URLs are resolved
        resolve = resolver if resolver is not None else _system_resolve
        vet = lambda name: _require_public_addresses(name, resolve)  # noqa: E731
    headers = _PAGE_HEADERS if purpose is Purpose.USER_URL else _ATS_HEADERS
    target = url
    try:
        for hop in range(_MAX_REDIRECTS + 1):
            response = _request(send, target, host, clock, sleep, current_time, vet, headers)
            if response.status not in _REDIRECT_STATUSES:
                break
            location = response.headers.get("location")
            if not location:
                raise FetchError("The board redirected without saying where.")
            if hop == _MAX_REDIRECTS:
                raise FetchError("The board sent too many redirects.")
            target = urljoin(target, location)
            host = check_url(target, purpose)  # every hop is checked before it is requested
        else:  # pragma: no cover - the loop always breaks or raises
            raise FetchError("The board sent too many redirects.")
    except TransportError:
        entry = _read_cache(path)
        stale = _hit(entry, None, stale=True) if entry is not None else None
        if stale is not None:
            return stale
        raise FetchError(f"could not reach {host}; check your internet connection.") from None

    fetched_at = current_time().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if response.status in (200, 404):
        try:
            _write_cache(path, url, response.status, response.headers, fetched_at, response.body)
        except OSError:
            pass  # the cache is a convenience; an unwritable folder must not fail the fetch
    return FetchResult(
        status=response.status,
        headers={k: v for k, v in response.headers.items() if k in _CACHED_HEADERS},
        body=response.body,
        from_cache=False,
        fetched_at=fetched_at,
    )


def decode_json(result: FetchResult) -> object:
    try:
        return json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise FetchError("the board returned data that is not valid JSON") from None
