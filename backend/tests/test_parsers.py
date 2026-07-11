import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ingest.parsers import UnsupportedFileType, extract_text


def make_pdf_bytes(text: str) -> bytes:
    """Build a minimal one-page PDF with a real, extractable text stream —
    avoids committing a binary fixture just to test PDF parsing."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)

    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    resources = DictionaryObject()
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = writer._add_object(font)
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_bytes_are_extractable_by_pypdf_directly() -> None:
    # Sanity check on the test helper itself, independent of our parser.
    reader = PdfReader(io.BytesIO(make_pdf_bytes("Hello PDF")))
    assert "Hello PDF" in reader.pages[0].extract_text()


def test_extract_text_markdown() -> None:
    assert extract_text("doc.md", b"# Title\n\nBody text.") == "# Title\n\nBody text."


def test_extract_text_markdown_alt_extension() -> None:
    assert extract_text("doc.markdown", b"content") == "content"


def test_extract_text_pdf() -> None:
    pdf_bytes = make_pdf_bytes("Hello PDF")
    assert "Hello PDF" in extract_text("doc.pdf", pdf_bytes)


def test_unsupported_extension_raises() -> None:
    with pytest.raises(UnsupportedFileType):
        extract_text("doc.docx", b"whatever")


def test_no_extension_raises() -> None:
    with pytest.raises(UnsupportedFileType):
        extract_text("README", b"whatever")


def test_extension_is_case_insensitive() -> None:
    assert extract_text("DOC.MD", b"content") == "content"
