"""Validate a criteria.json the user, or a model writing for the user, produced.

Purpose: scoring trusts the config, so every load checks it first and refuses a bad file with
sentences the user can act on, instead of failing halfway through a ranking.
Logic: validate_criteria(data) returns a sorted list of plain-English errors, each starting with
the path of the field to fix, such as criteria[1].params.bands; an empty list means valid. It
checks every key in schemas/criteria.schema.json (required keys, unlisted keys, types, enums),
the evaluator params rows and the hard filter types pinned in docs/data-files.md (required and
optional keys, ranges, band order, vocabularies, reason placeholders; level_match may carry the
bool cap_off_level and keyword_coverage the bool cap_no_skill, and a non-bool is refused;
age_bands may carry the whole-day limits cap_after_days and reject_after_days), title
families (ids, rung, term length, regex length and compilation, and the optional exclude
words: text, at most 20, no repeat, none matching the family's own terms), bonus points, tiers
exactly A, B, C with strictly descending integer min_score, the banding threshold and the
one-per-company switch. Weights must be integers summing to 100. No schema-validation library
is a dependency, so the checks are written by hand against the same schema file, which is read
for key lists and enums so the two cannot drift apart silently.
Guardrails: KNOWN_EVALUATORS is the schema's evaluator enum minus judged_scale (held back until
research enrichment ships, 1.1), so a config that names it is refused with a plain message.
Evaluators that are valid here but not registered in scoring.EVALUATORS yet are refused later,
at scoring time, naming the evaluator. Regex entries are length-capped and compile-checked here
because the scan compiles them. Booleans never count as numbers. The error list is sorted so
the same bad config always reads the same way.
QA checklist: a new rule adds one sentence to the error list and one test in
tests/test_criteria.py; error text names the path and at most the offending value.
"""

from __future__ import annotations

import functools
import json
import math
import re
from collections.abc import Callable
from pathlib import Path

from jobsearch.directory import SIZES, STAGES
from jobsearch.families import LEVELS, REGEX_MAX_LENGTH, TERM_MAX, exclude_word_errors
from jobsearch.filters import REASON_FIELDS
from jobsearch.models import WORK_MODES

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "criteria.schema.json"

