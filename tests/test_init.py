"""Data-folder refusal matrix, git-ignore proof, idempotent init."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jobsearch import cli, fetch, paths

EXPECTED_WROTE = [".gitignore", "cache/http", "jobsearch.workspace.json"]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A fake plugin root, HOME and plugin data dir inside tmp; host env cleared."""
    base = tmp_path.resolve()
    root = base / "plugin-root"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "0.0.1"}), encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "scripts").mkdir()
    home = base / "home"
    home.mkdir()
    plugin_data = base / "plugin-data"
    plugin_data.mkdir()
    outside = base / "outside"
    outside.mkdir()
    monkeypatch.setattr(paths, "plugin_root", lambda: root)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return {"base": base, "root": root, "home": home, "plugin_data": plugin_data,
            "outside": outside}


def _snapshot(base: Path) -> list[tuple[str, int]]:
    rows = []
    for path in sorted(base.rglob("*")):
        rows.append((path.relative_to(base).as_posix(),
                     path.stat().st_size if path.is_file() else -1))
    return rows


def _refusal_cases() -> list[str]:
    return ["root", "inside_root", "dotdot", "symlink", "case", "plugin_data",
            "inside_plugin_data", "home_plugins", "cache_env", "config_env", "relative",
            "trailing_sep"]


