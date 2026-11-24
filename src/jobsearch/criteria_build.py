"""Build the scoring config and scan scope from the interview answers, with no judgment calls.

Purpose: the agent runs the interview, but the arithmetic (weights, filters, bands) is code, so
the same answers always give the same config and a stranger can read why each number
is what it is.
Logic: importance_for maps the interview to an importance of 1 to 5 (skills default
to 5 unless in dont_care);
IMPORTANCE_POINTS is the one fixed table from importance to points; weights_from_points turns
points into integer weights that sum to 100 by the largest-remainder method, ties going to the
lower criterion id. build_criteria assembles hard filters (the unknown-fact defaults), title
families (one stated family per target title), the criteria in CRITERION_IDS order with the
documented evaluator params, tiers, banding and the one-per-company switch. Title rungs
are one code rule: family_rung gives rung 3 to the family of the user's first choice
(answers first_choice_title), or to every stated family when that question was skipped; rung 2 to
the other stated families when a first choice is named; for a family title discovery found, rung 2,
or rung 1 when fewer than THIN_EVIDENCE_POSTINGS sampled postings tied to the user's skills or the
evidence is missing. When every family ends up with the same rung title_fit could only score a
constant, so it is left out and weights_from_points shares its points among the other criteria. In the
skills criterion a skill the user marked as mattering most gets weight 2 and priority true, and
every other skill weight 1, so chosen skills count double in the matched share; share is true, so a
role earns 3 x matched weight / total weight and a single real hit earns credit; rarity is
on, so scoring also multiplies each skill by how rare it is in the scan, never below factor 2 for a
priority skill. The level
criterion carries cap_off_level, true unless the user put level in dont_care (that
removes the tier cap but keeps the importance-1 weight). The skills criterion carries cap_no_skill,
true unless the user put skills in dont_care, so a role whose readable description names
none of the skills is held at tier B whatever the weights are. The freshness criterion carries cap_after_days 60 and reject_after_days 180:
a role posted more than 60 days before the scan is held at tier B and one posted more than
180 days before it is rejected, whatever the user's importance for freshness, dont_care included. Banding is provisional_below_coverage
60, so a role scored on under 60 percent of the criteria is held at tier B. The result is checked by answers.validate_answers first. A regenerated stated family
keeps the exclude words the user approved for the family with the same id in the previous config, except a word that now matches one of its terms. build_scan_scope narrows the starter directory.
Guardrails: the build is a pure function of its inputs; every list taken from the answers is
sorted before it enters the config, so the order of keys or list items in answers.json never
changes the output bytes. Money is Decimal with half-up rounding. Terms enter the config as
plain phrases, never as patterns. Nothing here reads a file, the clock or the network.
QA checklist: tests/test_criteria_build.py and tests/test_answers.py (including the priority
weights, the zero-hit reason, the rung rule, the thin-evidence boundary at 4 and 5, and the
omitted title_fit); a changed default needs a case there and, if it changes a file shape, an
update to docs/data-files.md.
"""

from __future__ import annotations

import copy
from decimal import ROUND_HALF_UP, Decimal

from jobsearch.answers import CRITERION_IDS, AnswersError, validate_answers
from jobsearch.criteria import join_or
from jobsearch.families import normalize_title, rules_out_own_terms
from jobsearch.filters import ANNUAL_FACTORS

STATED_PREFIX = "tf-s-"  # ids criteria-build regenerates from the answers; titles-apply may not use it

IMPORTANCE_POINTS = {1: 1, 2: 2, 3: 4, 4: 6, 5: 9}

