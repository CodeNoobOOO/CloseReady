import re
from datetime import date
from uuid import uuid4

from closeready.document_models import (
    DocumentExtraction,
    DocumentFinding,
    MatchResult,
)
from closeready.document_normalization import (
    accounts_match,
    entities_match,
)
from closeready.llm_document_analyzer import LLMDocumentAnalysis
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


def _llm_match_result(value: bool | None) -> MatchResult:
    if value is True:
        return "match"

    if value is False:
        return "mismatch"

    return "unknown"


def _normalized_llm_period(analysis: LLMDocumentAnalysis) -> str | None:
    if analysis.detected_period is not None and re.fullmatch(
        r"\d{4}-(0[1-9]|1[0-2])",
        analysis.detected_period,
    ):
        return analysis.detected_period

    if (
        analysis.coverage_start is not None
        and analysis.coverage_end is not None
        and analysis.coverage_start.year == analysis.coverage_end.year
        and analysis.coverage_start.month == analysis.coverage_end.month
    ):
        return (
            f"{analysis.coverage_start.year:04d}-"
            f"{analysis.coverage_start.month:02d}"
        )

    return None


def _material_uncertainty_reasons(
    requirement: Requirement,
    analysis: LLMDocumentAnalysis,
    entity_match: MatchResult,
    account_match: MatchResult | None,
) -> list[str]:
    """Keep only uncertainty that is unresolved for this Requirement.

    Structured facts and application comparisons take precedence over model
    prose. Reasons without codes are retained as a conservative compatibility
    fallback for scripted or older callers.
    """
    if not analysis.uncertainty_reasons:
        return []
    codes = set(analysis.uncertainty_codes)
    if not codes:
        return list(analysis.uncertainty_reasons)

    material = "document_quality_problem" in codes
    material = material or (
        "document_type_unclear" in codes
        and analysis.detected_type != requirement.document_type
    )
    material = material or (
        "entity_unclear" in codes and entity_match != "match"
    )
    material = material or (
        "account_unclear" in codes
        and requirement.document_type == "bank_statement"
        and account_match != "match"
    )
    material = material or (
        "period_unclear" in codes
        and analysis.detected_period != requirement.accounting_period
    )

    if "coverage_unclear" in codes and requirement.completion_rule.kind == "coverage":
        material = material or (
            analysis.coverage_start is None
            or analysis.coverage_end is None
            or analysis.coverage_start > requirement.scope.coverage_start
            or analysis.coverage_end < requirement.scope.coverage_end
        )

    if (
        "item_reference_unclear" in codes
        and requirement.completion_rule.kind == "explicit_items"
    ):
        matched = set(analysis.matched_item_refs)
        expected = set(requirement.completion_rule.expected_item_refs)
        material = material or not matched or not matched.issubset(expected)

    return list(analysis.uncertainty_reasons) if material else []


