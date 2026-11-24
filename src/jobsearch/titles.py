"""Counted title discovery: sample real postings, count titles in code, apply the user's decisions.

Purpose: users under-name the titles their skills fit. The model may name and explain clusters of
titles, but every number the user sees must come from code, or it can be invented.
Logic: stratified_boards picks up to SAMPLE_BOARDS boards round-robin across the industry and size
strata of the whole starter directory (not only the user's industries). pick_postings keeps
up to POSTINGS_PER_BOARD postings per board, ordered by a hash of the role id, so the pick is
spread but repeatable. A posting asks for the user's skills when at least min(2, number of
skills) distinct skills (the term or an alias) appear in its description, matched exactly as
scoring's skill_mentioned matches them on NFKC text: whole words, any case, variants from
skill_words.json, and not-a-skill phrases such as "excel at" blanked.
count_titles groups postings by normalized title and builds the evidence list. check_proposal
reads the agent's family proposal against that evidence and renders the view with the counts.
apply_decisions turns the user's approve, reject and edit decisions into config families; every
family's rung is set by criteria_build.family_rung from the user's first choice and the counted
evidence, never from the decision file.
Guardrails: a rung written in decisions.json is never read: the decision is applied and a notice
says the rung was ignored, so an older file keeps working and the agent cannot steer scoring.
A proposal that carries any number or example is refused, so a model-invented count
cannot reach the user. Every output list is sorted, the sample and evidence files carry
no timestamps beyond the date, and no function here touches the network or the clock except
through the callers that pass the date in.
apply_exclusions writes the words the user approved for each title family
(title.families[].exclude) from titles/exclusions.json; apply_decisions keeps a family's words
across a re-approval or an edit. The words are the user's: the agent writes only what the user
approved, code validates them (text, 1 to 80 characters, at most 20, no repeat, none that matches
the family's own terms, which would rule out every title), sorts them, and they are only ever
matched as escaped whole words, never as patterns. A word an edit makes match the family's new
terms is dropped with a notice.
QA checklist: a changed file shape is documented in docs/data-files.md in the same change; a new
rule gets a named test in tests/test_titles.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from jobsearch import fsio, paths
from jobsearch.criteria_build import STATED_PREFIX, carried_exclude, family_rung, first_choice_key
from jobsearch.directory import BoardEntry
from jobsearch.errors import UserError
from jobsearch.families import TERM_MAX, _term_words, exclude_word_errors, normalize_title
from jobsearch.models import Posting
from jobsearch.scoring import skill_mentioned

if TYPE_CHECKING:
    from jobsearch.scan import BoardFetch

SAMPLE_BOARDS = 40
POSTINGS_PER_BOARD = 30
MAX_EVIDENCE_TITLES = 150
MAX_FAMILIES = 8
_FAMILY_ID = re.compile(r"tf-[a-z0-9-]+")
_FAMILY_KEYS = ("id", "label", "titles", "terms")
_NUMBER_KEYS = frozenset({
    "count", "counts", "share", "shares", "percent", "n_postings", "n_with_skills", "postings",
    "postings_sampled", "asking_for_your_skills", "examples", "example", "top_skills",
    "companies",
})

_NOTE = ("Sampled {postings} postings from {boards} boards on Greenhouse, Lever and Ashby across "
         "{industries} industries. These boards lean toward tech and mid-market companies, so "
         "titles common at large enterprises, hospitals and government are under-counted.")
_COUNT_TEXT = "{count} of {total} sampled postings"


# Sampling -----------------------------------------------------------------------------------

def stratified_boards(entries: list[BoardEntry], limit: int = SAMPLE_BOARDS) -> list[BoardEntry]:
    """Round-robin across (industry, size) strata, strata in sorted order, boards in file order."""
    strata: dict[tuple[str, str], list[BoardEntry]] = {}
    for entry in entries:
        strata.setdefault((entry.industry or "", entry.size or ""), []).append(entry)
    queues = [strata[key] for key in sorted(strata)]
    picked: list[BoardEntry] = []
    depth = 0
    while len(picked) < limit and any(len(queue) > depth for queue in queues):
        for queue in queues:
            if len(picked) >= limit:
                break
            if len(queue) > depth:
                picked.append(queue[depth])
        depth += 1
    return picked


def pick_postings(postings: list[Posting]) -> list[Posting]:
    """Up to POSTINGS_PER_BOARD postings, in the order of the sha256 of each role id."""
    ordered = sorted(postings, key=lambda p: hashlib.sha256(p.role_id.encode("utf-8")).hexdigest())
    return ordered[:POSTINGS_PER_BOARD]


# Skills -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillMatcher:
    term: str
    names: tuple[str, ...]  # the term, then its non-empty aliases


def skill_matchers(answers: dict) -> list[SkillMatcher]:
    """One matcher per skill the user listed; names are matched by scoring, never as patterns."""
    found: list[SkillMatcher] = []
    for skill in answers.get("skills", []):
        if not isinstance(skill, dict) or not isinstance(skill.get("term"), str):
            continue
        aliases = skill.get("aliases")
        phrases = [skill["term"], *(a for a in (aliases if isinstance(aliases, list) else [])
                                    if isinstance(a, str))]
        names = tuple(p for p in phrases if p.split())
        if names:
            found.append(SkillMatcher(skill["term"], names))
    return sorted(found, key=lambda m: (m.term.casefold(), m.term))


def skills_hit(text: str, matchers: list[SkillMatcher]) -> list[str]:
    """The terms the text names, matched exactly as scoring's skill_mentioned matches them."""
    normalized = unicodedata.normalize("NFKC", text)
    return sorted({m.term for m in matchers if skill_mentioned(normalized, m.names)})


