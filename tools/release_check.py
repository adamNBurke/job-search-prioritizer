"""Maintainer tool: the release gate for the public surface (README.md first).

Purpose
    The README is the first thing a stranger reads, and some of it arrives late: the demo link,
    the screenshots, the measured usage note and the author link. This gate names every rule
    the README must meet before a release and fails while any input is still pending, so a
    half-finished README cannot ship by accident. Four more checks cover the release files: the top
    CHANGELOG heading names the version in plugin.json, marketplace.json carries no version,
    the export allowlist ships the release files, and the casey-ops demo is present.

Logic
    Each check is a function that takes the tree root and returns a list of findings (a
    Finding is a level, FAIL or WARN, and a text). No findings means PASS. The CLI runs every
    check, or only the --check names given, always in the fixed order of CHECKS, and prints
    `PASS <name>`, or one `WARN <name>: <text>` or `FAIL <name>: <text>` line per finding.
    Exit 0 when nothing FAILed, 1 when any check FAILed, 2 for a bad argument.
    Sections are read from `## ` headings outside code fences, so an example inside a fence
    never counts as a section.

Guardrails
    Read-only: it opens files for reading and never writes, deletes or contacts the network, so
    an interrupted run leaves nothing behind and two runs on one tree print the same lines.
    Every file is opened with encoding utf-8. Besides README.md it reads only CHANGELOG.md, the
    two .claude-plugin JSON files, and the two demo files it checks exist.

QA checklist
    - A new check gets a passing fixture tree and a failing one that names the fault in
      tests/test_release_check.py, and a place in CHECKS.
    - Run `uv run --frozen pytest -q tests/test_release_check.py` and then
      `uv run --frozen python tools/release_check.py` on the real tree.

Run: uv run --frozen python tools/release_check.py [--root DIR] [--check NAME ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import export_public  # noqa: E402
from jobsearch import tone_gate  # noqa: E402

FAIL = "FAIL"
WARN = "WARN"

README_SECTIONS = (
    "What it does",
    "Why prioritization",
    "Works best for",
    "See a full run",
    "What you need",
    "Install",
    "Turn on auto-update",
    "What it will not do",
    "Cover letters in your own words",
    "Using it with career-ops",
    "Claude usage",
    "Maintenance",
    "Help and feedback",
    "Credits",
    "License",
    "Author",
)

# Top-level names the export must ship for a release; a gap fails allowlist_ships.
RELEASE_FILES = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "demo", "docs",
                 "skills", "src", "scripts", ".claude-plugin", ".github")
DEMO_FILES = ("README.md", "4-letter/letter.md")

_HEADING = re.compile(r"^## (.+?)\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_CHANGELOG_HEADING = re.compile(r"^## (\S+) - (20\d\d-\d\d-\d\d)$")
_PENDING = re.compile(r"<!--\s*pending:\s*(.*?)\s*-->", re.DOTALL)
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
EM_DASH = chr(0x2014)
INSTALL_COMMANDS = (
    "/plugin marketplace add adamNBurke/job-search-prioritizer",
    "/plugin install job-search-prioritizer@job-search-prioritizer",
)
# Built from parts so this file, which ships in the export, holds no LinkedIn host literal.
_PROFILE_LINK = re.compile(r"https://(?:[a-z]{2,3}\.)?linked" + r"in\.com/in/[A-Za-z0-9_%.-]+/?")
_TONE_KINDS = (tone_gate.STOCK_PHRASE, tone_gate.COLON_PREFACE, tone_gate.CONTRAST_FRAME)


@dataclass(frozen=True)
class Finding:
    level: str
    text: str


@dataclass(frozen=True)
class Section:
    name: str
    line: int
    body: str


def _read_readme(root: Path) -> str | None:
    path = root / "README.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _missing_readme() -> list[Finding]:
    return [Finding(FAIL, "README.md was not found under the root")]


def parse_sections(text: str) -> list[Section]:
    """The `## ` sections of a README, in file order; headings inside a code fence are skipped."""
    lines = text.split("\n")
    found = [(heading.group(1), number) for number, line in _lines_outside_fences(text)
             if (heading := _HEADING.match(line))]
    sections = []
    for index, (name, number) in enumerate(found):
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        sections.append(Section(name, number, "\n".join(lines[number:end])))
    return sections


def section_body(text: str, name: str) -> str | None:
    """The body of the named section up to the next heading, or None when it is absent."""
    for section in parse_sections(text):
        if section.name == name:
            return section.body
    return None


def readme_sections(root: Path) -> list[Finding]:
    """Every README section present, nothing else, in order, and Author last."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    names = [section.name for section in parse_sections(text)]
    findings = [Finding(FAIL, f'missing section "{name}"')
                for name in README_SECTIONS if name not in names]
    findings += [Finding(FAIL, f'unexpected section "{name}"')
                 for name in names if name not in README_SECTIONS]
    findings += [Finding(FAIL, f'duplicate section "{name}"')
                 for name in sorted({n for n in names if names.count(n) > 1})]
    expected = [name for name in README_SECTIONS if name in names]
    actual = [name for name in dict.fromkeys(names) if name in README_SECTIONS]
    if actual != expected:
        for position, (want, got) in enumerate(zip(expected, actual, strict=True)):
            if want != got:
                findings.append(Finding(
                    FAIL, f'section "{got}" is out of order at position {position + 1} '
                          f'(expected "{want}")'))
                break
    if "Author" in names and names[-1] != "Author":
        findings.append(Finding(FAIL, 'section "Author" must be the last section'))
    return findings


