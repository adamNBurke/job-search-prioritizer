"""UTF-8 round trips, empty files, and an interrupted atomic write."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from jobsearch import fsio, paths

FOLDER_NAME = "données café 日本語"


def _payload() -> dict[str, str]:
    em_dash = chr(0x2014)
    return {
        "name": "José",
        "dash": f"ops {em_dash} growth",
        "quotes": f"{chr(0x201c)}quoted{chr(0x201d)} and {chr(0x2018)}single{chr(0x2019)}",
        "cjk": "日本語",
    }


def test_json_round_trips_in_a_non_ascii_data_folder(tmp_path: Path) -> None:
    folder = tmp_path / FOLDER_NAME
    paths.init_data_dir(folder, None, "0.0.1")
    target = folder / "profile.json"
    fsio.write_json_atomic(target, _payload())

    assert fsio.read_json(target) == _payload()
    raw = target.read_bytes()
    assert "日本語".encode("utf-8") in raw
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM
    assert b"\r\n" not in raw  # no newline translation on Windows


def test_non_ascii_folder_names_compare_equal_after_nfc(tmp_path: Path) -> None:
    folder = tmp_path / FOLDER_NAME
    paths.init_data_dir(folder, None, "0.0.1")
    listed = [unicodedata.normalize("NFC", name) for name in sorted(os.listdir(tmp_path))]
    assert unicodedata.normalize("NFC", FOLDER_NAME) in listed


def test_empty_round_trip_is_zero_bytes_with_no_bom(tmp_path: Path) -> None:
    target = tmp_path / "empty.txt"
    fsio.write_text_atomic(target, "")
    assert target.read_bytes() == b""
    assert fsio.read_text(target) == ""


def test_text_bytes_are_exact_utf8_with_lf_endings(tmp_path: Path) -> None:
    target = tmp_path / "t.txt"
    text = f"line one\nline two {chr(0x2014)} {chr(0xe9)}\n"
    fsio.write_text_atomic(target, text)
    assert target.read_bytes() == text.encode("utf-8")


def test_interrupted_replace_keeps_the_original_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "keep.txt"
    fsio.write_text_atomic(target, "original")

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated crash at os.replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        fsio.write_text_atomic(target, "replacement")
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]
    assert not list(tmp_path.glob("*.tmp"))


def test_temp_file_is_dot_prefixed_and_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real_replace = os.replace

    def spy(src: object, dst: object) -> None:
        seen.append(str(src))
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", spy)
    fsio.write_text_atomic(tmp_path / "a.txt", "x")
    assert len(seen) == 1
    temp = Path(seen[0])
    assert temp.parent == tmp_path
    assert temp.name.startswith(".a.txt.") and temp.name.endswith(".tmp")


def test_jsonl_lines_are_sorted_keys_non_ascii_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "rows.jsonl"
    fsio.write_jsonl_atomic(target, [{"b": 1, "a": "é"}, {}])
    assert target.read_bytes() == '{"a": "é", "b": 1}\n{}\n'.encode("utf-8")
    fsio.write_jsonl_atomic(target, [])
    assert target.read_bytes() == b""
