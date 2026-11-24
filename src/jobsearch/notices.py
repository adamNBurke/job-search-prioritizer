"""The two short notes the skills may show: a newer release (update-check) and a win invite.

The invite reaches the user as rank's data.notice.

Purpose: tell the user in one line when a newer release of this plugin exists, and
invite them once, after their first ranked list, to share a win or send feedback.
Logic: update_notice reads the user's switch (config/settings.json), then the code-owned state
(config/notices.json). When checks are on and today has not been checked, it stamps today FIRST
and then reads the latest GitHub release through the shared fetch layer under the UPDATE_CHECK
purpose, compares its tag with the installed version as three whole numbers, and returns one
line built from those numbers. An offline, refused or failed check is silent and still counts
as that day's check. The first notice ever adds how to turn the notes off.
Guardrails: the only address contacted is RELEASES_URL (no query string; fetch sends only its
fixed User-Agent and Accept headers). This module imports no HTTP library; network access is
fetch.fetch only. A tag that is not three whole numbers never reaches the notice text.
QA checklist: tests/test_notices.py drives update-check through cli.main (recording transport) and
feedback_invite directly; tests/test_output.py checks the invite in rank's output;
tests/test_netguard.py proves the purpose reaches api.github.com only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from jobsearch import clock, fetch, fsio, paths
from jobsearch.netguard import Purpose, UrlRefused

RELEASES_URL = "https://api.github.com/repos/adamNBurke/job-search-prioritizer/releases/latest"
UPDATE_HOW = "/plugin marketplace update job-search-prioritizer"
DISCUSSIONS_WINS_URL = "https://github.com/adamNBurke/job-search-prioritizer/discussions/categories/wins"
INVITE_TEXT = (
    f"If this list helps, share a win at {DISCUSSIONS_WINS_URL} "
    "or tell Adam what to fix with /job-search-prioritizer:feedback."
)

_VERSION = re.compile(r"v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})")

SETTINGS_SCHEMA = 1
STATE_SCHEMA = 1
OFF_HINT = "To stop these notes, ask to turn off update checks."

Version = tuple[int, int, int]


@dataclass
class NoticeOutcome:
    notice: str = ""
    latest: str | None = None
    checked: bool = False
    warnings: list[str] = field(default_factory=list)
    wrote: list[str] = field(default_factory=list)


def parse_version(text: object) -> Version | None:
    """Three dot-separated whole numbers with an optional leading v, or None."""
    if not isinstance(text, str):
        return None
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def _dotted(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _fetch_latest_tag(folder: Path, transport: fetch.Transport | None) -> object:
    result = fetch.fetch(
        RELEASES_URL,
        purpose=Purpose.UPDATE_CHECK,
        cache_dir=paths.cache_http_dir(folder),
        transport=transport,
    )
    if result.status != 200:
        return None
    body = fetch.decode_json(result)
    return body["tag_name"] if isinstance(body, dict) else None


def _settings_path(folder: Path) -> Path:
    return folder / paths.LAYOUT["settings"]


def _state_path(folder: Path) -> Path:
    return folder / paths.LAYOUT["notices"]


def load_settings(folder: Path) -> tuple[bool, list[str]]:
    """(update_check, warnings). Absent means on; a damaged file means on, with a warning."""
    path = _settings_path(folder)
    if not path.exists():
        return True, []
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and type(data.get("schema_version")) is int:
        value = data.get("update_check", True)
        if isinstance(value, bool):
            return value, []
    return True, [f"{paths.LAYOUT['settings']} could not be read, so update checks stay on. "
                  "Turn them on or off with update-check --turn-on or --turn-off to rewrite it."]


def save_settings(folder: Path, update_check: bool) -> None:
    fsio.write_json_atomic(
        _settings_path(folder),
        {"schema_version": SETTINGS_SCHEMA, "update_check": update_check},
    )


def _fresh_state() -> dict:
    return {"schema_version": STATE_SCHEMA, "update_checked_on": None,
            "update_off_hint_shown": False, "feedback_invite_shown_on": None}


def _valid_state(data: object) -> dict | None:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
        return None
    if data["schema_version"] != STATE_SCHEMA:
        return None
    state = _fresh_state()
    for key in ("update_checked_on", "feedback_invite_shown_on"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return None
        state[key] = value
    hint = data.get("update_off_hint_shown", False)
    if not isinstance(hint, bool):
        return None
    state["update_off_hint_shown"] = hint
    return state


def load_state(folder: Path) -> tuple[dict, list[str]]:
    """(state, warnings). A damaged file is replaced by a fresh one that counts the invite as
    shown, so the invite can never repeat."""
    path = _state_path(folder)
    if not path.exists():
        return _fresh_state(), []
    try:
        data = fsio.read_json(path)
    except (OSError, ValueError):
        data = None
    state = _valid_state(data)
    if state is not None:
        return state, []
    state = _fresh_state()
    state["feedback_invite_shown_on"] = clock.today_iso()
    warning = f"{paths.LAYOUT['notices']} was damaged, so it was replaced with a fresh one."
    try:
        save_state(folder, state)
    except OSError:
        warning = f"{paths.LAYOUT['notices']} was damaged and could not be replaced."
    return state, [warning]


def save_state(folder: Path, state: dict) -> None:
    fsio.write_json_atomic(_state_path(folder), state)


def update_notice(folder: Path, *, transport: fetch.Transport | None = None) -> NoticeOutcome:
    """One line naming a strictly newer release, or an empty notice. Never raises."""
    update_on, warnings = load_settings(folder)
    outcome = NoticeOutcome(warnings=warnings)
    if not update_on:
        return outcome
    state, state_warnings = load_state(folder)
    outcome.warnings.extend(state_warnings)
    today = clock.today_iso()
    if state["update_checked_on"] == today:
        return outcome
    current = parse_version(paths.plugin_version())
    if current is None:  # no installed version means no notice can be built, so no request
        return outcome
    state["update_checked_on"] = today
    try:  # the day is stamped before the request; a stamp that cannot be saved means no request
        save_state(folder, state)
    except OSError:
        return outcome
    outcome.wrote.append(paths.LAYOUT["notices"])
    outcome.checked = True
    try:
        tag = _fetch_latest_tag(folder, transport)
    except (fetch.FetchError, UrlRefused, OSError, ValueError, KeyError, TypeError):
        return outcome
    latest = parse_version(tag)
    if latest is None or latest <= current:
        return outcome
    outcome.latest = _dotted(latest)
    notice = (
        f"Version {_dotted(latest)} of job-search-prioritizer is out "
        f"(you have {_dotted(current)}). To update, run {UPDATE_HOW}."
    )
    if not state["update_off_hint_shown"]:
        state["update_off_hint_shown"] = True
        try:
            save_state(folder, state)
        except OSError:
            pass
        notice = f"{notice} {OFF_HINT}"
    outcome.notice = notice
    return outcome


def _has_ranked_file(folder: Path) -> bool:
    ranked = folder / paths.LAYOUT["ranked"]
    try:
        return ranked.is_dir() and any(ranked.glob("ranked-*.json"))
    except OSError:
        return False


def feedback_invite(folder: Path) -> NoticeOutcome:
    """The win invite, once ever, after the first ranked file exists. Never raises."""
    outcome = NoticeOutcome()
    if not _has_ranked_file(folder):
        return outcome
    state, outcome.warnings = load_state(folder)
    if state["feedback_invite_shown_on"] is not None:
        return outcome
    state["feedback_invite_shown_on"] = clock.today_iso()
    try:  # stamp first: an invite that cannot be recorded is not shown, so it can never repeat
        save_state(folder, state)
    except OSError:
        return outcome
    outcome.wrote.append(paths.LAYOUT["notices"])
    outcome.notice = INVITE_TEXT
    return outcome
