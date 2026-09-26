from typing import Annotated, Literal

from pydantic import Field, model_validator

from closeready.models import (
    CalendarDate,
    ContractModel,
    DocumentType,
    EvidenceRef,
    Period,
    PositiveInt,
    Text,
    Timestamp,
)

DocumentAssessmentResult = Literal[
    "satisfies",
    "needs_correction",
    "needs_review",
    "unmatched",
]

DocumentStatus = Literal[
    "queued",
    "processing",
    "processed",
    "needs_review",
    "failed",
    "stale",
    "rejected",
]

DocumentJobStatus = Literal[
    "queued",
    "processing",
    "completed",
    "needs_review",
    "failed",
    "stale",
    "rejected",
]

DocumentReviewDecision = Literal[
    "accept_for_requirement",
    "reject_document",
    "reassign_for_processing",
]


class DocumentUploadRequest(ContractModel):
    """Validated upload metadata; the binary content stays outside JSON contracts."""

    expected_state_version: PositiveInt
    requirement_id: Text | None
    original_filename: Annotated[Text, Field(max_length=255)]
    media_type: Literal["application/pdf"]


class DocumentRecord(ContractModel):
    document_id: Text
    case_id: Text
    requirement_id: Text | None
    input_state_version: PositiveInt
    original_filename: Annotated[Text, Field(max_length=255)]
    media_type: Literal["application/pdf"]
    size_bytes: int = Field(strict=True, gt=0)
    file_hash: Text
    status: DocumentStatus
    duplicate_of_document_id: Text | None = None
    created_by: Text
    created_at: Timestamp


class DocumentJobRecord(ContractModel):
    job_id: Text
    document_id: Text
    case_id: Text
    status: DocumentJobStatus
    attempt_count: int = Field(strict=True, ge=0)
    created_at: Timestamp
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    lease_expires_at: Timestamp | None = None
    error_code: Text | None = None


class DocumentPage(ContractModel):
    items: list[DocumentRecord]
    next_cursor: Text | None


class DocumentReviewDecisionRequest(ContractModel):
    expected_state_version: PositiveInt
    decision: DocumentReviewDecision
    target_requirement_id: Text | None = None
    reason: Annotated[Text, Field(max_length=2000)]
    prepare_correction_email: bool = False

    @model_validator(mode="after")
    def decision_has_required_target(self):
        if self.decision == "reject_document" and self.target_requirement_id is not None:
            raise ValueError("Rejected documents cannot target a requirement")
        if self.decision != "reject_document" and self.target_requirement_id is None:
            raise ValueError("This decision requires a target requirement")
        if self.decision != "reject_document" and self.prepare_correction_email:
            raise ValueError("Only a rejected document can prepare a correction email")
        return self


class DocumentReviewDecisionRecord(ContractModel):
    decision_id: Text
    case_id: Text
    document_id: Text
    job_id: Text
    decision: DocumentReviewDecision
    target_requirement_id: Text | None
    reviewer_user_id: Text
    reason: Text
    decided_at: Timestamp
    resulting_state_version: PositiveInt
    document_status: DocumentStatus
    source_finding: "DocumentFinding"
    follow_up_run_id: Text | None = None


class DocumentReviewDecisionPage(ContractModel):
    items: list[DocumentReviewDecisionRecord]
    next_cursor: Text | None

MatchResult = Literal[
    "match",
    "mismatch",
    "unknown",
]


class ExtractedPage(ContractModel):
    page: PositiveInt
    text: str
    extraction_method: Literal["embedded_text", "ocr", "none"] = "embedded_text"
    ocr_confidence: float | None = Field(default=None, ge=0, le=100)


class DocumentExtraction(ContractModel):
    file_hash: Text
    page_count: int = Field(strict=True, ge=0)
    pages: list[ExtractedPage]
    readable: bool

    @model_validator(mode="after")
    def consistent_page_count(self):
        if self.page_count != len(self.pages):
            raise ValueError("page_count must match number of extracted pages")

        return self


class DocumentFinding(ContractModel):
    finding_id: Text
    responsibility: Literal["document_assessment"]
    case_id: Text
    input_state_version: PositiveInt

    document_id: Text
    requirement_id: Text | None

    result: DocumentAssessmentResult

    detected_type: DocumentType | None
    detected_period: Period | None

    entity_match: MatchResult
    account_match: MatchResult | None

    coverage_start: CalendarDate | None
    coverage_end: CalendarDate | None

    matched_item_refs: list[Text]
    uncertainty_reasons: list[Text]
    evidence_refs: list[EvidenceRef]
    issues: list[Text]


    @model_validator(mode="after")
    def validate_coverage(self):
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError(
                "coverage_start and coverage_end must both be supplied or both be null"
            )

        if (
            self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_end < self.coverage_start
        ):
            raise ValueError("coverage_end precedes coverage_start")

        return self
