"""The casey-ops demo is held to the code by a byte compare.

Purpose: demo/casey-ops is what a visitor reads on GitHub before installing, so it must never
drift from what the tool really writes. Every generated demo file is rebuilt
here from fixtures, with no network, and compared byte for byte with the committed file.
Logic: tools/build_demo.py runs the start skill's commands in order over three fictional
fixture boards, with the clock frozen at 2026-11-17 12:00 UTC. These tests build into a
temporary folder and compare. A one-byte change to any generated file fails the compare, and
a section that would be empty stops the build with a message that names it.
Guardrails: tests build under tmp_path only and never write into demo/. Casey's letter
answers are the author's dictation; no test writes or rewords them. The letter must be at least 80%
Casey's words by the same count letter-check uses, and every screenshot must be small,
allowlisted and shown in a README.
QA checklist: after any change to the ranked, summary or letter writers, run
`uv run --frozen python tools/build_demo.py` and commit the regenerated files with the change.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import build_demo
from jobsearch import letters

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo" / "casey-ops"


def build(tmp_path: Path, monkeypatch, name: str = "a", **kwargs) -> Path:
    """Build the demo into tmp_path/out-<name> and return that folder."""
    build_demo.configure_offline(monkeypatch.setattr, tmp_path / f"boards-{name}")
    workspace = tmp_path / f"ws-{name}"
    build_demo.build_workspace(workspace, DEMO, **kwargs)
    out = tmp_path / f"out-{name}"
    build_demo.render_demo(workspace, out)
    return out


def test_the_demo_matches_a_fresh_build(tmp_path, monkeypatch):
    out = build(tmp_path, monkeypatch)
    assert build_demo.compare(out, DEMO) == []
    for rel in build_demo.GENERATED_FILES:
        assert (DEMO / rel).is_file(), rel


def test_a_one_byte_change_is_caught(tmp_path, monkeypatch):
    out = build(tmp_path, monkeypatch)
    copy = tmp_path / "copy"
    shutil.copytree(DEMO, copy)
    target = copy / "3-ranked" / "ranked.md"
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0x01
    target.write_bytes(bytes(data))
    assert build_demo.compare(out, copy) == ["3-ranked/ranked.md"]


def test_a_missing_generated_file_is_caught(tmp_path, monkeypatch):
    out = build(tmp_path, monkeypatch)
    copy = tmp_path / "copy"
    shutil.copytree(DEMO, copy)
    (copy / "3-ranked" / "funnel.md").unlink()
    assert build_demo.compare(out, copy) == ["3-ranked/funnel.md"]


def test_two_builds_are_identical_and_lf(tmp_path, monkeypatch):
    first = build(tmp_path, monkeypatch, "a")
    second = build(tmp_path, monkeypatch, "b")
    assert build_demo.compare(first, second) == []
    for rel in build_demo.GENERATED_FILES:
        data = (first / rel).read_bytes()
        assert b"\r" not in data, rel
        assert str(tmp_path).encode() not in data, rel


def test_the_ranked_file_lists_a_and_b_roles_with_a_link_to_apply():
    text = (DEMO / "3-ranked" / "ranked.md").read_text(encoding="utf-8")
    assert text.index("## A") < text.index("## B") < text.index("## C")
    a_tier = text[text.index("## A"):text.index("## B")]
    b_tier = text[text.index("## B"):text.index("## C")]
    assert "Apply: <https://" in a_tier and "Apply: <https://" in b_tier
    view = (DEMO / "2-titles" / "proposal-view.md").read_text(encoding="utf-8")
    assert "sampled postings" in view


def test_an_empty_section_stops_the_build(tmp_path, monkeypatch):
    # A pay floor no role meets and a region none of them is in: nothing passes, so there is
    # nothing to rank.
    answers = {"pay_floor": {"amount": 10_000_000, "currency": "USD", "interval": "year"},
               "regions": ["JP"]}
    with pytest.raises(SystemExit) as stop:
        build(tmp_path, monkeypatch, "empty", answers_update=answers)
    assert "ranked" in str(stop.value)


def test_a_demo_with_no_title_family_stops_the_build(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    shutil.copytree(DEMO, bare)
    (bare / "inputs" / "titles-decisions.json").write_text(
        (DEMO / "inputs" / "titles-decisions.json").read_text(encoding="utf-8")
        .replace('"approve"', '"reject"'), encoding="utf-8", newline="\n")
    build_demo.configure_offline(monkeypatch.setattr, tmp_path / "boards-bare")
    with pytest.raises(SystemExit) as stop:
        build_demo.build_workspace(tmp_path / "ws-bare", bare)
    assert "titles" in str(stop.value)


# The demo narrative a stranger reads ---------------------------------------------

def _readme() -> str:
    return " ".join((DEMO / "README.md").read_text(encoding="utf-8").split())


def test_the_readme_uses_the_tool_name_and_names_every_kept_title():
    readme = _readme()
    assert "job-search-prioritizer" in readme and "Job Search Prioritization Engine" not in readme
    kept = (DEMO / "2-titles" / "summary.md").read_text(encoding="utf-8").split("## Kept")[1]
    labels = [line[2:].split(" (")[0].split(":")[0]
              for line in kept.split("## Dropped")[0].splitlines() if line.startswith("- ")]
    assert len(labels) >= 3
    for label in labels:
        assert label in readme, label
    assert "version 1.0" in readme and "readback" in readme


def test_the_demo_employers_are_fictional_and_named_as_such():
    resume = (DEMO / "1-intake" / "resume.md").read_text(encoding="utf-8")
    employers = [line.split(", ", 1)[1].rsplit(" (", 1)[0]
                 for line in resume.splitlines() if line.startswith("### ")]
    assert employers
    for employer in employers:
        assert employer.endswith(" Example"), employer
        assert employer in _readme(), employer


# The letter and the screenshots ---------------------------------------------

def test_the_letter_is_at_least_80_percent_caseys_words():
    raw = (DEMO / "inputs" / "letter-answers.md").read_text(encoding="utf-8")
    draft = (DEMO / "inputs" / "letter-draft.md").read_text(encoding="utf-8")
    sources = [letters.answer_sources(raw)]
    fixes = letters.load_fixes(DEMO / "inputs" / "letter-fixes.json", DEMO, sources)
    rows = letters.changed_words(draft, sources, fixes, letters.frame_lines(raw))
    changed, total = sum(r[2] for r in rows), sum(r[3] for r in rows)
    assert total > 0 and changed * 100 <= letters.EDIT_SHARE_MAX * total
    check = (DEMO / "4-letter" / "check.md").read_text(encoding="utf-8")
    assert f"{changed} of {total} words in the letter body ({-(-changed * 100 // total)}%)" in check
    assert "letter.md was saved." in check


def test_the_letter_part_is_built_and_compared():
    assert {"4-letter/check.md", "4-letter/letter.md"} <= set(build_demo.GENERATED_FILES)


def test_images_are_small_allowlisted_and_shown():
    allowed = set(json.loads((ROOT / "tools" / "leak_policy.json")
                             .read_text(encoding="utf-8"))["binary_allowlist"])
    shown = (ROOT / "README.md").read_text(encoding="utf-8") + (DEMO / "README.md").read_text(encoding="utf-8")
    images = sorted((DEMO / "screenshots").iterdir())
    assert images
    for image in images:
        rel = image.relative_to(ROOT).as_posix()
        assert image.stat().st_size <= 300 * 1024, rel
        assert rel in allowed, rel
        assert rel in shown, rel
    assert all((ROOT / rel).is_file() for rel in allowed)


def test_the_readback_records_that_the_first_choice_was_skipped():
    text = (DEMO / "1-intake" / "summary.md").read_text(encoding="utf-8")
    assert "first choice: skipped" in text
