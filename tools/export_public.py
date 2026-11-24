# /// script
# requires-python = ">=3.11.4"
# dependencies = []
# ///
"""Export the public tree from the development repo's index, gate it, and commit it fresh.

Purpose
    The development repo holds planning notes and personal context that must never ship. The
    public repository is a downstream copy produced only by this tool: an allowlisted
    slice of the index tree, scanned by the leak gate, committed under the GitHub noreply
    identity with a message that carries no trailers. It is never edited by hand.

Logic
    1. `git write-tree` captures the index (staged files export before they are committed;
       unstaged edits do not).
    2. `git ls-tree` lists the top-level names of that tree. Only names on ALLOWLIST are
       archived with `git archive --format=tar`, so an absent allowlisted path cannot raise a
       pathspec error and a new unlisted top-level folder is silently excluded (fail closed).
    3. The tar is extracted with tarfile's data filter, which needs Python 3.11.4 or newer; an
       older Python stops with a clear error before anything is written. The extracted file
       set must equal `git ls-tree -r` for the same paths, or the export aborts.
    4. --out DIR stops there. --commit (with --out) runs the leak gate with the personal
       denylist required, then runs `git init` and makes exactly one commit. --into CLONE
       gates first, then replaces the clone's tracked file set with the export (stale files
       removed with `git rm`, exported files added by explicit path) and commits only when
       something changed.

Identity
    The author email must end with the GitHub noreply domain, or the tool exits 2 before it
    creates anything. Commits are made with the identity forced through `-c` options and the
    GIT_AUTHOR_* and GIT_COMMITTER_* variables, with the user's global and system git
    configuration and every hook disabled, then read back and compared. Both commit dates are
    pinned to the current time in UTC (+0000) and read back too, so the maintainer's local
    UTC offset never reaches the public history. No `git config` write is ever issued.

Guardrails
    - Never changes the development repo: it reads the index and writes only loose objects.
    - --out must be outside the repo and empty or absent.
    - A gate failure leaves no export and no commit behind.
    - The gate output never contains denylist text (see leak_check.py).

QA checklist
    - Add a top-level folder to ALLOWLIST only when it is meant to be public.
    - After any change run tests/test_export.py and export the real tree to a temp folder:
      it must hold scripts/jobsearch.py and neither .planning nor .claude.
    - A new commit-making path must go through make_commit() so the read-back runs.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from io import BytesIO
from pathlib import Path

ALLOWLIST = (
    ".claude-plugin",
    ".github",
    ".gitattributes",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "demo",
    "docs",
    "pyproject.toml",
    "scripts",
    "skills",
    "src",
    "tests",
    "tools",
    "uv.lock",
)
NOREPLY_SUFFIX = "@users.noreply.github.com"
DEFAULT_MESSAGE = "Sync from the development tree"
EXIT_OK = 0
EXIT_GATE = 1
EXIT_CONFIG = 2


class ExportError(Exception):
    """A configuration or integrity fault. Exit code 2 unless stated otherwise."""


class ReadBackError(ExportError):
    """A commit was made but did not read back as requested. Exit code 1."""


# ---------------------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------------------


def _clean_env() -> dict[str, str]:
    """Environment for commands that touch the export or clone: no inherited GIT_* values."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def _isolated_env(name: str, email: str) -> dict[str, str]:
    env = _clean_env()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = email
    # A commit date records the local UTC offset, which is personal context. Pin both
    # dates to the current instant in UTC, and the zone itself, so no offset can leak.
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{int(time.time())} +0000"
    env["TZ"] = "UTC"
    return env


def _git(
    cwd: Path | str,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
    stdin: bytes | None = None,
    binary: bool = False,
):
    command = ["git", "-C", str(cwd), *args]
    try:
        if binary or stdin is not None:
            done = subprocess.run(
                command, capture_output=True, input=stdin, env=env, check=False
            )
        else:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
            )
    except OSError as exc:
        raise ExportError(f"git could not be run ({type(exc).__name__})") from None
    if check and done.returncode != 0:
        detail = done.stderr if isinstance(done.stderr, str) else done.stderr.decode("utf-8", "replace")
        raise ExportError(f"git {args[0]} failed: {detail.strip()[:300]}")
    return done


def repo_toplevel(repo: Path | str) -> Path:
    done = _git(repo, "rev-parse", "--show-toplevel")
    return Path(done.stdout.strip()).resolve()


# ---------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------


def _extract(data: bytes, out: Path) -> None:
    with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
        archive.extractall(out, filter="data")


def _expected_files(repo: Path, tree: str, present: list[str]) -> list[str]:
    done = _git(repo, "ls-tree", "-r", "-z", "--full-tree", tree, "--", *present)
    files = []
    for entry in done.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if meta.split(" ", 1)[0] == "160000":
            raise ExportError("an allowlisted path holds a submodule; the export refuses it")
        files.append(path)
    return sorted(files)


