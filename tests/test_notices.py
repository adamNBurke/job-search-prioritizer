"""The once-a-day release note and the once-ever win invite.

Purpose: the start skill shows update-check's one line, and start, scan and review show the invite
that rank returns in data.notice; neither may nag, leak anything about the user, or fail loudly.
Logic: update-check tests drive cli.main the way the skills do, with a recording transport
standing in for the network (fetch._default_transport) and a frozen clock; the invite tests call
notices.feedback_invite directly. The plugin version is patched so a release newer or older than
the installed copy can be staged.
Guardrails: no test touches the network. Fixture data is fictional.
QA checklist: a new rule about the notes gets a named test here; the request-recording tests
keep checking the URL and the header names that leave the machine; tests/test_output.py checks
the invite in rank's output.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobsearch import cli, clock, commands, fetch, notices, paths
from jobsearch.fetch import TransportError, TransportResponse

REAL_PLUGIN_VERSION = paths.plugin_version  # captured before any fixture patches it
DAY_ONE = datetime(2026, 11, 22, 9, 0, 0, tzinfo=timezone.utc)
INSTALLED = "1.0.0"
OFF_HINT = "To stop these notes, ask to turn off update checks."


class Recorder:
    """A transport that answers every request with one canned response and records each."""

    def __init__(self, tag: str | None = "1.1.0", status: int = 200, error: bool = False) -> None:
        self.tag, self.status, self.error = tag, status, error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers, clock=None):  # noqa: ARG002
        self.calls.append((url, dict(headers)))
        if self.error:
            raise TransportError("offline")
        body = json.dumps({"tag_name": self.tag, "html_url": "https://example.test/x"})
        if self.status != 200:
            body = json.dumps({"message": "Not Found"})
        return TransportResponse(self.status, {"content-type": "application/json"},
                                 body.encode("utf-8"))


class Env:
    def __init__(self, folder: Path, transport: Recorder, capsys, monkeypatch) -> None:
        self.folder, self.transport = folder, transport
        self.capsys, self.monkeypatch = capsys, monkeypatch
        self.now = DAY_ONE

    def set_day(self, moment: datetime) -> None:
        self.now = moment

    def run(self, *argv: str) -> dict:
        code = cli.main([*argv, "--data-dir", str(self.folder)])
        printed = self.capsys.readouterr().out
        if not printed.strip():
            return {"ok": False, "error": f"the command line was refused (exit {code})"}
        return json.loads(printed)

    def rank(self, scan_date: str = "2026-11-21") -> None:
        path = self.folder / "ranked" / f"ranked-{scan_date}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "roles": []}), encoding="utf-8")

    def state(self) -> dict:
        return json.loads((self.folder / "config" / "notices.json").read_text(encoding="utf-8"))

    def settings(self) -> dict:
        return json.loads((self.folder / "config" / "settings.json").read_text(encoding="utf-8"))


@pytest.fixture
def make_env(tmp_path, monkeypatch, capsys):
    def build(tag: str | None = "1.1.0", status: int = 200, error: bool = False,
              installed: str = INSTALLED) -> Env:
        transport = Recorder(tag, status, error)
        env = Env(tmp_path / "ws", transport, capsys, monkeypatch)
        monkeypatch.setattr(clock, "now_utc", lambda: env.now)
        monkeypatch.setattr(paths, "plugin_version", lambda: installed)
        monkeypatch.setattr(fetch, "_default_transport", transport)
        assert cli.main(["init", "--data-dir", str(env.folder)]) == 0
        capsys.readouterr()
        return env

    return build


@pytest.mark.parametrize("manifest", [None, "{not json", "[]", '{"name": "job-search-prioritizer"}'],
                         ids=["no file", "not json", "not an object", "no version"])
def test_an_unreadable_manifest_keeps_the_update_check_silent(make_env, tmp_path, monkeypatch,
                                                              manifest: str | None) -> None:
    """A manifest that cannot be used gives version "unknown" and no request, not a failure."""
    env = make_env("1.1.0")
    plugin = tmp_path / "plugin"
    if manifest is not None:
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(paths, "plugin_root", lambda: plugin)
    monkeypatch.setattr(paths, "plugin_version", REAL_PLUGIN_VERSION)
    out = env.run("update-check")
    assert out["ok"] is True
    assert out["data"]["notice"] == "" and out["data"]["current"] == "unknown"
    assert out["data"]["checked"] is False
    assert env.transport.calls == []


# --- Task 1: the tracer -------------------------------------------------------------------


def test_a_newer_release_gives_one_notice_line(make_env) -> None:
    env = make_env("1.1.0")
    out = env.run("update-check")
    assert out["ok"] is True
    notice = out["data"]["notice"]
    assert "Version 1.1.0" in notice and "you have 1.0.0" in notice
    assert "\n" not in notice
    assert out["data"]["latest"] == "1.1.0" and out["data"]["current"] == "1.0.0"
    assert out["data"]["checked"] is True
    assert len(env.transport.calls) == 1
    url, headers = env.transport.calls[0]
    assert url == notices.RELEASES_URL
    assert "?" not in url
    assert set(headers) == {"User-Agent", "Accept"}


@pytest.mark.parametrize("tag", ["1.0.0", "0.9.9", "v1.0.0", "1.0", "1.0.0-beta", "latest",
                                 "1.0.0.1", "1.1.0\n", " 1.1.0", "\u0661.\u0662.\u0663", "", None])
def test_equal_or_older_or_unparseable_gives_no_notice(make_env, tag) -> None:
    env = make_env(tag)
    out = env.run("update-check")
    assert out["ok"] is True
    assert out["data"]["notice"] == ""


def test_a_leading_v_and_numeric_ordering(make_env) -> None:
    env = make_env("v1.10.0", installed="1.9.0")
    out = env.run("update-check")
    assert "Version 1.10.0" in out["data"]["notice"]
    assert "you have 1.9.0" in out["data"]["notice"]


def test_a_hostile_tag_never_reaches_the_notice(make_env) -> None:
    env = make_env("1.1.0 <script>run this</script>")
    out = env.run("update-check")
    assert out["data"]["notice"] == ""


def test_parse_version() -> None:
    assert notices.parse_version("1.2.3") == (1, 2, 3)
    assert notices.parse_version("v1.2.3") == (1, 2, 3)
    assert notices.parse_version("1.2") is None
    assert notices.parse_version("1.2.3-rc1") is None
    assert notices.parse_version("1.2.x") is None
    assert notices.parse_version("1" * 40 + ".0.0") is None


@pytest.mark.parametrize("kind", ["not_found", "offline"])
def test_a_404_or_transport_failure_is_silent(make_env, kind) -> None:
    env = make_env(status=404 if kind == "not_found" else 200, error=kind == "offline")
    out = env.run("update-check")
    assert out["ok"] is True
    assert out["data"]["notice"] == ""
    assert out["error"] is None


def test_an_invalid_json_body_is_silent(make_env, monkeypatch) -> None:
    env = make_env()

    def garbled(url, headers, clock=None):  # noqa: ARG001
        return TransportResponse(200, {}, b"<html>not json</html>")

    monkeypatch.setattr(fetch, "_default_transport", garbled)
    assert env.run("update-check")["data"]["notice"] == ""


# --- Task 2: once a day, the off switch, the first-time hint -------------------------------


def test_one_check_a_day(make_env) -> None:
    env = make_env()
    first = env.run("update-check")
    second = env.run("update-check")
    assert first["data"]["notice"] != ""
    assert second["data"]["notice"] == "" and second["data"]["checked"] is False
    assert len(env.transport.calls) == 1
    env.set_day(DAY_ONE + timedelta(days=1))
    third = env.run("update-check")
    assert len(env.transport.calls) == 2
    assert third["data"]["notice"] != ""


def test_the_day_is_stamped_before_the_request(make_env) -> None:
    env = make_env(error=True)
    out = env.run("update-check")
    assert out["ok"] is True and out["data"]["notice"] == ""
    assert env.state()["update_checked_on"] == "2026-11-22"
    again = env.run("update-check")
    assert again["data"]["notice"] == ""
    assert len(env.transport.calls) == 1


def test_turn_off_and_on(make_env) -> None:
    env = make_env()
    off = env.run("update-check", "--turn-off")
    assert off["ok"] is True and off["data"]["update_check"] is False
    assert "config/settings.json" in off["wrote"]
    assert env.settings() == {"schema_version": 1, "update_check": False}
    silent = env.run("update-check")
    assert silent["data"]["notice"] == "" and silent["data"]["checked"] is False
    assert env.transport.calls == []
    assert not (env.folder / "config" / "notices.json").exists()  # off writes no stamp
    on = env.run("update-check", "--turn-on")
    assert on["data"]["update_check"] is True
    assert env.settings() == {"schema_version": 1, "update_check": True}
    assert env.run("update-check")["data"]["notice"] != ""
    assert len(env.transport.calls) == 1


def test_both_switch_flags_together_are_refused(make_env) -> None:
    env = make_env()
    out = env.run("update-check", "--turn-off", "--turn-on")
    assert out["ok"] is False
    assert not (env.folder / "config" / "settings.json").exists()


def test_the_first_notice_names_the_off_switch_once(make_env) -> None:
    env = make_env()
    first = env.run("update-check")["data"]["notice"]
    assert first.endswith(OFF_HINT)
    assert env.state()["update_off_hint_shown"] is True
    env.set_day(DAY_ONE + timedelta(days=1))
    later = env.run("update-check")["data"]["notice"]
    assert later != "" and OFF_HINT not in later


def test_a_silent_check_does_not_use_up_the_hint(make_env) -> None:
    env = make_env(error=True)
    env.run("update-check")
    assert env.state()["update_off_hint_shown"] is False


def test_a_damaged_settings_file_counts_as_on_with_a_warning(make_env) -> None:
    env = make_env()
    (env.folder / "config").mkdir(exist_ok=True)
    (env.folder / "config" / "settings.json").write_text("{ not json", encoding="utf-8")
    out = env.run("update-check")
    assert out["data"]["notice"] != ""
    assert any("config/settings.json" in w for w in out["warnings"])
    env.run("update-check", "--turn-off")
    assert env.settings() == {"schema_version": 1, "update_check": False}


def test_a_damaged_state_file_is_replaced_and_counts_the_invite_as_shown(make_env) -> None:
    env = make_env()
    (env.folder / "config").mkdir(exist_ok=True)
    (env.folder / "config" / "notices.json").write_text("[1, 2", encoding="utf-8")
    out = env.run("update-check")
    assert sum("config/notices.json" in w for w in out["warnings"]) == 1
    assert out["data"]["notice"] != ""
    assert env.state()["feedback_invite_shown_on"] == "2026-11-22"


def test_a_state_file_with_the_wrong_field_types_is_damaged(make_env) -> None:
    env = make_env()
    (env.folder / "config").mkdir(exist_ok=True)
    (env.folder / "config" / "notices.json").write_text(
        json.dumps({"schema_version": 1, "update_off_hint_shown": "yes"}), encoding="utf-8")
    out = env.run("update-check")
    assert any("config/notices.json" in w for w in out["warnings"])
    assert env.state()["update_off_hint_shown"] is True  # replaced, then the first notice set it


def test_the_notice_files_are_written_without_temp_leftovers(make_env) -> None:
    env = make_env()
    env.run("update-check")
    env.run("update-check", "--turn-off")
    leftovers = [p.name for p in (env.folder / "config").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    raw = (env.folder / "config" / "notices.json").read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")


# --- Task 3: the win invite ----------------------------------------------------------------


def test_the_invite_has_no_command_of_its_own() -> None:
    assert "feedback-invite" not in {m.NAME for m in commands.discover()}


def test_no_ranked_file_means_no_invite_and_no_write(make_env) -> None:
    env = make_env()
    out = notices.feedback_invite(env.folder)
    assert out.notice == "" and out.wrote == []
    assert not (env.folder / "config" / "notices.json").exists()


def test_the_invite_shows_once(make_env) -> None:
    env = make_env()
    env.rank()
    first = notices.feedback_invite(env.folder)
    notice = first.notice
    assert "\n" not in notice and notice != ""
    assert "discussions/categories/wins" in notice
    assert "/job-search-prioritizer:feedback" in notice
    assert first.wrote == ["config/notices.json"]
    assert env.state()["feedback_invite_shown_on"] == "2026-11-22"
    assert notices.feedback_invite(env.folder).notice == ""
    env.rank("2026-11-22")
    again = notices.feedback_invite(env.folder)
    assert again.notice == "" and again.wrote == []


def test_both_notices_due_print_their_own_lines(make_env) -> None:
    env = make_env()
    env.rank()
    update = env.run("update-check")["data"]["notice"]
    invite = notices.feedback_invite(env.folder).notice
    assert "Version 1.1.0" in update and "wins" not in update
    assert "wins" in invite and "Version" not in invite
    state = env.state()
    assert state["update_off_hint_shown"] is True
    assert state["feedback_invite_shown_on"] == "2026-11-22"
    assert state["update_checked_on"] == "2026-11-22"


def test_the_invite_leaves_the_update_stamp_alone(make_env) -> None:
    env = make_env()
    env.run("update-check")
    before = env.state()
    env.rank()
    notices.feedback_invite(env.folder)
    after = env.state()
    assert after["update_checked_on"] == before["update_checked_on"]
    assert after["update_off_hint_shown"] == before["update_off_hint_shown"]


def test_the_invite_makes_no_network_call(make_env) -> None:
    env = make_env()
    env.rank()
    notices.feedback_invite(env.folder)
    assert env.transport.calls == []


def test_a_damaged_state_file_never_lets_the_invite_repeat(make_env) -> None:
    env = make_env()
    (env.folder / "config").mkdir(exist_ok=True)
    (env.folder / "config" / "notices.json").write_text("[1, 2", encoding="utf-8")
    env.run("update-check")
    env.rank()
    assert notices.feedback_invite(env.folder).notice == ""
