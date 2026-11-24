"""Turn a job description's HTML into plain text, read pay out of text, and normalize dates.

Purpose: Greenhouse sends descriptions as HTML that is entity-escaped, and the scorer and the
skill read plain text, never markup. Greenhouse also states pay only as text inside the
description, so parse_pay_text reads an amount range from a short pay string.
Logic: when unescape_first, html.unescape runs once to turn the escaped markup into real HTML.
The stdlib html.parser then drops tags (block tags become line breaks, script and style bodies
are dropped), html.unescape runs again for the entities that were escaped twice (live boards
still carry &amp; and &nbsp; after the first pass), and runs of spaces and blank lines collapse.
parse_pay_text finds the first amount range ("$139,200 - $235,200 USD", "EUR 60.000 - 75.000",
"$90k to $120k"), reads the currency from a known ISO code first and then from a symbol, and the
interval from words such as "per hour". iso_to_utc_z converts one ISO 8601 string, whatever its
offset, to UTC with a Z suffix.
Guardrails: nothing is executed or fetched; the parser only collects text. Output is plain
text, so a later writer must still escape it for its own format. Every pattern is linear time:
no quantifier is nested inside another, and the amount grammar uses fixed-size groups, so a
hostile 100 KB string cannot stall a scan. The input is capped before matching.
same_currency_ranges keeps only the pay ranges that share the first stated currency, so a posting
that lists a USD and a CAD range never mixes them into one min-max pair. pick_ranges is that
merge plus its two warnings, pay_fields turns parsed ranges into the five comp fields, NO_PAY is
the no-pay answer, and split_locations splits a location on | and ; for every source.
QA checklist: a new board adapter reuses these functions; do not write a second HTML stripper,
pay parser or date converter.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, TypeVar

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "hr", "section", "article", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
})
_SKIP_TAGS = frozenset({"script", "style"})
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag != "li":  # a list item breaks once, at its start
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str, *, unescape_first: bool) -> str:
    text = html.unescape(raw) if unescape_first else raw
    collector = _Collector()
    collector.feed(text)
    collector.close()
    text = html.unescape("".join(collector.parts))
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = _BLANK_RUNS.sub("\n\n", "\n".join(lines))
    return text.strip()


def clean_text(value: object) -> str | None:
    """A stripped non-empty string, else None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def iso_to_utc_z(value: object) -> str | None:
    """One ISO 8601 string (any offset, naive means UTC) as "YYYY-MM-DDTHH:MM:SSZ", else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PayParse:
    min: float
    max: float
    currency: str | None
    interval: str | None


_PAY_TEXT_LIMIT = 2000
_THOUSANDS_SEPARATORS = ",.\u2009\u202f\u00a0"
_AMOUNT = (
    r"(?:\d{1,3}(?:[,.\u2009\u202f\u00a0]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)"
)
_RANGE = re.compile(
    rf"(?P<a>{_AMOUNT})\s?(?P<ak>[kK])?(?:\s?(?:[A-Za-z]{{3}}|[$\u20ac\u00a3]))?"
    rf"\s?(?:-|\u2013|\u2014|to)\s?(?:[A-Za-z]{{3}}\s?|[$\u20ac\u00a3]\s?)?"
    rf"(?P<b>{_AMOUNT})\s?(?P<bk>[kK])?"
)
_SINGLE = re.compile(rf"(?:[$\u20ac\u00a3]\s?)(?P<a>{_AMOUNT})\s?(?P<ak>[kK])?")
_CODES = (
    "USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK", "JPY", "INR",
    "SGD", "HKD", "MXN", "BRL", "PLN", "CZK", "ZAR", "ILS", "AED", "CNY", "KRW",
)
_CODE = re.compile(r"\b(" + "|".join(_CODES) + r")\b")
_SYMBOLS = (("$", "USD"), ("\u20ac", "EUR"), ("\u00a3", "GBP"))
_INTERVAL_WORDS = (
    ("hour", re.compile(r"per hour|hourly|/\s?hr\b|/\s?hour|an hour|per hr\b")),
    ("year", re.compile(r"per year|annual|/\s?yr\b|/\s?year|a year|per annum|yearly")),
    ("month", re.compile(r"per month|monthly|/\s?mo\b|a month")),
)


def finite_number(value: object) -> float | None:
    """A board's number as a float; None for a non-number, a bool, or one that is infinite, NaN
    or too large for a float, so a hostile pay figure reads as no pay."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _to_number(raw: str, thousands_k: bool) -> float | None:
    """Digits with "," "." or thin-space groups; a trailing 1-2 digit group is the decimals."""
    digits = raw
    decimals = ""
    for index in range(len(raw) - 1, -1, -1):
        char = raw[index]
        if char in ",." and index >= 1:
            tail = raw[index + 1:]
            before = raw[:index]
            is_group = len(tail) == 3 and tail.isdigit() and before[-1:].isdigit()
            if not is_group and tail.isdigit() and 1 <= len(tail) <= 2:
                digits, decimals = before, tail
            break
    for sep in _THOUSANDS_SEPARATORS:
        digits = digits.replace(sep, "")
    try:
        value = float(f"{digits}.{decimals}" if decimals else digits)
    except ValueError:
        return None
    value = value * 1000 if thousands_k else value
    return value if math.isfinite(value) else None


