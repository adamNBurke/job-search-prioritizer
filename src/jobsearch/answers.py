"""Read and check profile/answers.json, the record of what the user said in the interview.

Purpose: the agent writes answers.json from the conversation, and criteria-build turns it into
the scoring config, so the file is checked field by field before any arithmetic runs.
The shape is frozen in docs/data-files.md ("answers.json").
Logic: load_answers reads the file from the data folder. validate_answers(data, resume_text)
returns a sorted list of plain-English errors, each starting with the JSON path to fix, such as
pay_floor.currency; an empty list means the answers are usable. It checks every key (all keys
except first_choice_title are required, unknown keys are refused), every type and every vocabulary, then the cross-field
rules. A skill may carry an optional priority (true or false, at most three true); a file
without it is valid and means no skill was singled out. The one optional top-level key is
first_choice_title (OPTIONAL_KEYS): the target title the user picked as their first choice.
Absent or null means the question was skipped, so a file written before it existed stays valid.
Guardrails: the check never repairs or guesses a value. Booleans never count as numbers. Terms
are plain text capped at 80 characters because the config turns them into escaped phrases and
never into patterns. A first choice must be text that normalizes to one of target_titles; it is compared as text and
never turned into a pattern. Work authorization is only ever the user's explicit true or false;
this module has no code that derives it from any other field. A file that is missing or is not
JSON is a UserError with the next step, never a stack trace.
QA checklist: tests/test_answers.py has one case per rule; a new field needs a row in
docs/data-files.md and a case here in the same commit. first_choice_title has a case for absent,
null, a title in the list, a title outside it, a non-text value, 81 characters and an empty
target_titles list.
"""

from __future__ import annotations

import datetime
import math
import re
from pathlib import Path

from jobsearch import fsio, paths
from jobsearch.directory import INDUSTRIES, SIZES, STAGES
from jobsearch.errors import UserError
from jobsearch.criteria import join_or
from jobsearch.families import LEVELS, TERM_MAX, normalize_title
from jobsearch.locations import user_place

CRITERION_IDS = ("title_fit", "skills", "level", "pay", "location", "freshness", "industry",
                 "company_size", "company_stage")

WORK_MODES = ("remote", "hybrid", "onsite")
INTERVALS = ("year", "month", "week", "day", "hour")
ORIGINS = ("resume", "stated")
STRICT_CHOICES = ("pay_floor", "work_mode", "region", "sponsorship")
PRIORITY_MAX = 3  # the most skills a user can mark as mattering most
PAY_FLOOR_MAX = 1e12  # well past any real pay in any currency; far below where the bands overflow
KEYS = (
    "schema_version", "answered_on", "target_titles", "target_level", "years_experience",
    "skills", "pay_floor", "work_modes", "locations", "regions", "needs_sponsorship",
    "industries_wanted", "industries_ruled_out", "company_sizes", "company_stages",
    "deal_breaker_terms", "companies_ruled_out", "strict", "top_three", "dont_care", "skipped",
)
# Keys a file may leave out. first_choice_title is the answer to the one skippable question about
# which target title is the user's first choice; absent or null means it was skipped.
OPTIONAL_KEYS = ("first_choice_title",)

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_REGION = re.compile(r"[A-Z]{2}|EU")


