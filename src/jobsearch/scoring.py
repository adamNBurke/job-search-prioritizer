"""Scoring: a role's score, coverage, plain-English reason and gaps, all from code.

Purpose: turn one role and the user's criteria.json into a number the user can trust and a
sentence that says why, without a model doing any arithmetic.
Logic: score_role first runs apply_filters; a role that fails is returned rejected with its
reason and no score. A role posted more than the age_bands criterion's reject_after_days before
the scan date is rejected the same way (filter id posting_age). Otherwise each criterion runs through its evaluator in EVALUATORS (the
eight evaluators docs/data-files.md lists) and returns a CriterionResult of 0 to 3 points, or a
gap. A gap leaves both the numerator and the denominator, its label is listed in the role's
gaps, and it lowers coverage but never the score. Points are whole numbers from 0 to 3, except
keyword_coverage with share true, whose points are an exact fraction, 3 x matched skill
weight / total skill weight, so a single real hit earns credit and no hit earns 0. The base score
is 100 times the earned share of attainable points over the evaluable criteria, computed as one
exact fraction (never rounded before the score) and rounded once, half-up, to an integer; never
float. For a config with no share every points value is whole, so the score is the same digits as before. Only
the ranked JSON rounds a non-whole points value for display (half-up, two decimals). Each bonus
adds max_points times points over 3 (a bonus gap adds 0), as an exact fraction, the bonus total
is rounded half-up to a whole point, and the sum is clamped to 0 through 100. coverage is the half-up integer
share of total weight that was evaluable. A role is provisional when coverage is below the
config's provisional_below_coverage (equal is not provisional) or when nothing could be
evaluated, and a provisional role is held at tier B at most.
Tier caps never change a score: ScoredRole.tier_cap is "B" when the role
carries a cap, and cap_reasons says why in plain words, from fixed vocabulary and integers only.
A level_match criterion whose params set cap_off_level true caps a role whose title names a
level other than the target, or whose description asks for more than the user's years plus two;
a title that names no level word is never capped for level. A keyword_coverage criterion whose
params set cap_no_skill true caps a role whose text is readable but names none of the skills;
a gap (no text, or no skills listed) is never capped for skills, and the cap reads the
matched skills, never the points. An age_bands criterion whose params set cap_after_days caps a role
posted more than that many days before the scan date ("it was posted N days ago"); a
role with no posted date is never capped or rejected for age, and the cap reads the age, never
the points. Reasons have fixed places: ScoredRole.reason_lines holds seven labeled
lines in one order, Why it fits, Where and how, Pay, Skills, Level, Posted, Company (SLOTS), and a
criterion's slot comes from its evaluator and field, never from its points. Why it fits takes the
title ladder, then a term_scale on the title or description, then any other evaluator, then the
reasons of bonuses that earned points; with no criterion there it names the title family the role
matched. A gapped criterion reads "not stated" when it is the slot's only entry and "<label>: not
stated." otherwise; a slot no criterion feeds reads "not one of your criteria". reason_text is the
same lines joined by single spaces, each as "<label>: <text>" ending in a period. Hard filters
that were unknown under policy "gap" join the gaps (filters.gap_filters) and never change score
or coverage; each is printed once, in the first slot whose criteria read one of its fields, or on
a trailing "Also not stated" line when no slot reads it. One missing fact is listed once: a filter
whose posting fields a gapped criterion already covers is not listed again, and neither is one
whose field an evaluator already says is not stated in its own sentence
(CriterionResult.unstated_fields, set by location_match when the work mode is unknown).
Skill matching: keyword_coverage reads skills through skill_mentioned, which adds the
variants and blanks the not-a-skill phrases listed in src/jobsearch/data/skill_words.json (the
verb "excel at" is not the Excel skill; "culture of continuous improvement" is not process
improvement; "budgets" hits budgeting). _mentions stays the plain matcher for the other readers.
Guardrails: the evaluator registry is closed and config can only name an entry in it, so a
config can never run code; a bonus or criterion that names an evaluator not registered here
raises UserError naming it. Reason templates are filled by one pass of plain replacement of
the documented reason keys, never str.format, so a template cannot reach object attributes;
skill and term texts are matched as escaped text on whole words, never as patterns. Ages are
measured to the scan date passed in as today, never the wall clock, so the same inputs always
give the same bytes. Hourly, weekly and monthly pay count as missing data (a planning
decision). Gaps are listed in a fixed order (by criterion id) so reordering the criteria never
changes a reason.
Skill rarity: a keyword_coverage criterion whose params set rarity true is scored
with each skill's weight multiplied by 3, 2 or 1 by the share of the scan's readable postings
that name it (skill_rarity; 1/20 or less, 1/5 or less, more; a skill no posting names counts once).
score_all measures it once over every role it is given, before filters, and scores with an internal
copy of the config, so results stay deterministic, details keep the criterion weight, and
a config without rarity scores exactly as before. A priority skill's weight 2 and the factor
multiply. A skill marked priority true is never given a factor below PRIORITY_RARITY_FLOOR (2),
so a common priority skill counts 2 x 2 = 4 and not 2; skill_rarity reports the plain
measurement.
QA checklist: a new evaluator is one function added to EVALUATORS with the signature
(facts, criterion, compiled, today) -> CriterionResult, a reason default, a row in
docs/data-files.md and a row in criteria.py; each evaluator needs a hit, a miss and a gap test.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from jobsearch import fsio
from jobsearch.errors import UserError
from jobsearch.families import (
    LEVELS, CompiledFamilies, _term_pattern, compile_families, match_family)
from jobsearch.filters import FilterFailure, annualize, apply_filters, expand_fields, field_value
from jobsearch.filters import _field_text, filter_fields, gap_filters, money
from jobsearch.locations import Place, place_matches, read_places, user_place
from jobsearch.roles_store import RoleFacts

__all__ = [
    "CriterionResult", "FilterFailure", "ScoredRole", "EVALUATORS", "score_role", "score_all",
    "title_level_named", "CAP_TIER", "skill_rarity", "skill_mentioned",
]

# The one tier a cap can hold a role at: a capped role is shown at B at most.
CAP_TIER = "B"


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    label: str
    weight: int
    points: int | Fraction
    max_points: int
    reason: str
    gap: bool
    cap_reason: str | None = None
    # Posting fields this result says in its own sentence are not stated, so a hard-filter
    # gap on the same field is not listed again (only location_match sets it today).
    unstated_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoredRole:
    role_id: str
    source: str
    company: str
    title: str
    locations: list[str]
    url_apply: str
    first_seen: str
    last_seen: str
    family_id: str | None
    family_label: str | None
    rejected: FilterFailure | None
    score: int
    coverage: int
    provisional: bool
    details: list[CriterionResult] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    reason_text: str = ""
    reason_lines: list[tuple[str, str]] = field(default_factory=list)
    bonus_points: int = 0
    tier_cap: str | None = None
    cap_reasons: list[str] = field(default_factory=list)
    # The posting's posted date, used only to break a tie between two roles at one company
    # (the newer posting wins). It is not written to any ranked file.
    posted: str | None = None


Evaluator = Callable[[RoleFacts, dict, CompiledFamilies, date], CriterionResult]


_PLACEHOLDER = re.compile(r"\{(\w+)\}")
MAX_POINTS = 3


def _template(criterion: dict, key: str, default: str, **values: object) -> str:
    """The reason text for one points value, from the config template or the code default.

    Only the keys passed in (the ones docs/data-files.md lists for the evaluator) are filled, in
    one pass over the template. Any other brace text stays literal, and str.format is never
    called on config text, so a template cannot reach object attributes.
    """
    reasons = criterion.get("reasons")
    reasons = reasons if isinstance(reasons, dict) else {}
    text = reasons.get(key) or reasons.get("default") or default
    if not isinstance(text, str):
        text = default

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER.sub(fill, text)


def _result(criterion: dict, points: int | Fraction, max_points: int, reason: str,
            gap: bool = False, cap_reason: str | None = None,
            unstated_fields: tuple[str, ...] = ()) -> CriterionResult:
    return CriterionResult(
        criterion_id=str(criterion["id"]), label=str(criterion.get("label", criterion["id"])),
        weight=int(criterion["weight"]), points=points, max_points=max_points,
        reason=reason, gap=gap, cap_reason=cap_reason, unstated_fields=unstated_fields,
    )


def _title_ladder(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                  today: date) -> CriterionResult:
    match = match_family(facts.posting.title, compiled)
    if match is None:
        return _result(criterion, 0, 3, _template(
            criterion, "0", "Title is outside every family you approved."))
    points = max(0, min(match.rung, 3))
    default = f'Title matches your "{match.label}" family.'
    return _result(criterion, points, 3, _template(
        criterion, str(points), default, family=match.label))


def _age_days(posted: str, today: date) -> int | None:
    """Whole days since the posting date, or None when the date is not a real calendar day.

    The posting model accepts any well-formed timestamp, so "2026-02-30T00:00:00Z" reaches here.
    It is treated like a missing date, never a crash.
    """
    try:
        posted_on = datetime.strptime(posted[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (today - posted_on).days)


def _age_clause(age: int) -> str:
    """The plain-words cap clause for an old posting: fixed words and one integer."""
    return f"it was posted {age} days ago"


def _day_limit(params: dict, key: str) -> int | None:
    """A whole-day limit from the age_bands params, or None when absent or malformed.

    A bool is never a number, so a hand-edited true cannot switch a rule on.
    """
    value = params.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _age_failure(facts: RoleFacts, criteria: dict, today: date) -> FilterFailure | None:
    """The age reject: a role posted more than reject_after_days before the scan.

    The rule is a param of the age_bands criterion, applied here because a hard filter never
    sees the scan date. A role with no posted date, or one that is not a real date, is never
    rejected for age.
    """
    posted = facts.posting.posted
    if posted is None:
        return None
    for criterion in criteria.get("criteria", []):
        if criterion.get("evaluator") != "age_bands":
            continue
        limit = _day_limit(criterion.get("params", {}), "reject_after_days")
        if limit is None:
            continue
        age = _age_days(posted, today)
        if age is None or age <= limit:
            return None
        return FilterFailure(
            "posting_age", f"Posted more than {limit} days ago",
            f"Posted {age} days ago, more than {limit} days, so it is likely filled or no "
            "longer being filled.")
    return None


def _age_bands(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
               today: date) -> CriterionResult:
    params = criterion.get("params", {})
    bands = sorted(((int(limit), int(points)) for limit, points in params.get("bands", [])))
    fallback = int(params.get("else", 0))
    max_points = MAX_POINTS
    age = None if facts.posting.posted is None else _age_days(facts.posting.posted, today)
    if age is None:
        return _result(criterion, 0, max_points, "posting date not published", gap=True)
    points = next((p for limit, p in bands if limit >= age), fallback)
    if age == 0:
        default = "Posted today."
    elif age == 1:
        default = "Posted 1 day ago."
    else:
        default = f"Posted {age} days ago."
    # The cap reads the age, never the points. Past cap_after_days it holds the role at B.
    cap_after = _day_limit(params, "cap_after_days")
    return _result(criterion, points, max_points, _template(
        criterion, str(points), default, days=age),
        cap_reason=_age_clause(age) if cap_after is not None and age > cap_after else None)


def _text_of(facts: RoleFacts, name: str) -> str | None:
    """The NFKC text a keyword or term evaluator reads, or None when there is nothing to read."""
    text = _field_text(facts, name)
    return unicodedata.normalize("NFKC", text) if text else None


@functools.lru_cache(maxsize=512)
def _patterns(names: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Whole-word, case-insensitive matchers for literal terms, compiled once per distinct set.

    A term is escaped text, never a pattern.
    """
    found = (_term_pattern(name) for name in names if isinstance(name, str))
    return tuple(p for p in found if p is not None)


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    return any(p.search(text) for p in _patterns(names))


