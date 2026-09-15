import re
from datetime import date
from uuid import uuid4

from closeready.document_models import (
    DocumentExtraction,
    DocumentFinding,
)
from closeready.models import (
    CaseSnapshot,
    DocumentType,
    EvidenceRef,
    Requirement,
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def detect_document_type(text: str) -> DocumentType:
    normalized = text.lower()

    if "bank statement" in normalized or "statement period" in normalized:
        return "bank_statement"

    if "invoice" in normalized:
        return "invoice"

    if "receipt" in normalized:
        return "receipt"

    return "other_supporting_document"


def _parse_written_date(value: str) -> date | None:
    match = re.fullmatch(
        r"\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*",
        value,
    )

    if match is None:
        return None

    day = int(match.group(1))
    month_name = match.group(2).lower()
    year = int(match.group(3))

    month = MONTHS.get(month_name)

    if month is None:
        return None

    try:
        return date(year, month, day)
    except ValueError:
        return None


def detect_coverage(text: str) -> tuple[date | None, date | None]:
    pattern = re.compile(
        r"(?:statement\s+period\s*[:\-]?\s*)?"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})"
        r"\s+(?:to|-)\s+"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        re.IGNORECASE,
    )

    match = pattern.search(text)

    if match is None:
        return None, None

    start = _parse_written_date(match.group(1))
    end = _parse_written_date(match.group(2))

    if start is None or end is None or end < start:
        return None, None

    return start, end


def detect_accounting_period(
    coverage_start: date | None,
    coverage_end: date | None,
) -> str | None:
    if coverage_start is None or coverage_end is None:
        return None

    if (
        coverage_start.year == coverage_end.year
        and coverage_start.month == coverage_end.month
    ):
        return f"{coverage_start.year:04d}-{coverage_start.month:02d}"

    return None


def build_evidence_refs(
    document_id: str,
    extraction: DocumentExtraction,
) -> list[EvidenceRef]:
    evidence: list[EvidenceRef] = []

    for page in extraction.pages:
        text = " ".join(page.text.split())

        if not text:
            continue

        evidence.append(
            EvidenceRef(
                document_id=document_id,
                page=page.page,
                excerpt=text[:500],
            )
        )

    return evidence


def _requirement_by_id(
    case: CaseSnapshot,
    requirement_id: str,
) -> Requirement | None:
    for requirement in case.requirements:
        if requirement.requirement_id == requirement_id:
            return requirement

    return None


def assess_document(
    *,
    case: CaseSnapshot,
    document_id: str,
    extraction: DocumentExtraction,
    requirement_id: str | None,
    duplicate: bool = False,
) -> DocumentFinding:

    evidence_refs = build_evidence_refs(
        document_id,
        extraction,
    )

    finding_id = f"finding_document_{uuid4().hex}"

    # Exact duplicate is not new evidence.
    if duplicate:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement_id,
            result="needs_review",
            detected_type=None,
            detected_period=None,
            entity_match="unknown",
            account_match=None,
            coverage_start=None,
            coverage_end=None,
            matched_item_refs=[],
            uncertainty_reasons=["Exact duplicate file hash detected."],
            evidence_refs=[],
            issues=["Duplicate document must not create duplicate satisfaction."],
        )

    # Valid PDF but no usable extracted text.
    if not extraction.readable:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement_id,
            result="needs_review",
            detected_type=None,
            detected_period=None,
            entity_match="unknown",
            account_match=None,
            coverage_start=None,
            coverage_end=None,
            matched_item_refs=[],
            uncertainty_reasons=[
                "No readable text could be extracted from the document."
            ],
            evidence_refs=[],
            issues=["Document requires manual review or OCR."],
        )

    text = "\n".join(page.text for page in extraction.pages)

    detected_type = detect_document_type(text)

    coverage_start, coverage_end = detect_coverage(text)

    detected_period = detect_accounting_period(
        coverage_start,
        coverage_end,
    )

    # No requirement was associated with the upload.
    if requirement_id is None:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=None,
            result="unmatched",
            detected_type=detected_type,
            detected_period=detected_period,
            entity_match="unknown",
            account_match=None,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            matched_item_refs=[],
            uncertainty_reasons=["No requirement was associated with this document."],
            evidence_refs=evidence_refs,
            issues=["Document could not be matched to a configured requirement."],
        )

    requirement = _requirement_by_id(
        case,
        requirement_id,
    )

    # Never trust a requirement ID that does not belong to this case.
    if requirement is None:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=None,
            result="unmatched",
            detected_type=detected_type,
            detected_period=detected_period,
            entity_match="unknown",
            account_match=None,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            matched_item_refs=[],
            uncertainty_reasons=["Requirement reference does not belong to the case."],
            evidence_refs=evidence_refs,
            issues=["Unknown or unauthorised requirement reference."],
        )

    # Wrong document category.
    if detected_type != requirement.document_type:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_correction",
            detected_type=detected_type,
            detected_period=detected_period,
            entity_match="unknown",
            account_match=None,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            matched_item_refs=[],
            uncertainty_reasons=[],
            evidence_refs=evidence_refs,
            issues=[
                (
                    f"Detected document type {detected_type}; "
                    f"{requirement.document_type} is required."
                )
            ],
        )

    # For the MVP we cannot safely verify entity/account identity
    # unless an explicit trusted extraction mechanism is added.
    entity_match = "unknown"

    account_match = "unknown" if requirement.scope.account_ref is not None else None

    # Coverage-based requirement, especially bank statements.
    if requirement.completion_rule.kind == "coverage":
        if coverage_start is None or coverage_end is None:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=detected_type,
                detected_period=detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=None,
                coverage_end=None,
                matched_item_refs=[],
                uncertainty_reasons=[
                    "Document coverage dates could not be determined."
                ],
                evidence_refs=evidence_refs,
                issues=["Coverage cannot be verified automatically."],
            )

        required_start = requirement.scope.coverage_start
        required_end = requirement.scope.coverage_end

        if (
            required_start is not None
            and required_end is not None
            and (coverage_start > required_start or coverage_end < required_end)
        ):
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_correction",
                detected_type=detected_type,
                detected_period=detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                matched_item_refs=[],
                uncertainty_reasons=[],
                evidence_refs=evidence_refs,
                issues=[
                    (
                        f"Document covers {coverage_start.isoformat()} "
                        f"to {coverage_end.isoformat()}; "
                        f"{required_start.isoformat()} "
                        f"to {required_end.isoformat()} is required."
                    )
                ],
            )

    # Account/entity identity is still unverified, therefore this
    # baseline must not claim satisfies.
    if entity_match == "unknown" or account_match == "unknown":
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_review",
            detected_type=detected_type,
            detected_period=detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            matched_item_refs=[],
            uncertainty_reasons=[
                "Client/entity or account identity has not been verified."
            ],
            evidence_refs=evidence_refs,
            issues=["Identity verification is required before satisfaction."],
        )

    return DocumentFinding(
        finding_id=finding_id,
        responsibility="document_assessment",
        case_id=case.case_id,
        input_state_version=case.state_version,
        document_id=document_id,
        requirement_id=requirement.requirement_id,
        result="satisfies",
        detected_type=detected_type,
        detected_period=detected_period,
        entity_match=entity_match,
        account_match=account_match,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        matched_item_refs=[],
        uncertainty_reasons=[],
        evidence_refs=evidence_refs,
        issues=[],
    )