_VERSION = re.compile(r"[0-9]+\.[0-9]+")
_CREATED_FROM = re.compile(r"intake|retune|proposal:p-[0-9]{4,}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_COUNTRY = re.compile(r"[A-Za-z]{2}|EU")
_UNKNOWN_POLICIES = ("gap", "pass", "reject")
_TIER_IDS = ["A", "B", "C"]
_MODES = ("remote", "hybrid", "onsite")
_BONUS_MAX_POINTS = 20

# Fields any `field` key may name (docs/data-files.md, "Evaluator params").
ROLE_FIELDS = (
    "title", "description_text", "text", "company", "company_industry", "company_size",
    "company_stage", "work_mode", "locations", "comp_min", "comp_max", "posted",
)
# enum_allow compares one value, so the combined `text` field makes no sense there.
_ENUM_FIELDS = tuple(f for f in ROLE_FIELDS if f != "text")
_TEXT_FIELDS = ("title", "description_text", "text", "company_industry")
_VOCABULARY = {
    "company_size": SIZES, "company_stage": STAGES, "work_mode": WORK_MODES,
}


@functools.cache
def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _criterion_keys() -> frozenset[str]:
    return frozenset(_schema()["properties"]["criteria"]["items"]["properties"])


def _evaluator_enum() -> list[str]:
    return list(_schema()["properties"]["criteria"]["items"]["properties"]["evaluator"]["enum"])


KNOWN_EVALUATORS = frozenset(_evaluator_enum()) - {"judged_scale"}


def _enum(*path: str) -> list[str]:
    node = _schema()
    for part in path:
        node = node[part]
    return list(node["enum"])


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_points(value: object) -> bool:
    return _is_int(value) and 0 <= value <= 3  # type: ignore[operator]


def join_or(words: tuple[str, ...] | list[str]) -> str:
    items = list(words)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _object(value: object, path: str, errors: list[str]) -> dict | None:
    if isinstance(value, dict):
        return value
    errors.append(f"{path}: must be an object.")
    return None


def _list(value: object, path: str, errors: list[str]) -> list | None:
    if isinstance(value, list):
        return value
    errors.append(f"{path}: must be a list.")
    return None


def _unlisted(obj: dict, allowed: frozenset[str] | set[str], path: str, what: str,
              errors: list[str]) -> None:
    for key in sorted(set(obj) - set(allowed)):
        errors.append(f'{path}: "{key}" is not a setting {what} uses.')


def _term_text(value: object, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: is empty; give it some text.")
    elif len(value) > TERM_MAX:
        errors.append(f"{path}: is longer than {TERM_MAX} characters.")


def _text_list(value: object, path: str, errors: list[str]) -> None:
    items = _list(value, path, errors)
    for index, item in enumerate(items or []):
        _term_text(item, f"{path}[{index}]", errors)


# Evaluator params ---------------------------------------------------------------------------

def _pairs(value: object, path: str, errors: list[str]) -> list[tuple[float, object]] | None:
    """A band list [[threshold, points], ...] as pairs, or None after reporting why not."""
    items = _list(value, path, errors)
    if items is None:
        return None
    pairs: list[tuple[float, object]] = []
    ok = True
    for index, item in enumerate(items):
        if not isinstance(item, list) or len(item) != 2 or not _is_number(item[0]):
            errors.append(f"{path}[{index}]: must be a pair of a number and points.")
            ok = False
        elif not _is_points(item[1]):
            errors.append(f"{path}[{index}]: points must be a whole number from 0 to 3.")
            ok = False
        else:
            pairs.append((item[0], item[1]))
    return pairs if ok else None


def _keyword_terms(value: object, path: str, errors: list[str]) -> None:
    items = _list(value, path, errors)
    for index, item in enumerate(items or []):
        here = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{here}: must be an object with term, aliases and weight.")
            continue
        _unlisted(item, {"term", "aliases", "weight", "priority"}, here, "a skill", errors)
        for key in ("term", "aliases", "weight"):
            if key not in item:
                errors.append(f'{here}: needs "{key}".')
        if "term" in item:
            _term_text(item["term"], f"{here}.term", errors)
        if "aliases" in item:
            _text_list(item["aliases"], f"{here}.aliases", errors)
        weight = item.get("weight")
        if "weight" in item and not (_is_int(weight) and 1 <= weight <= 5):
            errors.append(f"{here}.weight: must be a whole number from 1 to 5.")
        if "priority" in item and not isinstance(item["priority"], bool):
            errors.append(f"{here}.priority: must be true or false.")


def _keyword_coverage(params: dict, path: str, field: object, errors: list[str]) -> None:
    for key in ("must_have", "nice"):
        if key in params:
            _keyword_terms(params[key], f"{path}.{key}", errors)
    if "rarity" in params and not isinstance(params["rarity"], bool):
        errors.append(f"{path}.rarity: must be true or false.")
    if "cap_no_skill" in params and not isinstance(params["cap_no_skill"], bool):
        errors.append(f"{path}.cap_no_skill: must be true or false.")
    if "share" in params and not isinstance(params["share"], bool):
        errors.append(f"{path}.share: must be true or false.")
        return
    if params.get("share") is True and "bands" in params:
        errors.append(f"{path}: use bands or share, not both.")
        return
    if "bands" not in params:
        if params.get("share") is not True:
            errors.append(f"{path}: needs bands, or share set to true.")
        return
    pairs = _pairs(params["bands"], f"{path}.bands", errors)
    if pairs is None:
        return
    shares = [share for share, _ in pairs]
    if any(not 0 <= share <= 1 for share in shares):
        errors.append(f"{path}.bands: each share must be a number from 0 to 1.")
    elif any(a <= b for a, b in zip(shares, shares[1:], strict=False)):
        errors.append(f"{path}.bands: thresholds must be in descending order.")
    elif not pairs or shares[-1] != 0:
        errors.append(f"{path}.bands: the last share must be 0, so every role lands in a band.")


def _age_bands(params: dict, path: str, field: object, errors: list[str]) -> None:
    if "bands" in params:
        pairs = _pairs(params["bands"], f"{path}.bands", errors)
        if pairs is not None:
            days = [d for d, _ in pairs]
            if any(not _is_int(d) or d < 0 for d in days):
                errors.append(f"{path}.bands: max_days must be a whole number, zero or more.")
            elif any(a >= b for a, b in zip(days, days[1:], strict=False)):
                errors.append(f"{path}.bands: max_days must be in ascending order.")
    if "else" in params and not _is_points(params["else"]):
        errors.append(f"{path}.else: must be a whole number from 0 to 3.")
    # The day limits of the age cap and the age reject. A bool is never a number.
    limits: dict[str, int] = {}
    for key in ("cap_after_days", "reject_after_days"):
        if key not in params:
            continue
        if _is_int(params[key]) and params[key] >= 1:
            limits[key] = params[key]
        else:
            errors.append(f"{path}.{key}: must be a whole number of days, 1 or more.")
    if len(limits) == 2 and limits["reject_after_days"] <= limits["cap_after_days"]:
        errors.append(f"{path}.reject_after_days: must be more than cap_after_days.")


def _banded_number(params: dict, path: str, field: object, errors: list[str]) -> None:
    if "bands" in params:
        pairs = _pairs(params["bands"], f"{path}.bands", errors)
        if pairs is not None:
            thresholds = [t for t, _ in pairs]
            if any(a <= b for a, b in zip(thresholds, thresholds[1:], strict=False)):
                errors.append(f"{path}.bands: thresholds must be in descending order.")
    currency = params.get("currency")
    if "currency" in params and not (isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
        errors.append(f"{path}.currency: must be a three-letter ISO 4217 code such as USD.")


def _scale(value: object, path: str, errors: list[str]) -> dict | None:
    scale = _object(value, path, errors)
    if scale is None:
        return None
    for key, points in scale.items():
        if not _is_points(points):
            errors.append(f'{path}: "{key}" needs points from 0 to 3.')
    return scale


def _enum_scale(params: dict, path: str, field: object, errors: list[str]) -> None:
    if "values" not in params:
        return
    scale = _scale(params["values"], f"{path}.values", errors)
    vocabulary = _VOCABULARY.get(field) if isinstance(field, str) else None
    if scale is not None and vocabulary is not None:
        for key in scale:
            if key not in vocabulary:
                errors.append(
                    f'{path}.values: "{key}" is not a {field} value ({", ".join(vocabulary)}).')


def _term_scale(params: dict, path: str, field: object, errors: list[str]) -> None:
    if "terms" not in params:
        return
    scale = _scale(params["terms"], f"{path}.terms", errors)
    for key in scale or {}:
        if not key.strip():
            errors.append(f"{path}.terms: a term is empty.")
        elif len(key) > TERM_MAX:
            errors.append(f"{path}.terms: a term is longer than {TERM_MAX} characters.")


def _level_match(params: dict, path: str, field: object, errors: list[str]) -> None:
    target = params.get("target")
    if "target" in params and target is not None and target not in LEVELS:
        errors.append(f"{path}.target: must be one of {', '.join(LEVELS)} or null.")
    years = params.get("years")
    if "years" in params and years is not None and not (_is_int(years) and years >= 0):
        errors.append(f"{path}.years: must be a whole number of zero or more, or null.")
    if "cap_off_level" in params and not isinstance(params["cap_off_level"], bool):
        errors.append(f"{path}.cap_off_level: must be true or false.")


def _location_match(params: dict, path: str, field: object, errors: list[str]) -> None:
    modes = params.get("modes")
    if "modes" in params:
        if not isinstance(modes, list):
            errors.append(f"{path}.modes: must be a list drawn from {join_or(_MODES)}.")
        else:
            for mode in modes:
                if mode not in _MODES:
                    errors.append(f'{path}.modes: "{mode}" is not {join_or(_MODES)}.')
    places = params.get("places")
    if "places" in params and not (
            isinstance(places, list) and all(isinstance(p, str) and p.strip() for p in places)):
        errors.append(f"{path}.places: must be a list of place names and region codes.")


ParamsCheck = Callable[[dict, str, object, list[str]], None]

# evaluator -> (fields it may read or None, field required, required params, optional params,
#               reason placeholders, params checker). Mirrors docs/data-files.md.
_EVALUATORS: dict[str, tuple[tuple[str, ...] | None, bool, frozenset[str], frozenset[str],
                             tuple[str, ...], ParamsCheck]] = {
    "title_ladder": (None, False, frozenset(), frozenset(), ("family",),
                     lambda params, path, field, errors: None),
    "keyword_coverage": (("text", "title", "description_text"), False,
                         frozenset({"must_have", "nice"}),
                         frozenset({"bands", "share", "rarity", "cap_no_skill"}), ("hit", "total", "hits_list"),
                         _keyword_coverage),
    "age_bands": (None, False, frozenset({"bands", "else"}),
                  frozenset({"cap_after_days", "reject_after_days"}), ("days",), _age_bands),
    "banded_number": (("comp_min", "comp_max"), True, frozenset({"bands"}),
                      frozenset({"currency"}), ("value", "currency"), _banded_number),
    "enum_scale": (("company_size", "company_stage", "work_mode"), True,
                   frozenset({"values"}), frozenset(), ("value",), _enum_scale),
    "term_scale": (("company_industry", "company", "title", "description_text", "text"), True,
                   frozenset({"terms"}), frozenset(), ("term",), _term_scale),
    "level_match": (None, False, frozenset({"target", "years"}), frozenset({"cap_off_level"}),
                    ("level", "target"), _level_match),
    "location_match": (None, False, frozenset({"modes", "places"}), frozenset(),
                       ("mode", "place"), _location_match),
}


def _check_evaluator(item: dict, path: str, errors: list[str]) -> str | None:
    """Check the evaluator name, its field and its params. Returns the name when it is usable."""
    evaluator = item.get("evaluator")
    if evaluator == "judged_scale":
        errors.append(f"{path}.evaluator: judged_scale needs researched values, "
                      "which arrive in 1.1 with company research.")
        return None
    if (not isinstance(evaluator, str) or evaluator not in KNOWN_EVALUATORS
            or evaluator not in _EVALUATORS):
        errors.append(f'{path}.evaluator: "{evaluator}" is not an evaluator this tool knows.')
        return None
    fields, field_required, required, optional, _, check = _EVALUATORS[evaluator]
    field = item.get("field")
    if fields is None:
        if "field" in item:
            errors.append(f"{path}.field: {evaluator} does not take a field.")
    elif field is None and not field_required:
        pass
    elif field not in fields:
        errors.append(f"{path}.field: {evaluator} reads {join_or(fields)}.")
    params = item.get("params", {})
    if not isinstance(params, dict):
        errors.append(f"{path}.params: must be an object.")
        return evaluator
    for key in sorted(required - set(params)):
        errors.append(f'{path}.params: needs "{key}".')
    for key in sorted(set(params) - required - optional):
        errors.append(f'{path}.params: "{key}" is not a setting this evaluator uses.')
    check(params, f"{path}.params", field, errors)
    return evaluator


def _check_reasons(item: dict, path: str, evaluator: str, errors: list[str]) -> None:
    if "reasons" not in item:
        return
    reasons = _object(item["reasons"], f"{path}.reasons", errors)
    if reasons is None:
        return
    allowed = _EVALUATORS[evaluator][4]
    for key in sorted(reasons, key=str):
        if key not in ("0", "1", "2", "3", "default"):
            errors.append(f'{path}.reasons: "{key}" is not a points value from 0 to 3 or default.')
        template = reasons[key]
        if not isinstance(template, str):
            errors.append(f"{path}.reasons.{key}: must be text.")
            continue
        for name in _PLACEHOLDER.findall(template):
            if name not in allowed:
                errors.append(
                    f'{path}.reasons.{key}: "{{{name}}}" is not a field this evaluator can '
                    f"fill; use {join_or(['{' + a + '}' for a in allowed])}.")


def _check_criteria(data: dict, errors: list[str]) -> None:
    criteria = _list(data.get("criteria"), "criteria", errors)
    if criteria is None:
        return
    allowed = _criterion_keys()
    seen: set[str] = set()
    total = 0
    for index, item in enumerate(criteria):
        path = f"criteria[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object.")
            continue
        _unlisted(item, allowed, path, "a criterion", errors)
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            errors.append(f"{path}.id: needs an id.")
        elif ident in seen:
            errors.append(f'{path}.id: "{ident}" is used more than once.')
        else:
            seen.add(ident)
        weight = item.get("weight")
        if not _is_int(weight) or not 0 <= weight <= 100:
            errors.append(f"{path}.weight: needs a whole-number weight from 0 to 100.")
        else:
            total += weight
        if not isinstance(item.get("label"), str) or not item["label"]:
            errors.append(f"{path}.label: needs a label.")
        evaluator = _check_evaluator(item, path, errors)
        if evaluator is not None:
            _check_reasons(item, path, evaluator, errors)
    if criteria and total != 100:
        errors.append(f"criteria: weights add up to {total}; they must add up to 100.")


def _check_bonuses(data: dict, errors: list[str]) -> None:
    bonuses = _list(data.get("bonuses"), "bonuses", errors)
    if bonuses is None:
        return
    allowed = frozenset(_schema()["properties"]["bonuses"]["items"]["properties"])
    seen: set[str] = set()
    for index, item in enumerate(bonuses):
        path = f"bonuses[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be an object.")
            continue
        _unlisted(item, allowed, path, "a bonus", errors)
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            errors.append(f"{path}.id: needs an id.")
        elif ident in seen:
            errors.append(f'{path}.id: "{ident}" is used more than once.')
        else:
            seen.add(ident)
        points = item.get("max_points")
        if not _is_int(points) or not 0 <= points <= _BONUS_MAX_POINTS:
            errors.append(f"{path}.max_points: needs a whole number from 0 to {_BONUS_MAX_POINTS}.")
        _check_evaluator(item, path, errors)


# Hard filters --------------------------------------------------------------------------------

# type -> (required keys, optional keys), beyond the keys every filter may carry.
_FILTER_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "enum_allow": (frozenset({"field", "allow"}), frozenset()),
    "min_number": (frozenset({"field", "value"}), frozenset({"currency"})),
    "region_allow": (frozenset({"allow"}), frozenset({"field"})),
    "deny_terms": (frozenset({"field", "terms"}), frozenset()),
    "deny_list": (frozenset({"names"}), frozenset()),
}
_FILTER_COMMON = frozenset({"id", "type", "unknown", "label", "reject_reason"})


