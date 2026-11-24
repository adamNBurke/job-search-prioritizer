"""Hard filters: the title gate, five filter types, the verdict contract and the unknown policy.

Purpose: a hard filter is a must the user set (work arrangement, pay floor, region, words that
rule a posting out, companies to skip). Every filter type answers in one shape, and the rule for
"the posting does not say" lives in exactly one place, so a new filter type never edits the
scorer. Every rejected role carries the id and label of the one filter that rejected it and a
plain-English reason; nothing leaves the ranking silently.
Logic: apply_filters runs the title gate first (no family, then an excluded term), then the
function check (a word the user approved for the title's own family, filter_id title_function;
it is not part of the title gate, so the scan keeps those roles and rank reports them),
then the function review (a reject verdict the agent wrote after reading the description,
filter_id function_review), then every hard_filters entry in config order
through FILTER_TYPES, and reports the first failure, so a role that fails several filters is
always reported against the same one. A type returns
FilterVerdict("pass"), FilterVerdict("fail", reason) or FilterVerdict("unknown"). apply_filters
applies the entry's unknown policy for every type: "reject" fails the role with a plain reason,
"gap" and "pass" let it through. gap_filters reruns the same checks and returns the entries
that were unknown under policy "gap", in config order, for the scorer to list among the
role's gaps. The types: enum_allow (a field's value is in a list), min_number (pay at or above a
floor after annualizing), region_allow (places from locations.parse_locations fall in the
allowed countries or EU; when the locations name no country and no zone, a place named in the
title decides, and a title that names nothing leaves the verdict unknown), deny_terms (none of
the phrases appear in a field), deny_list (the company is not on the list).
Guardrails: an unregistered filter type or a malformed entry raises UserError rather than
silently passing, because a filter that does not run would let rejected roles through. Deny
phrases are escaped, never patterns, so a config value cannot run code or stall the matcher.
Locations and title places resolve from local tables only; nothing here touches the network. An
unknown region is never turned into a reject: the title is read only when the locations name no
country and no zone, and it can reject only by naming a place outside the allowed regions.
Reject reasons are config templates filled from the documented fields only; a field with no
value reads "not stated" and any other brace text stays literal. The function_review reason is
the agent's one-line text, already checked by verdicts.validate_verdicts; it can only reject a
role (RoleFacts.function_verdict is set only by a reject verdict) and never raises one.
QA checklist: a new filter type is one function in FILTER_TYPES returning a FilterVerdict, a row
in docs/data-files.md and in criteria.py, and a pass, a fail and an unknown test; it never edits
apply_filters, gap_filters or scoring.py.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, fields
from decimal import Decimal

from jobsearch.errors import UserError
from jobsearch.families import (
    CompiledFamilies, _term_pattern, excluded_term, function_excluded, match_family)
from jobsearch.identity import normalize_company
from jobsearch.locations import Place, in_regions, parse_locations, place_label, title_places
from jobsearch.models import Posting
from jobsearch.roles_store import RoleFacts

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_POSTING_FIELDS = frozenset(f.name for f in fields(Posting))
REASON_FIELDS = (
    "company", "title", "value", "comp_max", "comp_currency", "work_mode_text", "term",
)
ANNUAL_FACTORS = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}
_YEAR_SIZED = 10000  # a pay figure with no interval counts as yearly only from this size up
NOT_STATED = "not stated"


@dataclass(frozen=True)
class FilterVerdict:
    state: str  # "pass", "fail" or "unknown"
    reason: str | None = None


@dataclass(frozen=True)
class FilterFailure:
    filter_id: str
    label: str
    reason: str


FilterType = Callable[[RoleFacts, dict, CompiledFamilies], FilterVerdict]


def field_value(facts: RoleFacts, name: str) -> object:
    """A Posting field by name, else a company fact such as company_industry."""
    if name in _POSTING_FIELDS:
        return getattr(facts.posting, name)
    return getattr(facts, name, None)


def render_reason(
    template: str, facts: RoleFacts, entry: dict, extras: dict[str, str] | None = None
) -> str:
    """Fill the documented placeholders of a reject_reason template.

    Only the fields in REASON_FIELDS are filled; a documented field with no value reads "not
    stated", and any other brace text stays exactly as written. extras holds values a filter
    type worked out itself, such as the annual figure behind {comp_max} or the matched {term}.
    """
    context: dict[str, object] = {
        "company": facts.posting.company,
        "title": facts.posting.title,
        "comp_currency": facts.posting.comp_currency,
        "work_mode_text": facts.posting.work_mode_text,
        "comp_max": (money(facts.posting.comp_max, facts.posting.comp_currency)
                     if facts.posting.comp_max is not None else None),
        "value": entry.get("value", entry.get("allow")),
    }
    context.update(extras or {})

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in REASON_FIELDS:
            return match.group(0)
        value = context.get(name)
        if value is None or value == "":
            return NOT_STATED
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    return _PLACEHOLDER.sub(fill, template)


def annualize(value: float | int | None, interval: str | None) -> Decimal | None:
    """A pay figure as a yearly amount, or None when it cannot be read.

    year 1, month 12, week 52, day 260, hour 2080 (docs/data-files.md). An interval of None
    counts as year only when the figure is at least 10,000; "other" and any word not in the
    table cannot be annualized. Decimal keeps "exactly the floor" exact.
    """
    if value is None or isinstance(value, bool):
        return None
    amount = Decimal(str(value))
    if interval is None:
        return amount if amount >= _YEAR_SIZED else None
    factor = ANNUAL_FACTORS.get(interval)
    return None if factor is None else amount * factor


def money(amount: Decimal | float | int, currency: str | None = None) -> str:
    """80000 -> "80,000", with the currency code after it when there is one."""
    number = Decimal(str(amount))
    text = f"{number:,.0f}" if number == number.to_integral_value() else f"{number:,.2f}"
    return f"{text} {currency}" if currency else text


def _label(entry: dict) -> str:
    return str(entry.get("label") or entry["id"])


def _entry_text(entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise UserError(f'The filter "{entry.get("id")}" needs a {key}.')
    return value


def _entry_list(entry: dict, key: str) -> list[str]:
    value = entry.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise UserError(f'The filter "{entry.get("id")}" needs a list called {key}.')
    return value


def _blank(value: object) -> bool:
    return value is None or value == "" or value == "unknown" or value == []


def _enum_allow(facts: RoleFacts, entry: dict, compiled: CompiledFamilies) -> FilterVerdict:
    value = field_value(facts, _entry_text(entry, "field"))
    allowed = {a.casefold() for a in _entry_list(entry, "allow")}
    if _blank(value):
        return FilterVerdict("unknown")
    values = value if isinstance(value, list) else [value]
    if any(str(v).casefold() in allowed for v in values):
        return FilterVerdict("pass")
    template = entry.get("reject_reason") or (
        f"{_label(entry)} is {', '.join(str(v) for v in values)}, "
        "which is outside what you allowed."
    )
    return FilterVerdict("fail", render_reason(template, facts, entry))


_PAY_FIELDS = ("comp_min", "comp_max")


def _min_number(facts: RoleFacts, entry: dict, compiled: CompiledFamilies) -> FilterVerdict:
    name = str(entry.get("field", ""))
    if name not in _PAY_FIELDS or not isinstance(entry.get("value"), (int, float)):
        raise UserError(
            f'The filter "{entry.get("id")}" needs a field of comp_min or comp_max and a '
            "number to compare it with."
        )
    posting = facts.posting
    stated = getattr(posting, name)
    want = entry.get("currency")
    if stated is None or (want and (posting.comp_currency or "").upper() != str(want).upper()):
        return FilterVerdict("unknown")
    annual = annualize(stated, posting.comp_interval)
    if annual is None:
        return FilterVerdict("unknown")
    floor = Decimal(str(entry["value"]))
    if annual >= floor:
        return FilterVerdict("pass")
    shown = money(annual, posting.comp_currency)
    floor_shown = money(floor, str(want) if want else posting.comp_currency)
    template = entry.get("reject_reason")
    if not template:
        side = "Top" if name == "comp_max" else "Bottom"
        return FilterVerdict(
            "fail", f"{side} of the stated range is {shown}, below your {floor_shown} floor.")
    comp_max = annualize(posting.comp_max, posting.comp_interval)
    extras = {
        "value": floor_shown,
        "comp_max": money(comp_max, posting.comp_currency) if comp_max is not None else "",
    }
    return FilterVerdict("fail", render_reason(template, facts, entry, extras))


def _region_allow(facts: RoleFacts, entry: dict, compiled: CompiledFamilies) -> FilterVerdict:
    allow = _entry_list(entry, "allow")
    if not any(a.strip() for a in allow):
        raise UserError(f'The filter "{entry.get("id")}" needs at least one region to allow.')
    places = parse_locations(facts.posting.locations)
    outcome = in_regions(places, allow)
    from_title: list[Place] = []
    if outcome is None and not any(p.country or p.zone for p in places):
        # The locations name no country and no zone: a place in the title decides. It can only
        # reject by naming a place outside the allowed regions; a title that names nothing, or
        # a zone that overlaps the allowed regions, leaves the verdict unknown.
        from_title = title_places(facts.posting.title)
        if from_title:
            outcome = in_regions(from_title, allow)
    if outcome is None:
        return FilterVerdict("unknown")
    if outcome:
        return FilterVerdict("pass")
    template = entry.get("reject_reason")
    if template:
        return FilterVerdict("fail", render_reason(template, facts, entry))
    allowed = ", ".join(allow)
    if from_title:
        named = " and ".join(dict.fromkeys(place_label(p) for p in from_title))
        return FilterVerdict(
            "fail",
            f"The title places the role in {named}, outside the regions you allowed ({allowed}).")
    where = "; ".join(facts.posting.locations)
    return FilterVerdict(
        "fail", f"The role is in {where}, outside the regions you allowed ({allowed}).")


_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'"})
_TEXT_FIELDS = {
    "title": "The title", "description_text": "The description", "text": "The posting",
    "company_industry": "The industry",
}


def _matchable(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_APOSTROPHES)


@functools.lru_cache(maxsize=256)
def _phrases(terms: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Whole-phrase matchers: spaces, hyphens and slashes interchangeable, never a pattern."""
    found = []
    for term in terms:
        pattern = _term_pattern(_matchable(term))
        if pattern is not None:
            found.append((term, pattern))
    return tuple(found)


