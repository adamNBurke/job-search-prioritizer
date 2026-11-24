"""Every text-mode file and process call names UTF-8 explicitly.

Purpose: Python 3.11 to 3.14 uses the ANSI code page for text I/O on Windows, so a call that
omits the encoding works on a developer's Mac and corrupts a stranger's data on Windows.
Ruff's PLW1514 catches the plain open() case; this AST check closes the measured gaps (io.open,
codecs.open, subprocess text mode, and a wrong literal such as "utf8" or "cp1252").
Logic: walk every Call node. A file call is text-mode when its mode is absent (for open-like
calls whose default is text) or a literal without "b"; it must then pass encoding as the literal
"utf-8" or "utf-8-sig". subprocess calls with text=True or universal_newlines=True must pass
encoding="utf-8". Violations are (path, line, rule) tuples, sorted.
Guardrails: tests/fixtures/utf8_bad/bad_calls.py marks every bad line BAD and every good line OK;
the checker must flag exactly the BAD lines. Calls using *args or **kwargs are reported as
unverifiable rather than passed. This is static analysis: a call built at runtime stays invisible.
QA checklist: a new violation in the real tree means fix the call, never widen the checker.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BAD_FILE = Path(__file__).resolve().parent / "fixtures" / "utf8_bad" / "bad_calls.py"
SCAN_DIRS = ("src", "scripts", "tools", "tests")
ALLOWED_ENCODINGS = {"utf-8", "utf-8-sig"}

Violation = tuple[str, int, str]

# name -> (mode positional index, encoding positional index, text mode when mode is absent)
_FILE_CALLS = {
    "open": (1, 3, True),  # builtin open and io.open
    "fdopen": (1, 3, True),
    "NamedTemporaryFile": (0, None, False),  # default "w+b"
    "TemporaryFile": (0, None, False),
    "SpooledTemporaryFile": (1, None, False),
}
_METHOD_OPEN = (0, 2, True)  # Path.open(mode, buffering, encoding)
_CODECS_OPEN = (1, 2, True)  # codecs.open(filename, mode, encoding)
_COMPRESSED_OPEN = (1, 3, False)  # gzip.open(filename, mode, compresslevel, encoding)
_BINARY_FIRST = {"gzip", "bz2", "lzma"}  # default "rb": text only when the mode says "t"
_SKIP_RECEIVERS = {"tarfile", "zipfile", "os", "webbrowser", "fsio"}
_SUBPROCESS = {"run", "check_output", "check_call", "call", "Popen"}
_TEXT_FLAGS = ("text", "universal_newlines")


def _receiver(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name):
            return value.id
        if isinstance(value, ast.Attribute):
            return value.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _argument(call: ast.Call, index: int | None, name: str) -> ast.expr | None:
    found = _keyword(call, name)
    if found is not None:
        return found
    if index is not None and index < len(call.args):
        return call.args[index]
    return None


def _is_literal(node: ast.expr | None, values: set[str]) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in values


def _text_mode(mode: ast.expr | None, default_text: bool, binary_default: bool) -> bool:
    if mode is None:
        return default_text and not binary_default
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        if binary_default:
            return "t" in mode.value
        return "b" not in mode.value
    return True  # a mode built at runtime is assumed to be text


def _file_rule(call: ast.Call) -> str | None:
    func = call.func
    receiver = _receiver(func)
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if receiver in _SKIP_RECEIVERS and name in {"open", "fdopen", "read_text", "write_text"}:
        return None

    if name in {"read_text", "write_text"}:
        if not isinstance(func, ast.Attribute):
            return None
        index = 0 if name == "read_text" else 1
        return None if _is_literal(_argument(call, index, "encoding"), ALLOWED_ENCODINGS) else "text-encoding"

    if name == "open" and isinstance(func, ast.Attribute) and receiver == "codecs":
        mode_index, enc_index, default_text = _CODECS_OPEN
    elif name == "open" and receiver in _BINARY_FIRST:
        mode_index, enc_index, default_text = _COMPRESSED_OPEN
    elif name == "open" and isinstance(func, ast.Attribute) and receiver not in {"io"}:
        mode_index, enc_index, default_text = _METHOD_OPEN
    elif name in _FILE_CALLS:
        mode_index, enc_index, default_text = _FILE_CALLS[name]
    else:
        return None

    binary_default = receiver in _BINARY_FIRST
    if not _text_mode(_argument(call, mode_index, "mode"), default_text, binary_default):
        return None
    encoding = _argument(call, enc_index, "encoding")
    return None if _is_literal(encoding, ALLOWED_ENCODINGS) else "text-encoding"


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _subprocess_rule(call: ast.Call) -> str | None:
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS and _receiver(func) == "subprocess"):
        return None
    if not any(_is_true(_keyword(call, flag)) for flag in _TEXT_FLAGS):
        return None
    return None if _is_literal(_keyword(call, "encoding"), {"utf-8"}) else "subprocess-encoding"


def check_source(path: str | Path, source: str) -> list[Violation]:
    """Every call in source that reads or writes text without an explicit UTF-8 encoding."""
    found: list[Violation] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        rule = _file_rule(node) or _subprocess_rule(node)
        if rule is None:
            continue
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            rule = "unverifiable-call"
        found.append((str(path), node.lineno, rule))
    return sorted(found)


def scan_tree(root: Path) -> list[Violation]:
    found: list[Violation] = []
    fixtures = root / "tests" / "fixtures"
    for directory in SCAN_DIRS:
        for file in sorted((root / directory).rglob("*.py")):
            if fixtures in file.parents or "__pycache__" in file.parts:
                continue
            found.extend(check_source(file.relative_to(root).as_posix(), file.read_text(encoding="utf-8")))
    return sorted(found)


def _marked_lines(marker: str) -> list[int]:
    lines = BAD_FILE.read_text(encoding="utf-8").splitlines()
    return [n for n, line in enumerate(lines, start=1) if re.search(rf"#\s*{marker}\b", line)]


def test_the_real_tree_has_no_unmarked_text_calls() -> None:
    assert scan_tree(ROOT) == []


def test_every_bad_line_is_flagged_and_no_ok_line_is_flagged() -> None:
    bad = _marked_lines("BAD")
    assert len(bad) >= 10
    violations = check_source(BAD_FILE, BAD_FILE.read_text(encoding="utf-8"))
    assert [line for _, line, _ in violations] == bad
    assert not set(_marked_lines("OK")) & {line for _, line, _ in violations}


@pytest.mark.parametrize("literal", ["utf8", "UTF-8", "utf_8", "cp1252", "latin-1", "ascii"])
def test_only_the_two_exact_literals_are_accepted(literal: str) -> None:
    source = f'open("a.txt", encoding="{literal}")\n'
    assert [rule for _, _, rule in check_source("x.py", source)] == ["text-encoding"]
    for accepted in ("utf-8", "utf-8-sig"):
        assert check_source("x.py", f'open("a.txt", encoding="{accepted}")\n') == []


def test_an_encoding_built_at_runtime_is_not_trusted() -> None:
    assert check_source("x.py", 'open("a.txt", encoding=ENC)\n') != []


def test_positional_encoding_is_read_from_the_right_slot() -> None:
    assert check_source("x.py", 'open("a.txt", "r", -1, "utf-8")\n') == []
    assert check_source("x.py", 'open("a.txt", "r", -1, "cp1252")\n') != []


def test_star_arguments_are_reported_as_unverifiable() -> None:
    violations = check_source("x.py", "open(*args, **kwargs)\n")
    assert [rule for _, _, rule in violations] == ["unverifiable-call"]


def test_binary_calls_and_the_fsio_helper_are_not_flagged() -> None:
    source = (
        'open("a", "rb")\nopen("a", "wb")\ngzip.open("a")\ngzip.open("a", "rt")\n'
        "tarfile.open(fileobj=f)\nfsio.read_text(p)\nsubprocess.run(x, capture_output=True)\n"
    )
    assert [line for _, line, _ in check_source("x.py", source)] == [4]  # gzip text mode only


def test_violations_come_back_sorted() -> None:
    source = 'open("b")\nopen("a")\n'
    violations = check_source("x.py", source)
    assert violations == sorted(violations)
