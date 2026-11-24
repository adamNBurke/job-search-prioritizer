"""The one place the program reads the wall clock.

Purpose: scan dates, role dates and tests all need "now", and a test must be able to freeze it.
Logic: now_utc returns an aware UTC datetime; iso_z and today_iso format it. today_iso calls
now_utc through the module namespace, so a test that monkeypatches clock.now_utc moves both.
Guardrails: always UTC, never local time, so a scan date is the same on every machine.
QA checklist: new code that needs the date calls clock.today_iso(), never datetime.now().
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(moment: datetime) -> str:
    """UTC ISO 8601 with a Z suffix, no fractional seconds."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return now_utc().astimezone(timezone.utc).date().isoformat()