def _plain_title(title: str) -> str:
    return normalize_title(title) or " ".join(title.lower().split())


def target_keys(target_titles: list[str]) -> set[str]:
    return {_plain_title(t) for t in target_titles if isinstance(t, str) and t.strip()}


def sample_rows(fetches: list[BoardFetch], matchers: list[SkillMatcher]) -> list[dict]:
    rows: list[dict] = []
    for fetched in fetches:
        if fetched.status != "ok":
            continue
        for posting in pick_postings(fetched.postings):
            rows.append({
                "role_id": posting.role_id,
                "company": posting.company,
                "industry": fetched.entry.industry,
                "size": fetched.entry.size,
                "title": posting.title,
                "normalized_title": _plain_title(posting.title),
                "skills_hit": skills_hit(posting.description_text, matchers),
            })
    rows.sort(key=lambda r: r["role_id"])
    return rows


# Counting -----------------------------------------------------------------------------------

def coverage_note(postings: int, boards: int, industries: int) -> str:
    return _NOTE.format(postings=postings, boards=boards, industries=industries)


def count_titles(rows: list[dict], skills: list[str], keep: set[str] | frozenset[str] = frozenset(),
                 ) -> list[dict]:
    """Evidence titles: the 150 with the most skill matches, plus any title in `keep`."""
    need = min(2, len(skills))
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["normalized_title"], []).append(row)
    entries: list[dict] = []
    for normalized, items in groups.items():
        asking = [r for r in items if need and len(r["skills_hit"]) >= need]
        raw = Counter(r["title"] for r in items)
        skill_counts = Counter(s for r in asking for s in r["skills_hit"])
        entries.append({
            "normalized": normalized,
            "n_postings": len(items),
            "n_with_skills": len(asking),
            "examples": [t for t, _ in sorted(raw.items(), key=lambda kv: (-kv[1], kv[0]))][:3],
            "top_skills": [s for s, _ in sorted(skill_counts.items(),
                                                key=lambda kv: (-kv[1], kv[0]))][:3],
        })
    entries.sort(key=lambda e: (-e["n_with_skills"], e["normalized"]))
    kept = entries[:MAX_EVIDENCE_TITLES] + [e for e in entries[MAX_EVIDENCE_TITLES:]
                                            if e["normalized"] in keep]
    return sorted(kept, key=lambda e: (-e["n_with_skills"], e["normalized"]))


def build_evidence(rows: list[dict], matchers: list[SkillMatcher], sample_date: str,
                   keep: set[str] | frozenset[str] = frozenset()) -> dict:
    skills = [m.term for m in matchers]
    boards = len({row["role_id"].rsplit(":", 1)[0] for row in rows})
    industries = sorted({row["industry"] for row in rows if row["industry"]})
    return {
        "sample_date": sample_date,
        "boards_sampled": boards,
        "postings_sampled": len(rows),
        "industries_covered": industries,
        "coverage_note": coverage_note(len(rows), boards, len(industries)),
        "skills_used": skills,
        "titles": count_titles(rows, skills, keep),
    }


def sample_paths(sample_date: str) -> tuple[str, str]:
    base = paths.LAYOUT["titles"]
    return f"{base}/sample-{sample_date}.jsonl", f"{base}/evidence-{sample_date}.json"


