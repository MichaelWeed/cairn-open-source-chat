import pytest

from app.ingest.chunking import chunk_text


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_short_text_is_a_single_chunk() -> None:
    assert chunk_text("hello world", chunk_size=100, overlap=10) == ["hello world"]


def test_long_text_is_split() -> None:
    text = "word " * 500
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 100 + 20 for c in chunks)


def test_reconstructs_full_content_allowing_for_overlap() -> None:
    text = (
        "Paragraph one.\n\nParagraph two.\n\nParagraph three is a fair bit longer than the others."
    )
    chunks = chunk_text(text, chunk_size=30, overlap=5)
    assert "Paragraph one." in chunks[0]
    assert any("Paragraph three" in c for c in chunks)


def test_prefers_paragraph_boundary() -> None:
    text = "A" * 40 + "\n\n" + "B" * 40
    chunks = chunk_text(text, chunk_size=45, overlap=5)
    assert chunks[0] == "A" * 40


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("hello", chunk_size=10, overlap=10)


def test_no_infinite_loop_on_worst_case() -> None:
    text = "x" * 10_000
    chunks = chunk_text(text, chunk_size=50, overlap=10)
    assert len(chunks) > 1
