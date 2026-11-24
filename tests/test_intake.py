"""A PDF, DOCX or Markdown resume becomes profile/resume.md, and the original is kept.

Purpose: prove the PDF and DOCX readers turn real files into the same plain Markdown shape as the
Markdown path, keep section and bullet order, and refuse or flag what they cannot read honestly
(an empty profile written silently would poison every later step).
Logic: every PDF and DOCX is built here at test time (a few lines of PDF syntax, python-docx),
so no binary fixture is committed. Integration tests drive cli.main("profile-import") on a data
folder under tmp_path and read the files back; unit tests call intake.import_resume directly.
Guardrails: every resume text here is fictional (Casey Example, example.com). No test touches the
network or the wall clock.
QA checklist: a new reader or refusal needs a named test here; a changed warning sentence needs
its test string updated in the same commit.
"""

from __future__ import annotations

import io
from pathlib import Path

import docx
from docx.oxml import parse_xml
import pytest
from helpers import cli_result
from pypdf import PdfReader, PdfWriter
from pypdf.errors import DependencyError

from jobsearch import cli, intake


def make_pdf(pages: list[list[str]]) -> bytes:
    """Minimal text PDF: one Helvetica line per entry, one page per inner list."""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, lines in enumerate(pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        parts = [b"BT /F1 12 Tf 14 TL 72 720 Td"]
        for line in lines:
            esc = (line.encode("cp1252").replace(b"\\", b"\\\\")
                   .replace(b"(", b"\\(").replace(b")", b"\\)"))
            parts.append(b"(" + esc + b") Tj T*")
        parts.append(b"ET")
        stream = b"\n".join(parts)
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


BULLET = "•"
RESUME_LINES = [
    "EXPERIENCE",
    f"{BULLET} Ran the front desk for a 40 person clinic",
    f"{BULLET} Trained six new coordinators",
    "EDUCATION",
    "BA Biology, Example State University",
]


def import_file(capsys, folder: Path, source: Path) -> tuple[int, dict]:
    return cli_result(capsys, "profile-import", "--resume", str(source), "--data-dir", str(folder))


@pytest.mark.parametrize("suffix, label", [
    (".md", "markdown"), (".markdown", "markdown"), (".txt", "text")])
def test_text_readers_report_their_format(suffix, label):
    result = intake._READERS[suffix](b"# Casey Example\n\n- ran ops\n")
    assert (result.format, result.text) == (label, "# Casey Example\n\n- ran ops\n")


def test_pdf_resume_imports_to_markdown_in_source_order(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    raw = make_pdf([RESUME_LINES])
    source.write_bytes(raw)
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True
    assert result["data"]["format"] == "pdf"
    text = (folder / "profile" / "resume.md").read_text(encoding="utf-8")
    shown = [line for line in text.split("\n") if line.strip()]
    assert shown == [
        "## Experience",
        "- Ran the front desk for a 40 person clinic",
        "- Trained six new coordinators",
        "## Education",
        "BA Biology, Example State University",
    ]
    assert (folder / "profile" / "source" / "casey.pdf").read_bytes() == raw


def test_progress_names_the_interview_after_a_pdf_import(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([RESUME_LINES]))
    assert import_file(capsys, folder, source)[0] == 0
    code, result = cli_result(capsys, "progress", "--data-dir", str(folder))
    assert code == 0 and result["ok"] is True
    assert result["data"]["stage"] == "no_answers"
    assert result["data"]["next_step"]


def test_pdf_name_line_and_mixed_case_section_names(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([[
        "CASEY EXAMPLE", "casey@example.com", "Work Experience:", f"{BULLET} Ran a desk",
        "ACME 2 LTD", "Skills"]]))
    assert import_file(capsys, folder, source)[0] == 0
    text = (folder / "profile" / "resume.md").read_text(encoding="utf-8")
    assert text.split("\n") == [
        "CASEY EXAMPLE", "casey@example.com", "", "## Work Experience", "- Ran a desk",
        "ACME 2 LTD", "", "## Skills", ""]


def test_pdf_short_all_caps_skill_lines_stay_text(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([[
        "CASEY EXAMPLE", "Skills", "SQL", "CRM", "AWS", "KEY PROJECTS", "Ran the intake desk"]]))
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["data"]["headings"] == 2
    text = (folder / "profile" / "resume.md").read_text(encoding="utf-8")
    assert text.split("\n") == [
        "CASEY EXAMPLE", "", "## Skills", "SQL", "CRM", "AWS", "", "## Key Projects",
        "Ran the intake desk", ""]


def test_pdf_bullet_glyph_alone_on_a_line_joins_the_next_line(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([["Skills", BULLET, "Scheduling", BULLET, "Budgeting"]]))
    assert import_file(capsys, folder, source)[0] == 0
    text = (folder / "profile" / "resume.md").read_text(encoding="utf-8")
    assert text == "## Skills\n- Scheduling\n- Budgeting\n"


# ---------------------------------------------------------------- Task 2: DOCX, refusals, edges

LOW_TEXT_WARNING = ("Little text came out of this PDF (it may be scanned or designed). "
                    "Read the PDF directly and write profile/resume.md from what you see.")
NO_TEXT_ERROR = ("No text came out of this PDF (it may be scanned or designed). "
                 "Read the PDF directly and write profile/resume.md from what you see.")
LONG_LINE = "Coordinated scheduling, intake and billing for a busy outpatient clinic every week"


def make_docx(build) -> bytes:
    document = docx.Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def resume_docx(document) -> None:
    document.add_heading("Skills", level=1)
    document.add_paragraph("Scheduling", style="List Bullet")
    document.add_paragraph("Budgeting", style="List Bullet")
    table = document.add_table(rows=2, cols=2)
    for row, cells in enumerate([("Tool", "Years"), ("Spreadsheets", "6")]):
        for col, value in enumerate(cells):
            table.cell(row, col).text = value


def encrypted_pdf(user_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf([RESUME_LINES]))))
    writer.encrypt(user_password, "owner-secret", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def nothing_written(folder: Path) -> bool:
    profile = folder / "profile"
    return (not (profile / "resume.md").exists()
            and not any((profile / "source").glob("*")))


def resume_md(folder: Path) -> str:
    return (folder / "profile" / "resume.md").read_text(encoding="utf-8")


def shown(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip()]


def test_docx_heading_then_list_then_table_converts_in_order(tmp_path, folder, capsys):
    source = tmp_path / "casey.docx"
    raw = make_docx(resume_docx)
    source.write_bytes(raw)
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True and result["data"]["format"] == "docx"
    assert shown(resume_md(folder)) == [
        "# Skills", "- Scheduling", "- Budgeting", "Tool | Years", "Spreadsheets | 6"]
    assert (folder / "profile" / "source" / "casey.docx").read_bytes() == raw


W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def sdt_paragraph(text: str):
    return parse_xml(
        f"<w:sdt {W_NS}><w:sdtPr/><w:sdtContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "</w:sdtContent></w:sdt>")


def hidden_content_docx(document) -> None:
    document.add_paragraph("Visible line")
    document.element.body.insert(2, sdt_paragraph("Inside content control Python SQL"))
    outer = document.add_table(rows=1, cols=1)
    cell = outer.cell(0, 0)
    cell.text = "Outer cell"
    inner = cell.add_table(rows=1, cols=2)
    inner.cell(0, 0).text = "Nested Skill"
    inner.cell(0, 1).text = "Nested Years"
    box = parse_xml(
        f'<w:p {W_NS}><w:r><w:txbxContent><w:p><w:r><w:t>Text box Leadership</w:t></w:r></w:p>'
        "</w:txbxContent></w:r></w:p>")
    document.element.body.insert(3, box)


def test_docx_text_in_content_controls_nested_tables_and_text_boxes_is_imported(
        tmp_path, folder, capsys):
    source = tmp_path / "templated.docx"
    source.write_bytes(make_docx(hidden_content_docx))
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True
    text = resume_md(folder)
    for expected in ("Visible line", "Inside content control Python SQL", "Outer cell",
                     "Nested Skill | Nested Years", "Text box Leadership"):
        assert expected in text, expected
    assert result["warnings"] == []


def test_docx_that_yields_far_less_text_than_it_holds_warns(tmp_path, folder, capsys):
    def build(document):
        document.add_paragraph("Visible line")
        # Text inside a structure the reader does not walk: a field-less custom XML wrapper.
        document.element.body.insert(2, parse_xml(
            f"<w:customXml {W_NS} w:element=\"x\"><w:p><w:r><w:t>"
            + "Hidden words that the reader never sees " * 10
            + "</w:t></w:r></w:p></w:customXml>"))

    source = tmp_path / "odd.docx"
    source.write_bytes(make_docx(build))
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True
    assert result["warnings"] and "DOCX" in result["warnings"][0]


def test_docx_styles_map_to_markdown_levels(tmp_path, folder, capsys):
    def build(document):
        document.add_paragraph("Casey Example", style="Title")
        document.add_heading("Experience", level=1)
        document.add_heading("Front desk lead", level=2)
        document.add_paragraph("Ran the desk.")
        document.add_paragraph("First step", style="List Number")
        document.add_paragraph("Second step", style="List Paragraph")
        document.add_paragraph("")
    source = tmp_path / "casey.docx"
    source.write_bytes(make_docx(build))
    assert import_file(capsys, folder, source)[0] == 0
    assert shown(resume_md(folder)) == [
        "# Casey Example", "# Experience", "## Front desk lead", "Ran the desk.",
        "- First step", "- Second step"]


def test_docx_heading_directly_followed_by_a_list_keeps_each_on_its_own_line(
        tmp_path, folder, capsys):
    source = tmp_path / "casey.docx"
    source.write_bytes(make_docx(resume_docx))
    assert import_file(capsys, folder, source)[0] == 0
    lines = resume_md(folder).split("\n")
    start = lines.index("# Skills")
    assert lines[start + 1:start + 3] == ["- Scheduling", "- Budgeting"]


def test_markdown_heading_directly_on_its_bullets_is_not_merged(tmp_path, folder, capsys):
    source = tmp_path / "casey.md"
    source.write_text("## Experience\n- Ran the desk\n- Trained staff\n## Education\n"
                      "- BA Biology\n", encoding="utf-8")
    assert import_file(capsys, folder, source)[0] == 0
    assert resume_md(folder).split("\n") == [
        "## Experience", "- Ran the desk", "- Trained staff", "## Education",
        "- BA Biology", ""]


def test_pdf_heading_directly_on_its_bullets_is_not_merged(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([[
        "Experience", f"{BULLET} Ran the desk", f"{BULLET} Trained staff", "Education",
        f"{BULLET} BA Biology"]]))
    assert import_file(capsys, folder, source)[0] == 0
    assert resume_md(folder).split("\n") == [
        "## Experience", "- Ran the desk", "- Trained staff", "", "## Education",
        "- BA Biology", ""]


def test_section_and_bullet_order_is_the_same_for_all_three_formats(tmp_path, capsys):
    sections = [("Experience", ["Ran the desk", "Trained staff", "Cut wait times"]),
                ("Education", ["BA Biology"]), ("Skills", ["Scheduling", "Budgeting"])]
    md = "\n".join(f"## {h}\n" + "\n".join(f"- {b}" for b in bs) for h, bs in sections) + "\n"
    pdf_lines = [x for h, bs in sections for x in [h.upper(), *[f"{BULLET} {b}" for b in bs]]]

    def build(document):
        for heading, bullets in sections:
            document.add_heading(heading, level=2)
            for bullet in bullets:
                document.add_paragraph(bullet, style="List Bullet")

    sources = {"casey.md": md.encode("utf-8"), "casey.pdf": make_pdf([pdf_lines]),
               "casey.docx": make_docx(build)}
    sequences = []
    for name, raw in sources.items():
        target = tmp_path / name.split(".")[1]
        assert cli.main(["init", "--data-dir", str(target)]) == 0
        source = tmp_path / name
        source.write_bytes(raw)
        assert import_file(capsys, target, source)[0] == 0
        sequences.append([line.lstrip("#- ").strip() for line in shown(resume_md(target))])
    assert sequences[0] == ["Experience", "Ran the desk", "Trained staff", "Cut wait times",
                            "Education", "BA Biology", "Skills", "Scheduling", "Budgeting"]
    assert sequences[0] == sequences[1] == sequences[2]


def test_pdf_pages_are_read_in_page_order(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([["Experience", LONG_LINE], ["Education", "BA Biology"]]))
    assert import_file(capsys, folder, source)[0] == 0
    assert shown(resume_md(folder)) == [
        "## Experience", LONG_LINE, "## Education", "BA Biology"]


def test_low_text_threshold_is_two_hundred_characters_per_page():
    assert intake.LOW_TEXT_CHARS_PER_PAGE == 200


def test_pdf_with_little_text_is_ok_with_a_low_text_warning(tmp_path, folder, capsys):
    source = tmp_path / "scan.pdf"
    source.write_bytes(make_pdf([["Experience", "Ran a desk"], ["Education", "BA"]]))
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True
    assert LOW_TEXT_WARNING in result["warnings"]
    assert (folder / "profile" / "resume.md").is_file()


def test_pdf_with_enough_text_has_no_low_text_warning(tmp_path, folder, capsys):
    source = tmp_path / "casey.pdf"
    source.write_bytes(make_pdf([["Experience", LONG_LINE, LONG_LINE, LONG_LINE]]))
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True
    assert LOW_TEXT_WARNING not in result["warnings"]


def test_pdf_with_no_text_at_all_is_refused_and_writes_nothing(tmp_path, folder, capsys):
    source = tmp_path / "scan.pdf"
    source.write_bytes(make_pdf([[]]))
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert result["error"] == NO_TEXT_ERROR
    assert nothing_written(folder)


@pytest.mark.parametrize("name", ["empty.pdf", "empty.docx", "empty.md", "empty.txt"])
def test_zero_byte_file_is_refused_and_writes_nothing(tmp_path, folder, capsys, name):
    source = tmp_path / name
    source.write_bytes(b"")
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert "empty" in result["error"]
    assert nothing_written(folder)


def test_doc_file_is_refused_with_the_save_as_hint(tmp_path, folder, capsys):
    source = tmp_path / "old.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0 old word file")
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert result["error"] == "Save it as .docx or PDF first."
    assert nothing_written(folder)


@pytest.mark.parametrize("name", ["Connections.csv", "linkedin-export.zip", "resume.rtf"])
def test_other_formats_are_not_accepted_and_name_the_three_that_are(
        tmp_path, folder, capsys, name):
    source = tmp_path / name
    source.write_bytes(b"data")
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert "PDF, DOCX or Markdown" in result["error"]
    assert nothing_written(folder)


def test_file_over_ten_megabytes_is_refused_and_writes_nothing(tmp_path, folder, capsys):
    source = tmp_path / "huge.md"
    source.write_bytes(b"a" * (10 * 1024 * 1024 + 1))
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert "10 MB" in result["error"]
    assert nothing_written(folder)


def test_password_protected_pdf_is_refused_and_writes_nothing(tmp_path, folder, capsys):
    source = tmp_path / "locked.pdf"
    source.write_bytes(encrypted_pdf("pw"))
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert result["error"] == ("This PDF is password-protected. Export a copy without a "
                               "password and try again.")
    assert nothing_written(folder)


def test_pdf_locked_only_against_editing_is_read_with_the_empty_password(
        tmp_path, folder, capsys):
    source = tmp_path / "restricted.pdf"
    source.write_bytes(encrypted_pdf(""))
    code, result = import_file(capsys, folder, source)
    assert code == 0 and result["ok"] is True
    assert "## Experience" in resume_md(folder)


def test_aes_pdf_this_build_cannot_decrypt_gets_an_accurate_message(
        tmp_path, folder, capsys, monkeypatch):
    """pypdf raises DependencyError for AES without the cryptography package; it is not a password."""
    def no_aes(self, password):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(PdfReader, "decrypt", no_aes)
    source = tmp_path / "aes.pdf"
    source.write_bytes(encrypted_pdf(""))
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert "password" not in result["error"]
    assert "encrypted with a method this tool cannot open" in result["error"]
    assert "unencrypted PDF" in result["error"] and "Markdown" in result["error"]
    assert nothing_written(folder)


@pytest.mark.parametrize("name", ["broken.pdf", "broken.docx"])
def test_unreadable_pdf_or_docx_is_a_plain_error_and_writes_nothing(
        tmp_path, folder, capsys, name):
    source = tmp_path / name
    source.write_bytes(b"this is not really a resume file at all")
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert "could not read" in result["error"].lower()
    assert "Traceback" not in result["error"]
    assert nothing_written(folder)


def test_docx_with_no_text_is_refused_and_writes_nothing(tmp_path, folder, capsys):
    source = tmp_path / "blank.docx"
    source.write_bytes(make_docx(lambda document: None))
    code, result = import_file(capsys, folder, source)
    assert code == 1 and result["ok"] is False
    assert "empty" in result["error"]
    assert nothing_written(folder)


def test_no_pdf_or_docx_binary_is_committed_as_a_persona_fixture():
    personas = Path(__file__).resolve().parent / "fixtures" / "personas"
    found = [p for pattern in ("*.pdf", "*.docx") for p in personas.rglob(pattern)]
    assert found == []
