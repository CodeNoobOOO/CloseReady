import hashlib
import os
from dataclasses import dataclass
from io import BytesIO
from typing import Callable

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from closeready.document_models import (
    DocumentExtraction,
    ExtractedPage,
)


@dataclass(frozen=True)
class OCRPageResult:
    page: int
    text: str
    confidence: float


OCRBackend = Callable[[bytes, list[int]], list[OCRPageResult]]


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _float_setting(
    name: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def local_tesseract_ocr(
    content: bytes,
    page_numbers: list[int],
) -> list[OCRPageResult]:
    """Render selected PDF pages and run the local Tesseract executable."""
    import pypdfium2 as pdfium
    import pytesseract

    command = os.getenv("CLOSEREADY_TESSERACT_CMD", "").strip()
    if command:
        pytesseract.pytesseract.tesseract_cmd = command

    dpi = _integer_setting("CLOSEREADY_OCR_DPI", 200, 100, 300)
    timeout = _float_setting("CLOSEREADY_OCR_PAGE_TIMEOUT_SECONDS", 20, 1, 60)
    language = os.getenv("CLOSEREADY_OCR_LANGUAGE", "eng").strip() or "eng"
    document = pdfium.PdfDocument(content)
    results: list[OCRPageResult] = []

    try:
        for page_number in page_numbers:
            page = document[page_number - 1]
            try:
                image = page.render(scale=dpi / 72).to_pil()
                data = pytesseract.image_to_data(
                    image,
                    lang=language,
                    timeout=timeout,
                    output_type=pytesseract.Output.DICT,
                )
            finally:
                page.close()

            words: list[str] = []
            confidences: list[float] = []
            for word, raw_confidence in zip(data["text"], data["conf"]):
                word = str(word).strip()
                if not word:
                    continue
                try:
                    confidence = float(raw_confidence)
                except (TypeError, ValueError):
                    continue
                if confidence < 0:
                    continue
                words.append(word)
                confidences.append(confidence)

            results.append(OCRPageResult(
                page=page_number,
                text=" ".join(words),
                confidence=(
                    sum(confidences) / len(confidences) if confidences else 0.0
                ),
            ))
    finally:
        document.close()

    return results


def calculate_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_pdf(
    content: bytes,
    *,
    ocr_backend: OCRBackend = local_tesseract_ocr,
) -> DocumentExtraction:
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
                    extraction_method=("embedded_text" if text.strip() else "none"),
                )
            )
    except (PdfReadError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("PDF text extraction failed") from exc

    minimum_text = _integer_setting("CLOSEREADY_OCR_MIN_TEXT_CHARS", 20, 1, 500)
    embedded_characters = sum(len(page.text.strip()) for page in pages)
    readable = embedded_characters >= minimum_text

    if not readable and pages:
        max_pages = _integer_setting("CLOSEREADY_OCR_MAX_PAGES", 20, 1, 100)
        if len(pages) <= max_pages:
            try:
                ocr_results = ocr_backend(content, [page.page for page in pages])
            except Exception:
                ocr_results = []

            by_page = {
                result.page: result
                for result in ocr_results
                if result.page in {page.page for page in pages}
            }
            pages = [
                ExtractedPage(
                    page=page.page,
                    text=(by_page[page.page].text if page.page in by_page else page.text),
                    extraction_method=(
                        "ocr" if page.page in by_page else page.extraction_method
                    ),
                    ocr_confidence=(
                        by_page[page.page].confidence
                        if page.page in by_page
                        else None
                    ),
                )
                for page in pages
            ]
            minimum_confidence = _float_setting(
                "CLOSEREADY_OCR_MIN_CONFIDENCE", 70, 0, 100
            )
            readable = len(by_page) == len(pages) and all(
                page.extraction_method == "ocr"
                and len(page.text.strip()) >= minimum_text
                and page.ocr_confidence is not None
                and page.ocr_confidence >= minimum_confidence
                for page in pages
            )

    return DocumentExtraction(
        file_hash=file_hash,
        page_count=len(pages),
        pages=pages,
        readable=readable,
    )


def combine_extracted_text(extraction: DocumentExtraction) -> str:
    return "\n".join(page.text for page in extraction.pages if page.text.strip())
