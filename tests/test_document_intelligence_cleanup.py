from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from closeready.case_requests import CreateCaseRequest
from closeready.document_assessment import assess_document, assess_llm_analysis
from closeready.document_extraction import calculate_file_hash
from closeready.document_processor import DocumentProcessor
from closeready.document_store import DocumentStore
from closeready.document_models import (
    DocumentExtraction,
    DocumentReviewDecisionRequest,
    ExtractedPage,
)
from closeready.llm import Completion, ProviderError, ToolCall
from closeready.llm_document_analyzer import (
    LLMDocumentAnalysis,
    LLMDocumentEvidence,
    LiveLLMDocumentAnalyzer,
    ScriptedDocumentAnalyzer,
)
from closeready.runtime_store import RuntimeStore
from closeready.store import Store
from closeready.worker import AgentWorker
from test_case_api import access_config, case_request


class SuccessfulProvider:
    provider_name = "successful_test"
    model = "successful-model"
    live = False

    def complete(self, messages, tools):
        return Completion(
            message={"role": "assistant", "content": None, "tool_calls": []},
            calls=[ToolCall(
                call_id="analysis-call",
                name="submit_document_analysis",
                arguments=(
                    '{"analysis":{"detected_type":"bank_statement",'
                    '"entity_name":"entity_demo","account_identifier":"****1234",'
                    '"coverage_start":"2026-07-01","coverage_end":"2026-07-31",'
                    '"evidence":[{"field":"account_identifier","page":1,'
                    '"excerpt":"Account ending in 1234"}]}}'
                ),
            )],
            usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            finish_reason="tool_calls",
            request_id="successful-request",
        )


class FailingProvider:
    provider_name = "failing_test"
    model = "failing-model"
    live = True

    def complete(self, messages, tools):
        raise ProviderError("NETWORK_ERROR", True)


class ReviewAnalysisProvider:
    provider_name = "review_test"
    model = "review-model"
    live = False

    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return Completion(
            message={"role": "assistant", "content": None, "tool_calls": []},
            calls=[ToolCall(
                call_id=f"review-call-{self.calls}",
                name="submit_document_analysis",
                arguments=(
                    '{"analysis":{"detected_type":"bank_statement",'
                    '"entity_name":"entity_demo",'
                    '"coverage_start":"2026-07-01",'
                    '"coverage_end":"2026-07-31",'
                    '"evidence":[{"field":"coverage_start",'
                    '"page":1,"excerpt":"Statement Period: '
                    '01 July 2026 to 31 July 2026"}]}}'
                ),
            )],
            usage={"total_tokens": 12},
            finish_reason="tool_calls",
            request_id=f"review-request-{self.calls}",
        )


def extraction(text, file_hash="hash"):
    return DocumentExtraction(
        file_hash=file_hash,
        page_count=1,
        pages=[ExtractedPage(page=1, text=text)],
        readable=bool(text.strip()),
    )


def llm_bank_analysis(account="Account ending in 1234"):
    return LLMDocumentAnalysis(
        detected_type="bank_statement",
        entity_name="entity_demo",
        account_identifier=account,
        coverage_start=date(2026, 7, 1),
        coverage_end=date(2026, 7, 31),
        evidence=[LLMDocumentEvidence(
            field="account_identifier", page=1, excerpt=account
        )],
    )


