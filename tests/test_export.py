"""Proofs for the public-tree export (tools/export_public.py).

Purpose: show that the public tree can only be produced from allowlisted index content, can
only be committed after the leak gate passes with the personal denylist, always carries the
GitHub noreply identity, and never changes the development repo.
Logic: every test builds a throwaway development repo and throwaway denylist under tmp_path
with fictional terms. An autouse fixture in conftest.py points every home-directory variable at a temp folder,
drops JSP_LEAK_DENYLIST and clears inherited GIT_* variables, so neither the maintainer's
denylist nor an ambient git environment can reach a test.
Guardrails: this file ships in the public export and is scanned by the gate, so planted
strings are composed from fragments at runtime. Git identity is passed only through -c.
QA checklist: new export mode? Add a before/after check that the dev repo is unchanged.
New allowlist entry? Extend test_allowlist_excludes_private_folders if the intent changes.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import export_public
import leak_check
import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
AT = chr(64)
EMAIL = "1234+example" + AT + "users.noreply.github.com"
TERM = "Zorblax" + " " + "Testington"
DENYLIST = [
    "Zorblax Testington",
    "415 555 0142",
    "1987-03-14",
    "742 Evergreen Terrace",
    "Quillon Marsh",
    "Filler One Alpha",
]


@pytest.fixture
def home(_isolate_environment):
    return _isolate_environment


def git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and done.returncode != 0:
        raise AssertionError(done.stderr)
    return done.stdout


def commit_dev(dev: Path, message: str = "dev work") -> None:
    git(
        dev, "-c", "user.name=Dev Person", "-c", "user.email=dev" + AT + "example.com",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", message,
    )


def put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def dev(tmp_path):
    repo = tmp_path / "dev"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    put(repo, ".planning/PROJECT.md", "private plans\n")
    put(repo, ".claude/CLAUDE.md", "private rules\n")
    put(repo, "notes/secret.md", "an unlisted folder\n")
    put(repo, "src/a.py", "VALUE = 1\n")
    put(repo, "README.md", "A public readme.\n")
    git(repo, "add", "--", ".planning/PROJECT.md", ".claude/CLAUDE.md", "notes/secret.md", "src/a.py", "README.md")
    commit_dev(repo, "init")
    return repo


@pytest.fixture
def denylist(tmp_path, monkeypatch):
    path = tmp_path / "deny" / "dl.txt"
    path.parent.mkdir()
    path.write_text("\n".join(DENYLIST) + "\n", encoding="utf-8")
    monkeypatch.setenv("JSP_LEAK_DENYLIST", str(path))
    return path


def snapshot(repo: Path) -> tuple[str, str]:
    return git(repo, "status", "--porcelain"), git(repo, "config", "--list", "--local")


def tracked(repo: Path) -> list[str]:
    return sorted(line for line in git(repo, "ls-files").splitlines() if line)


# ---------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------


def test_export_unlisted_folder_planning_and_claude_are_absent(dev, tmp_path):
    out = tmp_path / "out"
    files = export_public.export_tree(dev, out)
    assert files == ["README.md", "src/a.py"]
    for private in (".planning", ".claude", "notes"):
        assert not (out / private).exists()
    assert (out / "src" / "a.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_allowlist_excludes_private_folders():
    assert ".planning" not in export_public.ALLOWLIST
    assert ".claude" not in export_public.ALLOWLIST
    assert "scripts" in export_public.ALLOWLIST
    assert "demo" in export_public.ALLOWLIST
    assert "CHANGELOG.md" in export_public.ALLOWLIST
    source = (TOOLS / "export_public.py").read_text(encoding="utf-8")
    for needle in ("write-tree", "ls-tree", "--format=tar"):
        assert needle in source


def test_export_staged_but_uncommitted_file_is_present(dev, tmp_path):
    put(dev, "docs/new.md", "staged only\n")
    git(dev, "add", "--", "docs/new.md")
    files = export_public.export_tree(dev, tmp_path / "out")
    assert "docs/new.md" in files


def test_export_unstaged_edit_is_absent(dev, tmp_path):
    put(dev, "src/a.py", "VALUE = 2  # edited, not staged\n")
    out = tmp_path / "out"
    export_public.export_tree(dev, out)
    assert (out / "src" / "a.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_export_untracked_file_is_absent(dev, tmp_path):
    put(dev, "docs/untracked.md", "never added\n")
    assert "docs/untracked.md" not in export_public.export_tree(dev, tmp_path / "out")


def test_export_with_no_allowlisted_content_is_empty_not_an_error(tmp_path):
    repo = tmp_path / "bare-dev"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    put(repo, ".planning/x.md", "private\n")
    git(repo, "add", "--", ".planning/x.md")
    assert export_public.export_tree(repo, tmp_path / "out") == []


def test_export_from_a_subdirectory_still_uses_the_whole_tree(dev, tmp_path):
    files = export_public.export_tree(dev / "src", tmp_path / "out")
    assert files == ["README.md", "src/a.py"]


def test_export_out_inside_repo_exits_2(dev, capsys):
    code = export_public.main(["--repo", str(dev), "--out", str(dev / "docs" / "export")])
    assert code == 2
    assert "ERROR" in capsys.readouterr().err


def test_export_non_empty_out_exits_2(dev, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    assert export_public.main(["--repo", str(dev), "--out", str(out)]) == 2
    assert (out / "keep.txt").exists()


def test_export_out_prints_the_file_count(dev, tmp_path, capsys):
    assert export_public.main(["--repo", str(dev), "--out", str(tmp_path / "out")]) == 0
    assert "exported 2 files" in capsys.readouterr().out


def test_export_refuses_a_python_without_the_tar_data_filter(dev, tmp_path, monkeypatch):
    monkeypatch.delattr(tarfile, "data_filter")
    with pytest.raises(export_public.ExportError, match="3.11.4"):
        export_public.export_tree(dev, tmp_path / "out")
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------------------
# --commit
# ---------------------------------------------------------------------------------------


def test_commit_identity_single_commit_and_no_trailers(dev, denylist, tmp_path):
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL])
    assert code == 0
    assert git(out, "rev-list", "--count", "HEAD").strip() == "1"
    identity = git(out, "log", "-1", "--format=%an|%ae|%cn|%ce").strip()
    assert identity == f"example|{EMAIL}|example|{EMAIL}"
    body = git(out, "log", "-1", "--format=%B").rstrip("\n")
    assert body == export_public.DEFAULT_MESSAGE
    assert not [line for line in body.splitlines() if re.match(r"^\s*[A-Za-z][A-Za-z-]*\s*:", line)]
    assert tracked(out) == ["README.md", "src/a.py"]


def test_commit_identity_is_not_overridden_by_global_git_config_or_hooks(dev, denylist, tmp_path, home):
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    leaky = "leaky" + AT + "corp-mail.test"
    config = f"[user]\n\tname = Leaky Person\n\temail = {leaky}\n[core]\n\thooksPath = {hooks.as_posix()}\n"
    (home / ".gitconfig").write_text(config, encoding="utf-8")
    if sys.platform != "win32":
        for hook in ("pre-commit", "commit-msg", "post-commit"):
            script = hooks / hook
            script.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
            script.chmod(0o755)
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL])
    assert code == 0
    shown = git(out, "log", "-1", "--format=%an|%ae|%cn|%ce")
    assert leaky not in shown and "Leaky" not in shown
    assert not marker.exists()


def test_global_git_config_cannot_shape_the_fresh_repository(dev, denylist, tmp_path, home):
    template = tmp_path / "template"
    template.mkdir()
    (template / "marker").write_text("from a global init template\n", encoding="utf-8")
    (home / ".gitconfig").write_text(
        f"[init]\n\ttemplateDir = {template.as_posix()}\n[core]\n\tautocrlf = true\n", encoding="utf-8"
    )
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL])
    assert code == 0
    assert not (out / ".git" / "marker").exists()


# A fixed zone five hours behind UTC with no daylight saving, in POSIX TZ syntax so it behaves
# the same on every OS. Git would write -0500 into each commit if the tool did not pin the date.
LOCAL_ZONE = "EST5"


def raw_dates(repo: Path) -> list[str]:
    shown = git(repo, "log", "-1", "--date=raw", "--format=%ad|%cd").strip()
    return shown.split("|")


def test_commit_dates_carry_no_local_utc_offset(dev, denylist, tmp_path, monkeypatch):
    # The maintainer's local offset in a public commit is personal context.
    monkeypatch.setenv("TZ", LOCAL_ZONE)
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL])
    assert code == 0
    author_date, committer_date = raw_dates(out)
    assert author_date.endswith(" +0000") and committer_date.endswith(" +0000")


def test_into_sync_commit_dates_carry_no_local_utc_offset(dev, denylist, tmp_path, monkeypatch):
    clone = make_clone(dev, tmp_path)
    put(dev, "src/b.py", "VALUE = 2\n")
    git(dev, "add", "--", "src/b.py")
    monkeypatch.setenv("TZ", LOCAL_ZONE)
    code = export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL])
    assert code == 0
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "2"
    author_date, committer_date = raw_dates(clone)
    assert author_date.endswith(" +0000") and committer_date.endswith(" +0000")


@pytest.mark.parametrize("pinned", ["author", "committer"])
def test_read_back_rejects_a_commit_dated_with_a_local_offset(tmp_path, monkeypatch, pinned):
    # If either date ever stops being UTC, make_commit must notice and fail, not ship it.
    repo = tmp_path / "fresh"
    repo.mkdir()
    real = export_public._isolated_env

    def leaky_env(name, email):
        env = real(name, email)
        env[f"GIT_{pinned.upper()}_DATE"] = "1700000000 -0500"
        return env

    monkeypatch.setattr(export_public, "_isolated_env", leaky_env)
    git(repo, "init", "-q", "-b", "main")
    put(repo, "a.txt", "x\n")
    git(repo, "add", "--", "a.txt")
    with pytest.raises(export_public.ReadBackError, match="UTC"):
        export_public.make_commit(repo, "example", EMAIL, "msg")


def test_non_noreply_email_exits_2_and_creates_no_repository(dev, denylist, tmp_path):
    out = tmp_path / "out"
    bad = "someone" + AT + "corp-mail.test"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", bad])
    assert code == 2
    assert not (out / ".git").exists()


@pytest.mark.parametrize("suffix", ["\n", "\r\n", " ", "\n\n"])
def test_resolve_identity_rejects_an_email_with_trailing_whitespace(suffix):
    # "$" matches before a trailing newline, so a newline-bearing email used to pass.
    with pytest.raises(export_public.ExportError, match="noreply"):
        export_public.resolve_identity("x", "a" + AT + "users.noreply.github.com" + suffix)


def test_missing_author_email_exits_2(dev, denylist, tmp_path):
    out = tmp_path / "out"
    assert export_public.main(["--repo", str(dev), "--out", str(out), "--commit"]) == 2
    assert not (out / ".git").exists()


def test_author_comes_from_environment_when_flags_are_absent(dev, denylist, tmp_path, monkeypatch):
    monkeypatch.setenv("JSP_EXPORT_AUTHOR_EMAIL", EMAIL)
    monkeypatch.setenv("JSP_EXPORT_AUTHOR_NAME", "Public Name")
    out = tmp_path / "out"
    assert export_public.main(["--repo", str(dev), "--out", str(out), "--commit"]) == 0
    assert git(out, "log", "-1", "--format=%an|%ae").strip() == f"Public Name|{EMAIL}"


def test_planted_term_aborts_the_commit_and_removes_the_export(dev, denylist, tmp_path, capsys):
    put(dev, "src/leak.py", "# written by " + TERM + "\n")
    git(dev, "add", "--", "src/leak.py")
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL])
    shown = capsys.readouterr()
    assert code == 1
    assert not out.exists()
    assert "zorblax" not in (shown.out + shown.err).lower()


def test_commit_without_a_denylist_exits_2_and_commits_nothing(dev, tmp_path):
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL])
    assert code == 2
    assert not (out / ".git").exists()


def test_commit_message_with_a_term_aborts(dev, denylist, tmp_path):
    out = tmp_path / "out"
    code = export_public.main(
        ["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL, "--message", "for " + TERM]
    )
    assert code == 1
    assert not (out / ".git").exists()


def test_commit_message_with_a_trailer_aborts(dev, denylist, tmp_path):
    out = tmp_path / "out"
    message = "Sync\n\nClaude-" + "Session: abc"
    code = export_public.main(
        ["--repo", str(dev), "--out", str(out), "--commit", "--author-email", EMAIL, "--message", message]
    )
    assert code == 1
    assert not (out / ".git").exists()


def test_commit_requires_out(dev, denylist):
    assert export_public.main(["--repo", str(dev), "--commit", "--author-email", EMAIL]) == 2


# ---------------------------------------------------------------------------------------
# --into
# ---------------------------------------------------------------------------------------


def make_clone(dev, tmp_path) -> Path:
    clone = tmp_path / "public"
    code = export_public.main(["--repo", str(dev), "--out", str(clone), "--commit", "--author-email", EMAIL])
    assert code == 0
    return clone


def test_into_sync_leaves_the_clone_tracking_exactly_the_export(dev, denylist, tmp_path):
    clone = make_clone(dev, tmp_path)
    git(dev, "rm", "-q", "--", "src/a.py")
    put(dev, "src/b.py", "VALUE = 2\n")
    put(dev, "docs/guide.md", "A guide.\n")
    git(dev, "add", "--", "src/b.py", "docs/guide.md")
    code = export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL])
    assert code == 0
    expected = export_public.export_tree(dev, tmp_path / "expected")
    assert tracked(clone) == expected
    assert not (clone / "src" / "a.py").exists()
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "2"
    assert git(clone, "log", "-1", "--format=%ae|%ce").strip() == f"{EMAIL}|{EMAIL}"
    assert git(clone, "status", "--porcelain").strip() == ""


def test_into_second_sync_with_no_change_prints_nothing_to_sync(dev, denylist, tmp_path, capsys):
    clone = make_clone(dev, tmp_path)
    put(dev, "src/b.py", "VALUE = 2\n")
    git(dev, "add", "--", "src/b.py")
    assert export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL]) == 0
    capsys.readouterr()
    assert export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL]) == 0
    assert "nothing to sync" in capsys.readouterr().out
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "2"


def test_into_with_an_empty_export_refuses_and_leaves_the_clone_alone(dev, denylist, tmp_path, capsys):
    # Pointing --repo at a repository with no allowlisted content must not wipe the clone.
    clone = make_clone(dev, tmp_path)
    before = tracked(clone)
    assert before
    wrong = tmp_path / "wrong-repo"
    wrong.mkdir()
    git(wrong, "init", "-q", "-b", "main")
    put(wrong, ".planning/x.md", "private\n")
    git(wrong, "add", "--", ".planning/x.md")
    code = export_public.main(["--repo", str(wrong), "--into", str(clone), "--author-email", EMAIL])
    assert code == 2
    assert "export is empty" in capsys.readouterr().err
    assert tracked(clone) == before
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(clone, "status", "--porcelain").strip() == ""


def test_sync_into_guards_an_empty_file_list_even_if_the_gate_passes(dev, denylist, tmp_path, monkeypatch):
    clone = make_clone(dev, tmp_path)
    monkeypatch.setattr(export_public, "export_tree", lambda repo, out: [])
    monkeypatch.setattr(export_public, "_gate", lambda *a, **k: 0)
    with pytest.raises(export_public.ExportError, match="export is empty"):
        export_public.sync_into(dev, clone, "n", EMAIL, "m", None)
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "1"


def test_commit_with_an_empty_export_exits_2(tmp_path, denylist):
    wrong = tmp_path / "wrong-repo"
    wrong.mkdir()
    git(wrong, "init", "-q", "-b", "main")
    put(wrong, ".planning/x.md", "private\n")
    git(wrong, "add", "--", ".planning/x.md")
    out = tmp_path / "out"
    code = export_public.main(["--repo", str(wrong), "--out", str(out), "--commit", "--author-email", EMAIL])
    assert code == 2
    assert not (out / ".git").exists()


def test_into_clone_with_uncommitted_changes_exits_2(dev, denylist, tmp_path):
    clone = make_clone(dev, tmp_path)
    put(clone, "scratch.txt", "uncommitted\n")
    code = export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL])
    assert code == 2
    assert git(clone, "rev-list", "--count", "HEAD").strip() == "1"


def test_into_a_folder_that_is_not_a_clone_exits_2(dev, denylist, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert export_public.main(["--repo", str(dev), "--into", str(plain), "--author-email", EMAIL]) == 2


def test_into_with_a_planted_term_exits_1_and_leaves_the_clone_untouched(dev, denylist, tmp_path):
    clone = make_clone(dev, tmp_path)
    before = (tracked(clone), git(clone, "rev-parse", "HEAD"))
    put(dev, "src/leak.py", "# " + TERM + "\n")
    git(dev, "add", "--", "src/leak.py")
    code = export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL])
    assert code == 1
    assert (tracked(clone), git(clone, "rev-parse", "HEAD")) == before
    assert git(clone, "status", "--porcelain").strip() == ""


def test_into_non_noreply_email_exits_2(dev, denylist, tmp_path):
    clone = make_clone(dev, tmp_path)
    bad = "someone" + AT + "corp-mail.test"
    assert export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", bad]) == 2


def test_into_cannot_combine_with_out(dev, denylist, tmp_path):
    clone = make_clone(dev, tmp_path)
    code = export_public.main(
        ["--repo", str(dev), "--into", str(clone), "--out", str(tmp_path / "x"), "--author-email", EMAIL]
    )
    assert code == 2


# ---------------------------------------------------------------------------------------
# The development repo is never changed
# ---------------------------------------------------------------------------------------


def test_no_mode_changes_the_development_repo(dev, denylist, tmp_path):
    put(dev, "src/a.py", "VALUE = 3  # unstaged\n")
    put(dev, "docs/staged.md", "staged\n")
    git(dev, "add", "--", "docs/staged.md")
    before = snapshot(dev)
    export_public.main(["--repo", str(dev), "--out", str(tmp_path / "plain")])
    assert snapshot(dev) == before
    clone = tmp_path / "committed"
    export_public.main(["--repo", str(dev), "--out", str(clone), "--commit", "--author-email", EMAIL])
    assert snapshot(dev) == before
    export_public.main(["--repo", str(dev), "--into", str(clone), "--author-email", EMAIL])
    assert snapshot(dev) == before
    leak_check.run_staged(dev, denylist)
    assert snapshot(dev) == before


# ---------------------------------------------------------------------------------------
# leak_check --staged and --message-file
# ---------------------------------------------------------------------------------------


def test_staged_mode_flags_a_staged_leak(dev, denylist, capsys):
    put(dev, "src/leak.py", "# " + TERM + "\n")
    git(dev, "add", "--", "src/leak.py")
    code = leak_check.main(["--staged", "--root", str(dev), "--require-denylist"])
    shown = capsys.readouterr()
    assert code == 1
    assert "FAIL src/leak.py:1 denylist term #1" in shown.out
    assert "zorblax" not in (shown.out + shown.err).lower()


def test_staged_mode_ignores_an_unstaged_working_copy_leak(dev, denylist):
    put(dev, "src/a.py", "# " + TERM + "\n")
    assert leak_check.main(["--staged", "--root", str(dev), "--require-denylist"]) == 0


def test_staged_mode_is_not_hidden_by_a_clean_unstaged_edit(dev, denylist):
    put(dev, "src/leak.py", "# " + TERM + "\n")
    git(dev, "add", "--", "src/leak.py")
    put(dev, "src/leak.py", "# now clean in the working copy\n")
    assert leak_check.main(["--staged", "--root", str(dev), "--require-denylist"]) == 1


def test_staged_mode_does_not_scan_private_folders(dev, denylist):
    put(dev, ".planning/PROJECT.md", TERM + "\n")
    git(dev, "add", "--", ".planning/PROJECT.md")
    assert leak_check.main(["--staged", "--root", str(dev), "--require-denylist"]) == 0


def test_staged_mode_requires_the_denylist_when_asked(dev):
    assert leak_check.main(["--staged", "--root", str(dev), "--require-denylist"]) == 2


def test_staged_mode_refuses_a_denylist_inside_the_repo(dev):
    inside = dev / "dl.txt"
    inside.write_text("\n".join(DENYLIST) + "\n", encoding="utf-8")
    code = leak_check.main(["--staged", "--root", str(dev), "--denylist", str(inside), "--require-denylist"])
    assert code == 2


def test_message_file_mode_flags_a_term(tmp_path, denylist, capsys):
    message = tmp_path / "msg.txt"
    message.write_text("Sync for " + TERM + "\n", encoding="utf-8")
    code = leak_check.main(["--message-file", str(message), "--require-denylist"])
    shown = capsys.readouterr()
    assert code == 1
    assert "denylist term #1" in shown.out
    assert "zorblax" not in (shown.out + shown.err).lower()


def test_message_file_mode_passes_a_clean_message_and_allows_trailers(tmp_path, denylist):
    message = tmp_path / "msg.txt"
    message.write_text("Sync\n\nCo-Authored-" + "By: A Person <a" + AT + "users.noreply.github.com>\n", encoding="utf-8")
    assert leak_check.main(["--message-file", str(message), "--require-denylist"]) == 0


def test_message_file_mode_requires_the_denylist_when_asked(tmp_path):
    message = tmp_path / "msg.txt"
    message.write_text("Sync\n", encoding="utf-8")
    assert leak_check.main(["--message-file", str(message), "--require-denylist"]) == 2


def test_message_file_mode_without_a_denylist_prints_skipped(tmp_path, capsys):
    message = tmp_path / "msg.txt"
    message.write_text("Sync\n", encoding="utf-8")
    assert leak_check.main(["--message-file", str(message)]) == 0
    assert "personal denylist check SKIPPED" in capsys.readouterr().out


def test_staged_and_message_file_cannot_be_combined(tmp_path):
    with pytest.raises(SystemExit) as raised:
        leak_check.main(["--staged", "--message-file", str(tmp_path / "m.txt")])
    assert raised.value.code == 2


# ---------------------------------------------------------------------------------------
# Module facts and the real tree
# ---------------------------------------------------------------------------------------


def test_export_public_is_stdlib_only_with_a_pep723_header():
    source = (TOOLS / "export_public.py").read_text(encoding="utf-8")
    assert "# /// script" in source
    assert 'requires-python = ">=3.11.4"' in source
    assert "dependencies = []" in source
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names or name == "leak_check", name


def test_the_tools_import_each_other_only_inside_functions():
    for script, sibling in (("export_public.py", "leak_check"), ("leak_check.py", "export_public")):
        tree = ast.parse((TOOLS / script).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert all(alias.name != sibling for alias in node.names), script
            if isinstance(node, ast.ImportFrom):
                assert node.module != sibling, script


@pytest.mark.skipif(not (REPO / ".git").exists(), reason="needs the git checkout this suite ships in")
def test_real_tree_export_holds_the_script_and_no_private_folders(tmp_path):
    out = tmp_path / "export"
    before = snapshot(REPO)
    done = subprocess.run(
        [sys.executable, str(TOOLS / "export_public.py"), "--repo", str(REPO), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (out / "scripts" / "jobsearch.py").is_file()
    assert (out / "tools" / "leak_check.py").is_file()
    assert (out / "demo" / "casey-ops" / "README.md").is_file()
    assert (out / "CONTRIBUTING.md").is_file()
    assert (out / ".github" / "SECURITY.md").is_file()
    assert (out / ".github" / "CODE_OF_CONDUCT.md").is_file()
    assert not (out / ".planning").exists()
    assert not (out / ".claude").exists()
    assert snapshot(REPO) == before
