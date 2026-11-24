"""Atomic, UTF-8 file helpers. Every text read and write names its encoding.

Purpose: a crash or a second process must never leave a half-written file, and bytes must be
identical on every OS.
Logic: write to a dot-prefixed temp file in the same directory, fsync, then os.replace. The
temp file is removed on any failure, so the original file stays intact. write_jsonl_atomic is
that write for every .jsonl file the tool rewrites, one sorted-key JSON object per line.
Guardrails: text is UTF-8 with LF line endings and no BOM. CSV is the one exception to the
plain "utf-8" rule: CSV files are READ with "utf-8-sig" (Excel adds a BOM) and WRITTEN with
utf-8-sig (a BOM for Excel) and newline="" by the code that owns the CSV. read_user_file reads a
user's file only when it resolves inside the data folder, with the byte cap checked before the read.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import IO

from jobsearch.errors import UserError

_REPLACE_TRIES = 3
_REPLACE_PAUSE_SECONDS = 0.1


def _temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")


def _replace(src: Path, dst: Path) -> None:
    """os.replace; on Windows retry briefly because antivirus or an indexer can hold the file."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_PAUSE_SECONDS)


def _finish(temp: Path, handle: IO, path: Path) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()
    _replace(temp, path)


def _discard(temp: Path) -> None:
    try:
        os.unlink(temp)
    except OSError:
        pass


def write_text_if_changed(path: Path, text: str) -> bool:
    """write_text_atomic, skipped when the file already holds exactly these bytes. True when the
    file was written, so a caller lists it as written only then."""
    if path.is_file() and path.read_bytes() == text.encode("utf-8"):
        return False
    write_text_atomic(path, text)
    return True


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_path(path)
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            _finish(temp, handle, path)
    except BaseException:
        _discard(temp)
        raise


def write_text_atomic(path: Path, text: str) -> None:
    """UTF-8, LF line endings, no BOM, so bytes are identical on every OS."""
    write_bytes_atomic(path, text.encode("utf-8"))


def write_json_atomic(path: Path, obj: object) -> None:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    write_text_atomic(path, text)


def write_jsonl_atomic(path: Path, records: list[dict]) -> None:
    """One JSON object per line, sorted keys, non-ASCII kept, LF after each; empty list, empty file."""
    write_text_atomic(path, "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_json(path: Path) -> object:
    return json.loads(read_text(path))


def read_user_file(path: Path, folder: Path, max_bytes: int, what: str) -> str:
    """Text of a user file: inside the data folder, under the byte cap, LF line endings."""
    target = path.expanduser().resolve()
    if not target.is_relative_to(folder.resolve()):
        raise UserError(f"{what} must be inside your data folder.")
    try:
        if target.stat().st_size > max_bytes:
            raise UserError(f"{what} is too long. Keep it under {max_bytes} bytes.")
        text = target.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise UserError(f"{what} could not be read as text.") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")