def write_sample(folder: Path, rows: list[dict], evidence: dict) -> list[str]:
    sample_rel, evidence_rel = sample_paths(evidence["sample_date"])
    fsio.write_jsonl_atomic(folder / sample_rel, rows)
    fsio.write_text_atomic(folder / evidence_rel, _evidence_text(evidence))
    return [sample_rel, evidence_rel]


def _evidence_text(evidence: dict) -> str:
    """The evidence as JSON with one title per line, so the start skill can read it in parts."""
    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    head = dump({k: v for k, v in evidence.items() if k != "titles"})  # "titles" sorts last
    body = ",\n".join(dump(entry) for entry in evidence["titles"])
    return f'{head[:-1]},"titles":[\n{body}\n]}}\n'



def latest_evidence(folder: Path) -> dict:
    directory = folder / paths.LAYOUT["titles"]
    found = sorted(directory.glob("evidence-*.json")) if directory.is_dir() else []
    if not found:
        raise UserError("No title sample yet. Run sample first.")
    try:
        data = fsio.read_json(found[-1])
    except (OSError, ValueError):
        raise UserError(f"{paths.LAYOUT['titles']}/{found[-1].name} is not readable JSON. "
                        "Run sample again.") from None
    if not isinstance(data, dict) or not isinstance(data.get("titles"), list):
        raise UserError(f"{paths.LAYOUT['titles']}/{found[-1].name} has the wrong shape. "
                        "Run sample again.")
    return data


def read_agent_file(folder: Path, name: str, next_step: str) -> object:
    path = folder / paths.LAYOUT["titles"] / name
    if not path.is_file():
        raise UserError(f"{paths.LAYOUT['titles']}/{name} does not exist yet. {next_step}")
    try:
        return fsio.read_json(path)
    except (OSError, ValueError):
        raise UserError(f"{paths.LAYOUT['titles']}/{name} is not readable JSON. Fix it and "
                        "try again.") from None


# Proposal -----------------------------------------------------------------------------------

@dataclass
class FamilyView:
    id: str
    label: str
    titles: list[str]
    terms: list[str]
    count: int
    postings: int
    examples: list[str]
    top_skills: list[str]
    listed: bool


@dataclass
class UncoveredTarget:
    title: str
    count: int


@dataclass
class ProposalCheck:
    errors: list[str] = field(default_factory=list)
    families: list[FamilyView] = field(default_factory=list)
    uncovered: list[UncoveredTarget] = field(default_factory=list)
    text: str = ""


def _top_shared(entries: list[dict]) -> list[str]:
    score: Counter[str] = Counter()
    for entry in entries:
        for skill in entry["top_skills"]:
            score[skill] += entry["n_with_skills"]
    return [s for s, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))][:3]


def _family_view(raw: dict, by_title: dict[str, dict], targets: set[str]) -> FamilyView:
    entries = sorted((by_title[t] for t in raw["titles"]),
                     key=lambda e: (-e["n_with_skills"], e["normalized"]))
    examples: list[str] = []
    for entry in entries:
        for example in entry["examples"]:
            if example not in examples and len(examples) < 3:
                examples.append(example)
    covered = {*raw["titles"], *(_plain_title(t) for t in raw["terms"])}
    return FamilyView(
        id=raw["id"], label=raw["label"], titles=sorted(raw["titles"]), terms=list(raw["terms"]),
        count=sum(e["n_with_skills"] for e in entries),
        postings=sum(e["n_postings"] for e in entries),
        examples=examples, top_skills=_top_shared(entries),
        listed=bool(covered & targets),
    )


def _text_values(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(v, str) and v.strip() for v in value)