@functools.cache
def _skill_words() -> dict:
    """Skill variants and not-a-skill phrases, from the package data file skill_words.json.

    The file holds literal phrases only; they are escaped and matched as text, never compiled as
    patterns from config or posting text.
    """
    path = Path(__file__).resolve().parent / "data" / "skill_words.json"
    return json.loads(fsio.read_text(path))


def _skill_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


@functools.lru_cache(maxsize=512)
def _skill_matcher(name: str) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    """(patterns to blank out, patterns to find) for one skill name, compiled once per name."""
    words = _skill_words()
    key = _skill_key(name)
    rules = words.get("not_skill", {}).get(key, {})
    blank = list(_patterns(tuple(rules.get("any_case", []))))
    for phrase in rules.get("exact_case", []):
        parts = phrase.split()
        if parts:
            body = r"\s+".join(re.escape(part) for part in parts)
            blank.append(re.compile(rf"(?<!\w){body}(?!\w)"))
    for phrase in rules.get("sentence_start", []):
        parts = phrase.split()
        if parts:
            body = r"\s+".join(re.escape(part) for part in parts)
            blank.append(re.compile(rf"(?:^|(?<=[.!?:;\n\u2022*\-]))\s*{body}(?!\w)"))
    find = _patterns((name, *words.get("variants", {}).get(key, [])))
    return tuple(blank), find


