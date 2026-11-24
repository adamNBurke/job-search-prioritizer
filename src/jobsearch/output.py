"""The ranked files: a Markdown list for reading, JSON for tools and a CSV for spreadsheets.

Purpose: the user's answer to "which jobs first" is a file with the reason first and a link to
apply, plus an honest account of what was filtered out and why, written the same way every time.
Logic: assign_tier gives A, B or C from the config's tier cutoffs (greater than or equal);
role_tier lowers that to the role's tier_cap (a cap only ever lowers a tier and never changes a
score) and is the tier shown and counted everywhere. A role whose tier a cap lowered gets one
"Held at tier B: ..." line after its Tier line. Each A and B block prints the role's
reason_lines: seven labeled lines in one fixed order, Why it fits first, then the Tier
line; a missing fact reads "not stated" in its line and is said once, so there is no Gaps line.
write_ranked sorts the passing roles by score descending, coverage descending, then company,
title and role_id, applies one_per_company and the C collapse (a count plus the top 5), and
writes ranked/ranked-<scan_date>.md, .json and .csv from that one list, plus
ranked/rejected-<scan_date>.json (every filtered-out role and why; no skill reads it). With one_per_company on,
the role kept per company is chosen by shown tier, then score, then the newer posting (a role
with no posted date loses a tie to a dated one), then coverage, title and role_id. A
company with N passing roles (N at least 2) shows "Best of N roles at <company> for you" on the
kept role, then one "Also tier A here: <title>, score <S>. Apply: <link>" line for each other
tier A role there, so no tier A role is hidden; the rest stay in the count. Every funnel count
comes from build_funnel, built once, so the three files agree and the identities in
docs/data-files.md hold. Roles closed since the last scan come from the scan's scan-meta.json.
The ranked md ends with a "Send to career-ops" block (every tier A role as a pipeline line
`- [ ] <link> | <company> | <title>`, text the user copies, no code dependency on career-ops) and
a "Next steps" block naming the letter and review skills; both only
restate the ranking. The same call writes ranked/funnel-<scan_date>.md (summaries.render_funnel,
every funnel count in words) so rank lists the same five files.
Guardrails: posting text is attacker-controlled. clean_text strips control, hidden
and bidirectional characters and em dashes; md_escape makes titles and companies literal
Markdown, the Best of and Also lines included; only https apply links with no whitespace render
as links, in every Apply and Also line; CSV cells that start with =, +, - or @ get an apostrophe
prefix; a career-ops line has its pipes turned to slashes, backticks removed and whitespace
collapsed, and a role without a plain https link is left out and counted. Descriptions never
appear in any output. No file holds a wall-clock timestamp, so the
same inputs give identical bytes, and every file goes through fsio, so a crash or a
second run at the same moment never leaves half a file.
QA checklist: a changed line format needs tests/test_output.py and tests/test_pipeline_tracer.py
updated in the same commit, the Why it fits line must lead the block (before the Tier and Apply
lines), and a changed file shape needs docs/data-files.md in the same commit.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path

from jobsearch import fsio, paths
from jobsearch.roles_store import RankInput
from jobsearch.scoring import ScoredRole


def assign_tier(score: int, criteria: dict) -> str:
    tiers = sorted(criteria["tiers"], key=lambda t: -t["min_score"])
    for tier in tiers:
        if score >= tier["min_score"]:
            return str(tier["id"])
    return str(tiers[-1]["id"])


_TIER_RANK = {"A": 0, "B": 1, "C": 2}


def role_tier(role: ScoredRole, criteria: dict) -> str:
    """The tier a role is shown in: its score's tier, lowered to the role's cap when the cap ranks
    below it. A cap only ever lowers a tier, and never changes the score."""
    tier = assign_tier(role.score, criteria)
    cap = role.tier_cap
    if cap is not None and _TIER_RANK.get(cap, 0) > _TIER_RANK.get(tier, 0):
        return cap
    return tier


# Characters that hide or reorder text are removed by Unicode category, not by a hand list, so a
# character added to Unicode later is still caught: control (Cc, newline stays), format (Cf:
# zero-width, bidirectional, soft hyphen, word joiner, deprecated format, tag characters, byte
# order mark), private use (Co) and surrogates (Cs). Variation selectors are marks (Mn), so they
# are listed, as are the tag block's unassigned code points.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_HIDDEN_RANGES = ((0x180B, 0x180F), (0xFE00, 0xFE0F), (0xE0000, 0xE007F), (0xE0100, 0xE01EF))
# Unicode line breaks that str.splitlines() honors but a text line does not: they become a newline.
_LINE_BREAKS = {"\u0085": "\n", "\u2028": "\n", "\u2029": "\n"}
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>&])")
_TITLE_FAMILY = "title_family"
_TITLE_FAMILY_LABEL = "Title outside your approved families"


@lru_cache(maxsize=4096)
def _is_hidden(char: str) -> bool:
    if char == "\n":
        return False
    if unicodedata.category(char) in _HIDDEN_CATEGORIES:
        return True
    code = ord(char)
    return any(low <= code <= high for low, high in _HIDDEN_RANGES)


def clean_text(text: str) -> str:
    """Strip hidden and control characters, use hyphens for em dashes, and normalize to NFC.

    U+0085, U+2028 and U+2029 become a newline first, so they neither join two words nor survive
    to split a JSONL line in a reader that uses str.splitlines().
    """
    for separator, newline in _LINE_BREAKS.items():
        text = text.replace(separator, newline)
    visible = "".join(char for char in text if not _is_hidden(char))
    return unicodedata.normalize("NFC", visible.replace("\u2014", "-"))


SHOWN_CHARS = 120


def shown(value: str) -> str:
    """A value safe to print in a message: cleaned, and cut to SHOWN_CHARS characters."""
    cleaned = clean_text(value)
    return cleaned if len(cleaned) <= SHOWN_CHARS else cleaned[:SHOWN_CHARS].rstrip() + "..."


def md_escape(text: str) -> str:
    """Posting text as literal Markdown: one line, with every syntax character escaped."""
    one_line = " ".join(clean_text(text).split())
    return _MD_SPECIAL.sub(r"\\\1", one_line)


def apply_link(url: str) -> str:
    """The Markdown for an apply link: a link for a plain https URL, a sentence otherwise."""
    cleaned = clean_text(url)
    if cleaned.startswith("https://") and not re.search(r"[\s<>]", cleaned):
        return f"<{cleaned}>"
    return "Apply link not available; open the posting from the company's careers page."


def funnel_line(*, scanned: int, pasted: int, passed: int, a: int, b: int, c: int) -> str:
    """The one-line funnel, for example "412 scanned, 120 passed filters, 9 A, 22 B, 89 C"."""
    parts = [f"{scanned} scanned"]
    if pasted:
        parts.append(f"{pasted} pasted")
    parts += [f"{passed} passed filters", f"{a} A", f"{b} B", f"{c} C"]
    return ", ".join(parts)


def build_funnel(rank_input: RankInput, scored: list[ScoredRole], criteria: dict) -> dict:
    """Every funnel count, built once so the Markdown, JSON and CSV agree (docs/data-files.md)."""
    passing = [s for s in scored if s.rejected is None]
    tiers = {"A": 0, "B": 0, "C": 0}
    for role in passing:
        tiers[role_tier(role, criteria)] += 1
    return {
        "scanned": rank_input.fetched_total,
        "pasted": rank_input.pasted_total,
        "duplicates_merged": rank_input.duplicates_merged,
        "title_gate_dropped": rank_input.title_gate_dropped,
        "rejected_by_filters": len(scored) - len(passing),
        "passed": len(passing),
        "tiers": tiers,
    }


def funnel_text(funnel: dict) -> str:
    tiers = funnel["tiers"]
    return funnel_line(scanned=funnel["scanned"], pasted=funnel["pasted"],
                       passed=funnel["passed"], a=tiers["A"], b=tiers["B"], c=tiers["C"])


def rejection_counts(scored: list[ScoredRole]) -> list[dict]:
    """Hard-filter rejections by filter, most first, then by filter id."""
    counts: dict[str, dict] = {}
    for role in scored:
        if role.rejected is not None:
            entry = counts.setdefault(role.rejected.filter_id, {
                "filter_id": role.rejected.filter_id, "label": role.rejected.label, "count": 0})
            entry["count"] += 1
    return sorted(counts.values(), key=lambda e: (-e["count"], e["filter_id"]))


def top_reasons(rejections: list[dict], title_gate_dropped: int, limit: int = 3) -> list[dict]:
    """The reasons roles disappeared, with the scan's title-gate drops counted as title_family."""
    merged = {e["filter_id"]: dict(e) for e in rejections}
    if title_gate_dropped:
        entry = merged.setdefault(_TITLE_FAMILY, {"filter_id": _TITLE_FAMILY, "count": 0})
        entry["count"] += title_gate_dropped
        entry["label"] = _TITLE_FAMILY_LABEL
    return sorted(merged.values(), key=lambda e: (-e["count"], e["filter_id"]))[:limit]