def _field_text(facts: RoleFacts, name: str) -> str | None:
    if name == "text":
        title, description = facts.posting.title, facts.posting.description_text
        return f"{title}\n{description}" if description.strip() else None
    value = field_value(facts, name)
    return value if isinstance(value, str) and value.strip() else None


def _deny_terms(facts: RoleFacts, entry: dict, compiled: CompiledFamilies) -> FilterVerdict:
    name = _entry_text(entry, "field")
    if name not in _TEXT_FIELDS:
        raise UserError(
            f'The filter "{entry.get("id")}" can read the title, description_text, text or '
            "company_industry, not " + name + "."
        )
    terms = tuple(_entry_list(entry, "terms"))
    text = _field_text(facts, name)
    if text is None:
        return FilterVerdict("unknown")
    haystack = _matchable(text)
    for term, pattern in _phrases(terms):
        if pattern.search(haystack):
            template = entry.get("reject_reason")
            if template:
                return FilterVerdict(
                    "fail", render_reason(template, facts, entry, {"term": term}))
            return FilterVerdict(
                "fail", f'{_TEXT_FIELDS[name]} mentions "{term}", which you ruled out.')
    return FilterVerdict("pass")


def _deny_list(facts: RoleFacts, entry: dict, compiled: CompiledFamilies) -> FilterVerdict:
    names = {normalize_company(n) for n in _entry_list(entry, "names")} - {""}
    company = normalize_company(facts.posting.company)
    if not company:
        return FilterVerdict("unknown")
    if company not in names:
        return FilterVerdict("pass")
    template = entry.get("reject_reason") or (
        f"{facts.posting.company} is on your list of companies to skip.")
    return FilterVerdict("fail", render_reason(template, facts, entry))