def _target_for(case: str, w: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    root, base = w["root"], w["base"]
    if case == "root":
        return root
    if case == "inside_root":
        return root / "src" / "newdir"
    if case == "dotdot":
        return root / "src" / ".." / "scripts" / "x"
    if case == "symlink":
        link = w["outside"] / "link"
        try:
            link.symlink_to(root, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not permitted here")
        return link / "sub"
    if case == "case":
        return base / "PLUGIN-ROOT" / "x"
    if case == "plugin_data":
        return w["plugin_data"]
    if case == "inside_plugin_data":
        return w["plugin_data"] / "x"
    if case == "home_plugins":
        return w["home"] / ".claude" / "plugins" / "cache" / "anything"
    if case == "cache_env":
        alt = base / "altcache"
        monkeypatch.setenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", str(alt))
        return alt / "x"
    if case == "config_env":
        cfg = base / "cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        return cfg / "plugins" / "x"
    if case == "relative":
        monkeypatch.chdir(root / "src")
        return Path("newdir")
    if case == "trailing_sep":
        return Path(str(root / "src" / "newdir") + os.sep)
    raise AssertionError(case)


@pytest.mark.parametrize("case", _refusal_cases())
def test_refusal_matrix_refuses_and_creates_nothing(
    case: str, world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target_for(case, world, monkeypatch)
    before = _snapshot(world["base"])
    for _ in range(2):  # idempotency: nothing on the first run, nothing on the second
        with pytest.raises(paths.DataDirRefused):
            paths.init_data_dir(target, world["plugin_data"], "0.0.1")
        assert _snapshot(world["base"]) == before


@pytest.mark.parametrize("raw", ["", "   ", "{DATA_FOLDER}", "<folder>", '"quoted"'])
def test_blank_and_placeholder_values_are_refused(raw: str) -> None:
    with pytest.raises(paths.DataDirRefused):
        paths.parse_data_dir_arg(raw)
    if raw.strip():
        with pytest.raises(paths.DataDirRefused):
            paths.check_data_dir(Path(raw), None)


@pytest.mark.parametrize("raw", ["", "   ", "{DATA_FOLDER}"])
def test_cli_init_refuses_blank_and_placeholder(
    raw: str, world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workdir = world["outside"]
    monkeypatch.chdir(workdir)
    assert cli.main(["init", "--data-dir", raw]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert sorted(p.name for p in workdir.iterdir()) == []


def test_non_empty_folder_without_marker_is_refused_and_untouched(
    world: dict[str, Path],
) -> None:
    folder = world["outside"] / "mine"
    folder.mkdir()
    (folder / "resume.md").write_text("keep me", encoding="utf-8")
    before = _snapshot(world["base"])
    with pytest.raises(paths.DataDirRefused):
        paths.init_data_dir(folder, None, "0.0.1")
    assert _snapshot(world["base"]) == before
    assert (folder / "resume.md").read_text(encoding="utf-8") == "keep me"
    assert not (folder / ".gitignore").exists()


def test_existing_file_path_is_refused(world: dict[str, Path]) -> None:
    file = world["outside"] / "afile"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(paths.DataDirRefused):
        paths.init_data_dir(file, None, "0.0.1")


@pytest.mark.parametrize("pre_create", [False, True])
def test_new_or_empty_folder_gets_exactly_three_entries(
    pre_create: bool, world: dict[str, Path]
) -> None:
    folder = world["outside"] / "data"
    if pre_create:
        folder.mkdir()
    outcome = paths.init_data_dir(folder, world["plugin_data"], "0.0.1")
    assert outcome.wrote == EXPECTED_WROTE
    assert (folder / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert (folder / "cache" / "http").is_dir()
    marker = json.loads((folder / paths.MARKER).read_text(encoding="utf-8"))
    assert marker["workspace_schema"] == 1
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        ["cache", ".gitignore", paths.MARKER]
    )


def test_second_init_is_byte_and_mtime_stable(world: dict[str, Path]) -> None:
    folder = world["outside"] / "data"
    paths.init_data_dir(folder, None, "0.0.1")

    def state() -> dict[str, tuple[bytes, int]]:
        return {
            p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()
        }

    before = state()
    again = paths.init_data_dir(folder, None, "0.0.1")
    assert again.wrote == []
    assert state() == before


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_data_folder_is_ignored_by_an_enclosing_git_repo(world: dict[str, Path]) -> None:
    parent = world["outside"] / "repo"
    parent.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=parent, check=True, text=True,
                   encoding="utf-8", capture_output=True)
    child = parent / "data"
    paths.init_data_dir(child, None, "0.0.1")
    (child / "resume.md").write_text("private", encoding="utf-8")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=parent, check=True,
                            text=True, encoding="utf-8", capture_output=True)
    assert status.stdout == ""
    check = subprocess.run(["git", "check-ignore", "-v", "data/resume.md"], cwd=parent,
                           check=True, text=True, encoding="utf-8", capture_output=True)
    assert "data/.gitignore" in check.stdout.replace("\\", "/")


def test_git_work_tree_with_a_remote_gets_a_warning(world: dict[str, Path]) -> None:
    repo = world["outside"] / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://example.invalid/x.git\n',
        encoding="utf-8",
    )
    warnings = paths.check_data_dir(repo / "data", None)
    assert any("remote" in w and "git" in w for w in warnings)


REMOTE_CONFIG = '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://example.invalid/x.git\n'


@pytest.mark.parametrize("kind", ["gitdir-file", "worktree-commondir"])
def test_git_work_tree_whose_dot_git_is_a_file_still_gets_the_remote_warning(
        world: dict[str, Path], kind: str) -> None:
    outside = world["outside"]
    repo = outside / "repo"
    repo.mkdir()
    if kind == "gitdir-file":
        (repo / ".git").write_text("gitdir: ../gitdirs/repo\n", encoding="utf-8")
        gitdir = outside / "gitdirs" / "repo"
        gitdir.mkdir(parents=True)
        (gitdir / "config").write_text(REMOTE_CONFIG, encoding="utf-8")
    else:
        (repo / ".git").write_text("gitdir: ../main/.git/worktrees/repo\n", encoding="utf-8")
        worktree = outside / "main" / ".git" / "worktrees" / "repo"
        worktree.mkdir(parents=True)
        (worktree / "commondir").write_text("../..\n", encoding="utf-8")
        (outside / "main" / ".git" / "config").write_text(REMOTE_CONFIG, encoding="utf-8")
    warnings = paths.check_data_dir(repo / "data", None)
    assert any("remote" in w and "git" in w for w in warnings)


def test_git_work_tree_without_a_remote_has_no_remote_warning(world: dict[str, Path]) -> None:
    repo = world["outside"] / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    assert not any("remote" in w for w in paths.check_data_dir(repo / "data", None))


@pytest.mark.parametrize("name", [
    "Desktop", "Documents", "OneDrive", "onedrive",
    # The other common sync clients
    "Dropbox", "Dropbox (Personal)", "Mobile Documents", "iCloud Drive", "Google Drive",
    "GoogleDrive-someone", "CloudStorage", "Box", "Box Sync",
])
def test_cloud_synced_looking_folder_gets_a_warning(name: str, world: dict[str, Path]) -> None:
    warnings = paths.check_data_dir(world["outside"] / name / "jobs", None)
    assert any("cloud" in w for w in warnings)


@pytest.mark.parametrize("name", ["Boxes", "notes", "storage", "mobile"])
def test_ordinary_folder_names_get_no_cloud_warning(name: str, world: dict[str, Path]) -> None:
    warnings = paths.check_data_dir(world["outside"] / name / "jobs", None)
    assert not any("cloud" in w for w in warnings)


def test_ancestor_of_a_plugin_dir_is_warned_or_refused_never_silent(
    world: dict[str, Path],
) -> None:
    home = world["home"]
    warnings = paths.check_data_dir(home, None)
    assert any("plugin" in w for w in warnings)
    (home / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(paths.DataDirRefused):
        paths.init_data_dir(home, None, "0.0.1")


def test_status_inside_the_plugin_root_exits_1(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(world["base"])
    code = cli.main(["status", "--data-dir", str(world["root"] / "src" / "data")])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert _snapshot(world["base"]) == before


def test_cli_init_with_non_ascii_folder_prints_ascii_only(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = world["outside"] / "données café 日本語"
    code = cli.main(["init", "--data-dir", str(folder)])
    out = capsys.readouterr().out
    assert code == 0
    assert all(ord(ch) < 128 for ch in out)
    assert json.loads(out)["wrote"] == EXPECTED_WROTE
    assert (folder / paths.MARKER).is_file()


def test_init_next_names_the_progress_line_the_start_skill_runs_after_it(
    world: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init", "--data-dir", str(world["outside"] / "fresh")]) == 0
    assert json.loads(capsys.readouterr().out)["next"] == "Run progress to see where you left off."


def test_status_expands_a_tilde_the_same_way_init_does(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The shell leaves "~" alone inside double quotes, so every command must expand it.
    monkeypatch.chdir(world["outside"])
    assert cli.main(["init", "--data-dir", "~/jobs"]) == 0
    init_out = json.loads(capsys.readouterr().out)
    expected = str(world["home"] / "jobs")
    assert init_out["data"]["data_dir"] == expected
    assert (world["home"] / "jobs" / paths.MARKER).is_file()

    assert cli.main(["status", "--data-dir", "~/jobs"]) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["ok"] is True
    assert status_out["data"]["data_dir"] == expected  # the resolved path, not "~/jobs"

    assert paths.find_data_dir("~/jobs", world["outside"]) == world["home"] / "jobs"
    assert not (world["outside"] / "~").exists()


def test_status_and_scan_apply_the_plugin_data_refusal_like_init_does(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A marker folder made inside the plugin data directory by other means must be
    # refused by every command that is given --plugin-data, not only by init.
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("fetch.fetch must not be called")

    monkeypatch.setattr(fetch, "fetch", boom)
    inside = world["plugin_data"] / "jobs"
    paths.init_data_dir(inside, None, "0.0.1")  # no plugin data given: the old, unchecked path
    assert cli.main(["status", "--data-dir", str(inside)]) == 0  # without the flag, unchanged
    capsys.readouterr()

    for command in (["status"], ["scan"]):
        code = cli.main([*command, "--data-dir", str(inside),
                         "--plugin-data", str(world["plugin_data"])])
        out = json.loads(capsys.readouterr().out)
        assert code == 1 and out["ok"] is False, command
        assert "plugin" in out["error"], command


def test_the_marker_timestamp_comes_from_the_frozen_clock(world, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from jobsearch import clock

    monkeypatch.setattr(clock, "now_utc",
                        lambda: datetime(2026, 11, 18, 9, 30, 0, tzinfo=timezone.utc))
    folder = tmp_path / "ws"
    paths.init_data_dir(folder, None, "0.0.1")
    marker = json.loads((folder / paths.MARKER).read_text(encoding="utf-8"))
    assert marker["created_at"] == "2026-11-18T09:30:00Z"


def test_only_the_clock_module_reads_the_wall_clock():
    src = Path(__file__).resolve().parents[1] / "src" / "jobsearch"
    offenders = [p.name for p in src.rglob("*.py")
                 if p.name != "clock.py" and "datetime.now" in p.read_text(encoding="utf-8")]
    assert offenders == []