class TestDocumentIntelligenceCleanup:
    def setup_method(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(
            "sqlite:///" + (Path(self.tmp.name) / "cleanup.db").as_posix(),
            access_config(),
        )
        self.documents = DocumentStore(self.store)
        self.actor = access_config().principals[0]

    def teardown_method(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def create_case(self, key="cleanup-case", explicit=False):
        payload = case_request()
        payload["requirements"][0]["scope"]["masked_account_identifier"] = "****1234"
        if explicit:
            payload["requirements"][0].update({
                "document_type": "invoice",
                "completion_rule": {
                    "kind": "explicit_items",
                    "expected_item_refs": ["INV-001"],
                    "allow_multiple_documents": False,
                },
            })
        return self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(payload), key
        )

    def worker(self, processor):
        return AgentWorker(
            RuntimeStore(self.store), object(), access_config(),
            document_store=self.documents, document_processor=processor,
        )

    def upload(self, case, requirement_id, content=b"document", key="document"):
        return self.documents.upload(
            self.actor, case.case_id, requirement_id=requirement_id,
            expected_state_version=case.state_version, filename="document.pdf",
            media_type="application/pdf", content=content, key=key,
        )

    def test_missing_masked_bank_identifier_is_unknown_and_unresolved(self):
        case = self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(case_request()), "missing-mask-case"
        )
        finding = assess_llm_analysis(
            case=case, document_id="document", analysis=llm_bank_analysis(),
            requirement_id=case.requirements[0].requirement_id,
        )
        assert finding.account_match == "unknown"
        assert finding.result == "needs_review"

    def test_explicit_item_positive_result_needs_review(self):
        case = self.create_case(explicit=True)
        requirement = case.requirements[0]
        job = self.upload(case, requirement.requirement_id, key="invoice")
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction("Invoice INV-001", calculate_file_hash(content)),
            analyzer=ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
                detected_type="invoice", matched_item_refs=["INV-001"],
                evidence=[LLMDocumentEvidence(
                    field="invoice_number", page=1, excerpt="INV-001"
                )],
            )),
        )
        result = self.worker(processor).run_once()
        assert result.status == "needs_review"
        assert self.store.get_case(self.actor, case.case_id).requirements[0].status == "missing"
        assert self.documents.get_finding(self.actor, case.case_id, job.document_id).result == "needs_review"

    def test_complete_rejection_is_contained_and_job_fails(self):
        case = self.create_case()
        job = self.upload(case, case.requirements[0].requirement_id, key="rejected")
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction(
                "DBS Bank Statement\nEntity ID: entity_demo\n"
                "Account Ref: account_demo\n"
                "Statement Period: 01 July 2026 to 31 July 2026",
                calculate_file_hash(content),
            ),
            assessor=lambda **kwargs: assess_document(**kwargs).model_copy(
                update={"evidence_refs": []}
            ),
        )
        result = self.worker(processor).run_once()
        assert result.status == "failed"
        assert result.error_code == "INVALID_DOCUMENT_RESULT"
        assert self.documents.get_job(self.actor, case.case_id, job.job_id).status == "failed"
        assert self.store.get_case(self.actor, case.case_id).requirements[0].status == "missing"

    def test_unbound_document_is_unmatched(self):
        case = self.create_case()
        job = self.upload(case, None, key="unbound")
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction("Invoice INV-001", calculate_file_hash(content)),
            analyzer=ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
                detected_type="invoice", matched_item_refs=["INV-001"],
                evidence=[LLMDocumentEvidence(
                    field="invoice_number", page=1, excerpt="INV-001"
                )],
            )),
        )
        result = self.worker(processor).run_once()
        assert result.status == "needs_review"
        assert self.documents.get_finding(self.actor, case.case_id, job.document_id).result == "unmatched"
        assert self.store.get_case(self.actor, case.case_id).requirements[0].status == "missing"

    def test_unbound_document_with_multiple_candidates_is_unmatched(self):
        payload = case_request()
        payload["requirements"][0]["scope"]["masked_account_identifier"] = "****1234"
        payload["requirements"].append({
            "document_type": "invoice",
            "accounting_period": "2026-07",
            "scope": {
                "entity_id": "entity_demo",
                "account_ref": None,
                "coverage_start": None,
                "coverage_end": None,
            },
            "completion_rule": {
                "kind": "explicit_items",
                "expected_item_refs": ["INV-001"],
                "allow_multiple_documents": False,
            },
        })
        case = self.store.create_case(
            self.actor,
            CreateCaseRequest.model_validate(payload),
            "unbound-multiple-case",
        )
        job = self.upload(case, None, content=b"unbound-multiple", key="unbound-multiple")
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction(
                "Invoice INV-001", calculate_file_hash(content)
            ),
            analyzer=ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
                detected_type="invoice",
                invoice_number="INV-001",
                matched_item_refs=["INV-001"],
                evidence=[LLMDocumentEvidence(
                    field="invoice_number", page=1, excerpt="INV-001"
                )],
            )),
        )

        result = self.worker(processor).run_once()

        finding = self.documents.get_finding(
            self.actor, case.case_id, job.document_id
        )
        changed = self.store.get_case(self.actor, case.case_id)
        assert result.status == "needs_review"
        assert finding.result == "unmatched"
        assert finding.requirement_id is None
        assert all(requirement.status == "missing" for requirement in changed.requirements)
        assert all(not requirement.evidence_refs for requirement in changed.requirements)
        assert changed.state_version == case.state_version

    def test_telemetry_persists_success_and_failure_without_document_content(self):
        case = self.create_case("telemetry-success-case")
        success_job = self.upload(case, case.requirements[0].requirement_id, key="success")
        success = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction(
                "DBS Bank Statement\nEntity ID: entity_demo\nAccount ending in 1234",
                calculate_file_hash(content),
            ),
            analyzer=LiveLLMDocumentAnalyzer(SuccessfulProvider()),
        )
        assert self.worker(success).run_once().status == "completed"

        failed_case = self.create_case("telemetry-failure-case")
        failed_job = self.upload(failed_case, failed_case.requirements[0].requirement_id, key="failure")
        failure = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction(
                "DBS Bank Statement", calculate_file_hash(content)
            ),
            analyzer=LiveLLMDocumentAnalyzer(FailingProvider()),
        )
        assert self.worker(failure).run_once().status == "failed"

        reloaded = DocumentStore(self.store)
        success_data = reloaded.get_analysis_telemetry(self.actor, case.case_id, success_job.job_id)
        failure_data = reloaded.get_analysis_telemetry(self.actor, failed_case.case_id, failed_job.job_id)
        assert success_data[0]["request_id"] == "successful-request"
        assert success_data[0]["usage"]["total_tokens"] == 18
        assert failure_data[0]["error_code"] == "NETWORK_ERROR"
        assert "Account ending in 1234" not in str(success_data)
        assert "api_key" not in str(failure_data)

    def test_requeued_document_persists_telemetry_for_each_attempt(self):
        case = self.create_case("telemetry-requeue-case")
        requirement = case.requirements[0]
        job = self.upload(case, requirement.requirement_id, key="telemetry-requeue")
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extraction(
                "DBS Bank Statement\nEntity ID: entity_demo\n"
                "Statement Period: 01 July 2026 to 31 July 2026",
                calculate_file_hash(content),
            ),
            analyzer=LiveLLMDocumentAnalyzer(ReviewAnalysisProvider()),
        )

        first = self.worker(processor).run_once()
        assert first.status == "needs_review"
        first_case = self.store.get_case(self.actor, case.case_id)
        self.documents.decide_review(
            self.actor,
            case.case_id,
            job.document_id,
            DocumentReviewDecisionRequest(
                expected_state_version=first_case.state_version,
                decision="reassign_for_processing",
                target_requirement_id=requirement.requirement_id,
                reason="Retry document analysis",
            ),
            "reassign-telemetry",
        )

        second = self.worker(processor).run_once()
        assert second.status == "needs_review"
        telemetry = self.documents.get_analysis_telemetry(
            self.actor, case.case_id, job.job_id
        )
        assert [item["attempt_number"] for item in telemetry] == [1, 2]
        assert [item["error_code"] for item in telemetry] == [None, None]