def scan_meta(folder: Path, scan_date: str) -> dict:
    """The parsed scan-meta.json of one scan; {} when it is missing, unreadable or not an object."""
    try:
        path = folder / paths.LAYOUT["scans"] / scan_date / "scan-meta.json"
        meta = json.loads(fsio.read_text(path))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def closed_roles(folder: Path, scan_date: str) -> list[dict]:
    """The roles the scan found closed, from its scan-meta.json, sorted for stable output."""
    found = scan_meta(folder, scan_date).get("closed_roles")
    rows = [{"role_id": str(r.get("role_id", "")), "company": str(r.get("company", "")),
             "title": str(r.get("title", ""))} for r in found or [] if isinstance(r, dict)]
    return sorted(rows, key=lambda r: (r["company"], r["title"], r["role_id"]))


ROLE_BREAK = "### "
C_TOP = 5
CSV_COLUMNS = ("tier", "score", "coverage", "provisional", "company", "title", "locations",
               "last_seen", "url_apply", "reason", "gaps", "role_id", "criteria_version")
BOARD_NOTE = "These boards lean toward tech and mid-market companies."
NONE_THIS_TIME = "None this time."
NO_ROLES_PASSED = "No roles passed your filters in this scan."
NO_TIER_A = "No tier A roles this time."
CAREER_OPS_INTRO = ("These are your tier A roles as career-ops pipeline lines. Paste any one link "
                    "into a career-ops session, or add the lines under the Pending heading of "
                    "career-ops' data/pipeline.md and run its pipeline mode.")
