"""Guard: the public export carries no references to the private build trail.

Purpose: keep the repository reading as a clean release. Comments, docstrings and docs must
state a rule and its reason in plain words, never point at decision, gap, review, audit,
threat or requirement ids, plan or phase numbers, private planning documents, or dated
decision notes that only the maintainer's private folder can resolve.
Logic: take the exact file list the export would ship (export_public.export_tree on the real
index), read each text file, and match one pattern (TRAIL) per line. A hit passes only when
its matched text is listed for that path in ALLOWED. An ALLOWED entry that no longer matches
anything fails the test, so the map cannot rot. Two controls pin the pattern itself.
Guardrails: ALLOWED is token-level per path, so it cannot hide a new reference in an allowed
file. This file skips its own scan because its pattern and samples are the trail. Only staged
content is exported, so stage edits before running this test.
QA checklist: a hit that is real prose, rewrite it. A hit that is test data or a user-facing
string, add the exact text to ALLOWED for that path. A new reference shape, add a catch
sample and extend TRAIL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import export_public
import pytest

REPO = Path(__file__).resolve().parent.parent
SELF = "tests/test_public_trail.py"

REQUIREMENT_PREFIXES = "DISC|FDBK|INST|INTK|INTR|LNCH|LRN|LTR|OUT|PRIV|RANK|RES|RSCH|SCAN|SRC|TRAK|TRST|UPD|UPS"
TRAIL = re.compile(
    r"\.planning\b"
    r"|\b(?:STACK|PITFALLS|ROADMAP|REQUIREMENTS)\b"
    r"|\b0[0-9]-(?:[0-9]{2}-)?[A-Z]+\.md\b"
    r"|\b(?:LD|D|IN)-[0-9]{2}\b|\b[AR]-[0-9]\b|\bSC[0-9]+\b"
    r"|\b(?:WR|CR)-[A-C]?[0-9]{2}\b|\bG-0[0-9]-[0-9]+[a-z]?\b|\bT-0[0-9]-[0-9]{2}\b|\bL[0-9]#[0-9]+"
    r"|\b[0-9]{2}[01][0-9][0-3][0-9]-[a-z0-9]{3}\b|(?<![0-9:.-])\b0[1-3]-[0-9]{2}\b(?![-:0-9])"
    r"|\bUAT\b|\bPhase [0-9]|\b[Pp]lan 0[0-9]|\bquick task\b|\bAdam\b"
    r"|\b(?:decided|revised|decision of)(?: by \w+)?(?: on)? 20[0-9]{2}-[0-9]{2}-[0-9]{2}"
    r"|\bat (?:commit )?(?=[0-9a-f]{0,39}[0-9])(?=[0-9a-f]{0,39}[a-f])[0-9a-f]{7,40}\b"
    rf"|\b(?:{REQUIREMENT_PREFIXES})-[0-9]{{2}}\b"
)

# Matched texts that may stay, per path: the author's name where it is the project's byline or a
# test-pinned string, and the private planning folder where the tools and their tests name it as
# a path to exclude or scan.
ALLOWED: dict[str, frozenset[str]] = {
    "LICENSE": frozenset({"Adam"}),
    "README.md": frozenset({"Adam"}),
    ".github/CODE_OF_CONDUCT.md": frozenset({"Adam"}),
    ".claude-plugin/plugin.json": frozenset({"Adam"}),
    ".claude-plugin/marketplace.json": frozenset({"Adam"}),
    "skills/feedback/SKILL.md": frozenset({"Adam"}),
    "src/jobsearch/notices.py": frozenset({"Adam"}),
    "tests/test_ci_workflow.py": frozenset({"Adam"}),
    "tools/export_public.py": frozenset({".planning"}),
    "tools/leak_policy.json": frozenset({".planning"}),
    "tests/test_export.py": frozenset({".planning"}),
    "tests/test_leak_gate.py": frozenset({".planning"}),
}

CATCH = [
    "D-02", "LD-03", "G-02-2d", "WR-07", "WR-C06", "IN-12", "CR-A01", "L4#3", "T-03-39",
    "A-1", "R-3", "261123-f66", "plan 02-04", "Plan 03-09", "(03-06)", "Phase 2", "UAT 4",
    "quick task", "(Adam, 2026-11-18)", "decided 2026-11-17", "revised by Adam on 2026-11-18",
    "INTK-04", "ROADMAP SC5", "PITFALLS 11", "STACK.md", "01-SECURITY.md", ".planning/x.md",
    "at 5684aab", "at commit 120363f",
]  # fmt: skip

LEAVE = [
    "UTF-8", "KNOWN_EVALUATORS", "2026-11-01T09:00:00-05:00", "1987-03-14", "03-14-1987",
    "SHA-256", "PDF-1", "Measured on 2026-11-24", ".github/SECURITY.md", "COVID-19", "A-Z",
    "at 10 percent", "at deadbeef", "Python 3.11", "v1.0.0", "evaluator", "a plan to apply",
]  # fmt: skip


def scan(root: Path, files: Iterable[str]) -> list[tuple[str, int, str]]:
    """Return (path, line number, matched text) for every TRAIL match in the text files."""
    hits: list[tuple[str, int, str]] = []
    for rel in files:
        if rel == SELF:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            hits.extend((rel, number, m.group(0)) for m in TRAIL.finditer(line))
    return hits


def unallowed(hits: Iterable[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    return [h for h in hits if h[2] not in ALLOWED.get(h[0], frozenset())]


def stale(hits: Iterable[tuple[str, int, str]]) -> list[tuple[str, str]]:
    seen = {(path, text) for path, _, text in hits}
    return sorted((p, t) for p, texts in ALLOWED.items() for t in texts if (p, t) not in seen)


@pytest.fixture(scope="module")
def export_hits(tmp_path_factory):
    if not (REPO / ".git").exists():
        pytest.skip("needs the git checkout this suite ships in")
    out = tmp_path_factory.mktemp("public") / "export"
    files = export_public.export_tree(REPO, out)
    return scan(out, files)


def test_the_pattern_catches_each_kind_of_build_reference():
    missed = [s for s in CATCH if not TRAIL.search(s)]
    assert missed == []


def test_the_pattern_leaves_dates_offsets_and_plain_words_alone():
    wrong = [s for s in LEAVE if TRAIL.search(s)]
    assert wrong == []


def test_the_public_export_carries_no_build_trail(export_hits):
    bad = unallowed(export_hits)
    shown = "\n".join(f"{p}:{n}: {t}" for p, n, t in bad[:40])
    assert bad == [], f"{len(bad)} build-trail references in the export; first 40:\n{shown}"
    gone = stale(export_hits)
    assert gone == [], f"an allowlist entry no longer matches anything and must be removed: {gone}"
