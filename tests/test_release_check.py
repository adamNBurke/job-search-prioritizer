"""tools/release_check.py: the release gate for the public surface, proven on fixture trees.

Purpose: README.md is the first thing a stranger reads, and some of it (the demo link, the
screenshots, the usage note and the author link) arrives
late. These tests prove the gate refuses a README that is out of shape or still holds a pending
marker, and that a run only reads.
Logic: each test builds a small tree in tmp_path, runs a check or the CLI (main), and compares
the printed lines and exit code. The valid README is built by valid_readme() so each negative
case changes exactly one thing.
Guardrails: offline; fixture text only; no literal
em dash sits in this file (it is built from its code point).
QA checklist: a new check gets one passing tree, one failing tree that names the fault, and a
line in the CLI order test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import EM_DASH

import release_check

# Built from parts so no LinkedIn host sits in this file; only README.md may carry one.
PROFILE_HOST = "www.linked" + "in.com"
INSTALL_ONE = "/plugin marketplace add adamNBurke/job-search-prioritizer"
INSTALL_TWO = "/plugin install job-search-prioritizer@job-search-prioritizer"

BODIES = {
    "What it does": "It ranks open roles.\n\n```text\nresume -> interview -> titles\n```\n",
    "Works best for": "Tech and mid-market roles.\n",
    "See a full run": "Read [the demo](docs/demo.md).\n",
    "What you need": "Claude Code and uv.\n",
    "Install": f"Run these two lines.\n\n```text\n{INSTALL_ONE}\n{INSTALL_TWO}\n```\n",
    "Turn on auto-update": "Open the plugin menu.\n",
    "What it will not do": "It never submits an application.\n",
    "Cover letters in your own words": "You answer, it cuts.\n",
    "Using it with career-ops": "Paste a link.\n",
    "Claude usage": "Measured on 2026-11-20, a full run used about a tenth of a session.\n",
    "Why prioritization": "Because the order matters.\n",
    "Maintenance": "One person, spare time.\n",
    "Help and feedback": "See [support](docs/demo.md).\n",
    "Credits": "Built with Claude Code.\n",
    "License": "MIT.\n",
    "Author": f"Find me on [LinkedIn](https://{PROFILE_HOST}/in/example-person).\n",
}


def valid_readme(**changes: str | None) -> str:
    """A README that passes every check; a section set to None is left out, a string replaces it."""
    parts = ["# job-search-prioritizer\n\n[![ci](https://example.com/badge.svg)](https://example.com)\n"]
    for name in release_check.README_SECTIONS:
        body = changes.get(name, BODIES[name])
        if body is not None:
            parts.append(f"## {name}\n\n{body}")
    return "\n".join(parts)


def make_tree(root: Path, readme: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(valid_readme() if readme is None else readme, encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "demo.md").write_text("demo\n", encoding="utf-8")
    write_release_files(root)
    return root


def write_release_files(root: Path, version: str = "1.0.0", changelog: str | None = None) -> None:
    """The manifest, marketplace, CHANGELOG and demo files the release checks read."""
    plugin = root / ".claude-plugin"
    plugin.mkdir(exist_ok=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "x", "version": version}), encoding="utf-8")
    (plugin / "marketplace.json").write_text(
        json.dumps({"name": "x", "plugins": [{"name": "x", "source": "./"}]}), encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\nChanges per release.\n\n## {version} - 2026-12-01\n\n### Added\n\n- A thing.\n"
        if changelog is None else changelog, encoding="utf-8")
    for name in ("README.md", "4-letter/letter.md"):
        path = root / "demo" / "casey-ops" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("demo\n", encoding="utf-8")


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def listing(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))]


def run(capsys, *argv: str) -> tuple[int, list[str]]:
    code = release_check.main(list(argv))
    return code, capsys.readouterr().out.splitlines()


# Task 1 --------------------------------------------------------------------------------------

def test_readme_sections_pass_in_order_and_fail_out_of_order(tmp_path):
    assert len(release_check.README_SECTIONS) == 16
    assert release_check.README_SECTIONS[-1] == "Author"
    assert release_check.README_SECTIONS[:2] == ("What it does", "Why prioritization")
    good = make_tree(tmp_path / "good")
    assert release_check.readme_sections(good) == []

    missing = make_tree(tmp_path / "missing", valid_readme(**{"Maintenance": None}))
    found = release_check.readme_sections(missing)
    assert found and found[0].level == "FAIL" and "Maintenance" in found[0].text

    swapped = valid_readme().replace("## Author", "## Elsewhere")
    swapped += "\n## Author\n\nAuthor text.\n"
    moved = make_tree(tmp_path / "moved", swapped)
    assert any("Elsewhere" in f.text for f in release_check.readme_sections(moved))

    author_first = valid_readme(Author=None)
    author_first = author_first.replace("## License", "## Author\n\nx\n\n## License")
    early = make_tree(tmp_path / "early", author_first)
    assert any("Author" in f.text for f in release_check.readme_sections(early))

    no_readme = tmp_path / "none"
    no_readme.mkdir()
    assert release_check.readme_sections(no_readme)[0].level == "FAIL"


def test_a_heading_inside_a_code_fence_is_not_a_section(tmp_path):
    body = "It ranks.\n\n```text\n## Not A Section\n```\n"
    tree = make_tree(tmp_path / "t", valid_readme(**{"What it does": body}))
    assert release_check.readme_sections(tree) == []


def test_pending_markers_fail(tmp_path):
    tree = make_tree(tmp_path / "t", valid_readme(**{"Claude usage": "<!-- pending: usage-note -->\n"}))
    found = release_check.readme_no_pending(tree)
    assert [f.level for f in found] == ["FAIL"]
    assert "usage-note" in found[0].text
    clean = make_tree(tmp_path / "clean")
    assert release_check.readme_no_pending(clean) == []


def test_every_pending_marker_is_named(tmp_path):
    readme = valid_readme(
        **{"Claude usage": "<!-- pending: usage-note -->\n",
           "Why prioritization": "<!-- pending: why-prioritization -->\n"})
    found = release_check.readme_no_pending(make_tree(tmp_path / "t", readme))
    text = " ".join(f.text for f in found)
    assert "usage-note" in text and "why-prioritization" in text


def test_the_cli_prints_one_line_per_check_and_exits_by_result(tmp_path, capsys):
    good = make_tree(tmp_path / "good")
    code, lines = run(capsys, "--root", str(good), "--check", "readme_sections",
                      "--check", "readme_no_pending")
    assert code == 0
    assert lines == ["PASS readme_sections", "PASS readme_no_pending"]

    bad = make_tree(tmp_path / "bad", valid_readme(**{"Claude usage": "<!-- pending: usage-note -->\n"}))
    code, lines = run(capsys, "--root", str(bad), "--check", "readme_sections",
                      "--check", "readme_no_pending")
    assert code == 1
    assert lines[0] == "PASS readme_sections"
    assert lines[1].startswith("FAIL readme_no_pending") and "usage-note" in lines[1]


def test_an_unknown_check_name_exits_2_with_a_message(tmp_path, capsys):
    tree = make_tree(tmp_path / "t")
    with pytest.raises(SystemExit) as raised:
        release_check.main(["--root", str(tree), "--check", "no_such_check"])
    assert raised.value.code == 2
    assert "no_such_check" in capsys.readouterr().err


def test_checks_run_in_a_fixed_order_whatever_the_flag_order(tmp_path, capsys):
    tree = make_tree(tmp_path / "t")
    _, forward = run(capsys, "--root", str(tree), "--check", "readme_sections",
                     "--check", "readme_no_pending")
    _, backward = run(capsys, "--root", str(tree), "--check", "readme_no_pending",
                      "--check", "readme_sections")
    assert forward == backward


def test_a_run_writes_nothing_and_repeats_exactly(tmp_path, capsys):
    bad = make_tree(tmp_path / "t", valid_readme(**{"Claude usage": "<!-- pending: usage-note -->\n"}))
    before_bytes, before_list = snapshot(bad), listing(bad)
    first = run(capsys, "--root", str(bad))
    second = run(capsys, "--root", str(bad))
    assert first == second
    assert first[0] == 1
    assert snapshot(bad) == before_bytes
    assert listing(bad) == before_list


# Task 2 --------------------------------------------------------------------------------------

def test_relative_links_must_resolve(tmp_path):
    tree = make_tree(tmp_path / "t")
    ok = valid_readme(**{"Help and feedback": (
        "See [a](docs/demo.md), [b](docs/demo.md#top), [c](https://example.com/x), "
        "[d](#install) and [e](mailto:someone@example.com).\n")})
    (tree / "README.md").write_text(ok, encoding="utf-8")
    assert release_check.readme_links(tree) == []

    broken = valid_readme(**{"Help and feedback": "See [a](docs/gone.md) and ![shot](docs/shot.png).\n"})
    (tree / "README.md").write_text(broken, encoding="utf-8")
    found = release_check.readme_links(tree)
    text = " ".join(f.text for f in found)
    assert all(f.level == "FAIL" for f in found)
    assert "docs/gone.md" in text and "docs/shot.png" in text


def test_a_link_cannot_climb_out_of_the_tree(tmp_path):
    tree = make_tree(tmp_path / "t")
    (tmp_path / "outside.md").write_text("x\n", encoding="utf-8")
    readme = valid_readme(**{"Help and feedback": "See [a](../outside.md).\n"})
    (tree / "README.md").write_text(readme, encoding="utf-8")
    assert release_check.readme_links(tree)


def test_a_link_inside_a_code_fence_is_not_checked(tmp_path):
    tree = make_tree(tmp_path / "t")
    body = "Example:\n\n```text\n[a](docs/gone.md)\n```\n"
    (tree / "README.md").write_text(valid_readme(**{"Help and feedback": body}), encoding="utf-8")
    assert release_check.readme_links(tree) == []


def test_em_dash_fails_and_stock_phrases_warn(tmp_path, capsys):
    clean = make_tree(tmp_path / "clean")
    assert release_check.readme_no_em_dash(clean) == []
    assert release_check.readme_tone(clean) == []

    dashed = make_tree(tmp_path / "dashed", valid_readme(**{"Maintenance": f"One person {EM_DASH} spare time.\n"}))
    found = release_check.readme_no_em_dash(dashed)
    assert [f.level for f in found] == ["FAIL"]
    assert "line" in found[0].text
    # The tone check leaves the em dash to readme_no_em_dash.
    assert release_check.readme_tone(dashed) == []

    stock = make_tree(tmp_path / "stock", valid_readme(**{"Maintenance": "We delve into your search.\n"}))
    warns = release_check.readme_tone(stock)
    assert warns and all(f.level == "WARN" for f in warns)
    assert "delve" in warns[0].text and "line" in warns[0].text
    code, lines = run(capsys, "--root", str(stock), "--check", "readme_tone")
    assert code == 0
    assert lines[0].startswith("WARN readme_tone")


def test_a_colon_preface_and_a_contrast_frame_warn(tmp_path):
    body = "The result: you rank faster.\n\nIt is not just a list but a plan.\n"
    tree = make_tree(tmp_path / "t", valid_readme(**{"Maintenance": body}))
    text = " ".join(f.text for f in release_check.readme_tone(tree))
    assert "colon" in text and "contrast" in text


def test_install_lines_are_exact(tmp_path):
    good = make_tree(tmp_path / "good")
    assert release_check.readme_install_commands(good) == []
    for name, broken in (
        ("one", valid_readme().replace(INSTALL_ONE, "/plugin marketplace add someone/else")),
        ("two", valid_readme().replace(INSTALL_TWO, INSTALL_TWO + " --force")),
        ("both", valid_readme().replace(INSTALL_ONE, "x").replace(INSTALL_TWO, "y")),
    ):
        tree = make_tree(tmp_path / name, broken)
        found = release_check.readme_install_commands(tree)
        assert found and all(f.level == "FAIL" for f in found), name


def test_usage_note_needs_a_date_and_measured(tmp_path):
    def usage(body: str) -> list:
        return release_check.readme_usage_dated(make_tree(tmp_path / str(abs(hash(body))),
                                                          valid_readme(**{"Claude usage": body})))
    assert usage("Measured on 2026-11-20, about a tenth of a session.\n") == []
    assert usage("Measured, about a tenth of a session.\n")
    assert usage("On 2026-11-20 it used about a tenth of a session.\n")
    pending = usage("<!-- pending: usage-note -->\n")
    assert pending and pending[0].level == "FAIL" and "pending" in pending[0].text


def test_author_link_is_a_linkedin_profile(tmp_path):
    good = make_tree(tmp_path / "good")
    assert release_check.readme_author_link(good) == []
    none = make_tree(tmp_path / "none", valid_readme(Author="Written by someone.\n"))
    assert release_check.readme_author_link(none)[0].level == "FAIL"
    other = make_tree(tmp_path / "other", valid_readme(Author="See https://example.com/in/person.\n"))
    assert release_check.readme_author_link(other)
    # A profile link outside the Author section does not count.
    elsewhere = valid_readme(Author="Written by someone.\n", License=f"MIT. https://{PROFILE_HOST}/in/example\n")
    assert release_check.readme_author_link(make_tree(tmp_path / "elsewhere", elsewhere))
    pending = make_tree(tmp_path / "pending", valid_readme(Author="<!-- pending: author-link -->\n"))
    found = release_check.readme_author_link(pending)
    assert found and "pending" in found[0].text


def test_a_duplicated_heading_is_a_fail_not_a_crash(tmp_path, capsys):
    root = make_tree(tmp_path / "dup", valid_readme() + "\n## What it does\n\nAgain.\n")
    found = release_check.readme_sections(root)
    assert 'duplicate section "What it does"' in [f.text for f in found]
    code, lines = run(capsys, "--root", str(root), "--check", "readme_sections")
    assert code == 1
    assert lines[0] == 'FAIL readme_sections: duplicate section "What it does"'


def test_author_link_accepts_every_profile_host_form_only_as_a_link(tmp_path):
    bare = PROFILE_HOST.removeprefix("www.")
    country = "uk." + bare

    def author(text: str, name: str) -> list:
        root = make_tree(tmp_path / name, valid_readme(Author=text))
        return release_check.readme_author_link(root)

    assert author(f"Find me on [LinkedIn](https://{bare}/in/example-person).\n", "bare") == []
    assert author(f"Find me on [LinkedIn](https://{country}/in/example-person/).\n", "country") == []
    assert author(f"Find me at https://{PROFILE_HOST}/in/example-person today.\n", "text")
    buried = f"[x](https://example.com/?u=https://{PROFILE_HOST}/in/x)\n"
    assert author(buried, "buried")


def test_a_readme_saved_with_crlf_passes_the_line_checks(tmp_path):
    root = make_tree(tmp_path / "crlf")
    (root / "README.md").write_bytes(valid_readme().replace("\n", "\r\n").encode("utf-8"))
    for check in (release_check.readme_sections, release_check.readme_no_em_dash,
                  release_check.readme_install_commands, release_check.readme_usage_dated):
        assert check(root) == [], check.__name__


def test_a_fully_valid_tree_passes_every_check_and_exits_0(tmp_path, capsys):
    tree = make_tree(tmp_path / "t")
    code, lines = run(capsys, "--root", str(tree))
    assert code == 0
    names = [line.split(" ", 2)[1].rstrip(":") for line in lines]
    assert names == list(release_check.CHECK_NAMES)
    assert lines == [f"PASS {n}" for n in names]


def test_the_retired_trace_option_is_refused(tmp_path):
    tree = make_tree(tmp_path / "t")
    retired = "--" + "dict" + "ation"  # split so the retired flag's name stays out of this file
    with pytest.raises(SystemExit) as raised:
        release_check.main(["--root", str(tree), retired, "x"])
    assert raised.value.code == 2


def test_the_default_run_order_is_fixed():
    assert release_check.CHECK_NAMES == (
        "readme_sections", "readme_no_pending", "readme_links", "readme_no_em_dash",
        "readme_install_commands", "readme_tone", "readme_usage_dated", "readme_author_link",
        "changelog_version", "marketplace_no_version", "allowlist_ships", "demo_present")


# Release files -------------------------------------------------------------------

def test_changelog_version_matches_the_manifest(tmp_path):
    good = make_tree(tmp_path / "good")
    assert release_check.changelog_version(good) == []

    stale = make_tree(tmp_path / "stale")
    write_release_files(stale, changelog="# Changelog\n\n## 0.9.0 - 2026-11-01\n\n- Old.\n")
    found = release_check.changelog_version(stale)
    assert [f.level for f in found] == ["FAIL"] and "1.0.0" in found[0].text

    headless = make_tree(tmp_path / "headless")
    write_release_files(headless, changelog="# Changelog\n\nNothing yet.\n")
    found = release_check.changelog_version(headless)
    assert found and found[0].level == "FAIL" and "1.0.0" in found[0].text

    undated = make_tree(tmp_path / "undated")
    write_release_files(undated, changelog="# Changelog\n\n## 1.0.0\n\n- A thing.\n")
    assert release_check.changelog_version(undated)

    fenced = make_tree(tmp_path / "fenced")
    write_release_files(fenced, changelog="# Changelog\n\n```text\n## 1.0.0 - 2026-12-01\n```\n")
    assert release_check.changelog_version(fenced)

    missing = make_tree(tmp_path / "missing")
    (missing / "CHANGELOG.md").unlink()
    assert release_check.changelog_version(missing)[0].level == "FAIL"

    no_manifest = make_tree(tmp_path / "nomanifest")
    (no_manifest / ".claude-plugin" / "plugin.json").unlink()
    assert release_check.changelog_version(no_manifest)[0].level == "FAIL"


def test_changelog_version_follows_a_manifest_bump(tmp_path):
    tree = make_tree(tmp_path / "t")
    write_release_files(tree, version="1.0.1", changelog="## 1.0.0 - 2026-12-01\n")
    found = release_check.changelog_version(tree)
    assert found and "1.0.1" in found[0].text


def test_marketplace_carries_no_version(tmp_path):
    good = make_tree(tmp_path / "good")
    assert release_check.marketplace_no_version(good) == []

    for name, doc in (
        ("top", {"name": "x", "version": "1.0.0", "plugins": []}),
        ("nested", {"name": "x", "plugins": [{"name": "x", "version": "1.0.0"}]}),
    ):
        tree = make_tree(tmp_path / name)
        (tree / ".claude-plugin" / "marketplace.json").write_text(json.dumps(doc), encoding="utf-8")
        found = release_check.marketplace_no_version(tree)
        assert found and found[0].level == "FAIL", name

    gone = make_tree(tmp_path / "gone")
    (gone / ".claude-plugin" / "marketplace.json").unlink()
    assert release_check.marketplace_no_version(gone)[0].level == "FAIL"


def test_allowlist_ships_the_release_files(tmp_path, monkeypatch):
    tree = make_tree(tmp_path / "t")
    assert release_check.allowlist_ships(tree) == []
    assert {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "demo", "docs", "skills",
            "src", "scripts", ".claude-plugin", ".github"} <= set(release_check.RELEASE_FILES)

    monkeypatch.setattr(release_check.export_public, "ALLOWLIST", ("README.md", "LICENSE"))
    found = release_check.allowlist_ships(tree)
    text = " ".join(f.text for f in found)
    assert all(f.level == "FAIL" for f in found)
    assert "CHANGELOG.md" in text and "demo" in text and "README.md" not in text


def test_demo_present(tmp_path):
    good = make_tree(tmp_path / "good")
    assert release_check.demo_present(good) == []
    for name in ("README.md", "4-letter/letter.md"):
        tree = make_tree(tmp_path / name.replace("/", "-"))
        (tree / "demo" / "casey-ops" / name).unlink()
        found = release_check.demo_present(tree)
        assert [f.level for f in found] == ["FAIL"] and name in found[0].text
