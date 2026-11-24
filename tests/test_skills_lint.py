"""Every script call in a skill is one plain, shell-neutral `uv run` line.

Purpose: skills run in the user's own shell, which may be bash, zsh, PowerShell 7 or Windows
PowerShell 5.1. A line that works in one and breaks in another fails a stranger's first run, so
the lint allows exactly one command shape and rejects everything else.
Logic: lint_skill(path, text) parses the frontmatter and walks the body. Fenced lines must be uv
lines. Every uv line must fully match one grammar: a fixed script prefix, a subcommand, then flags,
bare words, quoted {PLACEHOLDER} tokens or the quoted plugin data variable. Chaining, piping,
substitution, dollar variables, tildes, heredocs, shell scripts and bang injection are flagged on
fenced lines and on lines that start like a command. lint_tree walks skills/**/SKILL.md and fails
when none exist, so an empty tree is never a vacuous pass.
Guardrails: a skill with no uv line may leave out allowed-tools, and a present key holds exactly the
two narrow entries. Violations are (path, line, rule) tuples sorted by path then line. Negative controls
(tests/fixtures/skills_bad, one defect each) prove each rule fires; a positive control
(tests/fixtures/skills_good/ok.md) and the real skills prove it does not over-fire.
QA checklist: new skill? Run this file. New rule? Add a skills_bad fixture and a parametrized
case. The fixtures use no real paths, emails or phone numbers so the leak gate stays clean.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

Violation = tuple[str, int, str]

SCRIPT_PREFIX = 'uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/jobsearch.py"'
# The only pre-approved commands: this script, never any other `uv run` invocation.
NARROW_BASH = f"Bash({SCRIPT_PREFIX} *)"
NARROW_POWERSHELL = f"PowerShell({SCRIPT_PREFIX} *)"
_TOKEN = (
    r'(?:--[a-z]+(?:-[a-z]+)*'  # a long flag
    r'|[A-Za-z0-9._-]+'  # a bare word
    r'|"\{[A-Z][A-Z0-9_]*\}"'  # a quoted placeholder such as "{DATA_FOLDER}"
    r'|"\$\{CLAUDE_PLUGIN_DATA\}")'  # the quoted plugin data variable
)
UV_LINE = re.compile(re.escape(SCRIPT_PREFIX) + r" [a-z]+(?:-[a-z]+)*(?: " + _TOKEN + r")*")

_ALLOWED_DOLLAR = re.compile(r"\$(?!\{CLAUDE_PLUGIN_(?:ROOT|DATA)\})")
_FENCE = re.compile(r"^\s*(```|~~~)(.*)$")


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """Return (key -> raw value, index of the first body line). Empty dict when absent."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return meta, index + 1
        key, sep, value = lines[index].partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return {}, 0  # never closed: treat as no frontmatter


def uv_lines(text: str) -> list[tuple[int, str]]:
    """The (1-based line number, stripped text) of every body line that starts with "uv "."""
    lines = text.splitlines()
    _, body_start = _parse_frontmatter(lines)
    return [
        (number, line.strip())
        for number, line in enumerate(lines, start=1)
        if number > body_start and line.strip().startswith("uv ")
    ]


def _command_rules(line: str) -> set[str]:
    rules: set[str] = set()
    if "&&" in line or "||" in line or ";" in line:
        rules.add("chaining")
    if "|" in line.replace("||", ""):
        rules.add("pipe")
    if "`" in line or "$(" in line:
        rules.add("substitution")
    if _ALLOWED_DOLLAR.search(line):
        rules.add("dollar")
    if "~" in line:
        rules.add("tilde")
    if "<<" in line:
        rules.add("heredoc")
    if re.search(r"\b(?:export|cd)\b", line):
        rules.add("export-cd")
    if ".sh" in line or "bin/" in line:
        rules.add("shell-script")
    if line.lstrip().startswith("!"):
        rules.add("bang")
    return rules


