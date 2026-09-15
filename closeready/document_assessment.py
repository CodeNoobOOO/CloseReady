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


def detect_account_ref(text: str) -> str | None:
    pattern = re.compile(
        r"account\s*(?:number|ref)?\s*[:\-]\s*([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )

    match = pattern.search(text)

    if match is None:
        return None

    return match.group(1)


def detect_entity_id(text: str) -> str | None:
    pattern = re.compile(
        r"entity\s*(?:id)?\s*[:\-]\s*([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )

    match = pattern.search(text)

    if match is None:
        return None

    return match.group(1)


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

    # 1. Exact duplicate
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

    # 2. Valid PDF, but no usable text could be extracted
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

    # 3. Combine extracted page text
    text = "\n".join(page.text for page in extraction.pages)

    # 4. Detect document information
    detected_type = detect_document_type(text)

    coverage_start, coverage_end = detect_coverage(text)

    detected_period = detect_accounting_period(
        coverage_start,
        coverage_end,
    )

    detected_entity_id = detect_entity_id(text)
    detected_account_ref = detect_account_ref(text)

    # 5. No requirement was supplied
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

    # 6. Find requirement inside the current case
    requirement = _requirement_by_id(
        case,
        requirement_id,
    )

    # Requirement does not belong to this case
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

    # 7. Wrong document type
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

    # 8. Compare entity
    if detected_entity_id is None:
        entity_match = "unknown"
    elif detected_entity_id == requirement.scope.entity_id:
        entity_match = "match"
    else:
        entity_match = "mismatch"

    # 9. Compare account
    if requirement.scope.account_ref is None:
        account_match = None
    elif detected_account_ref is None:
        account_match = "unknown"
    elif detected_account_ref == requirement.scope.account_ref:
        account_match = "match"
    else:
        account_match = "mismatch"

    # 10. Coverage-based requirements
    if requirement.completion_rule.kind == "coverage":

        # Could not determine coverage
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

        # Coverage is incomplete or wrong
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
                        f"Document covers "
                        f"{coverage_start.isoformat()} "
                        f"to {coverage_end.isoformat()}; "
                        f"{required_start.isoformat()} "
                        f"to {required_end.isoformat()} is required."
                    )
                ],
            )

    # 11. Explicit entity/account mismatch
    if entity_match == "mismatch" or account_match == "mismatch":
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
            issues=["Document entity or account does not match the requirement."],
        )

    # 12. Entity/account could not be verified
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

    # 13. Everything required has been verified
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