def skill_mentioned(text: str, names: tuple[str, ...]) -> bool:
    """Whether the text names the skill or one of its aliases, as a skill and not as other words.

    Per name, the not_skill phrases (the verb "excel at", values boilerplate such as "culture of
    continuous improvement") are blanked first, then the name and its variants are searched as
    whole words, case-insensitively. A name with no entry in skill_words.json matches exactly as
    _mentions does.
    """
    for name in names:
        if not isinstance(name, str):
            continue
        blank, find = _skill_matcher(name)
        cleaned = text
        for pattern in blank:
            cleaned = pattern.sub(" ", cleaned)
        if any(p.search(cleaned) for p in find):
            return True
    return False


def _field_name(criterion: dict, default: str) -> str:
    name = criterion.get("field")
    return name if isinstance(name, str) and name else default


def _skill_entries(params: dict) -> list[tuple[str, tuple[str, ...], int]]:
    entries = []
    for key in ("must_have", "nice"):
        for item in params.get(key) or []:
            name = str(item.get("term", "")).strip()
            if not name:
                continue
            aliases = tuple(str(a) for a in item.get("aliases") or [])
            entries.append((name, (name, *aliases), max(1, int(item.get("weight", 1)))))
    return entries


def _no_skill_clause(total: int, where: str) -> str:
    """The tier-cap clause for a readable role that names none of the user's skills.

    Fixed words and one integer only, so posting text and skill names never reach it."""
    if total == 1:
        return f"your key skill does not appear in {where}"
    return f"none of your {total} key skills appear in {where}"