def _check_filter_values(kind: str, entry: dict, path: str, errors: list[str]) -> None:
    if kind == "enum_allow":
        if "field" in entry and entry["field"] not in _ENUM_FIELDS:
            errors.append(f'{path}.field: "{entry["field"]}" is not a posting field; '
                          f"use {join_or(_ENUM_FIELDS)}.")
        allow = entry.get("allow")
        if "allow" in entry and not (
                isinstance(allow, list) and allow
                and all(isinstance(a, str) and a for a in allow)):
            errors.append(f"{path}.allow: needs a list with at least one value.")
    elif kind == "min_number":
        if "field" in entry and entry["field"] not in ("comp_min", "comp_max"):
            errors.append(f"{path}.field: min_number reads comp_min or comp_max.")
        value = entry.get("value")
        if "value" in entry and not (_is_number(value) and value >= 0):
            errors.append(f"{path}.value: must be a number, zero or more.")
        currency = entry.get("currency")
        if "currency" in entry and not (
                isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
            errors.append(f"{path}.currency: must be a three-letter ISO 4217 code such as USD.")
    elif kind == "region_allow":
        allow = _list(entry.get("allow"), f"{path}.allow", errors) if "allow" in entry else None
        if allow is not None and not allow:
            errors.append(f"{path}.allow: needs at least one country code or EU.")
        for index, code in enumerate(allow or []):
            if not (isinstance(code, str) and _COUNTRY.fullmatch(code)):
                errors.append(f'{path}.allow[{index}]: "{code}" must be a two-letter country '
                              "code or EU.")
        if "field" in entry and entry["field"] != "locations":
            errors.append(f'{path}.field: region_allow reads "locations".')
    elif kind == "deny_terms":
        if "field" in entry and entry["field"] not in _TEXT_FIELDS:
            errors.append(f"{path}.field: deny_terms reads {join_or(_TEXT_FIELDS)}.")
        if "terms" in entry:
            _text_list(entry["terms"], f"{path}.terms", errors)
    elif kind == "deny_list" and "names" in entry:
        names = _list(entry["names"], f"{path}.names", errors)
        for index, name in enumerate(names or []):
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{path}.names[{index}]: is empty; give it a company name.")


def _check_hard_filters(data: dict, errors: list[str]) -> None:
    entries = _list(data.get("hard_filters"), "hard_filters", errors)
    if entries is None:
        return
    types = _enum("properties", "hard_filters", "items", "properties", "type")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        path = f"hard_filters[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: must be an object.")
            continue
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            errors.append(f"{path}.id: needs an id.")
        elif ident in seen:
            errors.append(f'{path}.id: "{ident}" is used more than once.')
        else:
            seen.add(ident)
        if "unknown" in entry and entry["unknown"] not in _UNKNOWN_POLICIES:
            errors.append(f"{path}.unknown: must be {join_or(_UNKNOWN_POLICIES)}.")
        if "label" in entry and not (isinstance(entry["label"], str) and entry["label"]):
            errors.append(f"{path}.label: must be text.")
        reason = entry.get("reject_reason")
        if "reject_reason" in entry:
            if not isinstance(reason, str):
                errors.append(f"{path}.reject_reason: must be text.")
            else:
                for name in _PLACEHOLDER.findall(reason):
                    if name not in REASON_FIELDS:
                        errors.append(
                            f'{path}.reject_reason: "{{{name}}}" is not a field a reason can '
                            f"use; use {join_or(['{' + f + '}' for f in REASON_FIELDS])}.")
        kind = entry.get("type")
        if kind not in types or kind not in _FILTER_KEYS:
            errors.append(f'{path}.type: "{kind}" is not a filter type; use {join_or(types)}.')
            continue
        required, optional = _FILTER_KEYS[kind]
        for key in sorted(required - set(entry)):
            errors.append(f'{path}: needs "{key}".')
        _unlisted(entry, required | optional | _FILTER_COMMON, path, f"a {kind} filter", errors)
        _check_filter_values(kind, entry, path, errors)


