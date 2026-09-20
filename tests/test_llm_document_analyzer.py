from datetime import date

import pytest
from pydantic import ValidationError

from closeready.llm import Completion, ProviderError, ToolCall
from closeready.llm_document_analyzer import (
    CandidateRequirement,
    DocumentAnalysisError,
    DocumentAnalysisRequest,
    DocumentAnalyzer,
    LiveLLMDocumentAnalyzer,
    LLMDocumentAnalysis,
    LLMDocumentEvidence,
    ScriptedDocumentAnalyzer,
)


class FakeProvider:
    provider_name = "fake"
    model = "fake-model"
    live = False

    def __init__(self, completions):
        self.completions = list(completions)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((messages, tools))

        result = self.completions.pop(0)

        if isinstance(result, Exception):
            raise result

        return result


def make_request() -> DocumentAnalysisRequest:
    return DocumentAnalysisRequest(
        pages=[
            "DBS Bank Statement",
            "Statement period 01 Jul 2026 to 31 Jul 2026",
        ],
        accounting_period="2026-07",
        candidate_requirements=[
            CandidateRequirement(
                requirement_id="req_july_bank",
                document_type="bank_statement",
                accounting_period="2026-07",
                entity_name="Northstar Pte Ltd",
                masked_account_identifier="****1234",
                coverage_start=date(2026, 7, 1),
                coverage_end=date(2026, 7, 31),
            )
        ],
    )


def test_valid_bank_statement_analysis():
    analysis = LLMDocumentAnalysis(
        detected_type="bank_statement",
        entity_name="Northstar Pte Ltd",
        bank_name="DBS Bank",
        account_identifier="****1234",
        detected_period="2026-07",
        coverage_start=date(2026, 7, 1),
        coverage_end=date(2026, 7, 31),
        currency="SGD",
        matched_item_refs=[],
        uncertainty_reasons=[],
        evidence=[
            LLMDocumentEvidence(
                field="coverage_start",
                page=1,
                excerpt="Statement period 01 Jul 2026 to 31 Jul 2026",
            )
        ],
    )

    assert analysis.detected_type == "bank_statement"
    assert analysis.entity_name == "Northstar Pte Ltd"
    assert analysis.bank_name == "DBS Bank"
    assert analysis.account_identifier == "****1234"
    assert analysis.detected_period == "2026-07"
    assert analysis.coverage_start == date(2026, 7, 1)
    assert analysis.coverage_end == date(2026, 7, 31)
    assert analysis.currency == "SGD"

    assert len(analysis.evidence) == 1
    assert analysis.evidence[0].page == 1


def test_analysis_allows_unknown_fields_as_none():
    analysis = LLMDocumentAnalysis()

    assert analysis.detected_type is None
    assert analysis.entity_name is None
    assert analysis.account_identifier is None
    assert analysis.detected_period is None
    assert analysis.coverage_start is None
    assert analysis.coverage_end is None

    assert analysis.matched_item_refs == []
    assert analysis.uncertainty_reasons == []
    assert analysis.evidence == []


def test_unknown_schema_fields_are_rejected():
    with pytest.raises(ValidationError):
        LLMDocumentAnalysis.model_validate(
            {
                "detected_type": "bank_statement",
                "invented_field": "fake",
            }
        )


def test_invalid_document_type_is_rejected():
    with pytest.raises(ValidationError):
        LLMDocumentAnalysis.model_validate(
            {
                "detected_type": "totally_fake_document",
            }
        )


def test_evidence_page_must_be_positive():
    with pytest.raises(ValidationError):
        LLMDocumentEvidence(
            field="coverage_start",
            page=0,
            excerpt="Statement period",
        )


def test_empty_evidence_excerpt_is_rejected():
    with pytest.raises(ValidationError):
        LLMDocumentEvidence(
            field="coverage_start",
            page=1,
            excerpt="",
        )