def _keyword_coverage(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                      today: date) -> CriterionResult:
    params = criterion.get("params", {})
    entries = _skill_entries(params)
    if not entries:
        return _result(criterion, 0, MAX_POINTS, "no key skills listed", gap=True)
    field_name = _field_name(criterion, "text")
    text = _text_of(facts, field_name)
    if text is None:
        return _result(criterion, 0, MAX_POINTS, "no description text to read", gap=True)
    hits = [name for name, names, _ in entries if skill_mentioned(text, names)]
    total_weight = sum(weight for _, _, weight in entries)
    hit_weight = sum(weight for name, _, weight in entries if name in hits)
    points: int | Fraction
    if params.get("share") is True:
        # 3 x matched weight / total weight, held exact so one real hit earns credit.
        points = Fraction(MAX_POINTS * hit_weight, total_weight)
    else:
        bands = sorted(((Decimal(str(share)), int(p)) for share, p in params["bands"]),
                       reverse=True)
        points = next((p for share, p in bands if Decimal(hit_weight) >= share * total_weight), 0)
    where = "the title" if field_name == "title" else "the description"
    total = len(entries)
    # The cap is decided on the matched skills, never on points (a band can score a real
    # hit at 0, and share gives a Fraction). Both gap returns above carry no cap.
    cap_reason = (_no_skill_clause(total, where)
                  if params.get("cap_no_skill") is True and not hits else None)
    if total == 1:
        default = (f"Your key skill appears in {where} ({hits[0]})." if hits
                   else f"Your key skill does not appear in {where}.")
    elif hits:
        default = f"{len(hits)} of your {total} key skills appear in {where} ({', '.join(hits)})."
    else:
        default = f"None of your {total} key skills appear in {where}."
    # The "0" template means no skill appeared. A role with a few hits can still earn 0 points
    # (a small share), and must not read "none", so it takes the default template instead.
    if params.get("share") is True:
        key = "0" if not hits else "default"
    else:
        key = str(points) if points else ("0" if not hits else "default")
    return _result(criterion, points, MAX_POINTS, _template(
        criterion, key, default,
        hit=len(hits), total=total, hits_list=", ".join(hits) or "none"), cap_reason=cap_reason)


# How rare a skill is in the scan's own postings sets its weight inside "skills".
# A skill named by 1/20 of the readable postings or fewer counts 3 times, by 1/5 or fewer 2 times,
# otherwise once; a skill no posting names counts once. Exact fractions, never floats.
RARE_SHARE = Fraction(1, 20)
UNCOMMON_SHARE = Fraction(1, 5)
# A skill the user marked as a priority never drops below this factor, so
# how common it is in the scan cannot cancel the user's own choice. skill_rarity stays the pure
# measurement; only _with_rarity applies the floor.
PRIORITY_RARITY_FLOOR = 2


def skill_rarity(facts_list: list[RoleFacts], criterion: dict) -> dict[str, int]:
    """The rarity factor (3, 2 or 1) of each skill the keyword_coverage criterion lists.

    The corpus is every role in facts_list whose text for the criterion's field is readable, so
    it is the same set the criterion reads, and a posting with no body counts for nothing.
    """
    entries = _skill_entries(criterion.get("params", {}))
    field_name = _field_name(criterion, "text")
    texts = [t for t in (_text_of(f, field_name) for f in facts_list) if t is not None]
    factors: dict[str, int] = {}
    for name, names, _ in entries:
        named = sum(1 for text in texts if skill_mentioned(text, names))
        share = Fraction(named, len(texts)) if texts else Fraction(0)
        if named == 0 or share > UNCOMMON_SHARE:
            factors[name] = 1
        elif share <= RARE_SHARE:
            factors[name] = 3
        else:
            factors[name] = 2
    return factors


def _with_rarity(facts_list: list[RoleFacts], criteria: dict) -> dict:
    """The config to score with: a copy whose rarity-on skill weights are multiplied by their
    factor. The caller's dict is never changed, and a config with no rarity is returned as is."""
    def wanted(item: object) -> bool:
        return (isinstance(item, dict) and item.get("evaluator") == "keyword_coverage"
                and isinstance(item.get("params"), dict) and item["params"].get("rarity") is True)

    if not any(wanted(i) for key in ("criteria", "bonuses") for i in criteria.get(key) or []):
        return criteria
    scoring_copy = copy.deepcopy(criteria)
    for key in ("criteria", "bonuses"):
        for item in scoring_copy.get(key) or []:
            if not wanted(item):
                continue
            factors = skill_rarity(facts_list, item)
            for group in ("must_have", "nice"):
                for skill in item["params"].get(group) or []:
                    name = str(skill.get("term", "")).strip()
                    factor = factors.get(name, 1)
                    if skill.get("priority") is True:
                        factor = max(factor, PRIORITY_RARITY_FLOOR)
                    skill["weight"] = max(1, int(skill.get("weight", 1))) * factor
    return scoring_copy


def _band_points(value: Decimal, bands: list) -> int:
    """Points of the first band whose threshold is at or below the value, else 0."""
    ordered = sorted(((Decimal(str(t)), int(p)) for t, p in bands), reverse=True)
    return next((points for threshold, points in ordered if value >= threshold), 0)


def _banded_number(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                   today: date) -> CriterionResult:
    posting = facts.posting
    name = _field_name(criterion, "comp_max")
    stated = getattr(posting, name, None)
    want = criterion.get("params", {}).get("currency")
    if stated is None:
        return _result(criterion, 0, MAX_POINTS, "pay not stated", gap=True)
    if want and (posting.comp_currency or "").upper() != str(want).upper():
        return _result(criterion, 0, MAX_POINTS, "pay not in your currency", gap=True)
    # Hourly, weekly and monthly figures are treated as missing data here.
    annual = annualize(stated, posting.comp_interval) if posting.comp_interval in (
        None, "year") else None
    if annual is None:
        return _result(criterion, 0, MAX_POINTS, "pay not stated as a yearly figure", gap=True)
    points = _band_points(annual, criterion.get("params", {}).get("bands", []))
    shown = money(annual, posting.comp_currency)
    side = "Top" if name == "comp_max" else "Bottom"
    return _result(criterion, points, MAX_POINTS, _template(
        criterion, str(points), f"{side} of the stated range is {shown}.",
        value=shown, currency=posting.comp_currency or ""))


