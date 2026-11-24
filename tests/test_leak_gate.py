"""Proofs for the leak gate (tools/leak_check.py), on synthetic data only.

Purpose: show that the gate catches planted identifiers and LinkedIn fetch code under every
variant the decision names, never echoes what it found, refuses to run blind when the personal
denylist is required, and passes on its own source.
Logic: every test builds a throwaway tree and a throwaway denylist under tmp_path with
fictional terms. An autouse fixture in conftest.py points every home-directory variable at a temp folder and
drops JSP_LEAK_DENYLIST, so the maintainer's real denylist can never load here.
Guardrails: tools/ and tests/ ship in the public export and the gate scans them, so every
planted string below is composed from fragments at runtime. No literal in this file may match
a gate rule, which the self-scan test proves.
QA checklist: new rule? Add a planted case to PATTERN_CASES (it also feeds the no-echo test).
New denylist kind? Add a variant test and a sentinel render in leak_check.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import leak_check
import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
POLICY = TOOLS / "leak_policy.json"

AT = chr(64)
FILLERS = ["Filler One Alpha", "Filler Two Beta", "Filler Three Gamma", "Filler Four Delta"]
SIX = [
    "Zorblax Testington",
    "415 555 0142",
    "1987-03-14",
    "742 Evergreen Terrace",
    "Quillon Marsh",
    "Zoë Quill-Testa",
]


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    put(root, "README.md", "A clean public readme.\n")
    return root


@pytest.fixture
def deny_dir(tmp_path):
    path = tmp_path / "deny"
    path.mkdir()
    return path


def put(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def write_denylist(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def with_first(term: str) -> list[str]:
    return [term, *FILLERS]


def gate(capsys, root, denylist=None, *, require=False, message=None):
    code = leak_check.run(root, denylist, require, POLICY, message)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def scan(capsys, tree, deny_dir, text, lines=SIX, rel="a.txt"):
    """Write a denylist, plant text (plus a newline) at rel, run the gate; return (code, out)."""
    dl = write_denylist(deny_dir / "dl.txt", lines)
    put(tree, rel, text + "\n")
    code, out, _ = gate(capsys, tree, dl)
    return code, out


def hit(capsys, tree, deny_dir, text, label, lines=SIX, rel="a.txt"):
    """scan, then assert the gate failed and printed label; return the output for more checks."""
    code, out = scan(capsys, tree, deny_dir, text, lines, rel)
    assert code == 1 and label in out, out
    return out


def git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def fragments(*parts: str) -> str:
    return "".join(parts)


# ---------------------------------------------------------------------------------------
# Personal denylist
# ---------------------------------------------------------------------------------------


def test_clean_tree_with_synthetic_denylist_passes(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    code, out, _ = gate(capsys, tree, dl)
    assert code == 0
    assert "denylist: 6 terms (term 3, phone 1, date 1, address 1)" in out
    assert "leak gate: PASS" in out
    assert "scanned 1 files" in out


def test_denylist_prefixes_set_the_kind_explicitly(capsys, tree, deny_dir):
    dl = write_denylist(
        deny_dir / "dl.txt",
        ["# a comment", "", "term:Zorblax", "phone:415 555 0142", "DATE:1987-03-14",
         "address:742 Evergreen Terrace", "Filler One Alpha"],
    )
    code, out, _ = gate(capsys, tree, dl)
    assert code == 0
    assert "denylist: 5 terms (term 2, phone 1, date 1, address 1)" in out


def test_default_denylist_path_comes_from_environment(capsys, tree, deny_dir, monkeypatch):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    monkeypatch.setenv("JSP_LEAK_DENYLIST", str(dl))
    put(tree, "notes.md", "written by " + "Zorblax" + " " + "Testington" + "\n")
    code, out, _ = gate(capsys, tree)
    assert code == 1
    assert "denylist term #1 (term)" in out


@pytest.mark.parametrize(
    "rel, text",
    [
        ("docs/notes.md", "Prepared by " + "Zorblax" + " " + "Testington" + " last week.\n"),
        ("docs/" + "Zorblax" + " " + "Testington" + " notes.md", "nothing here\n"),
        ("src/mod.py", "x = 1  # " + "Zorblax" + " " + "Testington" + " wrote this\n"),
    ],
    ids=["content", "file-name", "code-comment"],
)
def test_planted_term_fails_and_is_never_echoed(capsys, tree, deny_dir, rel, text):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    put(tree, rel, text)
    code, out, err = gate(capsys, tree, dl)
    shown = (out + err).lower()
    assert code == 1
    assert "denylist term #1" in out
    assert "zorblax" not in shown
    assert "testington" not in shown


def test_term_is_not_echoed_on_a_passing_run_either(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    code, out, err = gate(capsys, tree, dl)
    shown = (out + err).lower()
    assert code == 0
    for part in ("zorblax", "testington", "415", "evergreen", "1987", "quillon"):
        assert part not in shown


def test_whole_word_adjacent_word_character_does_not_match(capsys, tree, deny_dir):
    code, _ = scan(capsys, tree, deny_dir, "The " + "Zorblax" + "ian visitor, and " + "Zorblax" + "2 too.",
                   with_first("Zorblax"))
    assert code == 0


def test_whole_word_adjacent_punctuation_matches(capsys, tree, deny_dir):
    hit(capsys, tree, deny_dir, "Hello, " + "Zorblax" + ", welcome.", "FAIL a.txt:1 denylist term #1 (term)",
        with_first("Zorblax"))


@pytest.mark.parametrize(
    "text",
    ["zoe quill testa", "ZOE QUILL" + "–" + "TESTA", "Zoë Quill-Testa", "zoe_quill_testa"],
    ids=["plain-lower", "upper-en-dash", "exact", "underscores"],
)
def test_diacritic_case_and_dash_insensitive(capsys, tree, deny_dir, text):
    hit(capsys, tree, deny_dir, f"signed {text}", "denylist term #6 (term)")


@pytest.mark.parametrize(
    "invisible",
    [0x200B, 0x200C, 0x200D, 0x2060, 0x00AD, 0xFEFF, 0x202E],
    ids=lambda cp: f"U+{cp:04X}",
)
def test_invisible_format_characters_inside_a_term_do_not_hide_it(capsys, tree, deny_dir, invisible):
    # Copy-paste can leave a zero-width or soft-hyphen character inside a word.
    mark = chr(invisible)
    hit(capsys, tree, deny_dir, f"signed Zor{mark}blax Test{mark}ington", "denylist term #1 (term)")


def test_invisible_character_between_a_phone_s_digits_does_not_hide_it(capsys, tree, deny_dir):
    hit(capsys, tree, deny_dir, "tel 415" + chr(0x200B) + " 555 0142", "denylist term #2 (phone)")


# An invisible character standing in for the space between words, and
# the invisible characters that are not category Cf, must not hide a multi-word term.
JANE = "Jane Doe"
INVISIBLE_PROBES = [
    ("zero-width-space-separator", "Jane" + chr(0x200B) + "Doe"),
    ("variation-selector", "Jane" + chr(0xFE0F) + " Doe"),
    ("combining-grapheme-joiner", "Jane" + chr(0x034F) + " Doe"),
    ("braille-blank", "Jane" + chr(0x2800) + "Doe"),
    ("hangul-filler", "Jane" + chr(0x3164) + "Doe"),
    ("halfwidth-hangul-filler", "Jane" + chr(0xFFA0) + "Doe"),
    ("word-joiner-separator", "Jane" + chr(0x2060) + "Doe"),
    ("soft-hyphen-separator", "Jane" + chr(0x00AD) + "Doe"),
    ("variation-selector-separator", "Jane" + chr(0xFE0F) + "Doe"),
]


@pytest.mark.parametrize(
    "text", [probe[1] for probe in INVISIBLE_PROBES], ids=[probe[0] for probe in INVISIBLE_PROBES]
)
def test_invisible_characters_between_words_do_not_hide_a_term(capsys, tree, deny_dir, text):
    hit(capsys, tree, deny_dir, f"signed {text}", "denylist term #1 (term)", with_first(JANE))


def test_invisible_characters_do_not_make_unrelated_words_match(capsys, tree, deny_dir):
    _, out = scan(capsys, tree, deny_dir, "Janet" + chr(0x200B) + "Doe and Jane" + chr(0xFE0F) + "Does",
                  with_first(JANE))
    assert "denylist term #1" not in out


def test_accent_stripping_still_works_for_decomposed_marks(capsys, tree, deny_dir):
    hit(capsys, tree, deny_dir, "signed Zoe" + chr(0x0308) + " Quill-Testa", "denylist term #6 (term)")


@pytest.mark.parametrize(
    "text",
    ["+1 (415) 555-0142", "4155550142", "415.555.0142", "1-415-555-0142", "tel 415 555 0142."],
)
def test_phone_matches_digits_only(capsys, tree, deny_dir, text):
    assert "phone-shape" not in hit(capsys, tree, deny_dir, text, "denylist term #2 (phone)")


@pytest.mark.parametrize(
    "text",
    [
        "Posted 2026-10-05 415-555-0142",
        "ids 12 34 56 415-555-0142",
        "ref 7 (415) 555-0142",
        "order 20261005 1 415 555 0142 shipped",
        "415-555-0142 2026-10-05",
    ],
    ids=["after-date", "after-ids", "after-digit-paren", "after-run-and-one", "before-date"],
)
def test_phone_next_to_other_digits_still_matches(capsys, tree, deny_dir, text):
    # The number must be found as a window of digit groups, not only as a whole run.
    hit(capsys, tree, deny_dir, text, "denylist term #2 (phone)")


@pytest.mark.parametrize(
    "text",
    ["Posted 2026-10-05 415-555-0143", "ids 12 34 56 415 555 014", "41555501425"],
)
def test_phone_windows_do_not_match_other_numbers(capsys, tree, deny_dir, text):
    _, out = scan(capsys, tree, deny_dir, text)
    assert "denylist term #2 (phone)" not in out


def test_phone_with_a_different_last_digit_does_not_match(capsys, tree, deny_dir):
    code, _ = scan(capsys, tree, deny_dir, "415 555 0143 and 14155550143")
    assert code == 0


@pytest.mark.parametrize(
    "text",
    [
        "March 14, 1987",
        "03/14/1987",
        "3/14/87",
        "14 Mar 1987",
        "1987-03-14",
        "Mar. 14th, 1987",
        "14th of March 1987",
        "1987/03/14",
        "03-14-1987",
        "14-03-1987",
        "14-3-1987",
        "due 14-03-1987",
    ],
)
def test_date_variants_match(capsys, tree, deny_dir, text):
    hit(capsys, tree, deny_dir, f"born {text}", "denylist term #3 (date)")


def test_year_less_spelled_date_is_reported_as_such(capsys, tree, deny_dir):
    out = hit(capsys, tree, deny_dir, "party on March 14.\nand 14th March too",
              "FAIL a.txt:1 denylist term #3 (date, year-less form)")
    assert "FAIL a.txt:2 denylist term #3 (date, year-less form)" in out


@pytest.mark.parametrize("text", ["March 15", "1987-03-15", "03/15/1987", "March 140"])
def test_other_dates_do_not_match(capsys, tree, deny_dir, text):
    code, _ = scan(capsys, tree, deny_dir, f"on {text}")
    assert code == 0


@pytest.mark.parametrize(
    "text",
    ["742 Evergreen Ter.", "742 evergreen terrace,", "see 742 EVERGREEN TERRACE today", "742 Evergreen Ter"],
)
def test_address_matches_across_suffix_abbreviations(capsys, tree, deny_dir, text):
    hit(capsys, tree, deny_dir, text, "denylist term #4 (address)")


@pytest.mark.parametrize("text", ["743 Evergreen Terrace", "742 Evergreen Street", "Evergreen Terrace"])
def test_other_addresses_do_not_match(capsys, tree, deny_dir, text):
    code, _ = scan(capsys, tree, deny_dir, text)
    assert code == 0


def test_address_directions_and_units_normalize(capsys, tree, deny_dir):
    hit(capsys, tree, deny_dir, "mail to 12 N. Maple Ave., Apt 4", "denylist term #1 (address)",
        with_first("12 North Maple Avenue Apartment 4"))


def test_findings_are_sorted_by_path_then_line(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    word = "Zorblax" + " " + "Testington"
    put(tree, "b.txt", f"x\nx\n{word}\n")
    put(tree, "a.txt", "x\n" + f"{word}\n" + "x\n" * 6 + f"{word}\n")
    code, out, _ = gate(capsys, tree, dl)
    assert code == 1
    order = [out.index(f"FAIL {key} ") for key in ("a.txt:2", "a.txt:9", "b.txt:3")]
    assert order == sorted(order)


# ---------------------------------------------------------------------------------------
# --require-denylist and its absence
# ---------------------------------------------------------------------------------------


def test_require_denylist_missing_file_exits_2(capsys, tree, deny_dir):
    code, out, err = gate(capsys, tree, deny_dir / "absent.txt", require=True)
    assert code == 2
    assert "leak gate: PASS" not in out + err


def test_require_denylist_comments_only_exits_2(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", ["# nothing here", "", "   # still nothing"])
    code, _, _ = gate(capsys, tree, dl, require=True)
    assert code == 2


def test_require_denylist_under_minimum_exits_2(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX[:4])
    code, _, _ = gate(capsys, tree, dl, require=True)
    assert code == 2


def test_require_denylist_inside_root_exits_2(capsys, tree):
    dl = write_denylist(tree / "dl.txt", SIX)
    code, out, err = gate(capsys, tree, dl, require=True)
    assert code == 2
    assert "zorblax" not in (out + err).lower()


def test_denylist_inside_root_exits_2_even_without_the_flag(capsys, tree):
    dl = write_denylist(tree / "dl.txt", SIX)
    code, _, _ = gate(capsys, tree, dl)
    assert code == 2


def test_require_denylist_tracked_by_git_exits_2(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    git(deny_dir, "init", "-q")
    git(deny_dir, "add", "dl.txt")
    code, _, _ = gate(capsys, tree, dl, require=True)
    assert code == 2


def test_untracked_denylist_in_a_git_repo_is_allowed(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    git(deny_dir, "init", "-q")
    code, _, _ = gate(capsys, tree, dl, require=True)
    assert code == 0


def test_no_denylist_and_no_flag_prints_skipped_and_a_qualified_pass(capsys, tree):
    code, out, _ = gate(capsys, tree)
    lines = [line for line in out.splitlines() if line.strip()]
    assert code == 0
    assert any(line.startswith("personal denylist check SKIPPED") for line in lines)
    assert lines[-1] == "leak gate: PASS (pattern rules only; personal denylist not checked)"
    assert "leak gate: PASS" not in lines


@pytest.mark.parametrize("attribute, replacement", [
    ("_strip_invisible", lambda text, replacement="": text),
    ("normalize", lambda text: ""),
], ids=["strip-invisible", "normalize"])
def test_sentinel_self_test_exits_2_when_the_text_cleaning_is_broken(
    capsys, tree, deny_dir, monkeypatch, attribute, replacement
):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    monkeypatch.setattr(leak_check, attribute, replacement)
    code, out, err = gate(capsys, tree, dl)
    assert code == 2
    assert "sentinel self-test failed" in out + err
    assert "zorblax" not in (out + err).lower()


def test_empty_root_exits_2_nothing_to_scan(capsys, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, err = gate(capsys, empty)
    assert code == 2
    assert "nothing to scan" in out + err


def test_empty_root_with_a_message_still_exits_2_nothing_to_scan(capsys, tmp_path):
    # A commit message must not turn "scanned 0 files" into a PASS.
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, err = gate(capsys, empty, message="Sync from the development tree")
    assert code == 2
    assert "nothing to scan" in out + err
    assert "PASS" not in out


def test_missing_root_exits_2(capsys, tmp_path):
    code, _, _ = gate(capsys, tmp_path / "does-not-exist")
    assert code == 2


# ---------------------------------------------------------------------------------------
# Pattern rules, no denylist
# ---------------------------------------------------------------------------------------

CARD = " ".join(["4111", "1111", "1111", "1111"])
BARE_CARD = "4" + "1" * 15
HOST = "https://www." + "linked" + "in.com/jobs/view/1"
PATTERN_CASES = [
    ("email", "docs/a.md", "reach " + "jane.doe" + AT + "corp-mail.test now", "email-domain", "jane.doe"),
    ("phone", "docs/a.md", "call " + "-".join(["415", "556", "0100"]), "phone-shape", "556"),
    ("phone-paren", "docs/a.md", "call (" + "415) 556" + "-0100", "phone-shape", "556"),
    ("phone-compact-paren", "docs/a.md", "call (" + "415)556" + "-0100", "phone-shape", "556"),
    ("phone-bare", "docs/a.md", "call " + "415" + "556" + "0100" + " now", "phone-shape", "556"),
    ("phone-bare-country-one", "docs/a.md", "call " + "1" + "415" + "556" + "0100", "phone-shape", "556"),
    ("phone-intl", "docs/a.md", "call " + "+" + "44 20 7946 0958", "phone-shape", "7946"),
    ("ssn", "docs/a.md", "id " + "-".join(["123", "45", "6789"]), "ssn-shape", "6789"),
    ("card", "docs/a.md", "card " + CARD, "card-shape", "4111"),
    ("card-bare", "docs/a.md", "card " + BARE_CARD, "card-shape", "4111"),
    ("linkedin-domain", "src/x.py", "URL = '" + HOST + "'", "linkedin-domain", "view/1"),
    ("linkedin-short", "src/x.py", "URL = 'https://" + "lnkd" + ".in/abc'", "linkedin-domain", "/abc"),
    ("linkedin-cdn", "src/x.py", "URL = 'https://media." + "lic" + "dn.com/x.png'", "linkedin-domain", "x.png"),
    ("linkedin-pct", "src/x.py", "URL = '" + "linked" + "in%2ecom'", "linkedin-domain", "%2e"),
    ("linkedin-bracket", "src/x.py", "URL = '" + "linked" + "in[.]com'", "linkedin-domain", "[.]"),
    ("linkedin-fetch-1", "src/jobsearch/netguard.py", "A = '" + "voy" + "ager" + "'", "linkedin-fetch", "voy"),
    ("linkedin-fetch-2", "src/x.py", "A = '" + "jobs" + "-guest" + "'", "linkedin-fetch", "guest"),
    ("linkedin-fetch-3", "src/x.py", "A = '" + "li_" + "at" + "'", "linkedin-fetch", "li_"),
    ("linkedin-fetch-4", "src/x.py", "A = '" + "JSESSION" + "ID" + "'", "linkedin-fetch", "JSESSION"),
    ("automation-1", "src/x.py", "import " + "play" + "wright", "automation-import", "play"),
    ("automation-2", "src/x.py", "from " + "sele" + "nium.webdriver import Chrome", "automation-import", "sele"),
    ("automation-3", "src/x.py", "import " + "linkedin" + "_api", "automation-import", "_api"),
    ("home-path", "docs/a.md", "see /" + "Users" + "/zorblax/proj", "home-path", "zorblax"),
    ("home-path-linux", "docs/a.md", "see /" + "home" + "/zorblax/proj", "home-path", "zorblax"),
    ("home-path-windows", "docs/a.md", "see C:" + "\\" + "Users" + "\\" + "zorblax" + "\\" + "x", "home-path", "zorblax"),
    ("session-url", "docs/a.md", "see https://" + "claude" + ".ai/code/session_abc", "session-trailer", "session_abc"),
    ("trailer-1", "docs/a.md", "Claude-" + "Session: abc", "session-trailer", "abc"),
    ("trailer-2", "docs/a.md", "Co-Authored-" + "By: A Person <a" + AT + "users.noreply.github.com>", "session-trailer", "A Person"),
    ("secret-gh", "docs/a.md", "token " + "gh" + "p_" + "a" * 36, "secret-shape", "aaaaaaaa"),
    ("secret-aws", "docs/a.md", "key " + "AK" + "IA" + "ABCDEFGHIJKLMNOP", "secret-shape", "ABCDEFGH"),
    ("secret-pem", "docs/a.md", "-----BEGIN " + "RSA PRIVATE KEY" + "-----", "secret-shape", "RSA"),
]


@pytest.mark.parametrize(
    "rel, text, rule, payload",
    [case[1:] for case in PATTERN_CASES],
    ids=[case[0] for case in PATTERN_CASES],
)
def test_pattern_rule_fails_without_a_denylist_and_never_echoes(capsys, tree, rel, text, rule, payload):
    put(tree, rel, text + "\n")
    code, out, err = gate(capsys, tree)
    assert code == 1
    assert f"FAIL {rel}:1 {rule}" in out
    assert payload not in out + err
    assert "leak gate: PASS" not in out


# The pattern rules also run on a lightly normalized copy of each line,
# so copy-paste variants of a phone, an email or a LinkedIn domain are still caught.
NBSP, ZWSP, DOT_LEADER = chr(0xA0), chr(0x200B), chr(0x2024)


def _phone(sep: str, tail: str = "") -> str:
    return sep.join(["415", "556", "0100"]) + tail


VARIANT_CASES = [
    ("phone-en-dash", "call " + _phone(chr(0x2013)), "phone-shape"),
    ("phone-minus-sign", "call " + _phone(chr(0x2212)), "phone-shape"),
    ("phone-nonbreaking-hyphen", "call (415)" + chr(0x2011) + "556" + chr(0x2011) + "0100", "phone-shape"),
    ("phone-nbsp", "call " + _phone(NBSP), "phone-shape"),
    ("phone-ext-x", "call " + _phone("-", "x23"), "phone-shape"),
    ("phone-ext-bare", "call " + _phone("-", "ext"), "phone-shape"),
    ("phone-ext-word", "call " + _phone("-", " ext 23"), "phone-shape"),
    ("phone-ext-dot", "call " + _phone("-", " ext.23"), "phone-shape"),
    ("phone-ext-glued-dot", "call " + _phone("-", "ext.23"), "phone-shape"),
    ("phone-word-prefix", "call tel" + _phone("-"), "phone-shape"),
    ("phone-zero-width", "call 415" + ZWSP + "-556-0100", "phone-shape"),
    ("domain-zero-width", "URL = '" + "linked" + "in" + ZWSP + ".com/in/x'", "linkedin-domain"),
    ("domain-fullwidth", "URL = '" + chr(0xFF4C) + "inkedin" + ".com'", "linkedin-domain"),
    ("email-zero-width", "mail a" + ZWSP + AT + "gmail.com", "email-domain"),
    ("email-dot-leader", "mail joe" + AT + "corp" + DOT_LEADER + "mail.test", "email-domain"),
]


@pytest.mark.parametrize(
    "text, rule",
    [case[1:] for case in VARIANT_CASES],
    ids=[case[0] for case in VARIANT_CASES],
)
def test_pattern_rules_catch_copy_paste_variants(capsys, tree, text, rule):
    # src/, not docs/: the LinkedIn domain rule exempts docs.
    put(tree, "src/a.py", "# " + text + "\n")
    code, out, err = gate(capsys, tree)
    assert code == 1
    assert f"FAIL src/a.py:1 {rule}" in out
    assert "leak gate: PASS" not in out


@pytest.mark.parametrize(
    "rel, text",
    [
        ("docs/a.md", "mail " + "someone" + AT + "example.com"),
        ("docs/a.md", "mail " + "1234+someone" + AT + "users.noreply.github.com"),
        ("docs/a.md", "call " + "-".join(["415", "555", "0142"])),
        ("docs/a.md", "call " + "-".join(["212", "555", "0100"])),
        ("docs/a.md", "built at 1700000000000 ms"),
        ("docs/a.md", "sha " + "ab" + BARE_CARD + "cd" + " end"),
        ("docs/a.md", "tag " + "pkg" + AT + "1.2.3"),
        ("src/jobsearch/netguard.py", "URL = '" + HOST + "'"),
        ("tests/test_netguard.py", "URL = '" + HOST + "'"),
        ("docs/notes.md", "URL = '" + HOST + "'"),
        ("docs/a.md", "see /" + "Users" + "/you/proj and /" + "home" + "/runner/work"),
        ("docs/a.md", "see /" + "Users" + "/<name>/proj and C:" + "\\" + "Users" + "\\" + "username" + "\\x"),
        ("docs/a.md", "the " + "voy" + "agers and a li" + "_attribute"),
        ("src/x.py", "# import playwright is documented as forbidden"),
        ("docs/a.md", "# " + "Claude-" + "Session" + " is stripped"),
        ("docs/a.md", "Example: 123-45-678 is not an ssn"),
        ("docs/a.md", "call (" + "415)555" + "-0123"),
        ("docs/a.md", "call " + "4155550123"),
        ("docs/a.md", "epoch " + "1731456000" + " seconds"),
        ("docs/a.md", "number " + "41555601005"),
        ("docs/a.md", "number " + "415556010099"),
        ("docs/a.md", "id v" + "415" + "556" + "0100"),
        # the copy-paste variants that must stay quiet
        ("docs/a.md", "call " + "-".join(["415", "555", "0142"]) + "x23"),
        ("docs/a.md", "call " + NBSP.join(["415", "555", "0142"])),
        ("docs/a.md", "call " + chr(0x2013).join(["212", "555", "0100"])),
        ("docs/a.md", "dial ext 23 or ext.23 or x23"),
        ("docs/a.md", "extension" + ZWSP + " numbers"),
        ("docs/a.md", "number " + "415556" + "01005"),
        ("docs/a.md", "see " + "linked" + "in" + " in prose"),
        ("docs/a.md", "mail someone" + ZWSP + AT + "example.com"),
    ],
    ids=[
        "example-email", "noreply-email", "phone-fiction", "phone-fiction-212", "epoch-ms",
        "hex-run", "package-at-version", "netguard-domain", "test-netguard-domain", "docs-domain",
        "home-placeholders", "home-angle-placeholders", "fetch-word-boundary",
        "import-in-prose", "trailer-not-line-start", "short-ssn",
        "phone-compact-fiction", "phone-bare-fiction", "epoch-seconds", "long-digit-run",
        "twelve-digit-run", "digit-run-after-letters",
        "fiction-ext", "fiction-nbsp", "fiction-en-dash", "ext-words-alone", "zero-width-prose",
        "longer-digit-run", "company-name-only", "allowed-domain-zero-width",
    ],
)
def test_pattern_rule_allows_the_safe_shape(capsys, tree, rel, text):
    put(tree, rel, text + "\n")
    code, out, _ = gate(capsys, tree)
    assert code == 0, out


def test_lockfile_style_hash_content_does_not_trip_card_or_phone_rules(capsys, tree):
    shutil.copy(REPO / "uv.lock", tree / "uv.lock")
    shutil.copy(REPO / "scripts" / "jobsearch.py.lock", tree / "jobsearch.py.lock")
    code, out, _ = gate(capsys, tree)
    assert code == 0, out


def test_linkedin_fetch_token_has_no_path_exemption(capsys, tree):
    put(tree, "src/jobsearch/netguard.py", "A = '" + "voy" + "ager" + "'\n")
    put(tree, "docs/a.md", "A = '" + "li_" + "at" + "'\n")
    code, out, _ = gate(capsys, tree)
    assert code == 1
    assert "FAIL src/jobsearch/netguard.py:1 linkedin-fetch" in out
    assert "FAIL docs/a.md:1 linkedin-fetch" in out


@pytest.mark.parametrize(
    "rel",
    [
        ".planning/x.md", ".claude/CLAUDE.md", ".env", "key.pem", "CLAUDE.md", "nested/deep/id.pem",
        ".env.local", ".env.production", "nested/.ENV.staging", "server.key", "nested/tls.KEY",
        "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub", "CLAUDE.local.md",
        "nested/notes.local.md", ".DS_Store", "nested/.DS_Store", "cert.p12", "nested/cert.PFX",
        ".mcp.json", "data.sqlite",
    ],
)
def test_forbidden_paths_fail(capsys, tree, rel):
    put(tree, rel, "harmless\n")
    code, out, _ = gate(capsys, tree)
    assert code == 1
    assert "forbidden-path" in out


def test_nested_claude_md_is_not_a_root_claude_md(capsys, tree):
    put(tree, "docs/CLAUDE.md", "harmless\n")
    code, _, _ = gate(capsys, tree)
    assert code == 0


def test_binary_file_fails(capsys, tree):
    put(tree, "blob.bin", b"abc\x00def")
    put(tree, "latin.txt", b"caf\xe9 not utf-8")
    code, out, _ = gate(capsys, tree)
    assert code == 1
    assert "FAIL blob.bin:0 binary-file" in out
    assert "FAIL latin.txt:0 binary-file" in out


def test_git_directories_are_skipped(capsys, tree):
    put(tree, ".git/config", "mail " + "jane.doe" + AT + "corp-mail.test\n")
    code, _, _ = gate(capsys, tree)
    assert code == 0


# ---------------------------------------------------------------------------------------
# Message mode and module facts
# ---------------------------------------------------------------------------------------


def test_commit_message_with_a_term_fails_and_is_not_echoed(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    code, out, err = gate(capsys, tree, dl, message="Sync for " + "Zorblax" + " " + "Testington" + "\n")
    assert code == 1
    assert "denylist term #1" in out
    assert "zorblax" not in (out + err).lower()


def test_commit_message_with_a_trailer_fails(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    code, out, _ = gate(capsys, tree, dl, message="Sync\n\nClaude-" + "Session: abc\n")
    assert code == 1
    assert "session-trailer" in out


def test_clean_commit_message_passes(capsys, tree, deny_dir):
    dl = write_denylist(deny_dir / "dl.txt", SIX)
    code, _, _ = gate(capsys, tree, dl, message="Sync from the development tree")
    assert code == 0


def test_check_message_applies_only_denylist_rules_unless_trailers_requested():
    terms = leak_check.parse_denylist(SIX)
    text = "Sync\n\nClaude-" + "Session: abc\n"
    assert leak_check.check_message(text, terms, trailers=False) == []
    assert [f.rule for f in leak_check.check_message(text, terms, trailers=True)] == ["session-trailer"]


def test_policy_file_is_generic_json():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert policy["min_denylist_terms"] == 5
    assert "users.noreply.github.com" in policy["allowed_email_domains"]
    assert policy["phone_fiction_exchange"] == "555"
    assert policy["phone_fiction_line_prefix"] == "01"
    raw = POLICY.read_text(encoding="utf-8").lower()
    assert "linked" + "in.com" not in raw


def test_readme_may_carry_a_linkedin_link_and_skills_may_not():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    line = "https://www.linked" + "in.com/in/example"
    assert "linkedin-domain" not in leak_check.pattern_rules_for_line("README.md", line, policy)
    assert "linkedin-domain" in leak_check.pattern_rules_for_line("skills/start/SKILL.md", line, policy)
    assert "linkedin-domain" in leak_check.pattern_rules_for_line("notes/README.md", line, policy)


def test_a_rule_id_outside_the_old_list_is_still_reported(monkeypatch):
    monkeypatch.setattr(leak_check, "_pattern_rules", lambda rel, line, policy: {"new-rule"})
    assert leak_check.pattern_rules_for_line("a.md", "plain text", {}) == ["new-rule"]


def test_leak_check_is_stdlib_only_with_a_pep723_header():
    source = (TOOLS / "leak_check.py").read_text(encoding="utf-8")
    assert "# /// script" in source
    assert 'requires-python = ">=3.11"' in source
    assert "dependencies = []" in source
    tree = ast.parse(source)
    allowed_siblings = {"export_public"}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            assert top in sys.stdlib_module_names or top in allowed_siblings, name


def test_command_line_runs_from_a_script_path(tree):
    done = subprocess.run(
        [sys.executable, str(TOOLS / "leak_check.py"), "--root", str(tree)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().splitlines()[-1].startswith("leak gate: PASS (pattern rules only")


def test_tool_cache_folders_fail_the_library_scan_but_not_a_command_line_scan(capsys, tree):
    put(tree, "tools/__pycache__/x.cpython-313.pyc", b"\x00\x01bytecode")
    code, out, _ = gate(capsys, tree)
    assert code == 1
    assert "binary-file" in out
    assert leak_check.main(["--root", str(tree)]) == 0
    assert "PASS (pattern rules only" in capsys.readouterr().out


def test_command_line_require_denylist_without_one_exits_2(tree):
    done = subprocess.run(
        [sys.executable, str(TOOLS / "leak_check.py"), "--root", str(tree), "--require-denylist"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 2


def test_self_scan_of_the_shipping_tree_is_clean(capsys, tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".DS_Store")
    for name in ("src", "scripts", "skills", "tools", "tests", "docs", ".claude-plugin"):
        source = REPO / name
        if source.is_dir():
            shutil.copytree(source, copy / name, ignore=skip)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(REPO / name, copy / name)
    code, out, _ = gate(capsys, copy)
    assert code == 0, out
    assert out.strip().splitlines()[-1].startswith("leak gate: PASS (pattern rules only")
