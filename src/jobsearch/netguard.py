"""Deny-first URL policy. A pure module: it never touches the network.

Every URL passes check_url before any cache or network access, and again on every
redirect hop. A host that cannot be read cleanly is denied (fail closed). This module only
reads names: it refuses IP literals and local-looking names. Names that merely resolve to a
private address are caught in fetch.py, the one module allowed to look names up.

Three purposes exist: ATS_API (the fixed job-board hosts), USER_URL (a link the user pastes) and
UPDATE_CHECK (the once-a-day release check, which may reach api.github.com and nothing else).
"""

from __future__ import annotations

import enum
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

DENIED_DOMAINS = ("linkedin.com", "licdn.com", "lnkd.in")
ATS_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "lever_eu": "api.eu.lever.co",
    "ashby": "api.ashbyhq.com",
}

UPDATE_HOST = "api.github.com"

_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}")
# A readable host is letters, digits, hyphens, underscores and dots, or an IPv6 literal.
# Anything else (a percent sign, for one) is refused: the stdlib idna codec lets it through.
_PLAIN_HOST = re.compile(r"[a-z0-9_.-]+")
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".corp")
PRIVATE_ADDRESS_MESSAGE = "Addresses on this computer or a private network are not contacted."


class Purpose(enum.Enum):
    ATS_API = "ats_api"
    USER_URL = "user_url"
    UPDATE_CHECK = "update_check"


class UrlRefused(Exception):
    """Message is plain English and safe to show the user."""


def host_of(raw: str) -> str | None:
    """Normalized ASCII host of a URL or bare host string, or None when it cannot be read."""
    text = raw.strip()
    if not text or any(ch in text for ch in "\\\x00\t\r\n "):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    if "://" not in text and not text.startswith("//"):
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = unicodedata.normalize("NFKC", host).lower()
    if host.endswith("."):
        host = host[:-1]
    # An empty label anywhere else ("linkedin.com..", ".x", "a..b") is unreadable: fail closed.
    if not host or host.startswith(".") or host.endswith(".") or ".." in host:
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in ascii_host:  # an IPv6 literal (urlsplit already stripped the brackets)
        try:
            ipaddress.IPv6Address(ascii_host)
        except ValueError:
            return None
        return ascii_host
    return ascii_host if _PLAIN_HOST.fullmatch(ascii_host) else None


def is_denied(raw: str) -> bool:
    host = host_of(raw)
    if host is None:
        return True
    return any(host == d or host.endswith("." + d) for d in DENIED_DOMAINS)


def _is_ip_like(host: str) -> bool:
    """True for IP literals and for the decimal, octal, hex and short forms browsers accept."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return all(_NUMERIC_LABEL.fullmatch(label) for label in host.split("."))
    return True


def check_url(raw: str, purpose: Purpose) -> str:
    """Return the normalized host, or raise UrlRefused."""
    host = host_of(raw)
    if host is None:
        raise UrlRefused("That web address could not be read, so it was not contacted.")
    if is_denied(raw):
        raise UrlRefused("LinkedIn is never contacted by this tool.")

    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        raise UrlRefused("That web address could not be read, so it was not contacted.") from None
    if parts.scheme.lower() != "https":
        raise UrlRefused("Only https web addresses are contacted.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRefused("Web addresses that carry a user name or password are not contacted.")
    if port is not None and port != 443:
        raise UrlRefused("Only the standard https port is contacted.")

    if purpose is Purpose.ATS_API:
        if host not in ATS_HOSTS.values():
            raise UrlRefused("That host is not an approved job-board host.")
    elif purpose is Purpose.UPDATE_CHECK and host != UPDATE_HOST:
        raise UrlRefused("That host is not the release-check host.")
    elif purpose is Purpose.USER_URL and (
        _is_ip_like(host) or host == "localhost" or host.endswith(_LOCAL_SUFFIXES)
    ):
        raise UrlRefused(PRIVATE_ADDRESS_MESSAGE)
    return host


def board_url(ats: str, slug: str, *, lean: bool = False) -> str:
    """The list URL for one board. lean drops the rich-content flags (Greenhouse content,
    Ashby compensation) so a verification request stays small; the scan never sets it."""
    if ats not in ATS_HOSTS:
        raise UrlRefused("That job-board system is not supported.")
    if not _SLUG.fullmatch(slug) or slug in (".", ".."):
        raise UrlRefused("That board name has characters that are not allowed.")
    if ats == "greenhouse":
        query = "" if lean else "?content=true"
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs{query}"
    if ats == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    if ats == "lever_eu":
        return f"https://api.eu.lever.co/v0/postings/{slug}?mode=json"
    query = "" if lean else "?includeCompensation=true"
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}{query}"