def _actual_files(out: Path) -> list[str]:
    found = []
    for current, _dirs, names in os.walk(out):
        for name in names:
            found.append((Path(current) / name).relative_to(out).as_posix())
    return sorted(found)


def export_tree(repo: Path | str, out: Path | str) -> list[str]:
    """Extract the allowlisted slice of the index tree into `out`; return the sorted file list."""
    if not hasattr(tarfile, "data_filter"):
        raise ExportError("the export needs Python 3.11.4 or newer (tarfile data filter)")
    repo = Path(repo)
    out = Path(out)
    top = repo_toplevel(repo)
    resolved_out = out.resolve()
    if resolved_out == top or resolved_out.is_relative_to(top) or top.is_relative_to(resolved_out):
        raise ExportError("the export folder overlaps the repository")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExportError("the export folder exists and is not empty")
    tree = _git(top, "write-tree").stdout.strip()
    names = [
        n
        for n in _git(top, "ls-tree", "-z", "--name-only", "--full-tree", tree).stdout.split("\0")
        if n
    ]
    present = sorted(name for name in names if name in ALLOWLIST)
    out.mkdir(parents=True, exist_ok=True)
    if not present:
        return []
    data = _git(top, "archive", "--format=tar", tree, "--", *present, binary=True).stdout
    _extract(data, out)
    expected = _expected_files(top, tree, present)
    actual = _actual_files(out)
    if expected != actual:
        raise ExportError("the extracted file set does not match the index tree")
    return actual


# ---------------------------------------------------------------------------------------
# Identity and commits
# ---------------------------------------------------------------------------------------

_NOREPLY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*" + re.escape(NOREPLY_SUFFIX))


def resolve_identity(name: str | None, email: str | None) -> tuple[str, str]:
    email = email or os.environ.get("JSP_EXPORT_AUTHOR_EMAIL") or ""
    name = name or os.environ.get("JSP_EXPORT_AUTHOR_NAME") or ""
    if not email:
        raise ExportError("an author email is required (--author-email or JSP_EXPORT_AUTHOR_EMAIL)")
    if not _NOREPLY.fullmatch(email):
        raise ExportError(f"the author email must be a GitHub noreply address ({NOREPLY_SUFFIX})")
    if not name:
        name = email[: -len(NOREPLY_SUFFIX)].split("+")[-1]
    return name, email


def _trailer_free(message: str) -> bool:
    import leak_check

    return not leak_check.check_message(message, [], trailers=True)


def make_commit(repo_dir: Path, name: str, email: str, message: str) -> None:
    """Commit the index with a forced identity and no hooks, then read the commit back."""
    env = _isolated_env(name, email)
    with tempfile.TemporaryDirectory() as hooks:
        _git(
            repo_dir,
            "-c", f"user.name={name}",
            "-c", f"user.email={email}",
            "-c", "commit.gpgsign=false",
            "-c", f"core.hooksPath={hooks}",
            "commit", "-q", "-m", message,
            env=env,
        )
    shown = _git(
        repo_dir, "log", "-1", "--date=raw",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%ad%x00%cd%x00%B", env=env,
    ).stdout
    parts = shown.split("\0", 6)
    if len(parts) != 7:
        raise ReadBackError("the new commit could not be read back")
    author, author_mail, committer, committer_mail, author_date, committer_date, body = parts
    if (author, author_mail, committer, committer_mail) != (name, email, name, email):
        raise ReadBackError("the commit identity does not match the requested identity")
    if not (author_date.endswith(" +0000") and committer_date.endswith(" +0000")):
        raise ReadBackError("the commit dates are not in UTC")
    if not _trailer_free(body):
        raise ReadBackError("the commit message carries a trailer line")


def _gate(export_dir: Path, repo: Path, denylist: str | None, message: str) -> int:
    import leak_check

    return leak_check.run(
        export_dir,
        denylist_path=denylist,
        require_denylist=True,
        message=message,
        extra_roots=[repo_toplevel(repo)],
    )


def _stage_all(repo_dir: Path, files: list[str], env: dict[str, str]) -> None:
    if files:
        payload = ("\0".join(files) + "\0").encode("utf-8")
        _git(repo_dir, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, stdin=payload)


def commit_fresh(out: Path, files: list[str], name: str, email: str, message: str) -> None:
    env = _isolated_env(name, email)
    _git(out, "init", "-q", "-b", "main", env=env)
    _stage_all(out, files, env)
    make_commit(out, name, email, message)
    count = _git(out, "rev-list", "--count", "HEAD", env=env).stdout.strip()
    if count != "1":
        raise ReadBackError("the fresh repository does not hold exactly one commit")


