from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from closeready.document_extraction import (
    OCRPageResult,
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


def create_text_pdf(text: str) -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 40 740 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
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


def test_embedded_text_is_preferred_without_calling_ocr():
    content = create_text_pdf("DBS bank statement with embedded searchable text")

    def unexpected_ocr(_content, _pages):
        raise AssertionError("OCR must not run for a readable text PDF")

    result = extract_pdf(content, ocr_backend=unexpected_ocr)

    assert result.readable is True
    assert result.pages[0].extraction_method == "embedded_text"
    assert result.pages[0].ocr_confidence is None


def test_image_only_pdf_uses_local_ocr_fallback():
    content = create_blank_pdf(2)
    calls = []

    def ocr_backend(pdf_content, page_numbers):
        calls.append((pdf_content, page_numbers))
        return [
            OCRPageResult(
                page=1,
                text="DBS bank statement account ending 1234",
                confidence=94.0,
            ),
            OCRPageResult(
                page=2,
                text="Statement period 01 September to 30 September 2026",
                confidence=91.0,
            ),
        ]

    result = extract_pdf(content, ocr_backend=ocr_backend)

    assert calls == [(content, [1, 2])]
    assert result.readable is True
    assert result.pages[0].extraction_method == "ocr"
    assert result.pages[0].ocr_confidence == 94.0
    assert "account ending 1234" in result.pages[0].text


def test_low_confidence_ocr_stays_unreadable_for_human_review():
    content = create_blank_pdf()

    result = extract_pdf(
        content,
        ocr_backend=lambda _content, _pages: [
            OCRPageResult(
                page=1,
                text="Possibly account 1234 but the scan is blurred",
                confidence=32.0,
            )
        ],
    )

    assert result.readable is False
    assert result.pages[0].extraction_method == "ocr"
    assert result.pages[0].text


def test_partial_ocr_failure_keeps_whole_scanned_pdf_unreadable():
    content = create_blank_pdf(2)

    result = extract_pdf(
        content,
        ocr_backend=lambda _content, _pages: [
            OCRPageResult(
                page=1,
                text="DBS bank statement account ending 1234",
                confidence=95.0,
            )
        ],
    )

    assert result.readable is False
    assert result.pages[0].extraction_method == "ocr"
    assert result.pages[1].extraction_method == "none"


def test_extract_pdf_rejects_invalid_pdf():
    with pytest.raises(
        ValueError,
        match="Invalid PDF document",
    ):
        extract_pdf(b"This is not a PDF")
