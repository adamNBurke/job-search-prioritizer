"""The saved explanations: what the tool understood, which titles it chose, what the scan found.

Purpose: the skills used to explain these three things only in chat, where they scroll away and
can drift from what the code decided. Each explanation is now
a file rendered by code from the saved state, so a skill quotes the file instead of paraphrasing
and the user can read it again later.
Logic: render_intake(answers, criteria) turns profile/answers.json into one labeled line per
topic, in the answers.json field order, ending with how to correct it. render_titles(evidence,
decisions, criteria) lists the kept title families in config order, each with its count stated as
"N of M sampled postings" from the evidence the family carries, then the dropped families.
render_funnel(folder, rank_input, scored, criteria) lists every count of one scan and ends with
output.funnel_line verbatim. Each function returns text only; the command that owns the state
writes it: criteria-build writes profile/summary.md, titles-apply writes titles/summary-<date>.md
and output.write_ranked writes ranked/funnel-<date>.md beside the ranked files.
Guardrails: every value that came from the user, a resume or a posting goes through
output.md_escape, so it cannot change a label or start a new line; labels and sentences are
code constants. A skipped topic reads "skipped" in its own line and is never listed twice. No
wall-clock time: the only date is the day the answers were given, so the same inputs give the
same bytes. LF line endings, no network, nothing here changes a score, a tier or a count.
QA checklist: a changed line needs tests/test_summaries.py and docs/data-files.md ("Summary
files") in the same commit; a new topic in answers.json needs a line here, or the summary stops
naming every answered topic.
"""

from __future__ import annotations

from pathlib import Path

from jobsearch import families
from jobsearch.criteria_build import first_choice_key
from jobsearch.output import build_funnel, funnel_text, md_escape, rejection_counts, scan_meta
from jobsearch.roles_store import RankInput
from jobsearch.scoring import ScoredRole

NONE_GIVEN = "none given"
SKIPPED = "skipped"
NOT_STATED = "not stated"
CORRECTION = ("If anything here is wrong, tell the start skill what to change and it rebuilds "
              "your config; the previous version is kept.")

# Plain-English names for the fixed criterion ids (docs/data-files.md, "Criterion ids").
_CRITERION_WORDS = {
    "title_fit": "job title", "skills": "skills", "level": "seniority level", "pay": "pay",
    "location": "where and how you work", "freshness": "how recent the posting is",
    "industry": "industry", "company_size": "company size", "company_stage": "company stage",
}
_STRICT_WORDS = {
    "pay_floor": "pay floor", "work_mode": "work arrangements", "region": "regions",
    "sponsorship": "visa sponsorship",
}


def _items(values: object) -> list[str]:
    return [md_escape(str(v)) for v in values] if isinstance(values, list) else []


def _list_line(label: str, key: str, answers: dict) -> str:
    """A labeled line for a list answer: the items, "skipped" when the topic was skipped and holds
    nothing, or "none given"."""
    items = _items(answers.get(key))
    if items:
        return f"- {label}: {', '.join(items)}"
    return f"- {label}: {SKIPPED if key in _skipped(answers) else NONE_GIVEN}"


def _skipped(answers: dict) -> set[str]:
    raw = answers.get("skipped")
    return {str(k) for k in raw} if isinstance(raw, list) else set()


def _criterion_words(ids: object) -> str:
    return ", ".join(_CRITERION_WORDS.get(str(i), md_escape(str(i))) for i in ids) \
        if isinstance(ids, list) and ids else NONE_GIVEN


def _titles_line(answers: dict) -> str:
    titles = answers.get("target_titles")
    if not isinstance(titles, list) or not titles:
        return f"- Job titles: {SKIPPED if 'target_titles' in _skipped(answers) else NONE_GIVEN}"
    first = answers.get("first_choice_title")
    first_key = families.normalize_title(first) if isinstance(first, str) and first else None
    marked = [md_escape(str(t)) + (" (first choice)"
                                   if first_key and families.normalize_title(str(t)) == first_key
                                   else "") for t in titles]
    line = f"- Job titles: {', '.join(marked)}"
    if not first_key:
        line += f"; first choice: {SKIPPED if 'first_choice_title' in _skipped(answers) else NOT_STATED}"
    return line


