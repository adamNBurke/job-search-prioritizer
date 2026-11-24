"""Drift tests: docs/data-files.md agrees with the vocabularies, dataclasses and schema in code.

Purpose: the parts of the tool meet only at the file shapes in
docs/data-files.md, so a shape that drifts from the code must fail a test, not a user.
Logic: the document is parsed as text. Headings are '## ' sections, vocabularies are bullet
lists under '### ' headings, and field lists are the first column of Markdown tables. Each
parsed list is compared with the constant or dataclass in code that owns it.
Guardrails: nothing here writes a file or opens a socket. The document is read as UTF-8.
QA checklist: a new file gets its '## ' heading added to HEADINGS; a new vocabulary,
evaluator or criterion id gets a case here in the same commit that adds it to the code.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from helpers import bullets, first_column, md_section, table_rows

from jobsearch import criteria, directory, paths, roles_store, scoring

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "data-files.md"
SCHEMA = ROOT / "src" / "jobsearch" / "schemas" / "criteria.schema.json"
FIXTURES = ROOT / "tests" / "fixtures" / "criteria"

HEADINGS = (
    "Purpose", "Logic", "Guardrails", "QA checklist", "answers.json",
    "Criterion ids and the importance table", "Evaluator params", "Hard filter types",
    "scan-scope.json", "Starter directory (src/jobsearch/data/boards.json)",
    "scans/<date>/postings.jsonl and scan-meta.json", "roles.jsonl", "pasted.jsonl",
    "targets.json", "boards.local.json", "Title discovery files", "calibration.json",
    "Ranked outputs", "marks.jsonl", "Letter files (applications/<role key>/)",
    "Writing samples (profile/samples/)", "settings.json", "notices.json",
)
CRITERION_IDS = ["title_fit", "skills", "level", "pay", "location", "freshness", "industry",
                 "company_size", "company_stage"]
FUNNEL_KEYS = ("scanned", "pasted", "duplicates_merged", "title_gate_dropped",
               "rejected_by_filters", "passed", "tiers")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(heading: str, level: str = "##") -> str:
    return md_section(_text(), heading, level, "docs/data-files.md")


def _ticks(text: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z0-9_.]+)`", text))


@pytest.mark.parametrize("heading", HEADINGS)
def test_every_required_heading_exists(heading):
    assert re.search(rf"^## {re.escape(heading)}$", _text(), re.M), heading


def test_the_document_has_no_em_dash():
    assert "—" not in _text()


@pytest.mark.parametrize(("heading", "expected"), [
    ("Industries", directory.INDUSTRIES),
    ("Sizes", directory.SIZES),
    ("Stages", directory.STAGES),
])
def test_directory_vocabularies_equal_the_documented_lists(heading, expected):
    assert tuple(bullets(_section(heading, "###"))) == expected


def test_scored_role_fields_are_all_named_in_ranked_outputs():
    ticks = _ticks(_section("Ranked outputs"))
    missing = [f.name for f in dataclasses.fields(scoring.ScoredRole) if f.name not in ticks]
    assert not missing, f"ScoredRole fields missing from Ranked outputs: {missing}"


def test_criterion_result_fields_are_all_named_in_evaluator_params():
    ticks = _ticks(_section("Evaluator params"))
    missing = [f.name for f in dataclasses.fields(scoring.CriterionResult)
               if f.name not in ticks]
    assert not missing, f"CriterionResult fields missing from Evaluator params: {missing}"


def test_rank_input_counts_and_funnel_keys_are_named_in_ranked_outputs():
    section = _section("Ranked outputs")
    ticks = _ticks(section)
    counts = ("fetched_total", "title_gate_dropped", "duplicates_merged", "pasted_total")
    rank_fields = {f.name for f in dataclasses.fields(roles_store.RankInput)}
    assert set(counts) <= rank_fields
    for name in counts:
        assert f"RankInput.{name}" in section or name in ticks, name
    for key in FUNNEL_KEYS:
        assert key in ticks, key


def test_the_two_funnel_identities_are_written_down():
    section = _section("Ranked outputs")
    assert ("`scanned + pasted = duplicates_merged + title_gate_dropped + "
            "rejected_by_filters + passed`") in section
    assert "`A + B + C = passed`" in section


def test_funnel_line_formats_are_written_down():
    section = _section("Ranked outputs")
    assert "412 scanned, 120 passed filters, 9 A, 22 B, 89 C" in section
    assert "412 scanned, 2 pasted, 120 passed filters, 9 A, 22 B, 89 C" in section
    assert "N roles closed since your last scan" in section


def test_evaluator_rows_equal_the_known_evaluators_plus_judged_scale():
    rows = first_column(_section("Evaluator params"))
    assert sorted(rows) == sorted(criteria.KNOWN_EVALUATORS | {"judged_scale"})
    assert len(rows) == len(set(rows))


def test_criterion_ids_equal_the_documented_list_and_use_known_evaluators():
    rows = table_rows(_section("Criterion ids and the importance table"))
    assert [r[0].strip("`") for r in rows] == CRITERION_IDS
    assert {r[1].strip("`") for r in rows} <= criteria.KNOWN_EVALUATORS
    assert "{1: 1, 2: 2, 3: 4, 4: 6, 5: 9}" in _section("Criterion ids and the importance table")


@pytest.mark.parametrize("name", ["minimal.json", "tracer.json"])
def test_fixture_criteria_use_documented_ids_and_params_keys(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    documented = {row[0].strip("`"): row for row in table_rows(_section("Evaluator params"))}
    for item in [*data["criteria"], *data["bonuses"]]:
        if "weight" in item:
            assert item["id"] in CRITERION_IDS, item["id"]
        row = documented[item["evaluator"]]
        allowed_params = _ticks(row[2])
        assert set(item.get("params", {})) <= allowed_params, (item["id"], row[2])
        if "field" in item:
            assert item["field"] in _ticks(row[1]), (item["id"], row[1])


def test_hard_filter_types_equal_the_schema_enum():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    enum = schema["properties"]["hard_filters"]["items"]["properties"]["type"]["enum"]
    assert sorted(first_column(_section("Hard filter types"))) == sorted(enum)


def test_roles_jsonl_fields_equal_the_role_record_fields():
    names = [f.name for f in dataclasses.fields(roles_store.RoleRecord)]
    assert first_column(_section("roles.jsonl")) == names


def test_every_documented_file_path_is_in_the_layout():
    text = _text()
    for name in ("answers", "scan_scope", "targets", "boards_local", "calibration",
                 "roles_file", "pasted", "marks", "function_worklist", "function_verdicts",
                 "writing_samples", "settings", "notices", "intake_summary"):
        assert f"`{paths.LAYOUT[name]}`" in text, name
    assert "titles/" in text


def test_the_layout_carries_the_phase_two_names():
    expected = {
        "answers": "profile/answers.json", "resume_md": "profile/resume.md",
        "profile_source": "profile/source", "scan_scope": "config/scan-scope.json",
        "targets": "config/targets.json", "boards_local": "config/boards.local.json",
        "calibration": "config/calibration.json", "roles_file": "roles/roles.jsonl",
        "pasted": "roles/pasted.jsonl", "titles": "titles", "marks": "feedback/marks.jsonl",
        "function_worklist": "roles/function-worklist.json",
        "function_verdicts": "roles/function-verdicts.json",
    }
    for name, path in expected.items():
        assert paths.LAYOUT[name] == path
    # the whole layout is pinned by test_layout_matches_documented_table in tests/test_contracts.py
    assert paths.LAYOUT["writing_samples"] == "profile/samples"


def test_the_function_review_files_are_documented_with_the_code_s_fields_and_limits():
    from jobsearch import verdicts

    worklist = _section("function-worklist.json")
    for field in ("role_id", "company", "title", "team", "family", "tier", "score", "description",
                  "scan_date", "criteria_version", "roles"):
        assert f"| `{field}` |" in worklist, field
    assert "at most 3000 characters" in worklist

    section = _section("function-verdicts.json")
    for field in verdicts._ALLOWED_KEYS:
        assert f"| `{field}` |" in section, field
    assert f"At most {verdicts.MAX_VERDICTS} entries" in section
    assert f"1 to {verdicts.MAX_REASON} characters" in section
    assert "function_review" in section and "Other function, read from the description" in section
    assert "function_review" in _section("Hard filter types")


def test_the_letter_files_and_samples_are_documented():
    letter = _section("Letter files (applications/<role key>/)")
    for name in ("role.json", "posting.txt", "answers.md", "draft.md", "fixes.json", "check.md",
                 "letter.md", "letter-v<N>.md"):
        assert f"`{name}`" in letter, name
    for fact in ("a-z, 0-9, period, underscore and hyphen", "8 hex", "## ", "8 or more words"):
        assert fact in letter, fact
    samples = _section("Writing samples (profile/samples/)")
    assert "`.md`" in samples and "`.txt`" in samples
    assert paths.LAYOUT["writing_samples"] == "profile/samples"
    assert "`profile/samples`" in _text()


def test_the_summary_files_are_documented():
    # The three code-rendered explanations, and the fourth ranked file.
    section = _section("Summary files")
    for name in ("profile/summary.md", "titles/summary-<date>.md", "ranked/funnel-<date>.md"):
        assert f"### {name}" in section, name
    for fact in ("`intake_summary`", "N of M sampled postings", "`output.funnel_line` verbatim",
                 "No boards were read in this scan.", "`write_ranked`"):
        assert fact in section, fact
    assert "ranked/funnel-<date>.md" in _section("Ranked outputs")
    assert "ranked/rejected-<date>.json" in _section("Ranked outputs")
    assert "Send to career-ops" in _text()