NEXT_STEPS = ("Write a cover letter in your own words for any role with "
              "/job-search-prioritizer:letter.",
              "Mark whether a ranking was right with /job-search-prioritizer:review.")


def _ordered(scored: list[ScoredRole]) -> list[ScoredRole]:
    passing = [s for s in scored if s.rejected is None]
    return sorted(passing, key=lambda s: (-s.score, -s.coverage, s.company, s.title, s.role_id))


def _newer_first(role: ScoredRole) -> int:
    """A sort key where a later posted date sorts first and a missing or unreadable date sorts
    last, so an unknown age is never called newer. Only the date part is compared."""
    if role.posted:
        try:
            return -date.fromisoformat(role.posted[:10]).toordinal()
        except ValueError:
            pass
    return 0


def _pick_key(role: ScoredRole, criteria: dict) -> tuple:
    """Which role is kept for a company: shown tier, then score, then the newer posting, then
    coverage, title and role_id. Company is not in the key: the roles share it."""
    return (_TIER_RANK[role_tier(role, criteria)], -role.score, _newer_first(role),
            -role.coverage, role.title, role.role_id)


def _one_per_company_on(criteria: dict) -> bool:
    one = criteria.get("one_per_company")
    return isinstance(one, dict) and one.get("enabled") is True