def test_analysis_request_contains_authorised_requirements():
    request = make_request()

    assert request.accounting_period == "2026-07"
    assert len(request.pages) == 2

    requirement = request.candidate_requirements[0]

    assert requirement.requirement_id == "req_july_bank"
    assert requirement.document_type == "bank_statement"
    assert requirement.masked_account_identifier == "****1234"
    assert requirement.coverage_start == date(2026, 7, 1)
    assert requirement.coverage_end == date(2026, 7, 31)


def test_candidate_requirement_does_not_expose_internal_account_ref():
    requirement = CandidateRequirement(
        requirement_id="req_july_bank",
        document_type="bank_statement",
        accounting_period="2026-07",
        masked_account_identifier="****1234",
    )

    data = requirement.model_dump()

    assert data["masked_account_identifier"] == "****1234"
    assert "account_ref" not in data


def test_analysis_request_requires_at_least_one_page():
    with pytest.raises(ValidationError):
        DocumentAnalysisRequest(
            pages=[],
            accounting_period="2026-07",
            candidate_requirements=[
                CandidateRequirement(
                    requirement_id="req_july_bank",
                    document_type="bank_statement",
                    accounting_period="2026-07",
                )
            ],
        )


def test_analysis_request_requires_candidate_requirement():
    with pytest.raises(ValidationError):
        DocumentAnalysisRequest(
            pages=["Bank statement"],
            accounting_period="2026-07",
            candidate_requirements=[],
        )


def test_scripted_analyzer_returns_configured_analysis():
    expected = LLMDocumentAnalysis(
        detected_type="bank_statement",
        entity_name="Northstar Pte Ltd",
        account_identifier="****1234",
        detected_period="2026-07",
        coverage_start=date(2026, 7, 1),
        coverage_end=date(2026, 7, 31),
    )

    analyzer = ScriptedDocumentAnalyzer(expected)

    request = make_request()
    result = analyzer.analyze(request)

    assert result == expected
    assert analyzer.last_request == request


def test_scripted_analyzer_matches_protocol():
    expected = LLMDocumentAnalysis(
        detected_type="bank_statement",
    )

    analyzer: DocumentAnalyzer = ScriptedDocumentAnalyzer(expected)

    result = analyzer.analyze(make_request())

    assert result == expected


def valid_completion():
    arguments = """
    {
      "analysis": {
        "detected_type": "bank_statement",
        "entity_name": "Northstar Pte Ltd",
        "bank_name": "DBS Bank",
        "account_identifier": "****1234",
        "detected_period": "2026-07",
        "coverage_start": "2026-07-01",
        "coverage_end": "2026-07-31",
        "currency": "SGD",
        "matched_item_refs": [],
        "uncertainty_reasons": [],
        "evidence": [
          {
            "field": "coverage_start",
            "page": 1,
            "excerpt": "Statement period 01 Jul 2026 to 31 Jul 2026"
          }
        ]
      }
    }
    """

    call = ToolCall(
        call_id="call_1",
        name="submit_document_analysis",
        arguments=arguments,
    )

    return Completion(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [],
        },
        calls=[call],
        usage={"total_tokens": 100},
        finish_reason="tool_calls",
        request_id="request_test_1",
    )


def test_live_analyzer_accepts_valid_structured_tool_call():
    provider = FakeProvider(
        [
            valid_completion(),
        ]
    )

    analyzer = LiveLLMDocumentAnalyzer(provider)

    result = analyzer.analyze(make_request())

    assert result.detected_type == "bank_statement"
    assert result.entity_name == "Northstar Pte Ltd"
    assert result.bank_name == "DBS Bank"
    assert result.account_identifier == "****1234"
    assert result.detected_period == "2026-07"
    assert result.coverage_start == date(2026, 7, 1)
    assert result.coverage_end == date(2026, 7, 31)
    assert result.currency == "SGD"

    assert len(provider.requests) == 1


def test_live_analyzer_converts_provider_error():
    provider = FakeProvider(
        [
            ProviderError(
                "NETWORK_ERROR",
                True,
            )
        ]
    )

    analyzer = LiveLLMDocumentAnalyzer(provider)

    with pytest.raises(DocumentAnalysisError) as exc:
        analyzer.analyze(make_request())

    assert exc.value.code == "NETWORK_ERROR"