TOP_IMPORTANCE = 5
DONT_CARE_IMPORTANCE = 1
DEFAULT_IMPORTANCE = 3
# Skills count as mattering most unless the user puts them in dont_care.
SKILLS_IMPORTANCE = TOP_IMPORTANCE
PRIORITY_SKILL_WEIGHT = 2  # a skill the user said matters most counts double inside "skills"
FRESHNESS_IMPORTANCE = 2
# A role posted more than AGE_CAP_DAYS before the scan is held at tier B, and one posted more than
# AGE_REJECT_DAYS before it is rejected as likely dead. Written into the freshness params.
AGE_CAP_DAYS = 60
AGE_REJECT_DAYS = 180
# Title rungs: the first choice is top, other stated families are one
# lower, and a discovered family is one lower again when its evidence is thin.
TOP_RUNG = 3
OTHER_RUNG = 2
THIN_RUNG = 1
# Fewer sampled postings than this asking for the user's skills is thin evidence: five is the
# smallest count that is not one or two coincidences in a sample of up to 1200 postings.
THIN_EVIDENCE_POSTINGS = 5
# A role scored on less than this share of the user's criteria is provisional and held at tier B
PROVISIONAL_BELOW_COVERAGE = 60
_ALL_MODES = ("hybrid", "onsite", "remote")
# Phrases that say a posting will not sponsor a visa. Only used when the user said they need one.
NO_SPONSOR_PHRASES = (
    "cannot sponsor", "no visa sponsorship", "not able to sponsor", "not sponsor",
    "unable to sponsor", "without sponsorship",
)


# Importance and weights ---------------------------------------------------------------------

def _skill_item(skill: dict) -> dict:
    """One must_have item. priority true appears only for a skill the user marked, so scoring reads
    the user's choice and not a weight."""
    item = {"term": skill["term"], "aliases": _sorted_unique(skill["aliases"]),
            "weight": PRIORITY_SKILL_WEIGHT if skill.get("priority") is True else 1}
    if skill.get("priority") is True:
        item["priority"] = True
    return item


def importance_for(criterion_id: str, answers: dict, has_data: bool) -> int | None:
    """Importance 1 to 5 for one criterion, or None when it has nothing to evaluate."""
    if not has_data:
        return None
    if criterion_id in answers["top_three"]:
        return TOP_IMPORTANCE
    if criterion_id in answers["dont_care"]:
        return DONT_CARE_IMPORTANCE
    if criterion_id == "freshness":
        return FRESHNESS_IMPORTANCE
    if criterion_id == "skills":
        return SKILLS_IMPORTANCE
    return DEFAULT_IMPORTANCE


def weights_from_points(points_by_id: dict[str, int]) -> dict[str, int]:
    """Integer weights summing to 100: floor shares first, then the largest remainders get one."""
    total = sum(points_by_id.values())
    if total <= 0:
        return {}
    floors: dict[str, int] = {}
    remainders: dict[str, int] = {}
    for criterion_id, points in points_by_id.items():
        floors[criterion_id], remainders[criterion_id] = divmod(100 * points, total)
    spare = 100 - sum(floors.values())
    for criterion_id in sorted(remainders, key=lambda i: (-remainders[i], i))[:spare]:
        floors[criterion_id] += 1
    return floors


def _has_data(criterion_id: str, answers: dict, title_fit_present: bool) -> bool:
    present = {
        "title_fit": title_fit_present,
        "skills": bool(answers["skills"]),
        "level": answers["target_level"] is not None or answers["years_experience"] is not None,
        "pay": answers["pay_floor"] is not None,
        "location": bool(answers["work_modes"] or answers["locations"] or answers["regions"]),
        "freshness": True,
        "industry": bool(answers["industries_wanted"]),
        "company_size": bool(answers["company_sizes"]),
        "company_stage": bool(answers["company_stages"]),
    }
    return present[criterion_id]


def _weights(answers: dict, title_fit_present: bool) -> dict[str, int]:
    points: dict[str, int] = {}
    for criterion_id in CRITERION_IDS:
        importance = importance_for(
            criterion_id, answers, _has_data(criterion_id, answers, title_fit_present))
        if importance is not None:
            points[criterion_id] = IMPORTANCE_POINTS[importance]
    return weights_from_points(points)


# Small helpers ------------------------------------------------------------------------------

def _sorted_unique(items: list[str]) -> list[str]:
    return sorted(set(items))