class AnswersError(UserError):
    """The answers file has problems. The message lists them in plain English."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        shown = errors[:10]
        more = "" if len(errors) <= 10 else f" ({len(errors) - 10} more not shown)"
        super().__init__("The interview answers have problems: " + " ".join(shown) + more)


def load_answers(folder: Path) -> dict:
    path = folder / paths.LAYOUT["answers"]
    if not path.is_file():
        raise UserError("No interview answers yet. Finish the interview so profile/answers.json "
                        "exists, then run criteria-build.")
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        raise UserError("profile/answers.json is not readable JSON. Fix it or run the "
                        "interview again.") from None
    if not isinstance(data, dict):
        raise UserError("profile/answers.json must be a JSON object.")
    return data


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (isinstance(value, int) or math.isfinite(value)))


def _text_items(data: dict, key: str, errors: list[str]) -> list:
    """The list at key, after reporting any item that is not usable text."""
    items = data.get(key)
    if not isinstance(items, list):
        errors.append(f"{key}: must be a list.")
        return []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{key}[{index}]: must be text, not empty.")
        elif len(item) > TERM_MAX:
            errors.append(f"{key}[{index}]: is longer than {TERM_MAX} characters.")
    return items


def _choice_items(data: dict, key: str, allowed: tuple[str, ...], errors: list[str]) -> list:
    items = data.get(key)
    if not isinstance(items, list):
        errors.append(f"{key}: must be a list.")
        return []
    for index, item in enumerate(items):
        if item not in allowed:
            errors.append(f'{key}[{index}]: "{item}" is not {join_or(allowed)}.')
    return items


def _check_skills(data: dict, errors: list[str]) -> None:
    skills = data.get("skills")
    if not isinstance(skills, list):
        errors.append("skills: must be a list.")
        return
    for index, skill in enumerate(skills):
        path = f"skills[{index}]"
        if not isinstance(skill, dict):
            errors.append(f"{path}: must be an object with term, aliases and origin.")
            continue
        for key in sorted(set(skill) - {"term", "aliases", "origin", "priority"}):
            errors.append(f'{path}: "{key}" is not a field a skill has.')
        for key in ("term", "aliases", "origin"):
            if key not in skill:
                errors.append(f'{path}: needs "{key}".')
        term = skill.get("term")
        if "term" in skill and (not isinstance(term, str) or not term.strip()):
            errors.append(f"{path}.term: must be text, not empty.")
        elif isinstance(term, str) and len(term) > TERM_MAX:
            errors.append(f"{path}.term: is longer than {TERM_MAX} characters.")
        aliases = skill.get("aliases")
        if "aliases" in skill:
            if not isinstance(aliases, list):
                errors.append(f"{path}.aliases: must be a list.")
            else:
                for position, alias in enumerate(aliases):
                    if not isinstance(alias, str) or not alias.strip():
                        errors.append(f"{path}.aliases[{position}]: must be text, not empty.")
                    elif len(alias) > TERM_MAX:
                        errors.append(
                            f"{path}.aliases[{position}]: is longer than {TERM_MAX} characters.")
        if "origin" in skill and skill["origin"] not in ORIGINS:
            errors.append(f"{path}.origin: must be {join_or(ORIGINS)}.")
        if "priority" in skill and not isinstance(skill["priority"], bool):
            errors.append(f"{path}.priority: must be true or false.")
    seen: dict[str, int] = {}
    for index, skill in enumerate(skills):
        term = skill.get("term") if isinstance(skill, dict) else None
        key = " ".join(term.casefold().split()) if isinstance(term, str) else ""
        if key and key in seen:
            errors.append(f'skills[{index}]: "{term}" repeats skills[{seen[key]}]; '
                          "list each skill once.")
        elif key:
            seen[key] = index
    if sum(1 for s in skills if isinstance(s, dict) and s.get("priority") is True) > PRIORITY_MAX:
        errors.append("skills: mark at most three skills as mattering most.")


def _check_titles(data: dict, errors: list[str]) -> None:
    if not isinstance(data["target_titles"], list):
        return  # _text_items already said it must be a list
    for index, title in enumerate(data["target_titles"]):
        if isinstance(title, str) and title.strip() and not normalize_title(title):
            errors.append(
                f'target_titles[{index}]: "{title}" has no job title left after removing level '
                "words such as senior or intern; add the role itself.")


def _check_first_choice(data: dict, errors: list[str]) -> None:
    """first_choice_title is null, absent, or one of target_titles after normalizing."""
    value = data.get("first_choice_title")
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        errors.append("first_choice_title: must be text, not empty, or null.")
        return
    if len(value) > TERM_MAX:
        errors.append(f"first_choice_title: is longer than {TERM_MAX} characters.")
        return
    titles = data["target_titles"]
    if not isinstance(titles, list):
        return  # _text_items already said target_titles must be a list
    wanted = normalize_title(value)
    listed = {normalize_title(t) for t in titles if isinstance(t, str) and t.strip()}
    if not wanted or wanted not in listed:
        errors.append(
            f'first_choice_title: "{value}" is not one of your target_titles; name one of them '
            "or leave it out.")


def _in_resume(term: str, resume_text: str) -> bool:
    pattern = rf"(?<!\w){re.escape(term.strip())}(?!\w)"
    return re.search(pattern, resume_text, re.IGNORECASE) is not None


def _check_grounding(data: dict, resume_text: str, errors: list[str]) -> None:
    """A skill marked as from the resume must appear in it."""
    if not isinstance(data["skills"], list):
        return  # _check_skills already said it must be a list
    for index, skill in enumerate(data["skills"]):
        if not isinstance(skill, dict) or skill.get("origin") != "resume":
            continue
        term = skill.get("term")
        aliases = skill.get("aliases")
        names = [n for n in [term, *(aliases if isinstance(aliases, list) else [])]
                 if isinstance(n, str) and n.strip()]
        if names and not any(_in_resume(name, resume_text) for name in names):
            errors.append(
                f'skills[{index}]: "{term}" is marked as from your resume, but your resume does '
                "not show it. Mark it as something you told me, or take it out.")


def _check_importance(data: dict, errors: list[str]) -> None:
    top = data["top_three"]
    if isinstance(top, list):
        if len(top) > 3:
            errors.append("top_three: lists more than 3 criteria; pick at most 3.")
        for criterion_id in sorted({c for c in top if isinstance(c, str) and top.count(c) > 1}):
            errors.append(f'top_three: "{criterion_id}" is listed more than once.')
    low = data["dont_care"]
    if isinstance(top, list) and isinstance(low, list):
        for criterion_id in sorted({c for c in top if isinstance(c, str) and c in low}):
            errors.append(f'"{criterion_id}" is in both top_three and dont_care; choose one.')
    wanted, ruled_out = data["industries_wanted"], data["industries_ruled_out"]
    if isinstance(wanted, list) and isinstance(ruled_out, list):
        for industry in sorted({i for i in wanted if isinstance(i, str) and i in ruled_out}):
            errors.append(f'industries_wanted: "{industry}" is both wanted and ruled out.')


def _check_pay_floor(data: dict, errors: list[str]) -> None:
    pay = data.get("pay_floor")
    if pay is None:
        return
    if not isinstance(pay, dict):
        errors.append("pay_floor: must be an object with amount, currency and interval, or null.")
        return
    for key in sorted(set(pay) - {"amount", "currency", "interval"}):
        errors.append(f'pay_floor: "{key}" is not a field the pay floor has.')
    for key in ("amount", "currency", "interval"):
        if key not in pay:
            errors.append(f'pay_floor: needs "{key}".')
    amount = pay.get("amount")
    if "amount" in pay and not (_is_number(amount) and 0 < amount <= PAY_FLOOR_MAX):
        errors.append("pay_floor.amount: must be a number above 0 and at most 1,000,000,000,000.")
    currency = pay.get("currency")
    if "currency" in pay and not (isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
        errors.append("pay_floor.currency: must be a three-letter uppercase code such as USD.")
    if "interval" in pay and pay["interval"] not in INTERVALS:
        errors.append(f"pay_floor.interval: must be {join_or(INTERVALS)}.")


def _check_locations(data: dict, errors: list[str]) -> None:
    """Refuse a place scoring cannot read, so a role is never ranked on a place that was dropped."""
    for index, item in enumerate(_text_items(data, "locations", errors)):
        if isinstance(item, str) and item.strip():
            found, unread = user_place(item)
            if not found or unread:
                shown = ", ".join(unread) if unread else item
                errors.append(
                    f'locations[{index}]: "{shown}" is not a place this tool can read. Write it '
                    "as City, ST, such as Phoenix, AZ (City, Country outside the US).")


def _check_regions(data: dict, errors: list[str]) -> None:
    regions = data.get("regions")
    if not isinstance(regions, list):
        errors.append("regions: must be a list.")
        return
    for index, code in enumerate(regions):
        if not (isinstance(code, str) and _REGION.fullmatch(code)):
            errors.append(
                f'regions[{index}]: "{code}" must be a two-letter uppercase country code or EU.')
        elif not user_place(code)[0]:
            errors.append(f'regions[{index}]: "{code}" is not a country code this tool knows.')


def validate_answers(data: object, resume_text: str) -> list[str]:
    """Plain-English problems with the answers, sorted; an empty list means usable."""
    if not isinstance(data, dict):
        return ["The interview answers must be a JSON object."]
    errors: list[str] = []
    for key in sorted(set(data) - set(KEYS) - set(OPTIONAL_KEYS)):
        errors.append(f'The interview answers have an unknown key: "{key}".')
    for key in KEYS:
        if key not in data:
            errors.append(f'The interview answers are missing "{key}".')
    if any(key not in data for key in KEYS):
        return sorted(errors)

    if not _is_int(data["schema_version"]) or data["schema_version"] != 1:
        errors.append("schema_version: must be 1.")
    answered = data["answered_on"]
    try:
        valid_date = isinstance(answered, str) and bool(_DATE.fullmatch(answered))
        if valid_date:
            datetime.date.fromisoformat(answered)
    except ValueError:
        valid_date = False
    if not valid_date:
        errors.append("answered_on: must be a date such as 2026-11-17.")
    _text_items(data, "target_titles", errors)
    level = data["target_level"]
    if level is not None and level not in LEVELS:
        errors.append(f"target_level: must be {join_or(LEVELS)} or null.")
    years = data["years_experience"]
    if years is not None and not (_is_int(years) and years >= 0):
        errors.append("years_experience: must be a whole number, 0 or more, or null.")
    _check_skills(data, errors)
    _check_pay_floor(data, errors)
    _choice_items(data, "work_modes", WORK_MODES, errors)
    _check_locations(data, errors)
    _check_regions(data, errors)
    if data["needs_sponsorship"] is not None and not isinstance(data["needs_sponsorship"], bool):
        errors.append("needs_sponsorship: must be true, false or null.")
    _choice_items(data, "industries_wanted", INDUSTRIES, errors)
    _choice_items(data, "industries_ruled_out", INDUSTRIES, errors)
    _choice_items(data, "company_sizes", SIZES, errors)
    _choice_items(data, "company_stages", STAGES, errors)
    _text_items(data, "deal_breaker_terms", errors)
    _text_items(data, "companies_ruled_out", errors)
    _choice_items(data, "strict", STRICT_CHOICES, errors)
    _choice_items(data, "top_three", CRITERION_IDS, errors)
    _choice_items(data, "dont_care", CRITERION_IDS, errors)
    _text_items(data, "skipped", errors)
    _check_titles(data, errors)
    _check_first_choice(data, errors)
    _check_importance(data, errors)
    _check_grounding(data, resume_text, errors)
    return sorted(errors)
