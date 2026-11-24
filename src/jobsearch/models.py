"""File contracts: the normalized Posting and the Result envelope printed on stdout.

docs/contracts.md describes the same field names; tests/test_contracts.py fails when the two
drift. Posting is frozen at schema_version 1 from the live shapes recorded in
docs/ats-live-vs-docs.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields

POSTING_SCHEMA_VERSION = 1

ATS_IDS = ("greenhouse", "lever", "lever_eu", "ashby")
WORK_MODES = ("remote", "hybrid", "onsite", "unknown")
SOURCES = ("ats", "pasted")
COMP_INTERVALS = ("year", "month", "week", "day", "hour", "other")
COMP_SOURCES = ("structured", "parsed_html")

_UTC_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


@dataclass
class Result:
    ok: bool
    command: str
    data: dict[str, object] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    wrote: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        """ASCII-only, sorted keys, one compact line: identical inputs give identical stdout."""
        return json.dumps(asdict(self), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def errors_result(command: str, errors: list[str], said: str, next: str,
                  warnings: list[str]) -> Result:
    """The failure for a file with problems: the first 10 listed, all of them counted."""
    return Result(ok=False, command=command, data={"errors": errors[:10]},
                  counts={"errors": len(errors)}, warnings=warnings,
                  error=said + " ".join(errors[:10]), next=next)


def make_role_id(ats: str, board_slug: str, posting_id: str) -> str:
    """Stable id "ats:board:posting" with exactly three ":"-separated parts."""
    if ats not in ATS_IDS:
        raise ValueError(f"ats must be one of {ATS_IDS}, got {ats!r}")
    for name, part in (("board_slug", board_slug), ("posting_id", posting_id)):
        if not isinstance(part, str) or not part or ":" in part:
            raise ValueError(f"{name} must be a non-empty string without ':', got {part!r}")
    return f"{ats}:{board_slug}:{posting_id}"


def make_pasted_role_id(url_or_text: str) -> str:
    """"pasted:-:" plus 12 hex chars of sha256 over the case-folded, trimmed input."""
    normalized = " ".join(url_or_text.casefold().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"pasted:-:{digest[:12]}"


def _bad(key: str, message: str) -> ValueError:
    return ValueError(f"Posting field {key!r}: {message}")


@dataclass(frozen=True)
class Posting:
    """One normalized open role. Field order is the contract (see docs/contracts.md)."""

    schema_version: int
    role_id: str
    source: str
    ats: str | None
    board_slug: str | None
    posting_id: str
    company: str
    title: str
    team: str | None
    locations: list[str]
    work_mode_text: str | None
    work_mode: str
    posted: str | None
    url_apply: str
    url_posting: str | None
    description_text: str
    comp_min: float | None
    comp_max: float | None
    comp_currency: str | None
    comp_interval: str | None
    comp_source: str | None
    fetched_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Posting:
        names = [f.name for f in fields(cls)]
        unknown = sorted(set(data) - set(names))
        if unknown:
            raise ValueError(f"Posting has unknown field(s): {', '.join(unknown)}")
        missing = [n for n in names if n not in data]
        if missing:
            raise ValueError(f"Posting is missing field(s): {', '.join(missing)}")

        if data["schema_version"] != POSTING_SCHEMA_VERSION:
            raise _bad("schema_version", f"expected {POSTING_SCHEMA_VERSION}")
        for key in ("role_id", "posting_id", "company", "title", "url_apply",
                    "description_text", "fetched_at"):
            if not isinstance(data[key], str):
                raise _bad(key, "must be a string")
        for key in ("board_slug", "team", "work_mode_text", "url_posting", "comp_currency"):
            if data[key] is not None and not isinstance(data[key], str):
                raise _bad(key, "must be a string or null")
        if data["source"] not in SOURCES:
            raise _bad("source", f"must be one of {SOURCES}")
        if data["ats"] is not None and data["ats"] not in ATS_IDS:
            raise _bad("ats", f"must be one of {ATS_IDS} or null")
        if (data["source"] == "ats") != (data["ats"] is not None):
            raise _bad("ats", "is set exactly when source is 'ats'")
        if data["work_mode"] not in WORK_MODES:
            raise _bad("work_mode", f"must be one of {WORK_MODES}")
        if data["comp_interval"] is not None and data["comp_interval"] not in COMP_INTERVALS:
            raise _bad("comp_interval", f"must be one of {COMP_INTERVALS} or null")
        if data["comp_source"] is not None and data["comp_source"] not in COMP_SOURCES:
            raise _bad("comp_source", f"must be one of {COMP_SOURCES} or null")
        locations = data["locations"]
        if not isinstance(locations, list) or not all(isinstance(x, str) for x in locations):
            raise _bad("locations", "must be a list of strings")
        for key in ("comp_min", "comp_max"):
            value = data[key]
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))):
                raise _bad(key, "must be a number or null")
            if isinstance(value, float) and not math.isfinite(value):
                raise _bad(key, "must be a finite number or null")
        for key in ("posted", "fetched_at"):
            value = data[key]
            if value is not None and not (isinstance(value, str) and _UTC_ISO.fullmatch(value)):
                raise _bad(key, "must be UTC ISO 8601 with a Z suffix")

        kwargs = {name: data[name] for name in names}
        kwargs["locations"] = list(locations)
        for key in ("comp_min", "comp_max"):
            if kwargs[key] is not None:
                kwargs[key] = float(kwargs[key])
        return cls(**kwargs)  # type: ignore[arg-type]
