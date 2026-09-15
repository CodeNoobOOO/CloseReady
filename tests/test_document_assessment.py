from datetime import date

from closeready.document_assessment import (
    assess_document,
    detect_accounting_period,
    detect_coverage,
    detect_document_type,
)
from closeready.document_models import (
    DocumentExtraction,
    ExtractedPage,
)
from closeready.models import CaseSnapshot


def make_case() -> CaseSnapshot:
    return CaseSnapshot.model_validate(
        {
            "case_id": "case_demo_july",
            "client_id": "client_demo",
            "accounting_period": "2026-07",
            "timezone": "Asia/Singapore",
            "state_version": 1,
            "readiness_status": "collecting",
            "owner_user_id": "user_manager_demo",
            "due_at": "2026-09-15T09:00:00+08:00",
            "policy_id": "policy_demo",
            "policy_version": 1,
            "requirements": [
                {
                    "requirement_id": "req_july_bank",
                    "document_type": "bank_statement",
                    "accounting_period": "2026-07",
                    "status": "missing",
                    "evidence_refs": [],
                    "reviewer_status": "not_required",
                    "scope": {
                        "entity_id": "entity_demo",
                        "account_ref": "account_demo",
                        "coverage_start": "2026-07-01",
                        "coverage_end": "2026-07-31",
                    },
                    "completion_rule": {
                        "kind": "coverage",
                        "expected_item_refs": [],
                        "allow_multiple_documents": True,
                    },
                }
            ],
        }
    )


def make_extraction(text: str) -> DocumentExtraction:
    return DocumentExtraction(
        file_hash="abc123",
        page_count=1,
        readable=bool(text.strip()),
        pages=[
            ExtractedPage(
                page=1,
                text=text,
            )
        ],
    )


def test_detect_bank_statement():
    assert (
        detect_document_type(
            "DBS Bank Statement\nStatement Period: 01 July 2026 to 31 July 2026"
        )
        == "bank_statement"
    )


def test_detect_invoice():
    assert detect_document_type("Tax Invoice\nInvoice No: INV-1001") == "invoice"


def test_detect_receipt():
    assert detect_document_type("Payment Receipt\nReceipt No: R100") == "receipt"


def test_detect_unknown_document():
    assert detect_document_type("Supporting information") == "other_supporting_document"


def test_detect_coverage():
    start, end = detect_coverage("Statement Period: 01 July 2026 to 31 July 2026")

    assert start == date(2026, 7, 1)
    assert end == date(2026, 7, 31)


def test_detect_accounting_period():
    result = detect_accounting_period(
        date(2026, 7, 1),
        date(2026, 7, 31),
    )

    assert result == "2026-07"


def test_wrong_period_needs_correction():
    case = make_case()

    extraction = make_extraction("""
        DBS Bank Statement
        Statement Period: 01 June 2026 to 30 June 2026
        """)

    finding = assess_document(
        case=case,
        document_id="document_june_demo",
        extraction=extraction,
        requirement_id="req_july_bank",
    )

    assert finding.result == "needs_correction"
    assert finding.detected_type == "bank_statement"
    assert finding.detected_period == "2026-06"
    assert finding.coverage_start == date(2026, 6, 1)
    assert finding.coverage_end == date(2026, 6, 30)


def test_unreadable_document_needs_review():
    case = make_case()

    extraction = make_extraction("")

    finding = assess_document(
        case=case,
        document_id="document_unreadable",
        extraction=extraction,
        requirement_id="req_july_bank",
    )

    assert finding.result == "needs_review"
    assert finding.evidence_refs == []


def test_duplicate_document_does_not_satisfy():
    case = make_case()

    extraction = make_extraction("""
        DBS Bank Statement
        Statement Period: 01 July 2026 to 31 July 2026
        """)

    finding = assess_document(
        case=case,
        document_id="document_duplicate",
        extraction=extraction,
        requirement_id="req_july_bank",
        duplicate=True,
    )

    assert finding.result == "needs_review"
    assert "duplicate" in finding.uncertainty_reasons[0].lower()


def test_unmatched_document_returns_unmatched():
    case = make_case()

    extraction = make_extraction("""
        TAX INVOICE
        Invoice No: INV-1001
        """)

    finding = assess_document(
        case=case,
        document_id="document_invoice",
        extraction=extraction,
        requirement_id=None,
    )

    assert finding.result == "unmatched"


def test_unknown_requirement_does_not_cross_scope():
    case = make_case()

    extraction = make_extraction("""
        DBS Bank Statement
        Statement Period: 01 July 2026 to 31 July 2026
        """)

    finding = assess_document(
        case=case,
        document_id="document_demo",
        extraction=extraction,
        requirement_id="req_from_another_case",
    )

    assert finding.result == "unmatched"
    assert finding.requirement_id is None


def test_correct_period_without_identity_verification_needs_review():
    case = make_case()

    extraction = make_extraction("""
        DBS Bank Statement
        Statement Period: 01 July 2026 to 31 July 2026
        """)

    finding = assess_document(
        case=case,
        document_id="document_july_demo",
        extraction=extraction,
        requirement_id="req_july_bank",
    )

    assert finding.detected_period == "2026-07"
    assert finding.result == "needs_review"
    assert finding.account_match == "unknown"