_FIELD_WORDS = {
    "company_size": "Company size", "company_stage": "Company stage",
    "work_mode": "Work arrangement", "company_industry": "Industry", "company": "Company",
    "title": "Title", "description_text": "Description", "text": "Posting",
}


def _enum_scale(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                today: date) -> CriterionResult:
    name = _field_name(criterion, "company_size")
    value = field_value(facts, name)
    if value is None or str(value).strip() in ("", "unknown"):
        return _result(criterion, 0, MAX_POINTS, f"{_FIELD_WORDS.get(name, name)} not stated",
                       gap=True)
    values = {str(k).casefold(): int(v) for k, v in criterion.get("params", {})
              .get("values", {}).items()}
    points = values.get(str(value).casefold(), 0)
    word = _FIELD_WORDS.get(name, name)
    return _result(criterion, points, MAX_POINTS, _template(
        criterion, str(points), f"{word} is {value}.", value=value))


def _term_scale(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                today: date) -> CriterionResult:
    name = _field_name(criterion, "company_industry")
    word = _FIELD_WORDS.get(name, name)
    text = _text_of(facts, name)
    if text is None:
        return _result(criterion, 0, MAX_POINTS, f"{word} not stated", gap=True)
    terms = criterion.get("params", {}).get("terms", {})
    found = sorted(((-int(points), term) for term, points in terms.items()
                    if _mentions(text, (term,))), key=lambda item: (item[0], item[1].casefold()))
    if not found:
        return _result(criterion, 0, MAX_POINTS, _template(
            criterion, "0", f"{word} mentions none of the terms you listed.", term=""))
    points, term = -found[0][0], found[0][1]
    return _result(criterion, points, MAX_POINTS, _template(
        criterion, str(points), f'{word} mentions "{term}".', term=term))




@functools.cache
def _seniority() -> dict:
    path = Path(__file__).resolve().parent / "data" / "seniority.json"
    return json.loads(fsio.read_text(path))


def seniority_tokens() -> dict[str, list[str]]:
    """Title words per level, from the package data file (src/jobsearch/data/seniority.json)."""
    return _seniority()["levels"]


@functools.cache
def _not_level_patterns() -> tuple[re.Pattern[str], ...]:
    """Phrases where "manager" names an individual-contributor job, not a level.

    Read from the "not_level" list in the same data file; a file without the key has none.
    """
    return _patterns(tuple(_seniority().get("not_level", [])))


@functools.cache
def _level_patterns() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    return tuple((level, _patterns(tuple(tokens))) for level, tokens in seniority_tokens().items())


def title_level_named(title: str) -> str | None:
    """The highest level any seniority word in the title names, or None when it names none.

    A phrase from the not_level list ("product manager") is blanked out first, so its "manager"
    is not read as a level while other words in the title ("senior") still count.
    """
    text = unicodedata.normalize("NFKC", title)
    for phrase in _not_level_patterns():
        text = phrase.sub(" ", text)
    named = [level for level, patterns in _level_patterns()
             if any(p.search(text) for p in patterns)]
    return max(named, key=LEVELS.index) if named else None


_YEARS_OF_EXPERIENCE = re.compile(
    r"(?<![\w.])(\d{1,2})\s*\+?\s*(?:(?:-|\u2013|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b"
    r"[^.\n;]{0,60}?\bexperience\b", re.IGNORECASE)