# Title ---------------------------------------------------------------------------------------

def _check_pattern(source: object, path: str, errors: list[str]) -> None:
    if not isinstance(source, str) or len(source) > REGEX_MAX_LENGTH:
        errors.append(f"{path}: this pattern is longer than {REGEX_MAX_LENGTH} characters "
                      "or is not text.")
        return
    try:
        re.compile(source)
    except re.error:
        errors.append(f"{path}: this is not a valid pattern.")


def _check_families(title: dict, errors: list[str]) -> None:
    families = _list(title.get("families", []), "title.families", errors)
    if families is None:
        return
    allowed = frozenset(_schema()["properties"]["title"]["properties"]["families"]
                        ["items"]["properties"])
    required = _schema()["properties"]["title"]["properties"]["families"]["items"]["required"]
    origins = _enum("properties", "title", "properties", "families", "items", "properties",
                    "origin")
    seen: set[str] = set()
    for index, family in enumerate(families):
        path = f"title.families[{index}]"
        if not isinstance(family, dict):
            errors.append(f"{path}: must be an object.")
            continue
        _unlisted(family, allowed, path, "a title family", errors)
        for key in required:
            if key == "origin" and key not in family:
                errors.append(f"{path}.origin: needs one of {join_or(origins)}.")
            elif key not in family:
                errors.append(f'{path}: needs "{key}".')
        ident = family.get("id")
        if "id" in family:
            if not isinstance(ident, str) or not ident:
                errors.append(f"{path}.id: needs an id.")
            elif ident in seen:
                errors.append(f'{path}.id: "{ident}" is used more than once.')
            else:
                seen.add(ident)
        if "label" in family and (not isinstance(family["label"], str) or not family["label"]):
            errors.append(f"{path}.label: needs a label.")
        rung = family.get("rung")
        if "rung" in family and not (_is_int(rung) and 0 <= rung <= 3):
            errors.append(f"{path}.rung: must be a whole number from 0 to 3.")
        if "origin" in family and family["origin"] not in origins:
            errors.append(f"{path}.origin: must be {join_or(origins)}.")
        terms = family.get("terms", [])
        _text_list(terms, f"{path}.terms", errors)
        regex = family.get("regex", [])
        patterns = _list(regex, f"{path}.regex", errors) if "regex" in family else []
        for position, source in enumerate(patterns or []):
            _check_pattern(source, f"{path}.regex[{position}]", errors)
        if "terms" in family and isinstance(terms, list) and not terms and not patterns:
            errors.append(f"{path}: needs at least one term or pattern.")
        if "exclude" in family:
            errors.extend(exclude_word_errors(f"{path}.exclude", family["exclude"], family))
        if "evidence" in family and not isinstance(family["evidence"], dict):
            errors.append(f"{path}.evidence: must be an object.")
        if "approved_at" in family and not isinstance(family["approved_at"], str):
            errors.append(f"{path}.approved_at: must be text.")