def lint_skill(path: str | Path, text: str) -> list[Violation]:
    """All violations in one skill file, sorted by line then rule."""
    name = str(path)
    found: list[Violation] = []
    lines = text.splitlines()
    meta, body_start = _parse_frontmatter(lines)

    if not meta:
        found.append((name, 1, "frontmatter-missing"))
    for key in ("name", "description"):
        if meta and not meta.get(key):
            found.append((name, 1, f"{key}-missing"))
    if "shell" in meta:
        found.append((name, 1, "shell-key"))
    allowed = meta.get("allowed-tools", "")
    if "allowed-tools" in meta or uv_lines(text):  # a skill with no command line may omit the key
        if NARROW_BASH not in allowed:
            found.append((name, 1, "allowed-tools-bash"))
        if NARROW_POWERSHELL not in allowed:
            found.append((name, 1, "allowed-tools-powershell"))
    if allowed.replace(NARROW_BASH, "").replace(NARROW_POWERSHELL, "").strip():
        found.append((name, 1, "allowed-tools-broad"))  # any entry beyond the two exact ones

    in_fence = False
    for number, raw in enumerate(lines, start=1):
        if number <= body_start:
            continue
        line = raw.strip()
        fence = _FENCE.match(raw)
        if fence:
            if not in_fence and fence.group(2).strip().startswith("!"):
                found.append((name, number, "bang"))  # a ```! fence runs its body as a command
            in_fence = not in_fence
            continue
        if "!`" in raw:
            found.append((name, number, "bang"))  # inline command injection
        if not line:
            continue
        is_uv = line.startswith("uv ")
        if in_fence or is_uv or line.startswith("!") or line.startswith("$ "):
            for rule in _command_rules(line):
                found.append((name, number, rule))
        if in_fence and not is_uv:
            found.append((name, number, "fence-not-uv"))
        if is_uv and not UV_LINE.fullmatch(line):
            found.append((name, number, "uv-grammar"))
    return sorted(set(found))


def lint_tree(skills_dir: Path) -> list[Violation]:
    """Lint every SKILL.md under skills_dir. An empty tree is itself a violation."""
    files = sorted(skills_dir.rglob("SKILL.md"))
    if not files:
        return [(str(skills_dir), 0, "no-skills")]
    found: list[Violation] = []
    prefixes: set[str] = set()
    for file in files:
        text = file.read_text(encoding="utf-8")
        found.extend(lint_skill(file, text))
        for _, line in uv_lines(text):
            prefixes.add(" ".join(line.split()[:4]))
    if len(prefixes) > 1:
        found.append((str(skills_dir), 0, "mixed-script-prefix"))
    return sorted(found)


BAD_FIXTURES = {
    "chained.md": "chaining",
    "env_var.md": "dollar",
    "tilde.md": "tilde",
    "heredoc.md": "heredoc",
    "shell_script.md": "shell-script",
    "shell_key.md": "shell-key",
    "bang_inject.md": "bang",
    "missing_powershell.md": "allowed-tools-powershell",
    "pipe.md": "pipe",
}


def test_the_real_skills_pass_the_lint() -> None:
    assert (SKILLS_DIR / "status" / "SKILL.md").is_file()
    assert lint_tree(SKILLS_DIR) == []


def test_the_positive_control_passes() -> None:
    fixture = FIXTURES / "skills_good" / "ok.md"
    assert lint_skill(fixture, fixture.read_text(encoding="utf-8")) == []


def test_every_bad_fixture_is_listed() -> None:
    on_disk = {p.name for p in (FIXTURES / "skills_bad").glob("*.md")}
    assert on_disk == set(BAD_FIXTURES)
    assert len(BAD_FIXTURES) == 9


@pytest.mark.parametrize("name", sorted(BAD_FIXTURES))
def test_each_bad_fixture_reports_its_defect(name: str) -> None:
    fixture = FIXTURES / "skills_bad" / name
    violations = lint_skill(fixture, fixture.read_text(encoding="utf-8"))
    assert violations, f"{name} should violate the lint"
    assert BAD_FIXTURES[name] in {rule for _, _, rule in violations}


def test_a_skill_with_no_command_line_may_leave_out_allowed_tools() -> None:
    """No uv line, nothing to pre-approve; a command line still needs both entries."""
    prose = "---\nname: x\ndescription: y\n---\n\nJust talk with the user.\n"
    assert lint_skill("x.md", prose) == []
    with_line = f"---\nname: x\ndescription: y\n---\n\n```text\n{SCRIPT_PREFIX} status\n```\n"
    assert "allowed-tools-bash" in {r for _, _, r in lint_skill("x.md", with_line)}


