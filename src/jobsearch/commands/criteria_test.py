"""criteria-test: preview how your config ranks your latest scan, before anything is saved.

Purpose: the user sees how many roles would land in each tier, and which cutoffs fit their own
results, before accepting any of them.
Logic: resolve the data folder, score the latest scan against config/criteria.json, count the
roles in each tier at the current cutoffs, propose cutoffs with calibrate.propose, and write
ranked/preview-<scan_date>.md with the funnel, the top three rejection reasons, the tier mix at
the current and the proposed cutoffs and up to three sample titles per family. stdout carries
only the counts and the proposed cutoffs. The counts come from calibrate.passing_with_caps, so a
role a tier cap holds at B counts as B, the same as in the ranked files.
Guardrails: read-only except for the preview file, written atomically; no network. With no
passing roles it says cutoffs cannot be proposed yet and names the top rejection reason. Posting
text in the preview goes through output.md_escape. The file holds no wall-clock timestamp.
QA checklist: tests/test_calibrate.py drives this through cli.main for the proposal and the
empty case.
"""

from __future__ import annotations

import argparse

from jobsearch import calibrate, fsio, output, paths, workspace
from jobsearch.families import compile_families
from jobsearch.models import Result
from jobsearch.roles_store import RankInput
from jobsearch.scoring import ScoredRole

NAME = "criteria-test"
HELP = "Preview how your config ranks your latest scan."
SAMPLES_PER_FAMILY = 3
NO_PROPOSAL = "No roles passed your filters, so cutoffs cannot be proposed yet."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    workspace.add_folder_args(parser)


def _mix(counts: dict[str, int]) -> str:
    return f"{counts['A']} A, {counts['B']} B, {counts['C']} C"


def _samples(criteria: dict, scored: list[ScoredRole]) -> list[str]:
    lines: list[str] = []
    for family in compile_families(criteria).families:
        titles = [s.title for s in scored if s.rejected is None and s.family_id == family.family_id]
        shown = sorted(set(titles))[:SAMPLES_PER_FAMILY]
        label = output.md_escape(family.label)
        lines.append(f"- {label}: " + ("; ".join(output.md_escape(t) for t in shown)
                                       if shown else "no roles in this scan"))
    return lines


def _preview(criteria: dict, rank_input: RankInput, scored: list[ScoredRole],
             funnel: dict, reasons: list[dict], current: dict, proposal) -> str:
    lines = ["# Preview of your tiers", "",
             f"Scoring config version {criteria['criteria_version']}. "
             f"Scan date {rank_input.scan_date}.", "",
             output.funnel_text(funnel), ""]
    if reasons:
        lines += ["Top reasons roles were filtered out:"]
        lines += [f"- {output.md_escape(r['label'])}: {r['count']}" for r in reasons]
        lines += [""]
    tiers = {t["id"]: t["min_score"] for t in criteria["tiers"]}
    lines += [f"Tier mix at your current cutoffs (A at {tiers['A']}, B at {tiers['B']}): "
              f"{_mix(current)}.", ""]
    if proposal is None:
        lines += [NO_PROPOSAL, ""]
    else:
        lines += [f"Proposed cutoffs from your own results: A at {proposal.a_min}, "
                  f"B at {proposal.b_min}.",
                  f"Tier mix at the proposed cutoffs: {_mix(proposal.counts)}.", ""]
        if proposal.caps_dominate:
            lines += [calibrate.CAPS_DOMINATE, ""]
    lines += ["Sample titles per family:", ""] + _samples(criteria, scored) + [""]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> Result:
    folder, warnings = workspace.require(NAME, args)
    criteria, rank_input, scored = calibrate.score_latest(folder)
    scores = calibrate.passing_with_caps(scored)
    tiers = {t["id"]: t["min_score"] for t in criteria["tiers"]}
    current = calibrate.counts_at(scores, tiers["A"], tiers["B"])
    proposal = calibrate.propose(scores)
    funnel = output.build_funnel(rank_input, scored, criteria)
    reasons = output.top_reasons(
        output.rejection_counts(scored), rank_input.title_gate_dropped)
    path = f"{paths.LAYOUT['ranked']}/preview-{rank_input.scan_date}.md"
    fsio.write_text_atomic(
        folder / path, _preview(criteria, rank_input, scored, funnel, reasons, current, proposal))
    data: dict[str, object] = {
        "scan_date": rank_input.scan_date, "criteria_version": criteria["criteria_version"],
        "funnel_line": output.funnel_text(funnel), "current_counts": current,
    }
    counts = {"scanned": funnel["scanned"], "passed": funnel["passed"],
              "rejected": funnel["rejected_by_filters"]}
    if proposal is None:
        top = reasons[0]["label"] if reasons else "the title families you approved"
        return Result(ok=True, command=NAME, data=data, counts=counts, wrote=[path],
                      warnings=[*warnings, NO_PROPOSAL],
                      next=f"Loosen the filter that removed the most roles ({top}), scan again "
                           "and run criteria-test again.")
    data.update(proposed_a_min=proposal.a_min, proposed_b_min=proposal.b_min,
                proposed_counts=proposal.counts)
    if proposal.caps_dominate:
        warnings = [*warnings, calibrate.CAPS_DOMINATE]
    return Result(ok=True, command=NAME, data=data, counts=counts, wrote=[path],
                  warnings=warnings,
                  next=f"Run calibrate --a-min {proposal.a_min} --b-min {proposal.b_min} to "
                       "accept these cutoffs.")