FILTER_TYPES: dict[str, FilterType] = {
    "enum_allow": _enum_allow,
    "min_number": _min_number,
    "region_allow": _region_allow,
    "deny_terms": _deny_terms,
    "deny_list": _deny_list,
}


def title_gate(title: str, compiled: CompiledFamilies) -> FilterFailure | None:
    if match_family(title, compiled) is None:
        return FilterFailure("title_family", "Title",
                             "Title is outside every family you approved.")
    term = excluded_term(title, compiled)
    if term is not None:
        return FilterFailure("title_exclude", "Title",
                             f'Title contains "{term}", which you excluded.')
    return None


def _verdict(entry: dict, facts: RoleFacts, compiled: CompiledFamilies) -> FilterVerdict:
    handler = FILTER_TYPES.get(str(entry.get("type")))
    if handler is None:
        raise UserError(
            f'The filter "{entry.get("id")}" uses the type "{entry.get("type")}", '
            "which this version cannot run yet."
        )
    return handler(facts, entry, compiled)


def apply_filters(
    facts: RoleFacts, criteria: dict, compiled: CompiledFamilies
) -> FilterFailure | None:
    failure = title_gate(facts.posting.title, compiled)
    if failure is not None:
        return failure
    ruled_out = function_excluded(facts.posting.title, compiled)
    if ruled_out is not None:
        word, label = ruled_out
        return FilterFailure("title_function", "Function you ruled out",
                             f'Title contains "{word}", which you ruled out for {label}.')
    if facts.function_verdict is not None:
        return FilterFailure("function_review", "Other function, read from the description",
                             f"Read from the description: {facts.function_verdict}")
    for entry in criteria.get("hard_filters", []):
        verdict = _verdict(entry, facts, compiled)
        if verdict.state == "fail":
            return FilterFailure(str(entry["id"]), _label(entry), verdict.reason or "")
        if verdict.state == "unknown" and entry.get("unknown", "gap") == "reject":
            return FilterFailure(
                str(entry["id"]), _label(entry),
                f"{_label(entry)} is not stated in the posting, and you marked it as a must.",
            )
    return None


def gap_filters(facts: RoleFacts, criteria: dict, compiled: CompiledFamilies) -> list[dict]:
    """The hard-filter entries whose verdict is unknown under policy "gap", in config order."""
    return [
        entry for entry in criteria.get("hard_filters", [])
        if _verdict(entry, facts, compiled).state == "unknown"
        and entry.get("unknown", "gap") == "gap"
    ]


def expand_fields(names: set[str] | frozenset[str]) -> frozenset[str]:
    """Field names with "text" (title plus description) spelled out as the two it reads."""
    return frozenset(
        part for name in names for part in
        (("title", "description_text") if name == "text" else (name,)))


def filter_fields(entry: dict) -> frozenset[str]:
    """The posting fields a hard filter reads, so a gap one criterion already names is not
    listed twice. region_allow reads the locations, deny_list the company, every other type
    its own field ("text" counts as the title and the description)."""
    kind = entry.get("type")
    if kind == "region_allow":
        return frozenset({"locations"})
    if kind == "deny_list":
        return frozenset({"company"})
    return expand_fields({str(entry.get("field", ""))} - {""})
