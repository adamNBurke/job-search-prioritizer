"""The trust files (issue forms, Wins form, SUPPORT, CONTRIBUTING, PR template,
security policy, code of conduct, agent doc) hold up.

Purpose: strangers post on a public page. Every form must open with a privacy warning, parse as a
GitHub form, and the agent doc must name every skill so a fresh agent can explain the tool.
Logic: PyYAML is not in the dev environment, so the forms are checked as text with the standard
library. form_problems(text) lists missing top-level keys and body items without a type line.
lacks_privacy_warning(text) is true unless the text names the resume plus a second private item.
has_em_dash(text) flags U+2014. Each check function takes text, so one test proves it fires on a
broken input and stays quiet on a good one (a negative control, as in tests/test_skills_content.py).
broken_links(text, base) reuses release_check's own link pattern, so the README gate and these docs
follow one rule: it names relative targets that miss a file or leave the repo.
Guardrails: issue forms need name, description and body (docs.github.com, syntax for issue forms,
fetched 2026-11-22). Discussion category forms document only title, labels and body, so a
discussion form is checked for body alone. No file here may carry an em dash.
QA checklist: new form file? Add it to ISSUE_FORMS or DISCUSSION_FORMS. New skill folder? The
agent doc test fails until for-agents.md names it as /job-search-prioritizer:<name>.
New prose doc? Add it to PROSE_DOCS, and its relative links are checked too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from helpers import EM_DASH, has_em_dash

import release_check

ROOT = Path(__file__).resolve().parent.parent
ISSUE_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"
DISCUSSION_DIR = ROOT / ".github" / "DISCUSSION_TEMPLATE"
SKILLS_DIR = ROOT / "skills"
ISSUE_KEYS = ("name", "description", "body")
DISCUSSION_KEYS = ("body",)
ISSUE_FORMS = ("bug.yml", "feature.yml")
DISCUSSION_FORMS = ("wins.yml",)
PROSE_DOCS = (
    ROOT / ".github" / "SUPPORT.md",
    ROOT / "docs" / "for-agents.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / ".github" / "pull_request_template.md",
    ROOT / ".github" / "SECURITY.md",
    ROOT / ".github" / "CODE_OF_CONDUCT.md",
)
DEMO_README = ROOT / "demo" / "casey-ops" / "README.md"
LINKED_DOCS = PROSE_DOCS + (DEMO_README,)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def form_problems(text: str, required: tuple[str, ...] = ISSUE_KEYS) -> list[str]:
    """Missing top-level keys, plus every body item that has no `type:` line."""
    problems = [f"missing top-level key: {key}" for key in required
                if not re.search(rf"^{key}:", text, flags=re.M)]
    body = re.split(r"^body:\s*$", text, maxsplit=1, flags=re.M)
    items = re.split(r"^  - ", body[1], flags=re.M)[1:] if len(body) > 1 else []
    if len(body) > 1 and not items:
        problems.append("body has no items")
    for number, item in enumerate(items, start=1):
        if not re.match(r"type:\s*\S", item):
            problems.append(f"body item {number} has no type")
    return problems


def lacks_privacy_warning(text: str) -> bool:
    flat = " ".join(text.lower().split())
    return not ("resume" in flat and ("answers.json" in flat or "email" in flat)
                and ("data folder" in flat or "employer" in flat))


def broken_links(text: str, base: Path) -> list[str]:
    """Relative link targets in text that miss a file or leave the repo, resolved against base."""
    broken = []
    for match in release_check._LINK.finditer(text):
        target = match.group(1)
        if target.startswith("#") or release_check._SCHEME.match(target):
            continue
        resolved = (base / target.split("#", 1)[0]).resolve()
        if not resolved.is_relative_to(ROOT) or not resolved.exists():
            broken.append(target)
    return broken


def skill_names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


@pytest.mark.parametrize("name", ISSUE_FORMS)
def test_issue_forms_parse_and_warn(name: str) -> None:
    text = _read(ISSUE_DIR / name)
    assert form_problems(text) == []
    assert not lacks_privacy_warning(text)
    assert not has_em_dash(text)


@pytest.mark.parametrize("name", DISCUSSION_FORMS)
def test_discussion_forms_parse_and_warn(name: str) -> None:
    text = _read(DISCUSSION_DIR / name)
    assert form_problems(text, DISCUSSION_KEYS) == []
    assert not lacks_privacy_warning(text)
    assert not has_em_dash(text)


@pytest.mark.parametrize("path", PROSE_DOCS, ids=lambda p: p.name)
def test_prose_docs_have_no_em_dash(path: Path) -> None:
    assert not has_em_dash(_read(path))


@pytest.mark.parametrize("path", LINKED_DOCS, ids=lambda p: p.name)
def test_doc_links_resolve(path: Path) -> None:
    assert broken_links(_read(path), path.parent) == []


def test_support_says_what_never_to_paste() -> None:
    assert not lacks_privacy_warning(_read(ROOT / ".github" / "SUPPORT.md"))


def test_agent_doc_names_every_skill() -> None:
    text = _read(ROOT / "docs" / "for-agents.md")
    assert skill_names(), "no skills found"
    assert [n for n in skill_names() if f"/job-search-prioritizer:{n}" not in text] == []
    for planned in ("start", "scan", "review", "letter", "feedback", "status"):
        assert f"/job-search-prioritizer:{planned}" in text


def test_agent_doc_points_at_the_file_docs() -> None:
    text = _read(ROOT / "docs" / "for-agents.md")
    assert "docs/contracts.md" in text and "docs/data-files.md" in text


def test_issue_config_turns_blank_issues_off_and_points_to_discussions() -> None:
    text = _read(ISSUE_DIR / "config.yml")
    assert "blank_issues_enabled: false" in text
    assert "/discussions" in text
    assert not has_em_dash(text)


def test_bug_form_asks_for_the_environment_and_scrubbed_output() -> None:
    text = _read(ISSUE_DIR / "bug.yml")
    for needle in ("id: os", "id: shell", "id: plugin-version", "id: output", "Remove names, emails and folder paths"):
        assert needle in text


def test_negative_controls_make_each_check_fire() -> None:
    good = "name: A\ndescription: B\nbody:\n  - type: markdown\n    attributes:\n      value: x\n"
    assert form_problems(good) == []
    assert form_problems(good.replace("name: A\n", "")) == ["missing top-level key: name"]
    assert form_problems(good.replace("description: B\n", "")) == ["missing top-level key: description"]
    assert form_problems("name: A\ndescription: B\n") == ["missing top-level key: body"]
    assert form_problems(good.replace("type: markdown", "attributes2: z")) == ["body item 1 has no type"]
    assert form_problems("name: A\ndescription: B\nbody:\n") == ["body has no items"]
    assert form_problems("body:\n  - type: input\n", DISCUSSION_KEYS) == []
    assert form_problems("title: x\n", DISCUSSION_KEYS) == ["missing top-level key: body"]
    assert [n for n in ("start", "ghost") if f"/job-search-prioritizer:{n}" not in "/job-search-prioritizer:start"] == ["ghost"]
    assert lacks_privacy_warning("Please be kind.")
    assert lacks_privacy_warning("Never paste your resume.")
    assert not lacks_privacy_warning("Never paste your resume, answers.json or any file from your data folder.")
    assert has_em_dash("one " + EM_DASH + " two") and not has_em_dash("one - two")
    assert broken_links("[a](nope.md) [b](https://example.com) [c](#top) [d](CONTRIBUTING.md#license)", ROOT) == ["nope.md"]
    assert broken_links("[e](../outside.md)", ROOT) == ["../outside.md"]
