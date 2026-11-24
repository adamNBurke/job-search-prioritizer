"""Proofs that the leak-gate hooks install safely and block real commits.

Purpose: show that tools/install_hooks.py writes working pre-commit and commit-msg hooks, that a
commit staging a planted identifier or carrying one in its message is refused, that a clean
commit goes through, and that reinstalling is a no-op that never clobbers a foreign hook.
Logic: each test builds a throwaway git repo under tmp_path with a copy of tools/ and a
throwaway denylist of fictional terms, passed through JSP_LEAK_DENYLIST. An autouse fixture
in conftest.py points every home-directory variable at a temp folder so the maintainer's real denylist can never
load.
Guardrails: planted strings are composed from fragments at runtime, because tests/ ships in the
public export and the gate scans it. Skipped on Windows: maintainer hooks are macOS and Linux
tooling in v1.
QA checklist: new hook behavior? Add a named test here. A new gate flag? Check both hook bodies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import install_hooks
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="maintainer hooks are macOS and Linux tooling in v1"
)

REPO = Path(__file__).resolve().parent.parent
PLANTED = "Qwerblix " + "Fakename"
AUTHOR_EMAIL = "author" + chr(64) + "example.invalid"
DENYLIST = [
    PLANTED,
    "Filler One Alpha",
    "Filler Two Beta",
    "Filler Three Gamma",
    "Filler Four Delta",
    "Filler Five Epsilon",
]


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL=AUTHOR_EMAIL,
    )
    env.update(extra or {})
    return env


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env or _env(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "dev"
    root.mkdir()
    assert git(root, "init", "-q").returncode == 0
    shutil.copytree(REPO / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "docs").mkdir()
    (root / "docs" / "clean.md").write_text("A clean public note.\n", encoding="utf-8")
    return root


@pytest.fixture
def hook_env(tmp_path):
    deny = tmp_path / "deny"
    deny.mkdir()
    path = deny / "dl.txt"
    path.write_text("\n".join(DENYLIST) + "\n", encoding="utf-8")
    return _env({"JSP_LEAK_DENYLIST": str(path)})


def installed(repo: Path) -> Path:
    assert install_hooks.main(["--repo", str(repo)]) == 0
    return repo / ".git" / "hooks"


def test_install_writes_executable_lf_hooks_with_the_gate_commands(repo):
    hooks = installed(repo)
    pre = hooks / "pre-commit"
    msg = hooks / "commit-msg"
    for path in (pre, msg):
        assert os.access(path, os.X_OK)
        raw = path.read_bytes()
        assert raw.startswith(b"#!/bin/sh\n")
        assert b"\r" not in raw
        assert b"tools/leak_check.py" in raw
    assert "--staged --require-denylist" in pre.read_text(encoding="utf-8")
    assert '--message-file "$1" --require-denylist' in msg.read_text(encoding="utf-8")


def test_commit_staging_a_planted_term_is_refused(repo, hook_env):
    installed(repo)
    (repo / "docs" / "leak.md").write_text(f"Contact {PLANTED} today.\n", encoding="utf-8")
    assert git(repo, "add", "docs/leak.md", env=hook_env).returncode == 0
    result = git(repo, "commit", "-q", "-m", "plant", env=hook_env)
    assert result.returncode != 0
    assert PLANTED not in result.stdout + result.stderr
    assert git(repo, "rev-parse", "--verify", "HEAD", env=hook_env).returncode != 0


def test_clean_commit_succeeds(repo, hook_env):
    installed(repo)
    assert git(repo, "add", "docs/clean.md", env=hook_env).returncode == 0
    result = git(repo, "commit", "-q", "-m", "clean note", env=hook_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "--verify", "HEAD", env=hook_env).returncode == 0


def test_commit_message_with_a_term_is_refused(repo, hook_env):
    installed(repo)
    assert git(repo, "add", "docs/clean.md", env=hook_env).returncode == 0
    result = git(repo, "commit", "-q", "-m", f"note for {PLANTED}", env=hook_env)
    assert result.returncode != 0
    assert PLANTED not in result.stdout + result.stderr
    assert git(repo, "rev-parse", "--verify", "HEAD", env=hook_env).returncode != 0


def test_commit_is_refused_when_the_denylist_is_missing(repo):
    installed(repo)
    assert git(repo, "add", "docs/clean.md").returncode == 0
    result = git(repo, "commit", "-q", "-m", "no denylist")
    assert result.returncode != 0


def test_reinstall_is_a_no_op(repo):
    hooks = installed(repo)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in hooks.glob("*") if p.is_file()}
    assert install_hooks.main(["--repo", str(repo)]) == 0
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in hooks.glob("*") if p.is_file()}
    assert before == after


def test_foreign_hook_is_preserved_without_force(repo):
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    foreign = hooks / "pre-commit"
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert install_hooks.main(["--repo", str(repo)]) == install_hooks.EXIT_REFUSED
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert (hooks / "commit-msg").read_text(encoding="utf-8") == install_hooks.COMMIT_MSG
    assert install_hooks.main(["--repo", str(repo), "--force"]) == 0
    assert foreign.read_text(encoding="utf-8") == install_hooks.PRE_COMMIT


def test_uninstall_removes_only_our_hooks(repo):
    hooks = installed(repo)
    (hooks / "commit-msg").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert install_hooks.main(["--repo", str(repo), "--uninstall"]) == 0
    assert not (hooks / "pre-commit").exists()
    assert (hooks / "commit-msg").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_not_a_git_checkout_exits_with_config_error(tmp_path, capsys):
    assert install_hooks.main(["--repo", str(tmp_path)]) == install_hooks.EXIT_CONFIG
    assert "not a git checkout" in capsys.readouterr().err