def _display_plan(ordered: list[ScoredRole], criteria: dict
                  ) -> tuple[dict[str, tuple[bool, int]], dict[str, list[ScoredRole]]]:
    """(role_id -> (shown, more_at_company), kept role_id -> its other tier A roles).

    One role per company is kept when one_per_company is on; more_at_company is
    the number of other passing roles at the company, and the other tier A roles come back in the
    ranking order so the block can name each of them. The C collapse follows.
    """
    collapse = _one_per_company_on(criteria)
    groups: dict[str, list[ScoredRole]] = {}
    for role in ordered:
        groups.setdefault(role.company.casefold(), []).append(role)
    more = {r.role_id: 0 for r in ordered}
    shown = {r.role_id: True for r in ordered}
    also: dict[str, list[ScoredRole]] = {}
    if collapse:
        for group in groups.values():
            # A role held at B by its cap never hides an open tier A role at the same company,
            # because the shown tier leads the key.
            best = min(group, key=lambda r: _pick_key(r, criteria))
            more[best.role_id] = len(group) - 1
            for role in group:
                if role is not best:
                    shown[role.role_id] = False
            others = [r for r in group if r is not best and role_tier(r, criteria) == "A"]
            if others:
                also[best.role_id] = others
    seen_c = 0
    for role in ordered:
        if shown[role.role_id] and role_tier(role, criteria) == "C":
            seen_c += 1
            if seen_c > C_TOP:
                shown[role.role_id] = False
    return {r.role_id: (shown[r.role_id], more[r.role_id]) for r in ordered}, also


def _role_block(role: ScoredRole, tier: str, more: int, held: bool = False,
                also: list[ScoredRole] | None = None) -> list[str]:
    marker = (f", provisional (based on {role.coverage} percent of your criteria)"
              if role.provisional else "")
    lines = [f"{ROLE_BREAK}{md_escape(role.title)} at {md_escape(role.company)}", ""]
    # The labels are code constants and only the text is escaped, so a posting cannot
    # change a label or start a new line.
    lines += [f"{label}: {md_escape(text)}" for label, text in role.reason_lines]
    lines.append(f"Tier {tier}, score {role.score}{marker}")
    if held:
        lines.append(f"Held at tier {tier}: " + md_escape("; ".join(role.cap_reasons)) + ".")
    if more:
        # The shown role says it is the best of the company's passing roles, and each
        # other tier A role there gets one short line, so no tier A role is hidden.
        lines.append(f"Best of {more + 1} roles at {md_escape(role.company)} for you")
        lines += [f"Also tier A here: {md_escape(other.title)}, score {other.score}. "
                  f"Apply: {apply_link(other.url_apply)}" for other in also or []]
    lines += [f"Last seen open: {clean_text(role.last_seen)}",
              f"Apply: {apply_link(role.url_apply)}", ""]
    return lines


def _career_ops_line(role: ScoredRole) -> str | None:
    """One career-ops pipeline line, "- [ ] <link> | <company> | <title>", or None when the role
    has no link another tool can safely take. The text sits in a fenced block the user copies, so
    it must stay on one line and cannot hold a column separator or a fence: a pipe becomes a
    slash, backticks go, whitespace collapses. A link counts only when apply_link accepts it and
    it holds no pipe or backtick (either would shift a column or close the fence)."""
    link = apply_link(role.url_apply)
    if not link.startswith("<"):
        return None
    url = link[1:-1]
    if "|" in url or "`" in url:
        return None

    def cell(text: str) -> str:
        return " ".join(clean_text(text).replace("`", "").split()).replace("|", "/")

    return f"- [ ] {url} | {cell(role.company)} | {cell(role.title)}"


def _career_ops_lines(tier_a: list[ScoredRole], plan: dict, also: dict[str, list[ScoredRole]]
                      ) -> list[str]:
    """The `## Send to career-ops` block: every shown tier A role, then its "Also tier A here"
    siblings, in the order the A section prints them. Reads the plan; never changes it."""
    roles: list[ScoredRole] = []
    for role in tier_a:
        if plan[role.role_id][0]:
            roles.append(role)
            roles += also.get(role.role_id, [])
    items = [_career_ops_line(r) for r in roles]
    lines = [item for item in items if item is not None]
    left_out = len(items) - len(lines)
    out = ["## Send to career-ops", "", CAREER_OPS_INTRO, ""]
    if not roles:
        return out + [NO_TIER_A, ""]
    if lines:
        out += ["```text", *lines, "```", ""]
    if left_out:
        noun = ("tier A role has" if left_out == 1 else "tier A roles have")
        verb = "is" if left_out == 1 else "are"
        out += [f"{left_out} {noun} no usable apply link and {verb} left out.", ""]
    return out


def _c_line(role: ScoredRole) -> str:
    return (f"- {md_escape(role.title)} at {md_escape(role.company)}, score {role.score}. "
            f"{md_escape(role.reason_text)} Apply: {apply_link(role.url_apply)}")


