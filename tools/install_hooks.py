# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install the pre-commit and commit-msg leak-gate hooks into a development checkout.

Purpose
    Every dev commit passes the leak gate before it exists. pre-commit scans the staged
    allowlisted tree and commit-msg scans the message, both with the personal denylist required,
    so a missing denylist blocks the commit instead of letting it through unchecked.

Logic
    1. `git rev-parse --git-path hooks` names the hooks folder, so core.hooksPath and linked
       worktrees are honored.
    2. Each hook is a two-line POSIX shell script that runs tools/leak_check.py through uv.
    3. Files are written LF, UTF-8, mode 0755. A reinstall with identical content changes nothing.
    4. A different existing hook is never overwritten unless --force is given.
    5. --uninstall removes only hooks whose content matches what this tool writes.

Guardrails
    - Hooks are a convenience: `git commit --no-verify` skips them. The authoritative gate is the
      export run with --require-denylist, repeated before every push.
    - Maintainer tooling for macOS and Linux. The hooks are shell scripts and Git for Windows runs
      them with its own sh, but v1 does not test that path.
    - No denylist term is read, printed or logged here.

QA checklist
    - Changed a hook command? Update tests/test_install_hooks.py and rerun it.
    - Ran it? Check `.git/hooks/pre-commit` and `.git/hooks/commit-msg` exist and are executable.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PRE_COMMIT = "#!/bin/sh\nexec uv run --script tools/leak_check.py --staged --require-denylist\n"
COMMIT_MSG = '#!/bin/sh\nexec uv run --script tools/leak_check.py --message-file "$1" --require-denylist\n'
HOOKS = {"pre-commit": PRE_COMMIT, "commit-msg": COMMIT_MSG}
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CONFIG = 2


def hooks_dir(repo: Path) -> Path:
    """Return the hooks folder of the checkout at `repo`, as git resolves it."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"not a git checkout: {repo}")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def read_text(path: Path) -> str:
    # newline="" keeps CRLF visible, so a hook with the wrong endings counts as different.
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def write_hook(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    path.chmod(0o755)


def install(directory: Path, force: bool) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    refused = []
    for name, content in HOOKS.items():
        path = directory / name
        if path.exists():
            if read_text(path) == content:
                path.chmod(0o755)
                print(f"hook {name}: already installed")
                continue
            if not force:
                print(f"hook {name}: a different hook exists; left alone (use --force to replace)")
                refused.append(name)
                continue
        write_hook(path, content)
        print(f"hook {name}: installed")
    return EXIT_REFUSED if refused else EXIT_OK


def uninstall(directory: Path) -> int:
    for name, content in HOOKS.items():
        path = directory / name
        if not path.exists():
            print(f"hook {name}: not present")
        elif read_text(path) == content:
            path.unlink()
            print(f"hook {name}: removed")
        else:
            print(f"hook {name}: not ours; left alone")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_hooks.py",
        description="Install the leak-gate pre-commit and commit-msg hooks.",
    )
    parser.add_argument("--repo", default=".", help="development checkout (default: current directory)")
    parser.add_argument("--force", action="store_true", help="replace a different existing hook")
    parser.add_argument("--uninstall", action="store_true", help="remove hooks this tool wrote")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        directory = hooks_dir(Path(args.repo).resolve())
    except RuntimeError as exc:
        print(f"install_hooks: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.uninstall:
        return uninstall(directory)
    return install(directory, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
