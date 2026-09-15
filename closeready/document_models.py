from typing import Literal

from pydantic import Field, model_validator

from closeready.models import (
    CalendarDate,
    ContractModel,
    DocumentType,
    EvidenceRef,
    Period,
    PositiveInt,
    Text,
)

DocumentAssessmentResult = Literal[
    "satisfies",
    "needs_correction",
    "needs_review",
    "unmatched",
]

MatchResult = Literal[
    "match",
    "mismatch",
    "unknown",
]


class ExtractedPage(ContractModel):
    page: PositiveInt
    text: str


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