def _count_line(roles: list[ScoredRole], plan: dict, listed: set[str], collapse: bool) -> str:
    """The count line for a tier. The count is every role in the tier, as the funnel counts
    it; the clause says how many one_per_company hid behind a listed company; the number after
    Top is the number of roles listed below it. An A or B tier prints this line only when all its
    roles are hidden, so "None this time." means the tier is empty."""
    shown = sum(1 for r in roles if plan[r.role_id][0])
    hidden = sum(1 for r in roles if not plan[r.role_id][0]
                 and r.company.casefold() in listed) if collapse else 0
    why = (f", {hidden} not shown because " + ("its company is" if hidden == 1 else "their company is")
           + " already listed") if hidden else ""
    top = f" Top {shown}:" if shown else ""
    return f"{len(roles)} {'role' if len(roles) == 1 else 'roles'}{why}.{top}"


def _markdown(rank_input: RankInput, ordered: list[ScoredRole], plan: dict,
              also: dict[str, list[ScoredRole]], funnel: dict, reasons: list[dict],
              closed: list[dict], criteria: dict) -> str:
    meanings = {t["id"]: str(t.get("meaning", "")) for t in criteria["tiers"]}
    by_tier: dict[str, list[ScoredRole]] = {"A": [], "B": [], "C": []}
    for role in ordered:
        by_tier[role_tier(role, criteria)].append(role)
    lines = ["# Ranked roles", "",
             f"Scoring config version {clean_text(str(criteria['criteria_version']))}. "
             f"Scan date {clean_text(rank_input.scan_date)}.", BOARD_NOTE, ""]
    if not ordered:
        lines += [NO_ROLES_PASSED, ""]
    listed = {r.company.casefold() for r in ordered if plan[r.role_id][0]}
    collapse = _one_per_company_on(criteria)
    for tier in ("A", "B"):
        lines += [f"## {tier}", "", md_escape(meanings.get(tier, "")), ""]
        shown = [r for r in by_tier[tier] if plan[r.role_id][0]]
        if not by_tier[tier]:
            lines += [NONE_THIS_TIME, ""]
        elif not shown:
            lines += [_count_line(by_tier[tier], plan, listed, collapse), ""]
        for role in shown:
            held = assign_tier(role.score, criteria) != tier
            lines += _role_block(role, tier, plan[role.role_id][1], held, also.get(role.role_id))
    c_head = _count_line(by_tier["C"], plan, listed, collapse) if by_tier["C"] else "0 roles."
    lines += ["## C", "", md_escape(meanings.get("C", "")), "", c_head, ""]
    top_c = [r for r in by_tier["C"] if plan[r.role_id][0]]
    lines += [_c_line(r) for r in top_c] + ([""] if top_c else [])
    lines += ["## Funnel", "", funnel_text(funnel), ""]
    if reasons:
        lines += ["Top reasons roles were filtered out:"]
        lines += [f"- {md_escape(r['label'])}: {r['count']}" for r in reasons] + [""]
    if closed:
        noun = "role" if len(closed) == 1 else "roles"
        lines += ["## Closed roles", "", f"{len(closed)} {noun} closed since your last scan", ""]
        lines += [f"- {md_escape(r['title'])} at {md_escape(r['company'])}" for r in closed]
        lines += [""]
    lines += _career_ops_lines(by_tier["A"], plan, also)
    lines += ["## Next steps", "", *[f"- {step}" for step in NEXT_STEPS], ""]
    return "\n".join(lines)


def _json_payload(rank_input: RankInput, scored: list[ScoredRole], ordered: list[ScoredRole],
                  plan: dict, funnel: dict, closed: list[dict], criteria: dict) -> dict:
    return {
        "schema_version": 1,
        "criteria_version": criteria["criteria_version"],
        "scan_date": rank_input.scan_date,
        "funnel": funnel,
        "rejections": rejection_counts(scored),
        "closed_since_last": [{k: clean_text(v) for k, v in r.items()} for r in closed],
        "roles": [
            {
                "role_id": r.role_id, "tier": role_tier(r, criteria), "score": r.score,
                "coverage": r.coverage, "provisional": r.provisional,
                "tier_cap": r.tier_cap, "cap_reasons": [clean_text(c) for c in r.cap_reasons],
                "company": clean_text(r.company), "title": clean_text(r.title),
                "locations": [clean_text(x) for x in r.locations],
                "url_apply": clean_text(r.url_apply), "first_seen": r.first_seen,
                "last_seen": r.last_seen, "source": r.source,
                "reason_text": clean_text(r.reason_text), "gaps": [clean_text(g) for g in r.gaps],
                "shown": plan[r.role_id][0],
                "more_at_company": plan[r.role_id][1],
            }
            for r in ordered
        ],
    }


