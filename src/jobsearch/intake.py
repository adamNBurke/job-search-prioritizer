"""Resume import: turn the user's resume file into profile/resume.md and keep the original.

Purpose: the interview and the skill check read one plain Markdown copy of the resume, whatever
file the user starts from (PDF, DOCX or Markdown are the only accepted formats).
Logic: import_resume dispatches on the lowercased file extension to a reader in _READERS (bytes
in, ReadResult out): Markdown and plain text pass through, a PDF is read page by page with pypdf
and a DOCX paragraph by paragraph (with tables, nested tables, content controls and text boxes, in
document order) with python-docx, by style name. Headings and bullets are rebuilt as Markdown, each heading on its own line, and the source
order is kept. The original bytes are copied to
profile/source/<file name> first, then the cleaned Markdown (UTF-8, LF line endings, byte order
mark dropped, one trailing newline) is written to profile/resume.md. Both writes go through fsio.
Guardrails: an unsupported extension (a .doc file gets its own hint; a LinkedIn export is just
another unsupported type), a missing file, a zero-byte file, a file over 10 MB, a file that
is not UTF-8 text, an encrypted PDF, a PDF or DOCX the parser cannot open, and a resume with no
text are plain UserError sentences naming the next step; nothing is written when any of them
happens. A PDF with under LOW_TEXT_CHARS_PER_PAGE characters of text per page (scanned or
designed) is imported with a warning telling the agent to read the PDF directly instead; a DOCX
that yields under half the text its main document holds gets a warning too. The file name is reduced to its last path part, so a name can never place the copy
outside profile/source. Nothing here reads the network, and nothing is inferred from the text.
QA checklist: tests/test_intake.py builds every PDF and DOCX at test time and drives this through
the profile-import command; a new reader needs a test for its low-text and unreadable cases.
"""

from __future__ import annotations

import functools
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PasswordType, PdfReader
from pypdf.errors import DependencyError

from jobsearch import fsio, paths
from jobsearch.errors import UserError

ACCEPTED_FORMATS = "PDF, DOCX or Markdown"
LOW_TEXT_CHARS_PER_PAGE = 200
MAX_RESUME_BYTES = 10 * 1024 * 1024

LOW_TEXT_WARNING = (
    "Little text came out of this PDF (it may be scanned or designed). "
    "Read the PDF directly and write profile/resume.md from what you see."
)
_NO_TEXT_PDF = (
    "No text came out of this PDF (it may be scanned or designed). "
    "Read the PDF directly and write profile/resume.md from what you see."
)
_UNSUPPORTED_ENCRYPTION_PDF = (
    "This PDF is encrypted with a method this tool cannot open. Export it as an "
    "unencrypted PDF, save the resume as DOCX, or paste it as Markdown.")
_LOCKED_PDF = ("This PDF is password-protected. Export a copy without a password and try "
               "again.")

_HEADING = re.compile(r"#{1,6}\s+\S")
_BULLET = re.compile(r"[-*+]\s+\S")


@dataclass(frozen=True)
class ImportOutcome:
    format: str
    chars: int
    pages: int
    headings: int
    bullets: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReadResult:
    format: str
    text: str
    pages: int
    warnings: list[str] = field(default_factory=list)


def _read_markdown(raw: bytes, label: str) -> ReadResult:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UserError(
            "That resume file is not UTF-8 text. Save it as UTF-8 Markdown (.md) or text, "
            f"or give me a {ACCEPTED_FORMATS} file."
        ) from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")
    return ReadResult(format=label, text=cleaned + "\n" if cleaned else "", pages=1)


# Section names a resume uses; a line equal to one of these (any case, optional colon) is a heading.
_SECTION_NAMES = frozenset({
    "summary", "profile", "experience", "work experience", "employment", "education", "skills",
    "projects", "certifications", "awards", "publications", "volunteer",
})
_BULLET_GLYPHS = "\u2022\u25cf\u25e6\u25aa\u25ab\u25a0\u25a1\u2023\u2043\u2219\u00b7\u25cb"
# shortcut: a 5+ letter skill alone on a line (EXCEL, PYTHON) still reads as a heading; revisit if a real resume shows it.
_HEADING_MIN, _HEADING_MAX = 5, 40


