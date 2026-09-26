"""Pure document functions coordinated through durable application state."""

from collections.abc import Callable
from dataclasses import dataclass


from .document_assessment import (
    assess_document,
    assess_llm_analysis,
)
from .document_extraction import extract_pdf
from .document_models import DocumentExtraction, DocumentFinding, DocumentJobRecord
from .document_normalization import accounts_match, entities_match
from .document_store import DocumentStore
from closeready.llm_document_analyzer import (
    CandidateRequirement,
    DocumentAnalysisRequest,
    DocumentAnalyzer,
    LLMDocumentAnalysis,
)


@dataclass(frozen=True)
class RequirementResolution:
    requirement_id: str | None
    outcome: str
    candidate_requirement_ids: tuple[str, ...]


def _matches_structured_analysis(requirement, analysis: LLMDocumentAnalysis) -> bool:
    if analysis.detected_type != requirement.document_type:
        return False
    if entities_match(requirement.scope.entity_id, analysis.entity_name) is not True:
        return False

    if requirement.completion_rule.kind == "coverage":
        if accounts_match(
            requirement.scope.masked_account_identifier,
            analysis.account_identifier,
        ) is not True:
            return False
        if analysis.coverage_start is None or analysis.coverage_end is None:
            return False
        return (
            analysis.coverage_start <= requirement.scope.coverage_start
            and analysis.coverage_end >= requirement.scope.coverage_end
        )

    if analysis.detected_period != requirement.accounting_period:
        return False
    matched = set(analysis.matched_item_refs)
    expected = set(requirement.completion_rule.expected_item_refs)
    return bool(matched) and matched.issubset(expected)


def resolve_requirement_id(case, analysis: LLMDocumentAnalysis) -> RequirementResolution:
    """Resolve only a unique, authorised outstanding Requirement.

    The model supplies document facts. Application code owns IDs and performs
    this deterministic selection so a model cannot invent or choose an ID.
    """
    outstanding = [
        requirement
        for requirement in case.requirements
        if requirement.status not in ("accepted", "waived")
    ]
    if len(outstanding) == 1:
        return RequirementResolution(
            outstanding[0].requirement_id,
            "single_candidate",
            (outstanding[0].requirement_id,),
        )

    matches = tuple(
        requirement.requirement_id
        for requirement in outstanding
        if _matches_structured_analysis(requirement, analysis)
    )
    if len(matches) == 1:
        return RequirementResolution(
            matches[0], "unique_structured_match", matches
        )
    return RequirementResolution(
        None,
        "ambiguous" if len(matches) > 1 else "no_match",
        matches,
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
        if requirement.status not in ("accepted", "waived")
        and (requirement_id is None or requirement.requirement_id == requirement_id)
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
            context = self.store.refresh_claimed_processing_context(job_id, token)
            if context is None:
                return self.store.mark_stale(job_id, token)
        try:
            extraction = self.extractor(context.content)
        except ValueError:
            return self.store.fail(job_id, token, "INVALID_PDF")
        except Exception:
            return self.store.fail(job_id, token, "DOCUMENT_PROCESSING_FAILED")

        try:
            if not extraction.readable:
                finding = self.assessor(
                    case=context.case,
                    document_id=context.document.document_id,
                    extraction=extraction,
                    requirement_id=context.document.requirement_id,
                    duplicate=(context.document.duplicate_of_document_id is not None),
                )
            elif self.analyzer is None:
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

                try:
                    analysis = self.analyzer.analyze(request)
                except Exception:
                    telemetry = getattr(self.analyzer, "last_telemetry", None)
                    if telemetry is not None:
                        self.store.persist_analysis_telemetry(job_id, token, telemetry)
                    raise
                telemetry = getattr(self.analyzer, "last_telemetry", None)
                if telemetry is not None:
                    self.store.persist_analysis_telemetry(job_id, token, telemetry)

                if not validate_analysis_evidence(
                    extraction,
                    analysis,
                ):
                    analysis = mark_invalid_evidence(
                        analysis,
                    )

                requirement_id = context.document.requirement_id
                resolution = None
                if requirement_id is None and not (
                    context.document.duplicate_of_document_id is not None
                ):
                    resolution = resolve_requirement_id(context.case, analysis)
                    if resolution.requirement_id is not None:
                        self.store.bind_claimed_requirement(
                            job_id,
                            token,
                            resolution.requirement_id,
                            resolution.outcome,
                            list(resolution.candidate_requirement_ids),
                        )
                        requirement_id = resolution.requirement_id

                finding = assess_llm_analysis(
                    case=context.case,
                    document_id=context.document.document_id,
                    analysis=analysis,
                    requirement_id=requirement_id,
                    duplicate=(
                        context.document.duplicate_of_document_id
                        is not None
                    ),
                )
                if resolution is not None and resolution.requirement_id is None:
                    reason = (
                        "Multiple outstanding requirements match the extracted "
                        "document facts."
                        if resolution.outcome == "ambiguous"
                        else "No outstanding requirement uniquely matches the "
                        "extracted document facts."
                    )
                    finding = finding.model_copy(update={
                        "uncertainty_reasons": [reason],
                        "issues": ["Document requires manual requirement assignment."],
                    })

        except Exception:
            return self.store.fail(
                job_id,
                token,
                "DOCUMENT_PROCESSING_FAILED",
            )

        try:
            return self.store.complete(job_id, token, extraction, finding)
        except Exception as exc:
            return self.store.fail(
                job_id,
                token,
                getattr(exc, "code", None) or "DOCUMENT_PROCESSING_FAILED",
            )