def assess_llm_analysis(
    *,
    case: CaseSnapshot,
    document_id: str,
    analysis: LLMDocumentAnalysis,
    requirement_id: str | None,
    duplicate: bool = False,
) -> DocumentFinding:
    finding_id = f"finding_document_{uuid4().hex}"
    analysis = analysis.model_copy(
        update={"detected_period": _normalized_llm_period(analysis)}
    )

    evidence_refs = [
        EvidenceRef(
            document_id=document_id,
            page=evidence.page,
            excerpt=evidence.excerpt,
        )
        for evidence in analysis.evidence
    ]

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
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match="unknown",
            account_match=None,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=["Exact duplicate file hash detected."],
            evidence_refs=evidence_refs,
            issues=["Duplicate document must not create duplicate satisfaction."],
        )

    # 2. No requirement supplied
    if requirement_id is None:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=None,
            result="unmatched",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match="unknown",
            account_match=None,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=[
                "No authorised requirement was associated with the document."
            ],
            evidence_refs=evidence_refs,
            issues=["Document requires manual requirement assignment."],
        )

    # 3. Requirement must belong to the current Case
    requirement = _requirement_by_id(
        case,
        requirement_id,
    )

    if requirement is None:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=None,
            result="unmatched",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match="unknown",
            account_match=None,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=[
                "Requirement reference is not authorised for this Case."
            ],
            evidence_refs=evidence_refs,
            issues=["Unknown or unauthorised requirement reference."],
        )

    # 4. Document type must be known
    if analysis.detected_type is None:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_review",
            detected_type=None,
            detected_period=analysis.detected_period,
            entity_match="unknown",
            account_match=(
                "unknown"
                if requirement.scope.masked_account_identifier is not None
                else None
            ),
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=[
                *analysis.uncertainty_reasons,
                "Document type could not be determined.",
            ],
            evidence_refs=evidence_refs,
            issues=["Document type requires human review."],
        )

    # 5. Explicit type mismatch
    if analysis.detected_type != requirement.document_type:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_correction",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match="unknown",
            account_match=(
                "unknown"
                if requirement.scope.masked_account_identifier is not None
                else None
            ),
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=analysis.uncertainty_reasons,
            evidence_refs=evidence_refs,
            issues=[
                (
                    f"Detected document type "
                    f"{analysis.detected_type}; "
                    f"{requirement.document_type} is required."
                )
            ],
        )

    # 6. Entity comparison
    entity_match = _llm_match_result(
        entities_match(
            requirement.scope.entity_id,
            analysis.entity_name,
        )
    )

    # 7. Account comparison
    #
    # account_ref is deliberately NOT used here.
    # It remains S1's stable internal account reference.
    #
    # S2 compares only the authorised masked identifier.
    expected_account = requirement.scope.masked_account_identifier

    if requirement.document_type == "bank_statement" and requirement.scope.account_ref is not None:
        if expected_account is None:
            account_match: MatchResult | None = "unknown"
        else:
            account_match = _llm_match_result(
                accounts_match(expected_account, analysis.account_identifier)
            )
    else:
        account_match = None
    if expected_account is not None and account_match is None:
        account_match = _llm_match_result(
            accounts_match(
                expected_account,
                analysis.account_identifier,
            )
        )

    material_uncertainty = _material_uncertainty_reasons(
        requirement,
        analysis,
        entity_match,
        account_match,
    )

    # 8. Only unresolved, requirement-relevant uncertainty requires review.
    if material_uncertainty:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_review",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=material_uncertainty,
            evidence_refs=evidence_refs,
            issues=["Document analysis contains material uncertainty."],
        )

    # 9. Entity could not be verified
    if entity_match == "unknown":
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_review",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=["Document entity could not be verified."],
            evidence_refs=evidence_refs,
            issues=["Entity verification requires human review."],
        )

    # 10. Clear entity mismatch
    if entity_match == "mismatch":
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_correction",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=[],
            evidence_refs=evidence_refs,
            issues=["Document entity does not match the requirement."],
        )

    # 11. Account-scoped requirements:
    #     mismatch and unknown both require human review.
    if account_match in ("unknown", "mismatch"):
        if account_match == "unknown":
            reason = "Document account could not be verified."
        else:
            reason = "Document account differs from the " "expected masked identifier."

        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_review",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=[reason],
            evidence_refs=evidence_refs,
            issues=["Account verification requires human review."],
        )

    # 12. Coverage-based completion rule
    if requirement.completion_rule.kind == "coverage":
        required_start = requirement.scope.coverage_start
        required_end = requirement.scope.coverage_end

        if analysis.coverage_start is None or analysis.coverage_end is None:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[
                    "Document coverage dates could not be determined."
                ],
                evidence_refs=evidence_refs,
                issues=["Coverage cannot be verified automatically."],
            )

        if (
            required_start is not None
            and required_end is not None
            and (
                analysis.coverage_start > required_start
                or analysis.coverage_end < required_end
            )
        ):
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_correction",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[],
                evidence_refs=evidence_refs,
                issues=[
                    (
                        f"Document covers "
                        f"{analysis.coverage_start.isoformat()} "
                        f"to {analysis.coverage_end.isoformat()}; "
                        f"{required_start.isoformat()} "
                        f"to {required_end.isoformat()} is required."
                    )
                ],
            )

    # 13. Explicit-item completion rule
    if requirement.completion_rule.kind == "explicit_items":
        expected_items = set(requirement.completion_rule.expected_item_refs)
        matched_items = set(analysis.matched_item_refs)

        # The LLM may never introduce an item outside the
        # authorised candidate set.
        if not matched_items.issubset(expected_items):
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[
                    "Document analysis returned an " "unauthorised item reference."
                ],
                evidence_refs=evidence_refs,
                issues=["Matched item references require human review."],
            )

        if not matched_items:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=[],
                uncertainty_reasons=[
                    "No expected items could be verified " "in this document."
                ],
                evidence_refs=evidence_refs,
                issues=["Expected-item matching requires human review."],
            )

        if len(expected_items) != 1:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[
                    "Multiple expected item references require separate "
                    "Requirements or human review."
                ],
                evidence_refs=evidence_refs,
                issues=["Multiple expected items cannot be accepted automatically."],
            )

        if requirement.document_type not in ("invoice", "receipt"):
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[
                    "This supporting document type requires human review."
                ],
                evidence_refs=evidence_refs,
                issues=["Supporting document matching is not automatically accepted."],
            )

        if matched_items != expected_items:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[
                    "The expected item reference was not matched exactly."
                ],
                evidence_refs=evidence_refs,
                issues=["Expected-item matching requires human review."],
            )

        if analysis.detected_period is None:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=None,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=["Document period could not be verified."],
                evidence_refs=evidence_refs,
                issues=["Period verification requires human review."],
            )

        if analysis.detected_period != requirement.accounting_period:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_correction",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[],
                evidence_refs=evidence_refs,
                issues=[
                    f"Document period {analysis.detected_period} does not match "
                    f"required period {requirement.accounting_period}."
                ],
            )

        if not evidence_refs:
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=["No valid document evidence was supplied."],
                evidence_refs=[],
                issues=["Evidence is required before satisfaction."],
            )

        expected_reference = next(iter(expected_items))
        if not any(
            expected_reference.casefold() in evidence.excerpt.casefold()
            for evidence in analysis.evidence
        ):
            return DocumentFinding(
                finding_id=finding_id,
                responsibility="document_assessment",
                case_id=case.case_id,
                input_state_version=case.state_version,
                document_id=document_id,
                requirement_id=requirement.requirement_id,
                result="needs_review",
                detected_type=analysis.detected_type,
                detected_period=analysis.detected_period,
                entity_match=entity_match,
                account_match=account_match,
                coverage_start=analysis.coverage_start,
                coverage_end=analysis.coverage_end,
                matched_item_refs=analysis.matched_item_refs,
                uncertainty_reasons=[
                    "The matched item reference is not supported by cited evidence."
                ],
                evidence_refs=evidence_refs,
                issues=["Reference evidence requires human review."],
            )

        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="satisfies",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=[],
            evidence_refs=evidence_refs,
            issues=[],
        )

    # 14. Evidence gate
    #
    # A model assertion without traceable evidence must never
    # create a satisfies finding.
    if not evidence_refs:
        return DocumentFinding(
            finding_id=finding_id,
            responsibility="document_assessment",
            case_id=case.case_id,
            input_state_version=case.state_version,
            document_id=document_id,
            requirement_id=requirement.requirement_id,
            result="needs_review",
            detected_type=analysis.detected_type,
            detected_period=analysis.detected_period,
            entity_match=entity_match,
            account_match=account_match,
            coverage_start=analysis.coverage_start,
            coverage_end=analysis.coverage_end,
            matched_item_refs=analysis.matched_item_refs,
            uncertainty_reasons=["No valid document evidence was supplied."],
            evidence_refs=[],
            issues=["Evidence is required before satisfaction."],
        )

    # 15. All deterministic checks passed.
    #
    # This is still only an evidence proposal. DocumentStore
    # remains responsible for application-layer revalidation
    # before the Requirement can become accepted.
    return DocumentFinding(
        finding_id=finding_id,
        responsibility="document_assessment",
        case_id=case.case_id,
        input_state_version=case.state_version,
        document_id=document_id,
        requirement_id=requirement.requirement_id,
        result="satisfies",
        detected_type=analysis.detected_type,
        detected_period=analysis.detected_period,
        entity_match=entity_match,
        account_match=account_match,
        coverage_start=analysis.coverage_start,
        coverage_end=analysis.coverage_end,
        matched_item_refs=analysis.matched_item_refs,
        uncertainty_reasons=[],
        evidence_refs=evidence_refs,
        issues=[],
    )