def test_a_chained_command_is_rejected_for_each_operator() -> None:
    for operator in ("&&", "||", ";", "|"):
        text = (
            f"---\nname: x\ndescription: y\nallowed-tools: {NARROW_BASH} {NARROW_POWERSHELL}\n"
            f"---\n\n```text\n{SCRIPT_PREFIX} status {operator} echo hi\n```\n"
        )
        rules = {rule for _, _, rule in lint_skill("x.md", text)}
        assert rules & {"chaining", "pipe"}, operator
        assert "uv-grammar" in rules, operator


def test_a_tree_with_no_skill_files_fails(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    assert lint_tree(tmp_path / "skills") == [(str(tmp_path / "skills"), 0, "no-skills")]


def test_a_missing_skills_directory_fails(tmp_path: Path) -> None:
    assert [rule for _, _, rule in lint_tree(tmp_path / "absent")] == ["no-skills"]


def test_violations_are_sorted_by_path_then_line(tmp_path: Path) -> None:
    bad = (FIXTURES / "skills_bad" / "chained.md").read_text(encoding="utf-8")
    for name in ("b", "a"):  # created out of order on purpose
        folder = tmp_path / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            bad + f"\n```text\n{SCRIPT_PREFIX} status && echo again\n```\n", encoding="utf-8"
        )
    violations = lint_tree(tmp_path / "skills")
    assert violations == sorted(violations, key=lambda v: (v[0], v[1], v[2]))
    paths = [path for path, _, _ in violations]
    assert paths == sorted(paths)
    assert Path(paths[0]).parent.name == "a" and Path(paths[-1]).parent.name == "b"
    lines_in_a = [line for path, line, _ in violations if Path(path).parent.name == "a"]
    assert lines_in_a == sorted(lines_in_a)
    assert len(set(lines_in_a)) >= 2


def test_adding_a_shell_key_to_a_copy_of_the_real_skill_is_caught(tmp_path: Path) -> None:
    real = (SKILLS_DIR / "status" / "SKILL.md").read_text(encoding="utf-8")
    assert lint_skill("real", real) == []
    broken = real.replace("allowed-tools:", "shell: bash\nallowed-tools:", 1)
    assert broken != real
    copy = tmp_path / "SKILL.md"
    copy.write_text(broken, encoding="utf-8")
    rules = {rule for _, _, rule in lint_skill(copy, copy.read_text(encoding="utf-8"))}
    assert "shell-key" in rules


def test_a_bang_fence_and_inline_injection_are_caught() -> None:
    head = f"---\nname: x\ndescription: y\nallowed-tools: {NARROW_BASH} {NARROW_POWERSHELL}\n---\n\n"
    assert "bang" in {r for _, _, r in lint_skill("x", head + "```!\nwhoami\n```\n")}
    assert "bang" in {r for _, _, r in lint_skill("x", head + "Today is !`date`.\n")}


def test_missing_frontmatter_and_empty_fields_are_caught() -> None:
    assert "frontmatter-missing" in {r for _, _, r in lint_skill("x", "# Title\n")}
    empty_name = f"---\nname:\ndescription: y\nallowed-tools: {NARROW_BASH} {NARROW_POWERSHELL}\n---\n"
    assert "name-missing" in {r for _, _, r in lint_skill("x", empty_name)}


def test_prose_may_mention_forbidden_characters() -> None:
    text = (
        f"---\nname: x\ndescription: y\nallowed-tools: {NARROW_BASH} {NARROW_POWERSHELL}\n---\n\n"
        "Refuse a path with a dollar sign, a semicolon, a pipe or a backtick (`like this`).\n"
    )
    assert lint_skill("x", text) == []


def test_the_real_skill_pre_approves_only_the_exact_script() -> None:
    # "uv run *" would also pre-approve `uv run python -c ...` and `uv run --with ...`.
    meta, _ = _parse_frontmatter(
        (SKILLS_DIR / "status" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    )
    assert meta["allowed-tools"] == f"{NARROW_BASH} {NARROW_POWERSHELL}"


@pytest.mark.parametrize("broad", ["Bash(uv run *)", "PowerShell(uv run *)", "Bash(uv *)"])
def test_a_broad_allowed_tools_pattern_is_rejected(broad: str) -> None:
    text = (
        f"---\nname: x\ndescription: y\nallowed-tools: {NARROW_BASH} {NARROW_POWERSHELL} {broad}\n"
        "---\n"
    )
    assert "allowed-tools-broad" in {r for _, _, r in lint_skill("x", text)}