def _is_heading(line: str, first: bool) -> bool:
    """A known section name, or a short all-caps line (never the first line: that is a name)."""
    if line.rstrip(":").strip().casefold() in _SECTION_NAMES:
        return True
    letters = sum(1 for c in line if c.isalpha())
    return (not first and _HEADING_MIN <= len(line) <= _HEADING_MAX and letters >= _HEADING_MIN
            and line.upper() == line and not any(c.isdigit() or c == "@" for c in line)
            and not line.endswith((".", ",", ";")))


def _pdf_markdown(lines: list[str]) -> str:
    """Rebuild Markdown from text lines: headings on their own line, bullet glyphs as "- "."""
    out: list[str] = []
    pending_glyph = False
    seen_text = False
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue
        if pending_glyph:
            line, pending_glyph = f"{_BULLET_GLYPHS[0]} {line}", False
        if line in _BULLET_GLYPHS:
            pending_glyph = True
            continue
        if line[0] in _BULLET_GLYPHS:
            body = line[1:].strip()
            out.append(f"- {body}")
        elif _is_heading(line, first=not seen_text):
            if out:
                out.append("")
            out.append(f"## {line.rstrip(':').strip().title()}")
        else:
            out.append(line)
        seen_text = True
    return "\n".join(out) + "\n" if out else ""


def _unreadable(kind: str) -> UserError:
    return UserError(
        f"I could not read that {kind} file. Check that it opens normally, or save the resume "
        f"as a PDF or Markdown (.md) file and try again."
    )


def _pdf_page_lines(raw: bytes) -> tuple[list[list[str]], int]:
    """Text lines for each page, and the page count. UserError for a locked or broken PDF."""
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("") != PasswordType.NOT_DECRYPTED
            except DependencyError:  # AES needs a package this build does not ship
                raise UserError(_UNSUPPORTED_ENCRYPTION_PDF) from None
            except Exception:
                unlocked = False
            if not unlocked:
                raise UserError(_LOCKED_PDF)
        pages = [(page.extract_text() or "").split("\n") for page in reader.pages]
    except UserError:
        raise
    except Exception:  # pypdf raises many types for a damaged file; none may escape raw
        raise _unreadable("PDF") from None
    return pages, len(pages)


def _read_pdf(raw: bytes) -> ReadResult:
    pages, count = _pdf_page_lines(raw)
    chars = sum(len(" ".join(" ".join(lines).split())) for lines in pages)
    if chars == 0:
        raise UserError(_NO_TEXT_PDF)
    warnings = [LOW_TEXT_WARNING] if chars / count < LOW_TEXT_CHARS_PER_PAGE else []
    text = _pdf_markdown([line for lines in pages for line in lines])
    return ReadResult(format="pdf", text=text, pages=count, warnings=warnings)


def _docx_heading_prefix(style: str) -> str | None:
    if style in ("Title", "Heading 1"):
        return "# "
    if style.startswith("Heading"):
        return "## "
    return None


_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_DOCX_LOW_YIELD_RATIO = 0.5
DOCX_LOW_TEXT_WARNING = (
    "Less text came out of this DOCX than the file holds (some content sits in a structure this "
    "reader skips). Check profile/resume.md against the original resume and add anything "
    "missing."
)


def _in_fallback(element) -> bool:
    """True inside mc:Fallback, the legacy copy of text Word already stored in mc:Choice."""
    return any(ancestor.tag == _MC_FALLBACK for ancestor in element.iterancestors())


def _docx_blocks(container, document):
    """Paragraphs and tables in document order, looking through content controls and
    yielding the paragraphs held in text boxes right after the paragraph that anchors them."""
    for child in container.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
            for box in child.iter(qn("w:txbxContent")):
                if not _in_fallback(box):
                    yield from _docx_blocks(box, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)
        elif child.tag == qn("w:sdt"):
            for content in child.iterchildren(qn("w:sdtContent")):
                yield from _docx_blocks(content, document)


def _cell_text_and_tables(cell, document) -> tuple[str, list[Table]]:
    parts: list[str] = []
    nested: list[Table] = []
    for block in _docx_blocks(cell._tc, document):
        if isinstance(block, Table):
            nested.append(block)
        else:
            parts.append(block.text)
    return " ".join(" ".join(parts).split()), nested


