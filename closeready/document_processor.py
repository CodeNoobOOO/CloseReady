"""Pure document functions coordinated through durable application state."""

from collections.abc import Callable


from .document_assessment import (
    assess_document,
    assess_llm_analysis,
)
from .document_extraction import extract_pdf
from .document_models import DocumentExtraction, DocumentFinding, DocumentJobRecord
from .document_store import DocumentStore
from closeready.llm_document_analyzer import (
    CandidateRequirement,
    DocumentAnalysisRequest,
    DocumentAnalyzer,
    LLMDocumentAnalysis,
)


def validate_analysis_evidence(
    extraction: DocumentExtraction,
    analysis: LLMDocumentAnalysis,
) -> bool:
    pages = {page.page: page.text for page in extraction.pages}

    for evidence in analysis.evidence:
        page_text = pages.get(evidence.page)

        if page_text is None:
            return False

        if evidence.excerpt not in page_text:
            return False

    return True


def mark_invalid_evidence(
    analysis: LLMDocumentAnalysis,
) -> LLMDocumentAnalysis:
    return analysis.model_copy(
        update={
            "uncertainty_reasons": [
                *analysis.uncertainty_reasons,
                "Model evidence could not be verified against extracted PDF text.",
            ]
        }
    )


def build_document_analysis_request(
    case,
    extraction: DocumentExtraction,
    requirement_id: str | None,
) -> DocumentAnalysisRequest:
    requirements = [
        requirement
        for requirement in case.requirements
        if (requirement_id is None or requirement.requirement_id == requirement_id)
    ]

    candidates = [
        CandidateRequirement(
            requirement_id=requirement.requirement_id,
            document_type=requirement.document_type,
            accounting_period=requirement.accounting_period,
            entity_name=requirement.scope.entity_id,
            masked_account_identifier=(requirement.scope.masked_account_identifier),
            coverage_start=requirement.scope.coverage_start,
            coverage_end=requirement.scope.coverage_end,
            expected_item_refs=list(requirement.completion_rule.expected_item_refs),
        )
        for requirement in requirements
    ]

    if not candidates:
        raise ValueError("No authorised candidate requirements are available.")

    pages = [page.text for page in extraction.pages]

    return DocumentAnalysisRequest(
        pages=pages,
        accounting_period=case.accounting_period,
        candidate_requirements=candidates,
    )


class DocumentProcessor:

    def __init__(
        self,
        store: DocumentStore,
        *,
        extractor: Callable[[bytes], DocumentExtraction] = extract_pdf,
        assessor: Callable[..., DocumentFinding] = assess_document,
        analyzer: DocumentAnalyzer | None = None,
    ):
        self.store = store
        self.extractor = extractor
        self.assessor = assessor
        self.analyzer = analyzer

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
            if self.analyzer is None:
                finding = self.assessor(
                    case=context.case,
                    document_id=context.document.document_id,
                    extraction=extraction,
                    requirement_id=context.document.requirement_id,
                    duplicate=(context.document.duplicate_of_document_id is not None),
                )
            else:
                request = build_document_analysis_request(
                    context.case,
                    extraction,
                    context.document.requirement_id,
                )

                analysis = self.analyzer.analyze(request)

                if not validate_analysis_evidence(
                    extraction,
                    analysis,
                ):
                    analysis = mark_invalid_evidence(
                        analysis,
                    )

                finding = assess_llm_analysis(
                    case=context.case,
                    document_id=context.document.document_id,
                    analysis=analysis,
                    requirement_id=context.document.requirement_id,
                    duplicate=(
                        context.document.duplicate_of_document_id
                        is not None
                    ),
                )

        except Exception:
            return self.store.fail(
                job_id,
                token,
                "DOCUMENT_PROCESSING_FAILED",
            )

        return self.store.complete(
            job_id,
            token,
            extraction,
            finding,
        )