def _currency(text: str) -> str | None:
    code = _CODE.search(text)
    if code:
        return code.group(1)
    for symbol, name in _SYMBOLS:
        if symbol in text:
            return name
    return None


def _interval(text: str) -> str | None:
    lowered = text.lower()
    for name, pattern in _INTERVAL_WORDS:
        if pattern.search(lowered):
            return name
    return None


def parse_pay_text(text: str) -> PayParse | None:
    """Read one pay range from a short pay string; None when there is no usable amount."""
    if not isinstance(text, str):
        return None
    text = text[:_PAY_TEXT_LIMIT]
    match = _RANGE.search(text)
    if match is not None:
        low = _to_number(match.group("a"), bool(match.group("ak")))
        high = _to_number(match.group("b"), bool(match.group("bk")))
        if match.group("bk") and not match.group("ak") and low is not None and low < 1000:
            low *= 1000
    else:
        single = _SINGLE.search(text)
        if single is None:
            return None
        low = high = _to_number(single.group("a"), bool(single.group("ak")))
    if low is None or high is None or low > high:
        return None
    return PayParse(min=low, max=high, currency=_currency(text), interval=_interval(text))


_Item = TypeVar("_Item")


def same_currency_ranges(
    items: list[_Item],
    currency_of: Callable[[_Item], str | None],
    interval_of: Callable[[_Item], str | None],
) -> tuple[list[_Item], str | None, list[str]]:
    """Keep the ranges that can be merged with the first range that states a currency.

    Returns (kept, currency, dropped), where dropped holds one label per range left out: its
    currency code, or "no currency" for an unlabeled range. A range with a different currency is
    never merged, because 150,000 CAD and 120,000 USD are not on one scale. A range with no
    currency joins the first currency only when its interval equals that range's interval (both
    absent counts as equal); otherwise its unit is unknown and it is left out. With no currency
    stated anywhere every range is kept, as before.
    """
    anchor = next((item for item in items if currency_of(item)), None)
    if anchor is None:
        return list(items), None, []
    currency, interval = currency_of(anchor), interval_of(anchor)
    kept: list[_Item] = []
    dropped: list[str] = []
    for item in items:
        own = currency_of(item)
        if own == currency or (own is None and interval_of(item) == interval):
            kept.append(item)
        else:
            dropped.append(own or "no currency")
    return kept, currency, dropped


def unused_ranges_warning(title: str, dropped: list[str], currency: str | None,
                          noun: str) -> str:
    """One plain-English sentence naming how many ranges in which other currencies were skipped."""
    count = len(dropped)
    codes = ", ".join(sorted(set(dropped)))
    label = noun if count == 1 else f"{noun}s"
    verb = "was" if count == 1 else "were"
    return (f"{title}: {count} {label} in {codes} {verb} not used; the pay shown is in "
            f"{currency} only.")


NO_PAY: dict[str, object] = {
    "comp_min": None,
    "comp_max": None,
    "comp_currency": None,
    "comp_interval": None,
    "comp_source": None,
}


def pick_ranges(
    items: list[_Item],
    currency_of: Callable[[_Item], str | None],
    interval_of: Callable[[_Item], str | None],
    title: str,
    noun: str,
    warnings: list[str] | None,
) -> tuple[list[_Item], str | None]:
    """same_currency_ranges plus the two plain-English warnings (skipped ranges, merged ranges)."""
    kept, currency, dropped = same_currency_ranges(items, currency_of, interval_of)
    if warnings is not None:
        if dropped:
            warnings.append(unused_ranges_warning(title, dropped, currency, noun))
        if len(kept) > 1:
            warnings.append(f"{title}: the posting lists {len(kept)} {noun}s; showing the "
                            "lowest minimum and the highest maximum.")
    return kept, currency


def pay_fields(parsed: list[PayParse], source: str, title: str,
               warnings: list[str] | None) -> dict[str, object]:
    """The five comp fields for parsed pay ranges, merged to the widest min-max pair."""
    if not parsed:
        return dict(NO_PAY)
    kept, currency = pick_ranges(
        parsed, lambda p: p.currency, lambda p: p.interval, title, "pay range", warnings)
    return {
        "comp_min": min(p.min for p in kept),
        "comp_max": max(p.max for p in kept),
        "comp_currency": currency,
        "comp_interval": next((p.interval for p in kept if p.interval), None),
        "comp_source": source,
    }


_LOCATION_SEPARATORS = re.compile(r"[|;]")


def split_locations(text: str) -> list[str]:
    return [part.strip() for part in _LOCATION_SEPARATORS.split(text) if part.strip()]
