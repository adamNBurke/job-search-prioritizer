"""Text-level assertions on the CI workflow and the README badge.

Purpose: keep .github/workflows/ci.yml honest without a YAML dependency. CI cannot run until the
maintainer pushes, so these checks are the only proof, before that push, that the workflow keeps
its three-OS matrix, its pinned actions, its read-only token, and its refusal to hide a failure.
Logic: read ci.yml and README.md as UTF-8 text and assert on substrings, regexes and a simple
split of the file into its top-level jobs.
Guardrails: the Python UTF-8 mode variable name is composed from fragments, so this file never
carries the literal name; CI must not set it, because the Windows plus 3.11 leg is the encoding
negative control. No test here reads a secret or the network.
QA checklist: a new job? Add it to JOBS_EXPECTED. A new action? It needs a 40-hex SHA pin and a
trailing tag comment. A new CLI pin? Update the validate test in the same commit.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
README = REPO / "README.md"

JOBS_EXPECTED = ("test", "stranger", "validate", "leak-gate")
UTF8_MODE_VAR = "PYTHON" + "UTF8"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job_block(name: str) -> str:
    """Return the text of one top-level job: from its `  name:` line to the next job or EOF."""
    text = workflow_text()
    jobs_text = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs_text)
    # parts = [preamble, name1, body1, name2, body2, ...]
    blocks = dict(zip(parts[1::2], parts[2::2], strict=True))
    assert name in blocks, f"job {name!r} missing from ci.yml"
    return blocks[name]


def test_workflow_file_exists_and_is_named_ci():
    assert WORKFLOW.is_file()
    assert re.search(r"(?m)^name: ci$", workflow_text())


def test_expected_jobs_are_present():
    for name in JOBS_EXPECTED:
        assert job_block(name)


def test_runs_on_push_and_pull_request():
    text = workflow_text()
    assert re.search(r"(?m)^on:\n  push:\n  pull_request:", text)


def test_three_operating_systems_in_the_test_matrix():
    block = job_block("test")
    for label in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert label in block


def test_three_operating_systems_in_the_stranger_matrix():
    block = job_block("stranger")
    for label in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert label in block


def test_python_versions_are_3_11_and_3_15():
    block = job_block("test")
    assert '"3.11"' in block
    assert '"3.15"' in block


def test_fail_fast_is_false_for_every_matrix():
    for name in ("test", "stranger"):
        assert "fail-fast: false" in job_block(name)
    assert "fail-fast: true" not in workflow_text()


def test_every_uses_line_pins_a_forty_hex_sha():
    lines = [ln for ln in workflow_text().splitlines() if re.match(r"\s*-?\s*uses:", ln)]
    assert lines, "no uses: lines found"
    for line in lines:
        assert re.search(r"@[0-9a-f]{40}(\s|$)", line), f"unpinned action: {line.strip()}"
        assert re.search(r"#\s*v\d", line), f"missing tag comment: {line.strip()}"


def test_every_checkout_drops_persisted_credentials():
    text = workflow_text()
    checkouts = text.count("uses: actions/checkout@")
    assert checkouts == len(JOBS_EXPECTED)
    assert text.count("persist-credentials: false") == checkouts


def test_token_is_read_only():
    assert re.search(r"(?m)^permissions:\n  contents: read$", workflow_text())


def test_no_pull_request_target():
    assert "pull_request" + "_target" not in workflow_text()


def test_no_secrets_context_is_referenced():
    text = workflow_text()
    assert not re.search(r"secrets\s*\.", text)
    assert not re.search(r"secrets\s*\[", text)
    assert "secrets: inherit" not in text


def test_utf8_mode_variable_is_never_set():
    assert UTF8_MODE_VAR not in workflow_text()


def test_windows_311_leg_exists_so_the_encoding_control_runs():
    block = job_block("test")
    assert "windows-latest" in block and '"3.11"' in block
    # The only exclusion is macOS plus 3.11, so Windows plus 3.11 stays in the matrix.
    excludes = re.findall(r"(?m)^\s+- os: (\S+)\n\s+python: \"([0-9.]+)\"", block)
    assert excludes == [("macos-latest", "3.11")]


def test_no_step_masks_a_failing_exit_code():
    text = workflow_text()
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "; exit 0" not in text
    assert "set +e" not in text


def test_test_job_runs_locks_lint_and_the_full_suite():
    block = job_block("test")
    assert "uv lock --check --script scripts/jobsearch.py" in block
    assert re.search(r"uv lock --check\s*$", block, re.MULTILINE)
    assert "uv run --frozen ruff check ." in block
    assert "uv run --frozen pytest -q" in block
    assert 'version: "0.13.7"' in block


def test_stranger_job_has_no_pinned_python_or_uv_and_runs_the_shells():
    block = job_block("stranger")
    assert "python-version" not in block
    assert "version:" not in block.replace("persist-credentials", "")
    assert "enable-cache: false" in block
    assert "-m shells" in block
    assert "uv run --frozen pytest -q -m shells" in block


def test_validate_job_is_blocking_and_pinned():
    block = job_block("validate")
    assert "claude plugin validate . --strict" in block
    assert "2.1.322" in block
    assert "DISABLE_AUTOUPDATER" in block


def test_leak_gate_job_runs_both_tools_with_pattern_rules_only():
    block = job_block("leak-gate")
    assert "tools/export_public.py" in block
    assert "tools/leak_check.py" in block
    assert "--require-denylist" not in block
    assert "--denylist" not in block


def test_readme_badge_names_the_workflow_file_and_the_file_exists():
    readme = README.read_text(encoding="utf-8")
    assert "actions/workflows/ci.yml/badge.svg" in readme
    assert "/actions/workflows/ci.yml)" in readme
    assert WORKFLOW.is_file()


def test_license_and_gitattributes_content():
    license_text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Adam N. Burke" in license_text
    for path, needle in (
        (".claude-plugin/plugin.json", '"name": "Adam N. Burke"'),
        (".claude-plugin/marketplace.json", '"name": "Adam N. Burke"'),
        ("README.md", "Adam N. Burke. [LinkedIn]("),
    ):
        assert needle in (REPO / path).read_text(encoding="utf-8"), path
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    # The two binary lines keep the demo screenshots out of text handling.
    assert attributes == "* text=auto eol=lf\n*.png binary\n*.jpg binary\n"
    assert "export-ignore" not in attributes