def sync_into(
    repo: Path, clone: Path, name: str, email: str, message: str, denylist: str | None
) -> int:
    env = _isolated_env(name, email)
    clone = clone.resolve()
    inside = _git(clone, "rev-parse", "--is-inside-work-tree", env=env, check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ExportError("the sync target is not a git work tree")
    if Path(_git(clone, "rev-parse", "--show-toplevel", env=env).stdout.strip()).resolve() != clone:
        raise ExportError("the sync target must be the top of its work tree")
    top = repo_toplevel(repo)
    if clone == top or clone.is_relative_to(top) or top.is_relative_to(clone):
        raise ExportError("the sync target overlaps the development repository")
    if _git(clone, "status", "--porcelain", env=env).stdout.strip():
        raise ExportError("the sync target has uncommitted changes")
    with tempfile.TemporaryDirectory() as tmp:
        export_dir = Path(tmp) / "export"
        files = export_tree(repo, export_dir)
        if not files:
            raise ExportError("the export is empty; refusing to sync (is --repo the right repository?)")
        code = _gate(export_dir, repo, denylist, f"{message}\n{name}\n{email}")
        if code != 0:
            return code
        wanted = set(files)
        tracked = [p for p in _git(clone, "ls-files", "-z", env=env).stdout.split("\0") if p]
        stale = sorted(p for p in tracked if p not in wanted)
        if stale:
            payload = ("\0".join(stale) + "\0").encode("utf-8")
            _git(clone, "rm", "-q", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, stdin=payload)
        for rel in files:
            source, target = export_dir / rel, clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
            else:
                shutil.copy2(source, target)
        _stage_all(clone, files, env)
    if _git(clone, "diff", "--cached", "--quiet", env=env, check=False).returncode == 0:
        print("nothing to sync")
        return EXIT_OK
    try:
        make_commit(clone, name, email, message)
    except ReadBackError:
        _undo_commit(clone, env)
        raise
    now_tracked = sorted(p for p in _git(clone, "ls-files", "-z", env=env).stdout.split("\0") if p)
    if now_tracked != sorted(files):
        raise ExportError("the clone's tracked files do not match the export")
    print(f"synced {len(files)} files")
    return EXIT_OK


def _undo_commit(clone: Path, env: dict[str, str]) -> None:
    done = _git(clone, "reset", "-q", "--soft", "HEAD~1", env=env, check=False)
    if done.returncode != 0:
        _git(clone, "update-ref", "-d", "HEAD", env=env, check=False)


# ---------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_public.py",
        description="Export the allowlisted public tree from the git index.",
    )
    parser.add_argument("--repo", default=".", help="development repository (default: current directory)")
    parser.add_argument("--out", help="folder to extract into (must be outside the repo, empty or absent)")
    parser.add_argument("--commit", action="store_true", help="gate the export, then make one fresh commit in --out")
    parser.add_argument("--into", help="existing clone to sync to the export, committing when it changed")
    parser.add_argument("--author-name", help="commit author name (default: derived from the email)")
    parser.add_argument("--author-email", help="commit author email, must be a GitHub noreply address")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="commit message")
    parser.add_argument("--denylist", help="personal denylist file (default: per-user config)")
    return parser


def _run(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    if args.into and (args.out or args.commit):
        raise ExportError("--into cannot be combined with --out or --commit")
    if args.commit and not args.out:
        raise ExportError("--commit needs --out")
    if not args.into and not args.out:
        raise ExportError("give --out DIR or --into CLONE")
    if args.into:
        name, email = resolve_identity(args.author_name, args.author_email)
        return sync_into(repo, Path(args.into), name, email, args.message, args.denylist)
    identity = resolve_identity(args.author_name, args.author_email) if args.commit else None
    out = Path(args.out)
    existed = out.exists()
    files = export_tree(repo, out)
    if not args.commit:
        print(f"exported {len(files)} files")
        return EXIT_OK
    assert identity is not None
    name, email = identity
    code = _gate(out, repo, args.denylist, f"{args.message}\n{name}\n{email}")
    if code != 0:
        shutil.rmtree(out, ignore_errors=True)
        if existed:
            out.mkdir(parents=True, exist_ok=True)
        return code
    try:
        commit_fresh(out, files, name, email, args.message)
    except ReadBackError:
        shutil.rmtree(out / ".git", ignore_errors=True)
        raise
    print(f"committed {len(files)} files")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ReadBackError as exc:
        print(f"export: ERROR {exc}", file=sys.stderr)
        return EXIT_GATE
    except ExportError as exc:
        print(f"export: ERROR {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
