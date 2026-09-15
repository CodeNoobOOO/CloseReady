from io import BytesIO

import pytest
from pypdf import PdfWriter

from closeready.document_extraction import (
    calculate_file_hash,
    extract_pdf,
)


def create_blank_pdf(page_count: int = 1) -> bytes:
    buffer = BytesIO()

    writer = PdfWriter()

    for _ in range(page_count):
        writer.add_blank_page(
            width=200,
            height=200,
        )

    writer.write(buffer)

    return buffer.getvalue()


def test_same_content_produces_same_hash():
    content = b"CloseReady test document"

    assert calculate_file_hash(content) == calculate_file_hash(content)


def test_different_content_produces_different_hash():
    assert calculate_file_hash(b"document A") != calculate_file_hash(b"document B")


def test_extract_pdf_returns_page_count():
    content = create_blank_pdf(1)

    result = extract_pdf(content)

    assert result.page_count == 1


def test_extract_pdf_returns_pages():
    content = create_blank_pdf(2)

    result = extract_pdf(content)

    assert result.page_count == 2

    assert result.pages[0].page == 1
    assert result.pages[1].page == 2


def test_blank_pdf_is_not_readable():
    content = create_blank_pdf()

    result = extract_pdf(content)

    assert result.readable is False


def test_extract_pdf_rejects_invalid_pdf():
    with pytest.raises(
        ValueError,
        match="Invalid PDF document",
    ):
        extract_pdf(b"This is not a PDF")
