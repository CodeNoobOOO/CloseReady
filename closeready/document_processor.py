"""Pure document functions coordinated through durable application state."""
from collections.abc import Callable

from .document_assessment import assess_document
from .document_extraction import extract_pdf
from .document_models import DocumentExtraction, DocumentFinding, DocumentJobRecord
from .document_store import DocumentStore


class DocumentProcessor:
    def __init__(
        self,
        store: DocumentStore,
        *,
        extractor: Callable[[bytes], DocumentExtraction] = extract_pdf,
        assessor: Callable[..., DocumentFinding] = assess_document,
    ):
        self.store = store
        self.extractor = extractor
        self.assessor = assessor

    def execute_claimed(self, job_id: str, token: str) -> DocumentJobRecord:
        context = self.store.processing_context(job_id, token)
        if context.case.state_version != context.document.input_state_version:
            return self.store.mark_stale(job_id, token)
        try:
            extraction = self.extractor(context.content)
        except ValueError:
            return self.store.fail(job_id, token, "INVALID_PDF")
        except Exception:
            return self.store.fail(job_id, token, "DOCUMENT_PROCESSING_FAILED")

        try:
            finding = self.assessor(
                case=context.case,
                document_id=context.document.document_id,
                extraction=extraction,
                requirement_id=context.document.requirement_id,
                duplicate=context.document.duplicate_of_document_id is not None,
            )
        except Exception:
            return self.store.fail(job_id, token, "DOCUMENT_PROCESSING_FAILED")
        return self.store.complete(job_id, token, extraction, finding)
