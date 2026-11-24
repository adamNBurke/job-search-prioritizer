"""Skill text stays at its lean size: each SKILL.md and the skill list must fit the README budgets.

Purpose: the launch pitch is a low token cost per run, so CI fails when a skill file, or the skill
list Claude carries in every session (each skill's name plus description), grows past its stated
budget. The README "Claude usage" table is the one source of the budgets, so there is no second copy
of the numbers. Raising a budget is a one-line README change that shows up in review.
Logic: readme_budgets() reads the table headed "Skill" in that README section (second column, about
tokens) and multiplies by four characters per token. skill_sizes() counts the characters of every
skills/*/SKILL.md plus the listing. Budgets were set at the next 100 characters above the size at
the time, divided by four, so a typo fix passes and a new sentence fails.
Guardrails: only the table whose header starts with "Skill" is read; any other pipe table in the
section is ignored. Every skill needs a row and every row needs a skill, so a new skill or a stale
row fails. The before and after columns are one-time estimates and the test does not read them.
QA checklist: new skill? Add a row to the README table. Skill text grew on purpose? Raise that
row's budget in the README and say why in the commit.
"""

from __future__ import annotations

from pathlib import Path

from helpers import md_section, table_rows
from test_skills_lint import SKILLS_DIR, _parse_frontmatter

LISTING = "Skill list, every session"  # the README row label for the always-on skill list
CHARS_PER_TOKEN = 4
README = Path(__file__).resolve().parent.parent / "README.md"


def budget_table(section: str) -> list[list[str]]:
    """Body rows of the one pipe table in a section whose header's first cell is "Skill"."""
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("|") and line.strip("|").split("|")[0].strip() == "Skill":
            block = []
            for row in lines[index:]:
                if not row.startswith("|"):
                    break
                block.append(row)
            return table_rows("\n".join(block))
    raise AssertionError('no table headed "Skill" in the section')


def readme_budgets() -> dict[str, int]:
    section = md_section(README.read_text(encoding="utf-8"), "Claude usage", doc="README.md")
    return {
        row[0].strip("`"): int(row[1].replace(",", "")) * CHARS_PER_TOKEN for row in budget_table(section)
    }


def skill_sizes() -> dict[str, int]:
    sizes: dict[str, int] = {}
    listing = 0
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")  # read_text turns CRLF into LF, so the count is exact on every OS
        sizes[path.parent.name] = len(text)
        meta, _ = _parse_frontmatter(text.splitlines())
        listing += len(f"{meta['name']}: {meta['description']}")
    sizes[LISTING] = listing
    return sizes


def test_skill_text_stays_within_the_readme_budgets():
    budgets, sizes = readme_budgets(), skill_sizes()
    assert sizes[LISTING] > 0
    assert set(budgets) == set(sizes), "every skill needs a README budget row and every row a skill"
    over = {name: (size, budgets[name]) for name, size in sizes.items() if size > budgets[name]}
    assert not over, f"over budget, as (characters, budget): {over}"


def test_budget_table_ignores_other_pipe_tables():
    section = (
        "<!-- pending: usage-note -->\n\n| Run | Tokens |\n|---|---|\n| first | 9 |\n\n"
        "| Skill | Text budget (about tokens) |\n|---|---:|\n| `start` | 1,500 |\n| `scan` | 20 |\n\n"
        "| Other | Figure |\n|---|---|\n| x | 1 |\n"
    )
    assert budget_table(section) == [["`start`", "1,500"], ["`scan`", "20"]]