def _skills_line(answers: dict) -> str:
    skills = answers.get("skills")
    if not isinstance(skills, list) or not skills:
        return f"- Skills: {SKIPPED if 'skills' in _skipped(answers) else NONE_GIVEN}"
    parts = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        notes = ["from your resume" if skill.get("origin") == "resume" else "you added"]
        if skill.get("priority") is True:
            notes.insert(0, "matters most")
        parts.append(f"{md_escape(str(skill.get('term', '')))} ({', '.join(notes)})")
    return f"- Skills: {', '.join(parts) if parts else NONE_GIVEN}"


def _pay_line(answers: dict) -> str:
    pay = answers.get("pay_floor")
    if not isinstance(pay, dict):
        return f"- Pay floor: {SKIPPED if 'pay_floor' in _skipped(answers) else NOT_STATED}"
    return (f"- Pay floor: {md_escape(str(pay.get('amount', '')))} "
            f"{md_escape(str(pay.get('currency', '')))} per {md_escape(str(pay.get('interval', '')))}")


def _sponsorship_line(answers: dict) -> str:
    need = answers.get("needs_sponsorship")
    if need is True:
        return "- Visa sponsorship: needed"
    if need is False:
        return "- Visa sponsorship: not needed"
    return f"- Visa sponsorship: {SKIPPED if 'needs_sponsorship' in _skipped(answers) else NOT_STATED}"


def _scalar_line(label: str, key: str, answers: dict) -> str:
    value = answers.get(key)
    if value is None or value == "":
        return f"- {label}: {SKIPPED if key in _skipped(answers) else NOT_STATED}"
    return f"- {label}: {md_escape(str(value))}"


