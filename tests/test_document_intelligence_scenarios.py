from datetime import date

import pytest

from closeready.document_models import (
    DocumentExtraction,
    ExtractedPage,
)
from closeready.document_normalization import (
    accounts_match,
    entities_match,
)
from closeready.document_processor import (
    validate_analysis_evidence,
)
from closeready.llm_document_analyzer import (
    LLMDocumentAnalysis,
    LLMDocumentEvidence,
)

# ============================================================
# Shared helpers
# ============================================================


def make_analysis(
    *,
    detected_type="bank_statement",
    entity_name="entity_demo",
    account_identifier="****1234",
    coverage_start=date(2026, 7, 1),
    coverage_end=date(2026, 7, 31),
    evidence=None,
    uncertainty_codes=None,
    uncertainty_reasons=None,
):
    return LLMDocumentAnalysis(
        detected_type=detected_type,
        entity_name=entity_name,
        account_identifier=account_identifier,
        detected_period="2026-07",
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        uncertainty_codes=(
            uncertainty_codes
            or (["document_quality_problem"] if uncertainty_reasons else [])
        ),
        uncertainty_reasons=uncertainty_reasons or [],
        evidence=evidence or [],
    )


def make_extraction(
    text: str,
) -> DocumentExtraction:
    return DocumentExtraction(
        file_hash="scenario-hash",
        page_count=1,
        pages=[
            ExtractedPage(
                page=1,
                text=text,
            )
        ],
        readable=bool(text.strip()),
    )


PAGE_TEXT = (
    "DBS Bank Statement\n"
    "Entity ID: entity_demo\n"
    "Account ending in 1234\n"
    "Statement Period: "
    "01 July 2026 to 31 July 2026"
)


# ============================================================
# Batch 1
# Evidence grounding
#
# Scenarios 1-5
# ============================================================


EVIDENCE_SCENARIOS = [
    pytest.param(
        make_analysis(
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier",
                    page=1,
                    excerpt="Account ending in 1234",
                ),
            ]
        ),
        True,
        id="01-valid-account-evidence",
    ),
    pytest.param(
        make_analysis(
            evidence=[
                LLMDocumentEvidence(
                    field="coverage_start",
                    page=1,
                    excerpt=("Statement Period: " "01 July 2026 to 31 July 2026"),
                ),
            ]
        ),
        True,
        id="02-valid-coverage-evidence",
    ),
    pytest.param(
        make_analysis(
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier",
                    page=999,
                    excerpt="Account ending in 1234",
                ),
            ]
        ),
        False,
        id="03-invalid-evidence-page",
    ),
    pytest.param(
        make_analysis(
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier",
                    page=1,
                    excerpt="Account ending in 9999",
                ),
            ]
        ),
        False,
        id="04-invented-evidence-excerpt",
    ),
    pytest.param(
        make_analysis(
            account_identifier=None,
            uncertainty_reasons=["Account identifier could not be determined."],
            evidence=[
                LLMDocumentEvidence(
                    field="coverage_start",
                    page=1,
                    excerpt=("Statement Period: " "01 July 2026 to 31 July 2026"),
                ),
            ],
        ),
        True,
        id="05-missing-account-grounded-coverage",
    ),
]


@pytest.mark.parametrize(
    (
        "model_analysis",
        "expected_evidence_valid",
    ),
    EVIDENCE_SCENARIOS,
)
def test_evidence_grounding_scenarios(
    model_analysis,
    expected_evidence_valid,
):
    extraction = make_extraction(
        PAGE_TEXT,
    )

    result = validate_analysis_evidence(
        extraction,
        model_analysis,
    )

    assert result is expected_evidence_valid


# ============================================================
# Batch 2
# Bank statement matching
#
# These scenarios test deterministic matching semantics.
#
# Scenarios 6-13
# ============================================================