def invalid_completion():
    call = ToolCall(
        call_id="call_invalid",
        name="submit_document_analysis",
        arguments="""
        {
          "analysis": {
            "detected_type": "not_a_real_type"
          }
        }
        """,
    )

    return Completion(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [],
        },
        calls=[call],
        usage=None,
        finish_reason="tool_calls",
        request_id="request_invalid",
    )


def test_live_analyzer_repairs_invalid_model_output():
    provider = FakeProvider(
        [
            invalid_completion(),
            valid_completion(),
        ]
    )

    analyzer = LiveLLMDocumentAnalyzer(
        provider,
        max_repairs=1,
    )

    result = analyzer.analyze(make_request())

    assert result.detected_type == "bank_statement"
    assert result.detected_period == "2026-07"
    assert len(provider.requests) == 2


def test_live_analyzer_stops_after_repair_limit():
    provider = FakeProvider(
        [
            invalid_completion(),
            invalid_completion(),
        ]
    )

    analyzer = LiveLLMDocumentAnalyzer(
        provider,
        max_repairs=1,
    )

    with pytest.raises(DocumentAnalysisError) as exc:
        analyzer.analyze(make_request())

    assert exc.value.code == "INVALID_MODEL_OUTPUT"
    assert len(provider.requests) == 2


def completion_without_analysis_tool():
    return Completion(
        message={
            "role": "assistant",
            "content": "I analysed the document.",
        },
        calls=[],
        usage=None,
        finish_reason="stop",
        request_id="request_missing_tool",
    )


def test_live_analyzer_repairs_missing_tool_call():
    provider = FakeProvider(
        [
            completion_without_analysis_tool(),
            valid_completion(),
        ]
    )

    analyzer = LiveLLMDocumentAnalyzer(
        provider,
        max_repairs=1,
    )

    result = analyzer.analyze(make_request())

    assert result.detected_type == "bank_statement"
    assert len(provider.requests) == 2


def test_live_analyzer_rejects_missing_tool_after_repair():
    provider = FakeProvider(
        [
            completion_without_analysis_tool(),
            completion_without_analysis_tool(),
        ]
    )

    analyzer = LiveLLMDocumentAnalyzer(
        provider,
        max_repairs=1,
    )

    with pytest.raises(DocumentAnalysisError) as exc:
        analyzer.analyze(make_request())

    assert exc.value.code == "MISSING_DOCUMENT_ANALYSIS"
    assert len(provider.requests) == 2


def test_valid_invoice_analysis():
    analysis = LLMDocumentAnalysis(
        detected_type="invoice",
        entity_name="Northstar Pte Ltd",
        supplier_name="ABC Supplies Pte Ltd",
        invoice_number="INV-001",
        invoice_date=date(2026, 7, 15),
        currency="SGD",
        total_amount="1250.00",
        matched_item_refs=["INV-001"],
    )

    assert analysis.detected_type == "invoice"
    assert analysis.invoice_number == "INV-001"
    assert analysis.supplier_name == "ABC Supplies Pte Ltd"
    assert analysis.total_amount == "1250.00"
    assert analysis.matched_item_refs == ["INV-001"]


def test_valid_receipt_analysis():
    analysis = LLMDocumentAnalysis(
        detected_type="receipt",
        receipt_number="REC-001",
        merchant_name="Example Store",
        transaction_date=date(2026, 7, 20),
        currency="SGD",
        amount="42.50",
    )

    assert analysis.detected_type == "receipt"
    assert analysis.receipt_number == "REC-001"
    assert analysis.merchant_name == "Example Store"
    assert analysis.amount == "42.50"


def test_valid_other_supporting_document_analysis():
    analysis = LLMDocumentAnalysis(
        detected_type="other_supporting_document",
        entity_name="Northstar Pte Ltd",
        purpose="Supporting schedule for July close",
    )

    assert analysis.detected_type == "other_supporting_document"
    assert analysis.purpose == "Supporting schedule for July close"