def render_intake(answers: dict, criteria: dict) -> str:
    """The text of profile/summary.md: what the tool understood, one line per topic, in the
    answers.json field order, then the config version and how to correct it."""
    strict = answers.get("strict")
    strict_words = ", ".join(_STRICT_WORDS.get(str(k), md_escape(str(k))) for k in strict) \
        if isinstance(strict, list) and strict else NONE_GIVEN
    lines = [
        "# What the tool understood", "",
        f"From your resume and your interview answers of {md_escape(str(answers.get('answered_on', '')))}.",
        "",
        _titles_line(answers),
        _scalar_line("Level", "target_level", answers),
        _scalar_line("Years of experience", "years_experience", answers),
        _skills_line(answers),
        _pay_line(answers),
        _list_line("Work arrangements", "work_modes", answers),
        _list_line("Places", "locations", answers),
        _list_line("Regions", "regions", answers),
        _sponsorship_line(answers),
        _list_line("Industries wanted", "industries_wanted", answers),
        _list_line("Industries ruled out", "industries_ruled_out", answers),
        _list_line("Company sizes", "company_sizes", answers),
        _list_line("Company stages", "company_stages", answers),
        _list_line("Deal-breaker words", "deal_breaker_terms", answers),
        _list_line("Companies you will not apply to", "companies_ruled_out", answers),
        f"- Treated as strict, so a posting that does not say is left out: {strict_words}",
        f"- Matters most: {_criterion_words(answers.get('top_three'))}",
        f"- Does not matter to you: {_criterion_words(answers.get('dont_care'))}",
        "",
        f"Scoring config version {md_escape(str(criteria.get('criteria_version', '')))}.",
        "",
        CORRECTION,
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ titles

_ORIGIN_WORDS = {"stated": "you listed this title", "discovered": "found in the sample"}


def _sampled(evidence: dict) -> str:
    """The "N of M sampled postings" wording, from the counts one family's evidence block holds."""
    asking, total = evidence.get("asking_for_your_skills"), evidence.get("postings_sampled")
    if isinstance(asking, int) and isinstance(total, int):
        return f"{asking} of {total} sampled postings ask for your skills"
    return "no sample count"


def _family_line(family: dict, first_key: str | None) -> str:
    notes = [_ORIGIN_WORDS.get(str(family.get("origin")), "chosen")]
    if first_key is not None and family.get("rung") == 3:
        notes.append("your first choice")
    evidence = family.get("evidence")
    line = f"- {md_escape(str(family.get('label', family.get('id', ''))))} ({', '.join(notes)})"
    if isinstance(evidence, dict):
        line += f": {_sampled(evidence)}"
        skills = _items(evidence.get("top_shared_skills"))
        if skills:
            line += f". Skills they share: {', '.join(skills)}"
    elif family.get("origin") != "stated":
        line += ": no sample count"
    words = _items(family.get("exclude"))
    if words:
        line += f". Words that rule a title out: {', '.join(words)}"
    return line + "."


def render_titles(evidence: dict, decisions: dict, criteria: dict,
                  proposal: dict | None = None, first_choice_title: object = None) -> str:
    """The text of titles/summary-<sample date>.md: the title families kept, in config order,
    each with its posting count and why it was kept, then the families the user dropped. The
    counts are the ones code stored in each family's evidence block; nothing is recounted."""
    first_key = first_choice_key(first_choice_title)
    kept = criteria.get("title", {}).get("families", [])
    labels = {str(f.get("id")): str(f.get("label", "")) for f in (proposal or {}).get(
        "families", []) if isinstance(f, dict)}
    dropped: list[str] = []
    for item in decisions.get("decisions", []) if isinstance(decisions, dict) else []:
        if isinstance(item, dict) and item.get("decision") == "reject":
            label = labels.get(str(item.get("id"))) or str(item.get("label") or item.get("id", ""))
            if md_escape(label) not in dropped:
                dropped.append(md_escape(label))
    lines = [
        "# Job titles chosen", "",
        f"Sample of {md_escape(str(evidence.get('sample_date', '')))}: "
        f"{md_escape(str(evidence.get('postings_sampled', 0)))} postings from "
        f"{md_escape(str(evidence.get('boards_sampled', 0)))} boards. "
        f"{md_escape(str(evidence.get('coverage_note', '')))}".rstrip(),
        "", "## Kept", "",
    ]
    lines += [_family_line(f, first_key) for f in kept if isinstance(f, dict)] or [NONE_GIVEN]
    lines += ["", "## Dropped", ""]
    lines += [f"- {label}" for label in dropped] or ["None."]
    lines += ["", "Say so and the start skill reworks these; the previous config version is kept.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ funnel

def _boards(folder: Path, scan_date: str) -> tuple[int, list[str]]:
    """(boards read, "<company> (<why>)" for each board that failed), from scan-meta.json."""
    boards = scan_meta(folder, scan_date).get("boards")
    read, failed = 0, []
    for board in boards if isinstance(boards, list) else []:
        if not isinstance(board, dict):
            continue
        status = board.get("status")
        if status == "ok":
            read += 1
        elif status in ("failed", "no_board"):
            why = "could not be reached" if status == "failed" else "was not found"
            failed.append(f"{md_escape(str(board.get('company') or board.get('slug', '')))} ({why})")
    return read, sorted(failed)


def render_funnel(folder: Path, rank_input: RankInput, scored: list[ScoredRole],
                  criteria: dict) -> str:
    """The text of ranked/funnel-<scan date>.md: every count of the scan, in the order a role
    meets them, ending with output.funnel_line verbatim. Counts come from build_funnel and
    rejection_counts, the same code the ranked files use, so the files cannot disagree."""
    funnel = build_funnel(rank_input, scored, criteria)
    tiers = funnel["tiers"]
    rejections = rejection_counts(scored)
    hidden = sum(e["count"] for e in rejections if e["filter_id"] == "function_review")
    others = sorted((e for e in rejections if e["filter_id"] != "function_review"),
                    key=lambda e: (-e["count"], str(e["label"])))
    read, failed = _boards(folder, rank_input.scan_date)
    lines = [
        "# What the scan found", "",
        f"Scan date {md_escape(rank_input.scan_date)}. "
        f"Scoring config version {md_escape(str(criteria['criteria_version']))}.", "",
        "## Where the roles came from", "",
    ]
    if read or failed:
        lines.append(f"- Boards read: {read}")
        lines.append(f"- Boards that failed: {', '.join(failed) if failed else 'none'}")
    else:
        lines.append("- Boards read: none. No boards were read in this scan.")
    lines += [
        f"- Postings fetched: {funnel['scanned']}",
        f"- Postings you pasted: {funnel['pasted']}",
        f"- Duplicates merged: {funnel['duplicates_merged']}",
        "", "## What was filtered out", "",
        f"- Title outside your approved families: {funnel['title_gate_dropped']}",
    ]
    lines += [f"- {md_escape(str(e['label']))}: {e['count']}" for e in others]
    lines += [
        f"- Hidden after reading the description: {hidden}",
        "", "## What is left", "",
        f"- Passed your filters: {funnel['passed']}",
        f"- Tier A: {tiers['A']}",
        f"- Tier B: {tiers['B']}",
        f"- Tier C: {tiers['C']}",
        "", funnel_text(funnel), "",
    ]
    return "\n".join(lines)