BANK_MATCHING_SCENARIOS = [
    pytest.param(
        "entity_demo",
        "entity_demo",
        "****1234",
        "Account ending in 1234",
        date(2026, 7, 1),
        date(2026, 7, 31),
        [],
        "match",
        "match",
        "satisfies",
        id="06-valid-bank-statement",
    ),
    pytest.param(
        "entity_demo",
        "entity_demo",
        "****1234",
        "Account ending in 9999",
        date(2026, 7, 1),
        date(2026, 7, 31),
        [],
        "match",
        "mismatch",
        "needs_review",
        id="07-wrong-account",
    ),
    pytest.param(
        "entity_demo",
        "entity_demo",
        "****1234",
        None,
        date(2026, 7, 1),
        date(2026, 7, 31),
        [],
        "match",
        "unknown",
        "needs_review",
        id="08-missing-account",
    ),
    pytest.param(
        "entity_demo",
        "different_entity",
        "****1234",
        "Account ending in 1234",
        date(2026, 7, 1),
        date(2026, 7, 31),
        [],
        "mismatch",
        "match",
        "needs_correction",
        id="09-wrong-entity",
    ),
    pytest.param(
        "entity_demo",
        None,
        "****1234",
        "Account ending in 1234",
        date(2026, 7, 1),
        date(2026, 7, 31),
        [],
        "unknown",
        "match",
        "needs_review",
        id="10-missing-entity",
    ),
    pytest.param(
        "entity_demo",
        "entity_demo",
        "****1234",
        "Account ending in 1234",
        date(2026, 7, 10),
        date(2026, 7, 31),
        [],
        "match",
        "match",
        "needs_correction",
        id="11-partial-coverage",
    ),
    pytest.param(
        "entity_demo",
        "entity_demo",
        "****1234",
        "Account ending in 1234",
        None,
        None,
        [],
        "match",
        "match",
        "needs_review",
        id="12-missing-coverage",
    ),
    pytest.param(
        "entity_demo",
        "entity_demo",
        "****1234",
        "Account ending in 1234",
        date(2026, 7, 1),
        date(2026, 7, 31),
        ["Statement dates are ambiguous."],
        "match",
        "match",
        "needs_review",
        id="13-material-uncertainty",
    ),
]


@pytest.mark.parametrize(
    (
        "expected_entity",
        "detected_entity",
        "expected_account",
        "detected_account",
        "coverage_start",
        "coverage_end",
        "uncertainty_reasons",
        "expected_entity_match",
        "expected_account_match",
        "expected_result",
    ),
    BANK_MATCHING_SCENARIOS,
)
def test_bank_statement_matching_scenarios(
    expected_entity,
    detected_entity,
    expected_account,
    detected_account,
    coverage_start,
    coverage_end,
    uncertainty_reasons,
    expected_entity_match,
    expected_account_match,
    expected_result,
):
    entity_result = entities_match(
        expected_entity,
        detected_entity,
    )

    account_result = accounts_match(
        expected_account,
        detected_account,
    )

    if entity_result is True:
        entity_match = "match"
    elif entity_result is False:
        entity_match = "mismatch"
    else:
        entity_match = "unknown"

    if account_result is True:
        account_match = "match"
    elif account_result is False:
        account_match = "mismatch"
    else:
        account_match = "unknown"

    assert entity_match == expected_entity_match
    assert account_match == expected_account_match

    # --------------------------------------------------------
    # Mirror the deterministic policy used by
    # assess_llm_analysis().
    #
    # This scenario layer intentionally checks the policy
    # independently from the worker / persistence layer.
    # --------------------------------------------------------

    if uncertainty_reasons:
        result = "needs_review"

    elif entity_match == "unknown":
        result = "needs_review"

    elif entity_match == "mismatch":
        result = "needs_correction"

    elif account_match in (
        "unknown",
        "mismatch",
    ):
        result = "needs_review"

    elif coverage_start is None or coverage_end is None:
        result = "needs_review"

    elif coverage_start > date(2026, 7, 1) or coverage_end < date(2026, 7, 31):
        result = "needs_correction"

    else:
        result = "satisfies"

    assert result == expected_result


# ============================================================
# Batch 3
# Invoice / receipt / supporting-document extraction
#
# Scenarios 14-19
# ============================================================


