"""The tone gate names machine-sounding patterns by line and kind.

Purpose: a letter in the user's own words should not read like a template, and the user should be
told which line gives it away. The gate advises and never blocks.
Logic: pure-function tests over tone_gate.check and render_hits. Every kind has at least one text
that must fire and one near miss that must not (the negative controls). The phrase data file is
checked for shape, so a careless edit cannot ship an unsorted or oversized list.
Guardrails: no files are written and nothing is read except the shipped data file.
QA checklist: a new kind needs a row in FIRES, a row in QUIET and an entry in tone_gate.KINDS; a
new stock phrase goes in src/jobsearch/data/tone_phrases.json in sorted order.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from jobsearch import tone_gate

PHRASES_PATH = Path(tone_gate.__file__).resolve().parent / "data" / "tone_phrases.json"

FIRES = [
    ("I led the team \u2014 and it grew.", "em_dash"),
    ("I led the team \u2015 and it grew.", "em_dash"),
    ("I led the team -- and it grew.", "em_dash"),
    ("I led the team \u2013 and it grew.", "em_dash"),
    ("I am passionate about logistics.", "stock_phrase"),
    ("The result: we cut costs by a third.", "colon_preface"),
    ("We shipped it. The result: we cut costs.", "colon_preface"),
    ("Skills: Excel and SQL.", "colon_preface"),
    ("Step 2: 5 minutes.", "colon_preface"),
    ("It is not just a job, but a calling.", "contrast_frame"),
    ("She was not only fast but also careful.", "contrast_frame"),
    ("It's not a job, it's a calling.", "contrast_frame"),
    ("It\u2019s not a job; it\u2019s a calling.", "contrast_frame"),
]
QUIET = [
    ("A follow-up call.", "em_dash"),
    ("From 2019-2021 I ran intake.", "em_dash"),
    ("From 2019\u20132021 I ran intake.", "em_dash"),
    ("A well--known fact.", "em_dash"),
    ("I am passionate.", "stock_phrase"),
    ("Its robustness was tested.", "stock_phrase"),
    ("Dear Northwind team:", "colon_preface"),
    ("Dear Sam: thanks for the call.", "colon_preface"),
    ("We met at 9:30 on Monday.", "colon_preface"),
    ("This sentence has five words: and then more text.", "colon_preface"),
    ("It was not a job I wanted.", "contrast_frame"),
    ("It is not a job.", "contrast_frame"),
    ("It's a job, and it's mine.", "contrast_frame"),
]


@pytest.mark.parametrize(("text", "kind"), FIRES)
def test_each_kind_fires(text, kind):
    assert [hit.kind for hit in tone_gate.check(text)] == [kind]


@pytest.mark.parametrize(("text", "kind"), QUIET)
def test_each_kind_has_a_negative_control_that_does_not_fire(text, kind):
    assert kind not in [hit.kind for hit in tone_gate.check(text)], text


def test_every_kind_has_a_positive_and_a_negative_case():
    assert {kind for _, kind in FIRES} == set(tone_gate.KINDS)
    assert {kind for _, kind in QUIET} == set(tone_gate.KINDS)


def test_the_label_names_the_kind_in_plain_words():
    labels = {hit.kind: hit.label for text, _ in FIRES for hit in tone_gate.check(text)}
    assert labels["em_dash"] == "em dash"
    assert labels["colon_preface"] == "colon-preface setup"
    assert labels["contrast_frame"] == "contrast framing"
    assert labels["stock_phrase"] == 'stock phrase "passionate about"'


def test_two_hits_on_one_line_are_two_rows_and_overlaps_keep_the_longest(monkeypatch):
    both = tone_gate.check("I am passionate about this \u2014 truly.")
    assert [hit.kind for hit in both] == ["stock_phrase", "em_dash"]
    monkeypatch.setattr(tone_gate, "_data", lambda: {
        "schema_version": 1, "colon_preface_exempt_starts": [],
        "phrases": ["excited to apply", "i am excited to apply"]})
    (hit,) = tone_gate.check("I am excited to apply")
    assert hit.kind == "stock_phrase" and hit.label == 'stock phrase "i am excited to apply"'
    assert hit.column == 1


def test_hits_are_ordered_and_byte_stable():
    text = ("Skills: Excel.\nI am passionate about it \u2014 truly, a team player.\n"
            "It is not just a job, but a calling.\n")
    first = tone_gate.check(text)
    assert first == tone_gate.check(text)
    assert [(h.line, h.column, h.kind) for h in first] == sorted(
        (h.line, h.column, h.kind) for h in first)
    assert [h.line for h in first] == [1, 2, 2, 2, 3]
    assert tone_gate.render_hits(first) == tone_gate.render_hits(tone_gate.check(text))


def test_curly_apostrophes_case_and_nfc_do_not_hide_a_hit():
    (hit,) = tone_gate.check("I\u2019M EXCITED TO APPLY")
    assert hit.kind == "stock_phrase" and "i'm excited to apply" in hit.label
    composed = tone_gate.check("caf\u00e9 \u2014 and tea")
    decomposed = tone_gate.check(unicodedata.normalize("NFD", "caf\u00e9 \u2014 and tea"))
    assert [h.column for h in composed] == [h.column for h in decomposed] == [6]


def test_columns_count_code_points_from_one_and_crlf_is_one_break():
    hits = tone_gate.check("ab\r\nxx \u2014 yy\rzz \u2014 ww\r\n")
    assert [(h.line, h.column) for h in hits] == [(2, 4), (3, 4)]


def test_an_empty_text_has_no_hits():
    assert tone_gate.check("") == [] and tone_gate.check("\n\n") == []


def test_render_hits_escapes_the_excerpt_and_names_line_and_label():
    (hit,) = tone_gate.check("Use *bold* \u2014 here")
    row = tone_gate.render_hits([hit])[0]
    assert row.startswith('- Line 1: em dash in "')
    assert "\\*bold\\*" in row
    long_line = "x" * 300 + " \u2014 end"
    (long_hit,) = tone_gate.check(long_line)
    assert len(tone_gate.render_hits([long_hit])[0]) < 140


def test_the_phrase_file_is_well_formed():
    data = json.loads(PHRASES_PATH.read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "phrases", "colon_preface_exempt_starts"}
    assert data["schema_version"] == 1
    phrases = data["phrases"]
    assert 30 <= len(phrases) <= 200
    assert phrases == sorted(set(phrases)), "phrases must be unique and sorted"
    assert all(p == p.lower() and p == p.strip() and 0 < len(p) <= 80 for p in phrases)
    assert all("\u2019" not in p for p in phrases), "use a straight apostrophe"
    starts = data["colon_preface_exempt_starts"]
    assert starts and len(starts) == len(set(starts)) and all(s == s.lower() for s in starts)