def _table_rows(table: Table, document) -> list[str]:
    rows = []
    for row in table.rows:
        cells, seen, nested = [], set(), []
        for cell in row.cells:
            if id(cell._tc) in seen:  # a merged cell is returned once per column it spans
                continue
            seen.add(id(cell._tc))
            text, inner = _cell_text_and_tables(cell, document)
            nested.extend(inner)
            if text:
                cells.append(text)
        if cells:
            rows.append(" | ".join(cells))
        for inner_table in nested:
            rows.extend(_table_rows(inner_table, document))
    return rows


def _docx_markdown(document: docx.document.Document) -> str:
    out: list[str] = []
    for block in _docx_blocks(document.element.body, document):
        if isinstance(block, Table):
            out.extend(_table_rows(block, document))
            continue
        text = " ".join(block.text.split())
        if not text:
            continue
        style = block.style.name if block.style is not None and block.style.name else ""
        prefix = _docx_heading_prefix(style)
        if prefix is not None:
            if out:
                out.append("")
            out.append(f"{prefix}{text}")
        elif style.startswith(("List Bullet", "List Number", "List Paragraph")):
            out.append(f"- {text}")
        else:
            out.append(text)
    return "\n".join(out) + "\n" if out else ""


def _docx_text_chars(document: docx.document.Document) -> int:
    """Characters of visible text in the main document part, counting each text box once."""
    pieces = [t.text or "" for t in document.element.body.iter(qn("w:t")) if not _in_fallback(t)]
    return len("".join("".join(pieces).split()))


def _read_docx(raw: bytes) -> ReadResult:
    try:
        document = docx.Document(io.BytesIO(raw))
        text = _docx_markdown(document)
        held = _docx_text_chars(document)
    except Exception:  # BadZipFile, missing parts, bad XML: all one plain sentence
        raise _unreadable("DOCX") from None
    got = len("".join(text.replace("#", "").replace("- ", "").replace("|", "").split()))
    warnings = [DOCX_LOW_TEXT_WARNING] if held and got < held * _DOCX_LOW_YIELD_RATIO else []
    return ReadResult(format="docx", text=text, pages=1, warnings=warnings)


_READERS: dict[str, Callable[[bytes], ReadResult]] = {
    ".md": functools.partial(_read_markdown, label="markdown"),
    ".markdown": functools.partial(_read_markdown, label="markdown"),
    ".txt": functools.partial(_read_markdown, label="text"),
    ".pdf": _read_pdf,
    ".docx": _read_docx,
}


def import_resume(source: Path, folder: Path) -> ImportOutcome:
    """Write profile/resume.md and profile/source/<name> for the resume at source."""
    suffix = source.suffix.lower()
    if suffix == ".doc":
        raise UserError("Save it as .docx or PDF first.")
    reader = _READERS.get(suffix)
    if reader is None:
        raise UserError(
            f"I can read a resume as {ACCEPTED_FORMATS}. "
            f"This file ends in \"{source.suffix or 'nothing'}\"."
        )
    try:
        size = source.stat().st_size
        raw = source.read_bytes() if size <= MAX_RESUME_BYTES else b""
    except OSError:
        raise UserError(
            "I could not open that resume file. Check the path and try again."
        ) from None
    if size > MAX_RESUME_BYTES:
        raise UserError(
            "That resume file is larger than 10 MB. A resume is much smaller than that, so "
            "check that you chose the right file."
        )
    if not raw:
        raise UserError("That resume file is empty. Check that you chose the right file.")
    result = reader(raw)
    if not result.text.strip():
        raise UserError("That resume file is empty. Check that you chose the right file.")
    fsio.write_bytes_atomic(folder / paths.LAYOUT["profile_source"] / source.name, raw)
    fsio.write_text_atomic(folder / paths.LAYOUT["resume_md"], result.text)
    lines = result.text.split("\n")
    return ImportOutcome(
        format=result.format,
        chars=len(result.text),
        pages=result.pages,
        headings=sum(1 for line in lines if _HEADING.match(line)),
        bullets=sum(1 for line in lines if _BULLET.match(line.lstrip())),
        warnings=list(result.warnings),
    )