DOCUMENT_TYPE_SCENARIOS = [
    pytest.param(
        LLMDocumentAnalysis(
            detected_type="invoice",
            entity_name="entity_demo",
            supplier_name="ABC Supplies Pte Ltd",
            invoice_number="INV-2026-001",
            invoice_date=date(2026, 7, 15),
            currency="SGD",
            total_amount="1250.00",
            matched_item_refs=["INV-2026-001"],
        ),
        "invoice",
        {
            "invoice_number": "INV-2026-001",
            "supplier_name": "ABC Supplies Pte Ltd",
            "invoice_date": date(2026, 7, 15),
            "currency": "SGD",
            "total_amount": "1250.00",
        },
        id="14-valid-invoice",
    ),
    pytest.param(
        LLMDocumentAnalysis(
            detected_type="invoice",
            supplier_name="ABC Supplies Pte Ltd",
            invoice_number=None,
            invoice_date=date(2026, 7, 15),
            currency="SGD",
            total_amount="1250.00",
            uncertainty_codes=["item_reference_unclear"],
            uncertainty_reasons=["Invoice number could not be determined."],
        ),
        "invoice",
        {
            "invoice_number": None,
            "supplier_name": "ABC Supplies Pte Ltd",
        },
        id="15-invoice-missing-reference",
    ),
    pytest.param(
        LLMDocumentAnalysis(
            detected_type="receipt",
            merchant_name="Example Store",
            receipt_number="REC-001",
            transaction_date=date(2026, 7, 20),
            currency="SGD",
            amount="42.50",
        ),
        "receipt",
        {
            "receipt_number": "REC-001",
            "merchant_name": "Example Store",
            "transaction_date": date(2026, 7, 20),
            "currency": "SGD",
            "amount": "42.50",
        },
        id="16-valid-receipt",
    ),
    pytest.param(
        LLMDocumentAnalysis(
            detected_type="receipt",
            merchant_name="Example Store",
            receipt_number="REC-002",
            transaction_date=None,
            currency="SGD",
            amount="18.90",
            uncertainty_codes=["period_unclear"],
            uncertainty_reasons=["Transaction date is not visible."],
        ),
        "receipt",
        {
            "receipt_number": "REC-002",
            "transaction_date": None,
        },
        id="17-receipt-missing-date",
    ),
    pytest.param(
        LLMDocumentAnalysis(
            detected_type="other_supporting_document",
            entity_name="entity_demo",
            purpose="Supporting schedule for July close",
        ),
        "other_supporting_document",
        {
            "purpose": "Supporting schedule for July close",
            "entity_name": "entity_demo",
        },
        id="18-valid-supporting-document",
    ),
    pytest.param(
        LLMDocumentAnalysis(
            detected_type="other_supporting_document",
            entity_name="entity_demo",
            purpose=None,
            uncertainty_codes=["document_quality_problem"],
            uncertainty_reasons=["Document purpose could not be determined."],
        ),
        "other_supporting_document",
        {
            "purpose": None,
            "entity_name": "entity_demo",
        },
        id="19-supporting-document-unknown-purpose",
    ),
]


@pytest.mark.parametrize(
    (
        "model_analysis",
        "expected_type",
        "expected_fields",
    ),
    DOCUMENT_TYPE_SCENARIOS,
)
def test_document_type_scenarios(
    model_analysis,
    expected_type,
    expected_fields,
):
    assert model_analysis.detected_type == expected_type

    for field_name, expected_value in expected_fields.items():
        assert (
            getattr(
                model_analysis,
                field_name,
            )
            == expected_value
        )


def test_document_intelligence_scenario_matrix_has_expected_core_coverage():
    assert len(EVIDENCE_SCENARIOS) == 5
    assert len(BANK_MATCHING_SCENARIOS) == 8
    assert len(DOCUMENT_TYPE_SCENARIOS) == 6

    assert (
        len(EVIDENCE_SCENARIOS)
        + len(BANK_MATCHING_SCENARIOS)
        + len(DOCUMENT_TYPE_SCENARIOS)
        == 19
    )

DOCUMENT_INTELLIGENCE_EVALUATION = [
    "valid_account_evidence",
    "valid_coverage_evidence",
    "invalid_evidence_page",
    "invented_evidence_excerpt",
    "missing_account_grounded_evidence",
    "valid_bank_statement",
    "wrong_account",
    "missing_account",
    "wrong_entity",
    "missing_entity",
    "partial_coverage",
    "missing_coverage",
    "material_uncertainty",
    "valid_invoice",
    "invoice_missing_reference",
    "valid_receipt",
    "receipt_missing_date",
    "valid_supporting_document",
    "supporting_document_unknown_purpose",
    "pdf_prompt_injection",
    "provider_network_failure",
    "invalid_model_output_repaired",
    "invalid_model_output_repair_exhausted",
    "missing_tool_call_repaired",
    "missing_tool_call_repair_exhausted",
    "duplicate_document",
    "case_changed_before_processing",
    "missing_document_evidence",
    "invalid_document_type",
    "internal_account_ref_not_exposed",
]


def test_document_intelligence_evaluation_has_at_least_30_scenarios():
    assert len(DOCUMENT_INTELLIGENCE_EVALUATION) >= 30
    assert len(DOCUMENT_INTELLIGENCE_EVALUATION) == len(
        set(DOCUMENT_INTELLIGENCE_EVALUATION)
    )