_AT_LEAST_YEARS = re.compile(
    r"\b(?:at least|minimum(?: of)?|min\.?)\s+(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE)


_REQUIREMENTS_HEADING = re.compile(
    r"^\W*(?:(?:minimum |basic |required |preferred )?(?:requirements?|qualifications?)"
    r"|what you(?:['\u2019]ll| will)? (?:need|bring)|what we(?:['\u2019]re| are) looking for"
    r"|about you|who you are|must[- ]haves?|you have)\W*$", re.IGNORECASE)
_SEGMENTS = re.compile(r"(?<=[.!?])\s+|\n+")
_COMPANY_VOICE = re.compile(r"\b(?:we|we['\u2019]ve|we['\u2019]re|our|us)\b", re.IGNORECASE)
_ASKING = re.compile(
    r"\b(?:you|your|candidates?|applicants?|required?|requires?|must|minimum|at least|"
    r"qualifications?|looking for|seeking|ideal)\b", re.IGNORECASE)


def _years_in(segments: list[str], skip_boilerplate: bool) -> list[int]:
    found: list[int] = []
    for segment in segments:
        if skip_boilerplate and _COMPANY_VOICE.search(segment) and not _ASKING.search(segment):
            continue  # "With over 20 years of experience in the industry, we serve ..."
        found += [int(m.group(1)) for pattern in (_YEARS_OF_EXPERIENCE, _AT_LEAST_YEARS)
                  for m in pattern.finditer(segment)]
    return found


def years_asked(description: str) -> int | None:
    """The most years of experience the description asks for (the low end of a range).

    Years under a requirements-style heading ("Requirements", "Qualifications", "About you")
    are read alone when there are any. Otherwise every sentence counts except one that speaks
    for the company ("we", "our") without asking anything of the reader, so a line such as
    "With over 20 years of experience in the industry, we serve clients" is not an ask.
    """
    lines = unicodedata.normalize("NFKC", description).split("\n")
    for index, line in enumerate(lines):
        if len(line) <= 50 and _REQUIREMENTS_HEADING.match(line.strip()):
            scoped = _years_in(_SEGMENTS.split("\n".join(lines[index + 1:])), True)
            if scoped:
                return max(scoped)
            break
    found = _years_in(_SEGMENTS.split("\n".join(lines)), True)
    return max(found) if found else None


def _level_match(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                 today: date) -> CriterionResult:
    params = criterion.get("params", {})
    target, years = params.get("target"), params.get("years")
    if target is None and years is None:
        return _result(criterion, 0, MAX_POINTS, "no level or experience given", gap=True)
    asked = years_asked(facts.posting.description_text) if years is not None else None
    too_senior = asked is not None and asked > years + 2
    named = title_level_named(facts.posting.title)
    level = named or "mid"
    if target is None:
        if asked is None:
            return _result(criterion, 0, MAX_POINTS, "no experience asked for", gap=True)
        base = MAX_POINTS
        default = f"The description asks for {asked} years of experience; you have {years}."
    else:
        base = max(0, MAX_POINTS - abs(LEVELS.index(level) - LEVELS.index(target)))
        default = f"Title reads as {level} level; you are aiming for {target}."
    points = max(0, base - 1) if too_senior else base
    if too_senior and target is not None:
        default += f" The description asks for {asked} years, more than your {years}."
    # The cap is a separate gate from the points: it only reads, never changes them.
    # It fires only when the config asks for it (the user cares about level), and a title that
    # names no level word is never capped, because "mid" there is a default and not a reading.
    clauses: list[str] = []
    if params.get("cap_off_level") is True:
        if target is not None and named is not None and named != target:
            clauses.append(f"the title reads as {named} level and you are aiming for {target}")
        if too_senior:
            clauses.append(f"the description asks for {asked} years and you have {years}")
    return _result(criterion, points, MAX_POINTS, _template(
        criterion, str(points), default, level=level, target=target or "any level"),
        cap_reason="; ".join(clauses) or None)


_MODE_WORDS = {"remote": "remote", "hybrid": "hybrid", "onsite": "on-site"}
_UNREAD_PLACE = "a place in the posting or in your list could not be read"


@functools.lru_cache(maxsize=64)
def _user_places(places: tuple[str, ...]) -> tuple[tuple[str, list[Place], bool], ...]:
    """Each listed place with the places read from it and whether any part of it was unreadable."""
    return tuple((text, found, bool(unread) or not found)
                 for text in places for found, unread in [user_place(text)])


def _location_match(facts: RoleFacts, criterion: dict, compiled: CompiledFamilies,
                    today: date) -> CriterionResult:
    params = criterion.get("params", {})
    wanted = set(params.get("modes") or []) or set(_MODE_WORDS)
    listed = _user_places(tuple(str(p) for p in params.get("places") or []))
    mine = [(text, found) for text, found, _ in listed if found]
    posting = facts.posting
    theirs, their_unread = read_places(list(posting.locations))
    # An unreadable place only matters when no readable one matches: it might have been the match.
    unread = bool(their_unread) or any(bad for _, _, bad in listed)
    mode = posting.work_mode
    if mode == "unknown" and any(p.remote for p in theirs):
        mode = "remote"
    matched = next((text for text, found in mine if place_matches(theirs, found)), None)
    where = matched or ", ".join(text for text, _ in mine)
    fill = {"mode": _MODE_WORDS.get(mode, "unstated"), "place": where}

    def done(points: int, default: str,
             unstated: tuple[str, ...] = ()) -> CriterionResult:
        return _result(criterion, points, MAX_POINTS,
                       _template(criterion, str(points), default, **fill),
                       unstated_fields=unstated)

    def gap(why: str) -> CriterionResult:
        return _result(criterion, 0, MAX_POINTS, why, gap=True)

    if mode == "unknown":
        if not theirs or not mine:
            return gap("work arrangement and place not stated")
        if matched:
            return done(2, f"The posting does not say if it is remote, but it is in {matched}.",
                        ("work_mode",))
        if unread:
            return gap(_UNREAD_PLACE)
        return done(0, "The posting does not say if it is remote, and it is not in "
                       f"{where}.", ("work_mode",))
    word = _MODE_WORDS[mode].capitalize()
    if mode not in wanted:
        return done(0, f"{word} work is not an arrangement you want.")
    if mode == "remote":
        return done(3, "Remote, which fits what you want.")
    if not listed:
        return done(3, f"{word} work fits what you want.")
    if not theirs:
        return gap("place not stated")
    if matched:
        return done(3, f"{word} in {matched}, which fits what you want.")
    if unread:
        return gap(_UNREAD_PLACE)
    return done(1, f"{word} work, but not in {where}.")


EVALUATORS: dict[str, Evaluator] = {
    "title_ladder": _title_ladder,
    "age_bands": _age_bands,
    "keyword_coverage": _keyword_coverage,
    "banded_number": _banded_number,
    "enum_scale": _enum_scale,
    "term_scale": _term_scale,
    "level_match": _level_match,
    "location_match": _location_match,
}


def _criterion_fields(criterion: dict) -> frozenset[str]:
    """The posting fields an evaluator reads, from its own defaults (an evaluator this version
    does not know reads none), so a filter gap on the same fact is not listed a second time."""
    name = criterion.get("evaluator")
    if name == "location_match":
        return frozenset({"work_mode", "locations"})
    if name == "level_match":
        return frozenset({"title", "description_text"})
    if name == "age_bands":
        return frozenset({"posted"})
    if name == "title_ladder":
        return frozenset({"title"})
    defaults = {"keyword_coverage": "text", "banded_number": "comp_max",
                "enum_scale": "company_size", "term_scale": "company_industry"}
    if name in defaults:
        return expand_fields({_field_name(criterion, defaults[name])})
    return frozenset()


def _half_up(value: Fraction) -> int:
    """An exact Fraction rounded once, half-up, to a whole number."""
    return math.floor(value + Fraction(1, 2))


# Every ranked entry reads the same seven labeled lines in this order. The slot of a
# criterion comes from its evaluator (and field), never from its id or its points.
SLOTS = ("Why it fits", "Where and how", "Pay", "Skills", "Level", "Posted", "Company")
ALSO_NOT_STATED = "Also not stated"
NOT_STATED = "not stated"
NOT_ONE_OF_YOUR_CRITERIA = "not one of your criteria"
_WHY, _WHERE, _PAY, _SKILLS, _LEVEL, _POSTED, _COMPANY = range(len(SLOTS))


def _slot_of(criterion: dict) -> tuple[int, int]:
    """(slot index, order inside the slot) of a criterion; ties inside a slot go by criterion id.

    Inside Why it fits the title ladder comes first, then a term_scale on the title or the
    description, then any evaluator this table does not name.
    """
    name = criterion.get("evaluator")
    fixed = {"location_match": _WHERE, "banded_number": _PAY, "keyword_coverage": _SKILLS,
             "level_match": _LEVEL, "age_bands": _POSTED}
    if name in fixed:
        return fixed[str(name)], 0
    if name == "title_ladder":
        return _WHY, 0
    if name == "enum_scale":
        field_name = _field_name(criterion, "company_size")
        if field_name == "work_mode":
            return _WHERE, 0
        if field_name in ("company_size", "company_stage"):
            return _COMPANY, 0
    if name == "term_scale":
        field_name = _field_name(criterion, "company_industry")
        if field_name in ("company_industry", "company"):
            return _COMPANY, 0
        if field_name in ("title", "description_text", "text"):
            return _WHY, 1
    return _WHY, 2


def _reason_lines(criteria: dict, details: list[CriterionResult],
                  bonus_reasons: list[str], filter_gaps: list[dict],
                  family_label: str | None) -> list[tuple[str, str]]:
    """The labeled lines of one role: the seven slots in order, then "Also not stated" when a
    hard-filter gap is read by no slot. Every criterion contributes its reason; a gapped one
    reads "not stated" when it is the slot's only entry and "<label>: not stated." otherwise.
    A slot nothing feeds reads "not one of your criteria", except Why it fits, which then names
    the matched title family. A filter gap goes to the first slot that reads one of its fields.
    """
    members: list[list[tuple[tuple[int, str], CriterionResult]]] = [[] for _ in SLOTS]
    read: list[set[str]] = [set() for _ in SLOTS]
    for criterion, detail in zip(criteria.get("criteria", []), details):
        index, order = _slot_of(criterion)
        members[index].append(((order, detail.criterion_id), detail))
        read[index] |= _criterion_fields(criterion)
    placed: list[list[str]] = [[] for _ in SLOTS]
    unplaced: list[str] = []
    for entry in filter_gaps:
        label = str(entry.get("label") or entry["id"])
        fields = filter_fields(entry)
        index = next((i for i in range(len(SLOTS)) if fields & read[i]), None)
        if index is None:
            unplaced.append(label)
        else:
            placed[index].append(f"{label}: {NOT_STATED}.")
    lines: list[tuple[str, str]] = []
    for index, label in enumerate(SLOTS):
        parts: list[CriterionResult | str] = [
            detail for _, detail in sorted(members[index], key=lambda m: m[0])]
        if index == _WHY:
            if not parts and family_label:
                parts.append(f'Title matches your "{family_label}" family.')
            parts += bonus_reasons
        parts += placed[index]
        texts = [part if isinstance(part, str)
                 else part.reason if not part.gap
                 else NOT_STATED if len(parts) == 1 else f"{part.label}: {NOT_STATED}."
                 for part in parts]
        lines.append((label, " ".join(texts) if texts else NOT_ONE_OF_YOUR_CRITERIA))
    if unplaced:
        lines.append((ALSO_NOT_STATED, ", ".join(unplaced) + "."))
    return lines


def _joined(lines: list[tuple[str, str]]) -> str:
    """The lines as one string (the ranked JSON reason_text and the CSV reason): "<label>: <text>"
    parts joined by single spaces, a period added where a text does not end in one."""
    return " ".join(f"{label}: {text}" if text.endswith(".") else f"{label}: {text}."
                    for label, text in lines)


def _run_evaluators(items: list[dict], facts: RoleFacts, compiled: CompiledFamilies,
                    today: date) -> list[CriterionResult]:
    results: list[CriterionResult] = []
    for criterion in items:
        name = str(criterion.get("evaluator"))
        evaluator = EVALUATORS.get(name)
        if evaluator is None:
            raise UserError(
                f'The criterion "{criterion.get("id")}" uses the evaluator "{name}", '
                "which this version cannot run yet."
            )
        results.append(evaluator(facts, criterion, compiled, today))
    return results


def _run_bonuses(criteria: dict, facts: RoleFacts, compiled: CompiledFamilies,
                 today: date) -> list[tuple[CriterionResult, Fraction]]:
    """Each bonus as (result, bonus points). A bonus gap earns nothing and is not listed."""
    items = [dict(bonus, weight=int(bonus["max_points"]))
             for bonus in criteria.get("bonuses") or []]
    earned = []
    for result in _run_evaluators(items, facts, compiled, today):
        points = (Fraction(0) if result.gap
                  else Fraction(result.weight) * Fraction(result.points) / MAX_POINTS)
        earned.append((result, points))
    return earned


def score_role(facts: RoleFacts, criteria: dict, compiled: CompiledFamilies,
               today: date) -> ScoredRole:
    posting = facts.posting
    match = match_family(posting.title, compiled)
    base = {
        "role_id": posting.role_id, "source": posting.source, "company": posting.company,
        "title": posting.title, "locations": list(posting.locations),
        "url_apply": posting.url_apply, "first_seen": facts.first_seen,
        "last_seen": facts.last_seen, "posted": posting.posted,
        "family_id": match.family_id if match else None,
        "family_label": match.label if match else None,
    }
    failure = apply_filters(facts, criteria, compiled)
    if failure is not None:
        return ScoredRole(**base, rejected=failure, score=0, coverage=0, provisional=False,
                          reason_text=failure.reason)
    aged = _age_failure(facts, criteria, today)
    if aged is not None:
        return ScoredRole(**base, rejected=aged, score=0, coverage=0, provisional=False,
                          reason_text=aged.reason)

    details = _run_evaluators(criteria.get("criteria", []), facts, compiled, today)
    evaluable = [d for d in details if not d.gap]
    total_weight = sum(d.weight for d in details)
    evaluable_weight = sum(d.weight for d in evaluable)
    # One exact division: 100 * sum(weight * points) / (3 * evaluable weight), held as a Fraction
    # (a share-shaped skills criterion has fractional points) and rounded once, half-up.
    earned = sum((Fraction(d.weight) * Fraction(d.points) for d in evaluable), Fraction(0))
    attainable = sum(d.weight * d.max_points for d in evaluable)
    base_score = _half_up(Fraction(100) * earned / attainable) if attainable else 0
    coverage = _half_up(Fraction(100 * evaluable_weight, total_weight)) if total_weight else 0
    threshold = int(criteria.get("banding", {}).get("provisional_below_coverage", 0))

    bonuses = _run_bonuses(criteria, facts, compiled, today)
    bonus_points = _half_up(sum((points for _, points in bonuses), Fraction(0)))
    final = max(0, min(100, base_score + bonus_points))

    # Criterion gaps by criterion id, then filter gaps in config order, so reordering the
    # criteria never changes the gap list. A filter gap is listed only when a gapped criterion
    # has not already named the fields it reads, or an evaluator has not already said in its own
    # sentence that they are not stated (unstated_fields), and only once per field.
    gaps: list[str] = []
    covered: set[str] = set()
    by_id = {str(c.get("id")): c for c in criteria.get("criteria", [])}
    for detail in sorted((d for d in details if d.gap), key=lambda d: d.criterion_id):
        if detail.label not in gaps:
            gaps.append(detail.label)
        covered |= _criterion_fields(by_id.get(detail.criterion_id, {}))
    for detail in details:
        covered |= set(detail.unstated_fields)
    listed_filters: list[dict] = []
    for entry in gap_filters(facts, criteria, compiled):
        fields = filter_fields(entry)
        label = str(entry.get("label") or entry["id"])
        if (fields and fields <= covered) or label in gaps:
            continue
        gaps.append(label)
        listed_filters.append(entry)
        covered |= fields
    won = sorted(((r, pts) for r, pts in bonuses if pts > 0),
                 key=lambda item: (-item[1], item[0].criterion_id))
    lines = _reason_lines(criteria, details, [r.reason for r, _ in won], listed_filters,
                          match.label if match else None)
    provisional = not evaluable or coverage < threshold
    # A provisional role is held at tier B, and
    # its clause comes first. It keeps its marker and gaps, and calibration still counts it.
    cap_reasons = ([f"it was scored on only {coverage} percent of your criteria"]
                   if provisional else [])
    cap_reasons += [d.cap_reason for d in sorted(evaluable, key=lambda d: d.criterion_id)
                    if d.cap_reason]
    return ScoredRole(
        **base, rejected=None, score=final, coverage=coverage,
        provisional=provisional,
        details=details, gaps=gaps, reason_text=_joined(lines), reason_lines=lines,
        bonus_points=bonus_points,
        tier_cap=CAP_TIER if cap_reasons else None, cap_reasons=cap_reasons,
    )


def score_all(facts_list: list[RoleFacts], criteria: dict, today: date) -> list[ScoredRole]:
    compiled = compile_families(criteria)
    # Rarity is measured on every role passed in, before any hard filter, so a filter change never
    # moves a skill's weight. Only the internal copy carries the multiplied weights.
    weighted = _with_rarity(facts_list, criteria)
    scored = [score_role(facts, weighted, compiled, today) for facts in facts_list]
    return sorted(scored, key=lambda s: s.role_id)
