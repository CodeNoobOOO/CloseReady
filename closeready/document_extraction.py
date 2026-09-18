import hashlib
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from closeready.document_models import (
    DocumentExtraction,
    ExtractedPage,
)


def calculate_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_pdf(content: bytes) -> DocumentExtraction:
    file_hash = calculate_file_hash(content)

    try:
        reader = PdfReader(BytesIO(content))
    except (PdfReadError, EOFError, ValueError) as exc:
        raise ValueError("Invalid PDF document") from exc

    pages: list[ExtractedPage] = []

    try:
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""

            pages.append(
                ExtractedPage(
                    page=page_number,
                    text=text,
                )
            )
    except (PdfReadError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("PDF text extraction failed") from exc

    readable = any(page.text.strip() for page in pages)

    return DocumentExtraction(
        file_hash=file_hash,
        page_count=len(pages),
        pages=pages,
        readable=readable,
    )


def combine_extracted_text(extraction: DocumentExtraction) -> str:
    return "\n".join(page.text for page in extraction.pages if page.text.strip())