def readme_no_pending(root: Path) -> list[Finding]:
    """No `<!-- pending: ... -->` marker may remain; each one found is named."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    return [Finding(FAIL, f"pending input remains: {' '.join(match.group(1).split()) or '(unnamed)'}")
            for match in _PENDING.finditer(text)]


def _lines_outside_fences(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) for every README line that is not inside a code fence."""
    kept: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.split("\n"), start=1):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token[0]
            elif token[0] == fence:
                fence = ""
            continue
        if not fence:
            kept.append((number, line))
    return kept


def readme_links(root: Path) -> list[Finding]:
    """Every relative link and image in README.md resolves to a file inside the tree."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    base = root.resolve()
    findings: list[Finding] = []
    for number, line in _lines_outside_fences(text):
        for match in _LINK.finditer(line):
            target = match.group(1)
            if target.startswith("#") or _SCHEME.match(target):
                continue
            relative = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not relative:
                continue
            path = (base / relative).resolve()
            if not path.is_relative_to(base):
                findings.append(Finding(FAIL, f"line {number}: link {target} points outside the tree"))
            elif not path.exists():
                findings.append(Finding(FAIL, f"line {number}: link target {target} does not exist"))
    return findings


def readme_no_em_dash(root: Path) -> list[Finding]:
    """No em dash (U+2014) anywhere in README.md; every line holding one is named."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    return [Finding(FAIL, f"line {number} holds an em dash")
            for number, line in enumerate(text.split("\n"), start=1) if EM_DASH in line]


def readme_install_commands(root: Path) -> list[Finding]:
    """Each install command appears alone on a line, exactly as the plugin needs it."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    lines = {line.strip() for line in text.split("\n")}
    return [Finding(FAIL, f"install command not found as an exact line: {command}")
            for command in INSTALL_COMMANDS if command not in lines]


def readme_tone(root: Path) -> list[Finding]:
    """The README goes through the same tone gate as a letter; hits are advice, never a FAIL."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    return [Finding(WARN, f"line {hit.line}: {hit.label}")
            for hit in tone_gate.check(text) if hit.kind in _TONE_KINDS]


def _pending_in(body: str) -> str | None:
    match = _PENDING.search(body)
    return " ".join(match.group(1).split()) if match else None