def _whole(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _slug(text: str) -> str:
    out: list[str] = []
    for char in text.lower():
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def annual_floor(pay_floor: dict) -> int:
    """The pay floor as a whole yearly amount, using the factors the min_number filter uses."""
    factor = ANNUAL_FACTORS[pay_floor["interval"]]
    return _whole(Decimal(str(pay_floor["amount"])) * factor)


# Hard filters -------------------------------------------------------------------------------

def _unknown(filter_name: str, answers: dict) -> str:
    return "reject" if filter_name in answers["strict"] else "gap"


def _hard_filters(answers: dict) -> list[dict]:
    filters: list[dict] = []
    pay = answers["pay_floor"]
    if pay is not None:
        filters.append({
            "id": "pay_floor",
            "type": "min_number",
            "field": "comp_max",
            "value": annual_floor(pay),
            "currency": pay["currency"],
            "unknown": _unknown("pay_floor", answers),
            "label": "Pay",
            "reject_reason": "Top of the stated range is {comp_max}, below your {value} floor.",
        })
    modes = _sorted_unique(answers["work_modes"])
    if modes and modes != list(_ALL_MODES):
        filters.append({
            "id": "work_mode",
            "type": "enum_allow",
            "field": "work_mode",
            "allow": modes,
            "unknown": _unknown("work_mode", answers),
            "label": "Work arrangement",
            "reject_reason": "{company} offers {work_mode_text} work; you accept "
                             f"{join_or(modes)} only.",
        })
    regions = _sorted_unique(answers["regions"])
    if regions:
        filters.append({
            "id": "region",
            "type": "region_allow",
            "allow": regions,
            "unknown": _unknown("region", answers),
            "label": "Region",
            "reject_reason": "{company} is hiring outside the regions you can work in "
                             f"({join_or(regions)}).",
        })
    if answers["needs_sponsorship"] is True:
        filters.append({
            "id": "sponsorship",
            "type": "deny_terms",
            "field": "description_text",
            "terms": list(NO_SPONSOR_PHRASES),
            "unknown": "reject" if "sponsorship" in answers["strict"] else "pass",
            "label": "Visa sponsorship",
            "reject_reason": "{company} says it cannot sponsor a visa (\"{term}\"), and you "
                             "need sponsorship.",
        })
    ruled_out = _sorted_unique(answers["industries_ruled_out"])
    if ruled_out:
        filters.append({
            "id": "industry_ruled_out",
            "type": "deny_terms",
            "field": "company_industry",
            "terms": ruled_out,
            "unknown": "pass",
            "label": "Industry you ruled out",
            "reject_reason": "{company} is in {term}, an industry you ruled out.",
        })
    deal_breakers = _sorted_unique(answers["deal_breaker_terms"])
    if deal_breakers:
        filters.append({
            "id": "deal_breakers",
            "type": "deny_terms",
            "field": "text",
            "terms": deal_breakers,
            "unknown": "pass",
            "label": "Deal-breaker words",
            "reject_reason": "The posting says \"{term}\", which you ruled out.",
        })
    companies = _sorted_unique(answers["companies_ruled_out"])
    if companies:
        filters.append({
            "id": "no_go_companies",
            "type": "deny_list",
            "names": companies,
            "label": "Companies you ruled out",
            "reject_reason": "{company} is on your list of companies you will not apply to.",
        })
    return filters


# Title families -----------------------------------------------------------------------------

def first_choice_key(value: object) -> str | None:
    """The normalized first-choice title, or None when the question was skipped."""
    if isinstance(value, str) and value.strip():
        return normalize_title(value) or None
    return None


def family_rung(family: dict, first_key: str | None) -> int:
    """The rung one family gets: code over the user's answer and the counted evidence."""
    if first_key is not None and first_key in {normalize_title(str(t))
                                               for t in family.get("terms", [])}:
        return TOP_RUNG
    if family.get("origin") == "discovered":
        evidence = family.get("evidence")
        count = evidence.get("asking_for_your_skills") if isinstance(evidence, dict) else None
        whole = isinstance(count, int) and not isinstance(count, bool)
        return OTHER_RUNG if whole and count >= THIN_EVIDENCE_POSTINGS else THIN_RUNG
    # A stated family: every one is top when the question was skipped.
    return TOP_RUNG if first_key is None else OTHER_RUNG


def with_rungs(families: list[dict], first_key: str | None) -> list[dict]:
    """Copies of the families with rungs set by family_rung, rung-descending then id."""
    out = copy.deepcopy(families)
    for family in out:
        family["rung"] = family_rung(family, first_key)
    return sorted(out, key=lambda f: (-f["rung"], f["id"]))


def title_fit_separates(families: list[dict]) -> bool:
    """title_fit can tell roles apart only when the families carry at least two rungs."""
    return len({f["rung"] for f in families}) >= 2


def _stated_families(answers: dict) -> list[dict]:
    """One family per distinct normalized target title; same-slug titles share a family."""
    by_slug: dict[str, dict] = {}
    for title in sorted(answers["target_titles"], key=lambda t: (normalize_title(t), t)):
        normalized = normalize_title(title)
        slug = _slug(normalized)
        family = by_slug.setdefault(slug, {
            "id": f"{STATED_PREFIX}{slug}",
            "label": title.strip(),
            "rung": TOP_RUNG,  # with_rungs sets the real rung from the first choice
            "terms": [],
            "regex": [],
            "origin": "stated",
        })
        if normalized not in family["terms"]:
            family["terms"].append(normalized)
    return [by_slug[slug] for slug in sorted(by_slug)]


# Criteria -----------------------------------------------------------------------------------

def _pay_bands(floor: int) -> list[list[int]]:
    low = floor
    mid = max(_whole(Decimal(floor) * Decimal("1.15")), low + 1)
    high = max(_whole(Decimal(floor) * Decimal("1.3")), mid + 1)
    return [[high, 3], [mid, 2], [low, 1]]


def _criterion(criterion_id: str, answers: dict, weight: int) -> dict:
    base = {"id": criterion_id, "weight": weight}
    if criterion_id == "title_fit":
        named = first_choice_key(answers.get("first_choice_title")) is not None
        if named:
            top = 'Title matches "{family}", your first choice.'
            other = 'Title matches your "{family}" family.'
        else:
            top = 'Title matches your target title "{family}".'
            other = 'Title matches "{family}", a family title discovery found.'
        return {**base, "label": "Title and scope match", "evaluator": "title_ladder",
                "reasons": {"3": top, "2": other,
                            "1": 'Title matches your "{family}" family, which few sampled '
                                  "postings tied to your skills.",
                            "0": "Title is outside every family you approved."}}
    if criterion_id == "skills":
        skills = sorted(answers["skills"], key=lambda s: (s["term"].casefold(), s["term"]))
        return {**base, "label": "Your skills in the description",
                "evaluator": "keyword_coverage",
                "params": {"must_have": [_skill_item(s) for s in skills],
                           "nice": [], "share": True, "rarity": True,
                           # dont_care skills means no tier cap, not zero weight.
                           "cap_no_skill": "skills" not in answers["dont_care"]},
                "reasons": {"0": "None of your {total} key skills appear in the description.",
                            "default": "{hit} of your {total} key skills appear in the "
                                       "description ({hits_list})."}}
    if criterion_id == "level":
        return {**base, "label": "Seniority level", "evaluator": "level_match",
                "params": {"target": answers["target_level"],
                           "years": answers["years_experience"],
                           # dont_care level means no tier cap, not zero weight.
                           "cap_off_level": "level" not in answers["dont_care"]}}
    if criterion_id == "pay":
        pay = answers["pay_floor"]
        return {**base, "label": "Pay", "evaluator": "banded_number", "field": "comp_max",
                "params": {"bands": _pay_bands(annual_floor(pay)), "currency": pay["currency"]}}
    if criterion_id == "location":
        places = _sorted_unique([*answers["locations"], *answers["regions"]])
        return {**base, "label": "Work arrangement and place", "evaluator": "location_match",
                "params": {"modes": _sorted_unique(answers["work_modes"]), "places": places}}
    if criterion_id == "freshness":
        return {**base, "label": "Posting age", "evaluator": "age_bands",
                "params": {"bands": [[14, 3], [45, 2], [90, 1]], "else": 0,
                           # The dead-posting rules hold whatever importance the user
                           # gives freshness, dont_care included.
                           "cap_after_days": AGE_CAP_DAYS,
                           "reject_after_days": AGE_REJECT_DAYS}}
    if criterion_id == "industry":
        return {**base, "label": "Industry you want", "evaluator": "term_scale",
                "field": "company_industry",
                "params": {"terms": {i: 3 for i in _sorted_unique(answers["industries_wanted"])}}}
    field = "company_size" if criterion_id == "company_size" else "company_stage"
    wanted = answers["company_sizes"] if field == "company_size" else answers["company_stages"]
    label = "Company size you prefer" if field == "company_size" else "Company stage you prefer"
    return {**base, "label": label, "evaluator": "enum_scale", "field": field,
            "params": {"values": {v: 3 for v in _sorted_unique(wanted)}}}



def carried_exclude(old: dict | None, terms: list[str]) -> tuple[list[str], list[str]]:
    """(kept, dropped) words from the old family's exclude list for a family with these terms.

    A word that matches one of the new terms would rule out every title of the family, so it is
    dropped; every other approved word is kept as written.
    """
    words = list((old or {}).get("exclude", []))
    dropped = [w for w in words if rules_out_own_terms(w, terms)]
    return [w for w in words if w not in dropped], dropped


def _carried_families(previous: dict | None, stated: list[dict]) -> list[dict]:
    """Families the user approved from title discovery, kept across a retune.

    criteria-build owns only the tf-s-<slug> families it regenerates from the answers. Every
    other family came from titles-apply (origin "discovered", or "stated" when the user edited
    a title they had listed), so it is carried, or the user's approved edits would be lost.
    """
    if previous is None:
        return []
    taken = {f["id"] for f in stated}
    kept = [f for f in previous["title"]["families"]
            if not f["id"].startswith(STATED_PREFIX) and f["id"] not in taken]
    return copy.deepcopy(sorted(kept, key=lambda f: f["id"]))


def _tiers() -> list[dict]:
    return [
        {"id": "A", "min_score": 70,
         "meaning": "Worth full effort: research, tailored resume, letter"},
        {"id": "B", "min_score": 50, "meaning": "Worth a tailored resume and the form"},
        {"id": "C", "min_score": 0, "meaning": "Keep in view; low priority"},
    ]


# Entry points -------------------------------------------------------------------------------

def build_criteria(answers: dict, resume_text: str, previous: dict | None) -> dict:
    """The scoring config for these answers. Raises AnswersError when the answers are unusable."""
    errors = validate_answers(answers, resume_text)
    if errors:
        raise AnswersError(errors)
    level = answers["target_level"]
    first_key = first_choice_key(answers.get("first_choice_title"))
    stated = _stated_families(answers)
    old_by_id = {f["id"]: f for f in previous["title"]["families"]} if previous else {}
    for family in stated:
        kept, _ = carried_exclude(old_by_id.get(family["id"]), family["terms"])
        if kept:
            family["exclude"] = kept  # the words the user approved survive a retune
    families = with_rungs([*stated, *_carried_families(previous, stated)], first_key)
    weights = _weights(answers, title_fit_separates(families))
    return {
        "schema_version": 1,
        "criteria_version": "1.0",
        "created_from": "intake",
        "hard_filters": _hard_filters(answers),
        "title": {
            # Rung-descending then id, as titles-apply writes them, so the bytes never depend on
            # input order. match_family no longer reads this order.
            "families": families,
            "exclude": ([] if level == "entry"
                        else [{"terms": ["intern", "internship"], "origin": "default"}]),
        },
        "criteria": [_criterion(i, answers, weights[i]) for i in CRITERION_IDS if i in weights],
        "bonuses": [],
        "tiers": copy.deepcopy(previous["tiers"]) if previous is not None else _tiers(),
        "banding": {"provisional_below_coverage": PROVISIONAL_BELOW_COVERAGE},
        "one_per_company": {"enabled": True, "keep": "best"},
        "reserved": {"tracks": None, "overrides": None},
    }


def build_scan_scope(answers: dict, criteria_version: str) -> dict:
    return {
        "schema_version": 1,
        "criteria_version": criteria_version,
        "industries": _sorted_unique(answers["industries_wanted"]),
        "exclude_industries": _sorted_unique(answers["industries_ruled_out"]),
        "sizes": _sorted_unique(answers["company_sizes"]),
    }