def _proposal_errors(proposal: object, by_title: dict[str, dict]) -> list[str]:
    if not isinstance(proposal, dict):
        return ["families: the proposal must be an object with a list called families."]
    errors: list[str] = []
    for key in sorted(set(proposal) - {"families"}):
        errors.append(_stray_key("proposal", key, ("families",)))
    families = proposal.get("families")
    if not isinstance(families, list):
        return [*errors, "families: the proposal needs a list called families."]
    if not families:
        errors.append("families: the proposal needs at least one family.")
    if len(families) > MAX_FAMILIES:
        errors.append(f"families: a proposal has at most {MAX_FAMILIES} families, this has "
                      f"{len(families)}.")
    seen_ids: set[str] = set()
    title_owner: dict[str, str] = {}
    for index, family in enumerate(families):
        path = f"families[{index}]"
        if not isinstance(family, dict):
            errors.append(f"{path}: must be an object with id, label, titles and terms.")
            continue
        for key in sorted(set(family) - set(_FAMILY_KEYS)):
            errors.append(_stray_key(path, key, _FAMILY_KEYS))
        ident = family.get("id")
        if not isinstance(ident, str) or not _FAMILY_ID.fullmatch(ident):
            errors.append(f"{path}.id: must be tf- followed by lowercase letters, digits and "
                          "hyphens.")
        elif ident.startswith(STATED_PREFIX):
            errors.append(f'{path}.id: "{ident}" uses the tf-s- prefix, which belongs to the '
                          "titles you listed. Pick another id.")
        elif ident in seen_ids:
            errors.append(f'{path}.id: "{ident}" is used more than once.')
        if isinstance(ident, str):
            seen_ids.add(ident)
        if not isinstance(family.get("label"), str) or not family["label"].strip():
            errors.append(f"{path}.label: needs text.")
        if not _text_values(family.get("titles")):
            errors.append(f"{path}.titles: needs a list of text values, at least one.")
        else:
            for title in family["titles"]:
                if title not in by_title:
                    errors.append(f'{path}.titles: "{title}" is not in the sample evidence.')
                elif title_owner.setdefault(title, str(ident)) != str(ident):
                    errors.append(f'{path}.titles: "{title}" is in both "{title_owner[title]}" '
                                  f'and "{ident}".')
        terms = family.get("terms")
        if not _text_values(terms):
            errors.append(f"{path}.terms: needs a list of text values, at least one.")
        else:
            errors += [f"{path}.terms[{i}]: is longer than {TERM_MAX} characters."
                       for i, term in enumerate(terms) if len(term) > TERM_MAX]
    return errors


def _stray_key(path: str, key: str, allowed: tuple[str, ...]) -> str:
    if key in _NUMBER_KEYS:
        return (f'{path}.{key}: the proposal may not carry numbers or examples; they are '
                "computed by code from the sample, so remove it.")
    return f'{path}.{key}: is not a field the proposal uses; use only {", ".join(allowed)}.'


def check_proposal(proposal: object, evidence: dict, target_titles: list[str]) -> ProposalCheck:
    """Check the agent's proposal against the code-built evidence and render the counted view."""
    by_title = {e["normalized"]: e for e in evidence["titles"]}
    errors = _proposal_errors(proposal, by_title)
    if errors:
        return ProposalCheck(errors=errors)
    assert isinstance(proposal, dict)
    targets = target_keys(target_titles)
    views = [_family_view(f, by_title, targets) for f in proposal["families"]]
    views.sort(key=lambda v: (-v.count, v.id))
    covered = {t for v in views for t in (*v.titles, *(_plain_title(x) for x in v.terms))}
    uncovered = sorted(
        (UncoveredTarget(t, by_title.get(t, {}).get("n_with_skills", 0))
         for t in targets - covered),
        key=lambda u: (-u.count, u.title))
    check = ProposalCheck(families=views, uncovered=uncovered)
    check.text = render_view(evidence, views, uncovered)
    return check


def render_view(evidence: dict, views: list[FamilyView], uncovered: list[UncoveredTarget]) -> str:
    total = evidence["postings_sampled"]
    lines = [
        "# Job title families found in real postings", "",
        evidence["coverage_note"], "",
        "Every count below was computed by code from the sample. A posting counts when it asks "
        f"for at least {min(2, len(evidence['skills_used']))} of your skills "
        f"({', '.join(evidence['skills_used'])}).", "",
    ]
    for view in views:
        lines += [
            f"### {view.label}", "",
            _COUNT_TEXT.format(count=view.count, total=total) + " ask for your skills.",
            f"Postings with these titles: {view.postings}.",
            f"Examples: {'; '.join(view.examples)}",
            f"You listed this: {'yes' if view.listed else 'no'}",
            f"Terms: {', '.join(view.terms)}", "",
        ]
    if uncovered:
        lines += ["## Titles you listed", ""]
        lines += [f"- {u.title}: " + _COUNT_TEXT.format(count=u.count, total=total)
                  + " ask for your skills." for u in uncovered]
        lines.append("")
    return "\n".join(lines)


# Decisions ----------------------------------------------------------------------------------

@dataclass
class ApplyOutcome:
    errors: list[str] = field(default_factory=list)
    config: dict | None = None
    approved: int = 0
    edited: int = 0
    rejected: int = 0
    unchanged: bool = False
    notices: list[str] = field(default_factory=list)  # said to the user, never an error