def readme_usage_dated(root: Path) -> list[Finding]:
    """The Claude usage note carries an ISO date and the word measured, never an estimate."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    body = section_body(text, "Claude usage")
    if body is None:
        return [Finding(FAIL, 'section "Claude usage" is missing')]
    pending = _pending_in(body)
    if pending is not None:
        return [Finding(FAIL, f"usage note is pending ({pending})")]
    findings = []
    if not _DATE.search(body):
        findings.append(Finding(FAIL, "usage note has no date (YYYY-MM-DD)"))
    if "measured" not in body.lower():
        findings.append(Finding(FAIL, 'usage note does not say the figure was "measured"'))
    return findings


def readme_author_link(root: Path) -> list[Finding]:
    """The Author section holds a Markdown link to a LinkedIn profile."""
    text = _read_readme(root)
    if text is None:
        return _missing_readme()
    body = section_body(text, "Author")
    if body is None:
        return [Finding(FAIL, 'section "Author" is missing')]
    pending = _pending_in(body)
    if pending is not None:
        return [Finding(FAIL, f"author link is pending ({pending})")]
    if not any(_PROFILE_LINK.match(match.group(1)) for match in _LINK.finditer(body)):
        return [Finding(FAIL, "the Author section has no LinkedIn profile link")]
    return []


def _read_json(path: Path) -> object | None:
    """The parsed JSON file, or None when it is absent or not valid JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def changelog_version(root: Path) -> list[Finding]:
    """The first `## ` heading of CHANGELOG.md is `## <plugin.json version> - <YYYY-MM-DD>`."""
    manifest = _read_json(root / ".claude-plugin" / "plugin.json")
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str):
        return [Finding(FAIL, ".claude-plugin/plugin.json has no readable version")]
    path = root / "CHANGELOG.md"
    if not path.is_file():
        return [Finding(FAIL, "CHANGELOG.md was not found under the root")]
    first = next((line for _, line in _lines_outside_fences(path.read_text(encoding="utf-8"))
                  if line.startswith("## ")), None)
    want = f"## {version} - YYYY-MM-DD"
    if first is None:
        return [Finding(FAIL, f"CHANGELOG.md has no release heading; plugin.json says {version}, "
                              f"so it must open with {want}")]
    match = _CHANGELOG_HEADING.match(first.rstrip())
    if match is None or match.group(1) != version:
        return [Finding(FAIL, f'top CHANGELOG heading "{first.strip()}" does not match '
                              f"plugin.json version {version} (expected {want})")]
    return []


def _has_key(node: object, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_has_key(value, key) for value in node.values())
    return isinstance(node, list) and any(_has_key(item, key) for item in node)


def marketplace_no_version(root: Path) -> list[Finding]:
    """marketplace.json holds no `version` key anywhere; the version lives in plugin.json only."""
    marketplace = _read_json(root / ".claude-plugin" / "marketplace.json")
    if marketplace is None:
        return [Finding(FAIL, ".claude-plugin/marketplace.json is missing or not valid JSON")]
    if _has_key(marketplace, "version"):
        return [Finding(FAIL, 'marketplace.json carries a "version" key; keep the version in '
                              "plugin.json only")]
    return []


def allowlist_ships(root: Path) -> list[Finding]:
    """Every release file's top-level name is on export_public.ALLOWLIST (the tree is not read)."""
    return [Finding(FAIL, f"{name} is not on the export ALLOWLIST, so the release would not ship it")
            for name in RELEASE_FILES if name not in export_public.ALLOWLIST]


def demo_present(root: Path) -> list[Finding]:
    """The casey-ops demo's README and letter exist."""
    return [Finding(FAIL, f"demo/casey-ops/{name} is missing")
            for name in DEMO_FILES if not (root / "demo" / "casey-ops" / name).is_file()]


# The fixed run order.
CHECKS: tuple[tuple[str, Callable[[Path], list[Finding]]], ...] = (
    ("readme_sections", readme_sections),
    ("readme_no_pending", readme_no_pending),
    ("readme_links", readme_links),
    ("readme_no_em_dash", readme_no_em_dash),
    ("readme_install_commands", readme_install_commands),
    ("readme_tone", readme_tone),
    ("readme_usage_dated", readme_usage_dated),
    ("readme_author_link", readme_author_link),
    ("changelog_version", changelog_version),
    ("marketplace_no_version", marketplace_no_version),
    ("allowlist_ships", allowlist_ships),
    ("demo_present", demo_present),
)
CHECK_NAMES = tuple(name for name, _ in CHECKS)


def run_checks(root: Path, names: list[str] | None) -> list[tuple[str, list[Finding]]]:
    wanted = set(names or CHECK_NAMES)
    return [(name, check(root)) for name, check in CHECKS if name in wanted]


def render(results: list[tuple[str, list[Finding]]]) -> tuple[list[str], int]:
    """The printed lines and the exit code for a list of (check name, findings)."""
    lines: list[str] = []
    code = 0
    for name, findings in results:
        if not findings:
            lines.append(f"PASS {name}")
            continue
        for finding in findings:
            lines.append(f"{finding.level} {name}: {finding.text}")
            if finding.level == FAIL:
                code = 1
    return lines, code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_check.py",
        description="Check the public README and release files against the release rules. Reads only.",
    )
    parser.add_argument("--root", default=".", help="tree to check (default: current directory)")
    parser.add_argument("--check", action="append", choices=CHECK_NAMES, metavar="NAME",
                        help="run only this check; repeat for several (default: all). "
                             f"Names: {', '.join(CHECK_NAMES)}")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    lines, code = render(run_checks(Path(args.root), args.check))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