def _rejected_payload(rank_input: RankInput, scored: list[ScoredRole]) -> dict:
    """Why each filtered-out role is not listed, for the user who asks "where is job X?". No skill
    reads it, so the ranked JSON the skills load stays small."""
    return {
        "schema_version": 1,
        "scan_date": rank_input.scan_date,
        "rejected": [
            {
                "role_id": r.role_id, "company": clean_text(r.company),
                "title": clean_text(r.title), "filter_id": r.rejected.filter_id,
                "reason": clean_text(r.rejected.reason),
            }
            for r in sorted(scored, key=lambda s: s.role_id) if r.rejected is not None
        ],
    }


_CSV_BOM = b"\xef\xbb\xbf"


def _csv_cell(text: str) -> str:
    """Cell text, prefixed with an apostrophe when a spreadsheet would read it as a formula."""
    cleaned = clean_text(text)
    return "'" + cleaned if cleaned.lstrip()[:1] in ("=", "+", "-", "@") else cleaned


def _csv(ordered: list[ScoredRole], criteria: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in ordered:
        writer.writerow([
            role_tier(r, criteria), r.score, r.coverage, "true" if r.provisional else "false",
            _csv_cell(r.company), _csv_cell(r.title), _csv_cell("; ".join(r.locations)),
            r.last_seen, _csv_cell(r.url_apply), _csv_cell(r.reason_text),
            _csv_cell("; ".join(r.gaps)), r.role_id, criteria["criteria_version"],
        ])
    return buffer.getvalue()


def write_ranked(folder: Path, rank_input: RankInput, scored: list[ScoredRole],
                 criteria: dict) -> list[str]:
    """Write the three ranked files, the funnel file and the rejected list from one scored list;
    returns their data-folder paths, sorted. rank calls this one writer, so the five files
    cannot disagree."""
    from jobsearch import summaries  # here, not at the top: summaries imports this module

    ordered = _ordered(scored)
    plan, also = _display_plan(ordered, criteria)
    funnel = build_funnel(rank_input, scored, criteria)
    reasons = top_reasons(rejection_counts(scored), rank_input.title_gate_dropped)
    closed = closed_roles(folder, rank_input.scan_date)
    ranked = paths.LAYOUT["ranked"]
    stem = f"{ranked}/ranked-{rank_input.scan_date}"
    fsio.write_text_atomic(folder / f"{stem}.md", _markdown(
        rank_input, ordered, plan, also, funnel, reasons, closed, criteria))
    fsio.write_json_atomic(folder / f"{stem}.json", _json_payload(
        rank_input, scored, ordered, plan, funnel, closed, criteria))
    # Only the CSV carries a byte order mark: Excel on Windows needs it to read accents as UTF-8.
    # The same bytes come out on every OS, and readers open the file with utf-8-sig.
    fsio.write_bytes_atomic(folder / f"{stem}.csv", _CSV_BOM + _csv(ordered, criteria).encode("utf-8"))
    # Not named ranked-*.json: newest_ranked_json, notices and progress glob that pattern. Written
    # on every rank, an empty list included, so a re-rank never leaves a stale list behind.
    rejected_path = f"{ranked}/rejected-{rank_input.scan_date}.json"
    fsio.write_json_atomic(folder / rejected_path, _rejected_payload(rank_input, scored))
    funnel_path = f"{ranked}/funnel-{rank_input.scan_date}.md"
    fsio.write_text_atomic(folder / funnel_path,
                           summaries.render_funnel(folder, rank_input, scored, criteria))
    return sorted([funnel_path, f"{stem}.csv", f"{stem}.json", f"{stem}.md", rejected_path])