def _check_exclude(title: dict, errors: list[str]) -> None:
    groups = _list(title.get("exclude", []), "title.exclude", errors)
    if groups is None:
        return
    origins = _enum("properties", "title", "properties", "exclude", "items", "properties",
                    "origin")
    for index, group in enumerate(groups):
        path = f"title.exclude[{index}]"
        if not isinstance(group, dict) or not isinstance(group.get("terms"), list):
            errors.append(f"{path}: must be an object with a list of terms.")
            continue
        _unlisted(group, {"terms", "origin"}, path, "an exclude group", errors)
        _text_list(group["terms"], f"{path}.terms", errors)
        if "origin" in group and group["origin"] not in origins:
            errors.append(f"{path}.origin: must be {join_or(origins)}.")


def _check_title(data: dict, errors: list[str]) -> None:
    title = data.get("title")
    if not isinstance(title, dict):
        errors.append("title: must be an object with families and exclude.")
        return
    for key in ("families", "exclude"):
        if key not in title:
            errors.append(f"title: is missing {key}.")
    _unlisted(title, {"families", "exclude"}, "title", "the title section", errors)
    _check_families(title, errors)
    _check_exclude(title, errors)


# Tiers, banding, one per company --------------------------------------------------------------

def _check_tiers(data: dict, errors: list[str]) -> None:
    tiers = data.get("tiers")
    if not isinstance(tiers, list) or [
        t.get("id") if isinstance(t, dict) else None for t in tiers
    ] != _TIER_IDS:
        errors.append("tiers: must be exactly A, B and C, in that order.")
        return
    for index, tier in enumerate(tiers):
        _unlisted(tier, {"id", "min_score", "meaning"}, f"tiers[{index}]", "a tier", errors)
        if "meaning" in tier and not isinstance(tier["meaning"], str):
            errors.append(f"tiers[{index}].meaning: must be text.")
    scores = [t.get("min_score") for t in tiers]
    if not all(_is_int(s) and 0 <= s <= 100 for s in scores):
        errors.append("tiers: every tier needs a min_score that is a whole number from 0 to 100.")
    elif not (scores[0] > scores[1] > scores[2]):
        errors.append("tiers: min_score values must go down from A to C with no ties.")