def _share(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return float((Decimal(count) / Decimal(total)).quantize(Decimal("0.001"), ROUND_HALF_UP))


def _ignored_rung_notices(items: object) -> list[str]:
    """One notice per decision that carries a rung key. The value is never read."""
    if not isinstance(items, list):
        return []
    return [f"decisions[{index}].rung: ignored. The tool sets each rung from your first-choice "
            "title and the evidence counts."
            for index, item in enumerate(items) if isinstance(item, dict) and "rung" in item]


def _decision_errors(item: object, index: int, by_id: dict[str, FamilyView],
                     seen: set[str]) -> list[str]:
    path = f"decisions[{index}]"
    if not isinstance(item, dict):
        return [f"{path}: must be an object with id and decision."]
    ident = item.get("id")
    if not isinstance(ident, str) or ident not in by_id:
        return [f'{path}.id: "{ident}" is not a family in the proposal.']
    if ident in seen:
        return [f'{path}.id: "{ident}" has more than one decision.']
    seen.add(ident)
    kind = item.get("decision")
    if kind not in ("approve", "reject", "edit"):
        return [f"{path}.decision: must be approve, reject or edit."]
    if kind == "reject":
        return []
    errors: list[str] = []
    view = by_id[ident]
    label, terms = item.get("label"), item.get("terms")
    if kind == "edit":
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{path}.label: an edit needs the new label.")
        if not _text_values(terms):
            errors.append(f"{path}.terms: an edit needs a list of text values, at least one.")
        elif any(len(t) > TERM_MAX for t in terms):
            errors.append(f"{path}.terms: every term must be {TERM_MAX} characters or fewer.")
    elif (label is not None and label != view.label) or (
            terms is not None and terms != view.terms):
        errors.append(f"{path}: an approve keeps the proposal as written; use edit to change "
                      "the label or terms.")
    return errors


def _shared_term_errors(families: list[dict]) -> list[str]:
    owners: dict[str, list[str]] = {}
    for family in families:
        for key in {" ".join(_term_words(t)) for t in family["terms"]}:
            owners.setdefault(key, []).append(family["id"])
    errors: list[str] = []
    for key in sorted(owners):
        if len(owners[key]) > 1:
            first, second = sorted(owners[key])[:2]
            errors.append(f'The term "{key}" is in both "{first}" and "{second}". Edit one '
                          "family so each term belongs to only one.")
    return errors


def _without_time(family: dict) -> dict:
    return {k: v for k, v in family.items() if k != "approved_at"}


def apply_decisions(config: dict, proposal: object, evidence: dict, decisions: object,
                    target_titles: list[str], approved_at: str, *,
                    first_choice_title: object = None) -> ApplyOutcome:
    items = decisions.get("decisions") if isinstance(decisions, dict) else None
    notices = _ignored_rung_notices(items)
    check = check_proposal(proposal, evidence, target_titles)
    if check.errors:
        return ApplyOutcome(errors=check.errors, notices=notices)
    by_id = {v.id: v for v in check.families}
    if not isinstance(items, list):
        return ApplyOutcome(errors=["decisions: the file needs a list called decisions."])
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        errors += _decision_errors(item, index, by_id, seen)
    missing = sorted(set(by_id) - seen)
    if missing:
        errors.append(f"decisions: no decision for {', '.join(missing)}. Every proposed family "
                      "needs approve, reject or edit.")
    if errors:
        return ApplyOutcome(errors=errors, notices=notices)

    first_key = first_choice_key(first_choice_title)
    existing = config["title"]["families"]
    prior = {f["id"]: f for f in existing}
    new_families: dict[str, dict] = {}
    dropped: set[str] = set()
    counts: Counter[str] = Counter()
    for item in items:
        view = by_id[item["id"]]
        kind = item["decision"]
        counts[kind] += 1
        if kind == "reject":
            dropped.add(view.id)
            continue
        label = item["label"].strip() if kind == "edit" else view.label
        terms = [t.strip() for t in (item["terms"] if kind == "edit" else view.terms)]
        new_families[view.id] = {
            "id": view.id, "label": label, "rung": 0,  # family_rung sets it below
            "terms": sorted(set(terms), key=lambda t: (t.casefold(), t)),
            "regex": [],
            "origin": "stated" if view.listed else "discovered",
            "evidence": {
                "sample_date": evidence["sample_date"],
                "postings_sampled": evidence["postings_sampled"],
                "asking_for_your_skills": view.count,
                "share": _share(view.count, evidence["postings_sampled"]),
                "top_shared_skills": view.top_skills,
            },
            "approved_at": approved_at,
        }
        new_families[view.id]["rung"] = family_rung(new_families[view.id], first_key)
        old = prior.get(view.id)
        kept_words, dropped_words = carried_exclude(old, new_families[view.id]["terms"])
        if kept_words:
            new_families[view.id]["exclude"] = kept_words
        notices += [f'The word "{w}" was removed from {view.id} because its new terms contain it, '
                    "so it would rule out every title in the family." for w in dropped_words]
        if old is not None and _without_time(old) == _without_time(new_families[view.id]):
            # Same family as before: keep when it was approved, so a re-run changes nothing.
            new_families[view.id]["approved_at"] = old.get("approved_at", approved_at)
    kept = [f for f in existing if f["id"] not in new_families and f["id"] not in dropped]
    families = sorted([*kept, *new_families.values()], key=lambda f: (-f["rung"], f["id"]))
    if not families:
        return ApplyOutcome(errors=[
            "You need at least one title family to scan. Approve or edit a proposed family, or "
            "list a job title in the interview."], notices=notices)
    conflicts = _shared_term_errors([f for f in families if f["id"] in new_families
                                     or f.get("origin") == "discovered"])
    if conflicts:
        return ApplyOutcome(errors=conflicts, notices=notices)
    result = copy.deepcopy(config)
    result["title"]["families"] = families
    unchanged = (sorted(families, key=lambda f: f["id"])
                 == sorted(existing, key=lambda f: f["id"]))
    return ApplyOutcome(config=result, approved=counts["approve"], edited=counts["edit"],
                        rejected=counts["reject"], unchanged=unchanged, notices=notices)


# Exclusion words ----------------------------------------------------------------------------

@dataclass
class ExcludeOutcome:
    errors: list[str] = field(default_factory=list)
    config: dict | None = None
    unchanged: bool = False
    families_with_words: int = 0
    words: int = 0


_EXCLUSION_KEYS = ("id", "exclude")


def _unknown_family(path: str, ident: object, families: list[dict]) -> str:
    valid = ", ".join(f'{f["id"]} ({f.get("label", f["id"])})' for f in families)
    return f'{path}.id: "{ident}" is not a title family in your config. The families are: {valid}.'


def apply_exclusions(config: dict, data: object) -> ExcludeOutcome:
    """Put the user's approved words into the families named in titles/exclusions.json.

    A family the file does not name keeps its words; an empty list clears them. Nothing is
    changed when any entry has a problem.
    """
    if not isinstance(data, dict):
        return ExcludeOutcome(errors=["families: the file must be an object with a list called "
                                      "families."])
    errors = [f'exclusions.{key}: is not a field this file uses; use only families.'
              for key in sorted(set(data) - {"families"})]
    entries = data.get("families")
    if not isinstance(entries, list):
        return ExcludeOutcome(errors=[*errors, "families: the file needs a list called families."])
    existing = config["title"]["families"]
    by_id = {f["id"]: f for f in existing}
    named: dict[str, list[str]] = {}
    for index, entry in enumerate(entries):
        path = f"families[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: must be an object with id and exclude.")
            continue
        errors += [f"{path}.{key}: is not a field this file uses; use only "
                   f"{', '.join(_EXCLUSION_KEYS)}." for key in sorted(set(entry) - set(_EXCLUSION_KEYS))]
        ident = entry.get("id")
        if not isinstance(ident, str) or ident not in by_id:
            errors.append(_unknown_family(path, ident, existing))
            continue
        if ident in named:
            errors.append(f'{path}.id: "{ident}" appears more than once.')
            continue
        if "exclude" not in entry:
            errors.append(f'{path}: needs "exclude", the words the user approved.')
            continue
        problems = exclude_word_errors(f"{path}.exclude", entry["exclude"], by_id[ident])
        errors += problems
        if not problems:
            named[ident] = sorted((w.strip() for w in entry["exclude"]),
                                  key=lambda w: (w.casefold(), w))
    if errors:
        return ExcludeOutcome(errors=errors)
    result = copy.deepcopy(config)
    for family in result["title"]["families"]:
        if family["id"] in named:
            if named[family["id"]]:
                family["exclude"] = named[family["id"]]
            else:
                family.pop("exclude", None)
    held = [f["exclude"] for f in result["title"]["families"] if f.get("exclude")]
    return ExcludeOutcome(config=result, unchanged=result["title"]["families"] == existing,
                          families_with_words=len(held), words=sum(len(w) for w in held))