def validate_criteria(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["The scoring config must be a JSON object."]
    errors: list[str] = []
    required = list(_schema()["required"])
    for key in required:
        if key not in data:
            errors.append(f"The scoring config is missing {key}.")
    for key in sorted(set(data) - set(required) - {"reserved"}):
        errors.append(f"The scoring config has an unknown key: {key}.")
    if "schema_version" in data and (not _is_int(data["schema_version"])
                                     or data["schema_version"] != 1):
        errors.append("schema_version must be 1.")
    version = data.get("criteria_version")
    if "criteria_version" in data and not (isinstance(version, str) and _VERSION.fullmatch(version)):
        errors.append("criteria_version must look like 1.0 or 2.3.")
    created = data.get("created_from")
    if "created_from" in data and not (isinstance(created, str) and _CREATED_FROM.fullmatch(created)):
        errors.append("created_from must be intake, retune or proposal:p-NNNN.")
    if "hard_filters" in data:
        _check_hard_filters(data, errors)
    if "title" in data:
        _check_title(data, errors)
    if "criteria" in data:
        _check_criteria(data, errors)
    if "bonuses" in data:
        _check_bonuses(data, errors)
    if "tiers" in data:
        _check_tiers(data, errors)
    if "banding" in data:
        banding = data["banding"]
        value = banding.get("provisional_below_coverage") if isinstance(banding, dict) else None
        if not _is_int(value) or not 0 <= value <= 100:
            errors.append("banding: provisional_below_coverage must be a whole number from 0 to 100.")
        elif isinstance(banding, dict):
            _unlisted(banding, {"provisional_below_coverage"}, "banding", "the banding section",
                      errors)
    if "one_per_company" in data:
        one = data["one_per_company"]
        if not isinstance(one, dict) or not isinstance(one.get("enabled"), bool):
            errors.append("one_per_company: enabled must be true or false.")
        else:
            _unlisted(one, {"enabled", "keep"}, "one_per_company", "this section", errors)
            if "keep" in one and one["keep"] != "best":
                errors.append('one_per_company.keep: must be "best".')
    if "reserved" in data and not isinstance(data["reserved"], dict):
        errors.append("reserved: must be an object.")
    return sorted(errors)
