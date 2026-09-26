"""Document jobs are claimed, recovered and applied through guarded transactions."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import update
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from closeready.case_requests import ChangeDeadlineRequest, CreateCaseRequest
from closeready.document_assessment import assess_document
from closeready.document_extraction import calculate_file_hash
from closeready.document_models import DocumentExtraction, DocumentFinding, ExtractedPage
from closeready.document_processor import (
    DocumentProcessor,
    build_document_analysis_request,
    resolve_requirement_id,
    validate_analysis_evidence,
)
from closeready.document_store import DocumentStore, document_jobs
from closeready.runtime_store import RuntimeStore
from closeready.store import DomainError, Store, cases
from closeready.worker import AgentWorker
from test_case_api import access_config, case_request
from closeready.llm import ProviderError
from closeready.llm_document_analyzer import (
    LLMDocumentAnalysis,
    LLMDocumentEvidence,
    LiveLLMDocumentAnalyzer,
    ScriptedDocumentAnalyzer,
)

GOOD_TEXT = """DBS Bank Statement
Statement Period: 01 July 2026 to 31 July 2026
Entity ID: entity_demo
Account Ref: account_demo
"""
class FailingDocumentProvider:
    provider_name = "failing_test"
    model = "failing-model"
    live = True

    def complete(self, messages, tools):
        raise ProviderError(
            "NETWORK_ERROR",
            True,
        )


def extracted(text=GOOD_TEXT, file_hash="test-hash"):
    return DocumentExtraction(
        file_hash=file_hash,
        page_count=1,
        pages=[ExtractedPage(page=1, text=text)],
        readable=bool(text.strip()),
    )


def text_pdf(text=GOOD_TEXT):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_ref}
            )
        }
    )
    stream = DecodedStreamObject()
    safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 10 Tf 40 740 Td ({safe_text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class DocumentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = "sqlite:///" + (Path(self.tmp.name) / "worker.db").as_posix()
        self.store = Store(self.url, access_config())
        self.documents = DocumentStore(self.store)
        self.actor = access_config().principals[0]
        self.case = self.store.create_case(
            self.actor,
            CreateCaseRequest.model_validate(case_request()),
            "document-worker-case",
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def queue(self, key="document-1", content=b"synthetic-pdf"):
        return self.documents.upload(
            self.actor,
            self.case.case_id,
            requirement_id=self.case.requirements[0].requirement_id,
            expected_state_version=self.case.state_version,
            filename="statement.pdf",
            media_type="application/pdf",
            content=content,
            key=key,
        )

    def test_only_one_worker_can_claim_a_job(self):
        job = self.queue()
        self.assertEqual(self.documents.queued_candidates(), [job.job_id])
        token = self.documents.claim(job.job_id)
        self.assertIsNotNone(token)
        self.assertIsNone(self.documents.claim(job.job_id))
        claimed = self.documents.get_job(self.actor, self.case.case_id, job.job_id)
        self.assertEqual(claimed.status, "processing")
        self.assertEqual(claimed.attempt_count, 1)
        self.assertIsNotNone(claimed.lease_expires_at)

    def test_expired_claim_is_returned_to_queue(self):
        job = self.queue()
        self.documents.claim(job.job_id)
        with self.store.write() as conn:
            conn.execute(
                update(document_jobs)
                .where(document_jobs.c.job_id == job.job_id)
                .values(lease_until=0)
            )

        self.assertEqual(self.documents.expired_job_candidates(), [job.job_id])
        recovered = self.documents.recover_expired_system(job.job_id)
        self.assertEqual(recovered.status, "queued")
        self.assertEqual(recovered.attempt_count, 1)

    def test_processing_persists_extraction_and_accepts_verified_requirement(self):
        job = self.queue()
        token = self.documents.claim(job.job_id)
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(file_hash=calculate_file_hash(content)),
            assessor=assess_document,
        )

        completed = processor.execute_claimed(job.job_id, token)

        self.assertEqual(completed.status, "completed")
        finding = self.documents.get_finding(
            self.actor, self.case.case_id, job.document_id
        )
        self.assertEqual(finding.result, "satisfies")
        self.assertEqual(finding.document_id, job.document_id)
        self.assertEqual(
            self.documents.get_extraction(self.actor, self.case.case_id, job.document_id),
            extracted(file_hash=calculate_file_hash(b"synthetic-pdf")),
        )
        changed = self.store.get_case(self.actor, self.case.case_id)
        self.assertEqual(changed.state_version, 2)
        self.assertEqual(changed.requirements[0].status, "accepted")
        self.assertEqual(changed.requirements[0].evidence_refs[0].document_id, job.document_id)
        self.assertEqual(changed.readiness_status, "ready_for_confirmation")
        with self.assertRaises(DomainError) as resolved:
            self.documents.upload(
                self.actor,
                self.case.case_id,
                requirement_id=self.case.requirements[0].requirement_id,
                expected_state_version=changed.state_version,
                filename="another.pdf",
                media_type="application/pdf",
                content=b"another document",
                key="resolved-requirement-upload",
            )
        self.assertEqual(resolved.exception.code, "REQUIREMENT_RESOLVED")

    def test_uncertain_finding_waits_for_review_and_does_not_change_case(self):
        job = self.queue()
        token = self.documents.claim(job.job_id)
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                "DBS Bank Statement", calculate_file_hash(content)
            ),
            assessor=assess_document,
        )

        result = processor.execute_claimed(job.job_id, token)

        self.assertEqual(result.status, "needs_review")
        finding = self.documents.get_finding(
            self.actor, self.case.case_id, job.document_id
        )
        self.assertEqual(finding.result, "needs_review")
        unchanged = self.store.get_case(self.actor, self.case.case_id)
        self.assertEqual(unchanged.state_version, 1)
        self.assertEqual(unchanged.requirements[0].status, "missing")
        audit = self.store.audit_events(self.actor, self.case.case_id, 0, 50).items
        self.assertEqual(audit[-1].action, "process_document")
        self.assertEqual(audit[-1].outcome, "needs_review")

    def test_invalid_pdf_is_a_terminal_sanitised_failure(self):
        job = self.queue(content=b"not-a-pdf")
        token = self.documents.claim(job.job_id)

        result = DocumentProcessor(self.documents).execute_claimed(job.job_id, token)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "INVALID_PDF")
        document = self.documents.get_document(
            self.actor, self.case.case_id, job.document_id
        )
        self.assertEqual(document.status, "failed")
        audit = self.store.audit_events(self.actor, self.case.case_id, 0, 50).items
        self.assertEqual(audit[-1].action, "process_document")
        self.assertEqual(audit[-1].outcome, "failed")

    def test_case_change_before_processing_makes_job_stale(self):
        job = self.queue()
        self.store.change_deadline(
            self.actor,
            self.case.case_id,
            ChangeDeadlineRequest(
                expected_state_version=1,
                due_at=datetime.now(timezone.utc) + timedelta(days=30),
                reason="Approved extension",
            ),
            "deadline-change",
        )
        token = self.documents.claim(job.job_id)

        result = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(file_hash=calculate_file_hash(content)),
        ).execute_claimed(job.job_id, token)

        self.assertEqual(result.status, "stale")
        self.assertEqual(result.error_code, "STALE_STATE")
        current = self.store.get_case(self.actor, self.case.case_id)
        self.assertEqual(current.state_version, 2)
        self.assertEqual(current.requirements[0].status, "missing")
        audit = self.store.audit_events(self.actor, self.case.case_id, 0, 50).items
        self.assertEqual(audit[-1].action, "process_document")
        self.assertEqual(audit[-1].outcome, "stale")

    def test_application_rejects_a_satisfies_claim_without_document_evidence(self):
        job = self.queue()
        token = self.documents.claim(job.job_id)
        context = self.documents.processing_context(job.job_id, token)
        extraction = extracted(file_hash=calculate_file_hash(context.content))
        assessed = assess_document(
            case=context.case,
            document_id=context.document.document_id,
            extraction=extraction,
            requirement_id=context.document.requirement_id,
        )
        self.assertEqual(assessed.result, "satisfies")
        forged = DocumentFinding.model_validate(
            {**assessed.model_dump(mode="json"), "evidence_refs": []}
        )

        with self.assertRaises(DomainError) as rejected:
            self.documents.complete(job.job_id, token, extraction, forged)

        self.assertEqual(rejected.exception.code, "INVALID_DOCUMENT_RESULT")
        unverified_identity = DocumentFinding.model_validate(
            {**assessed.model_dump(mode="json"), "entity_match": "unknown"}
        )
        with self.assertRaises(DomainError) as identity_rejected:
            self.documents.complete(
                job.job_id, token, extraction, unverified_identity
            )
        self.assertEqual(identity_rejected.exception.code, "INVALID_DOCUMENT_RESULT")
        current = self.store.get_case(self.actor, self.case.case_id)
        self.assertEqual(current.state_version, 1)
        self.assertEqual(current.requirements[0].status, "missing")

    def test_existing_worker_service_dispatches_a_document_job(self):
        job = self.queue(content=text_pdf())
        processor = DocumentProcessor(self.documents)
        worker = AgentWorker(
            RuntimeStore(self.store),
            provider=object(),
            access=access_config(),
            document_store=self.documents,
            document_processor=processor,
        )

        result = worker.run_once()

        self.assertEqual(result.job_id, job.job_id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            self.store.get_case(self.actor, self.case.case_id).requirements[0].status,
            "accepted",
        )

    def test_document_processor_accepts_optional_analyzer(self):
        analyzer = ScriptedDocumentAnalyzer(
            LLMDocumentAnalysis(
                detected_type="bank_statement",
            )
        )

        processor = DocumentProcessor(
            self.documents,
            analyzer=analyzer,
        )

        self.assertIs(processor.analyzer, analyzer)

    def test_unreadable_ocr_result_bypasses_llm_and_requires_human_review(self):
        job = self.queue()
        token = self.documents.claim(job.job_id)
        analyzer = ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
            detected_type="bank_statement",
            entity_name="entity_demo",
            account_identifier="1234",
            detected_period="2026-07",
            coverage_start=datetime(2026, 7, 1).date(),
            coverage_end=datetime(2026, 7, 31).date(),
            evidence=[],
        ))
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda _content: DocumentExtraction(
                file_hash=calculate_file_hash(b"synthetic-pdf"),
                page_count=1,
                pages=[ExtractedPage(
                    page=1,
                    text="blurred OCR text",
                    extraction_method="ocr",
                    ocr_confidence=25.0,
                )],
                readable=False,
            ),
            analyzer=analyzer,
        )

        result = processor.execute_claimed(job.job_id, token)

        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(analyzer.last_request)
        current = self.store.get_case(self.actor, self.case.case_id)
        self.assertEqual(current.requirements[0].status, "missing")

    def test_build_document_analysis_request_uses_authorised_context(self):
        case = self.store.get_case(
            self.actor,
            self.case.case_id,
        )

        requirement = case.requirements[0]

        updated_scope = requirement.scope.model_copy(
            update={
                "masked_account_identifier": "****1234",
            }
        )

        updated_requirement = requirement.model_copy(
            update={
                "scope": updated_scope,
            }
        )

        case = case.model_copy(
            update={
                "requirements": [
                    updated_requirement,
                    *case.requirements[1:],
                ]
            }
        )

        extraction = extracted(
            text=(
                "DBS Bank Statement "
                "Account ending in 1234"
            )
        )

        request = build_document_analysis_request(
            case,
            extraction,
            updated_requirement.requirement_id,
        )

        self.assertEqual(
            request.accounting_period,
            case.accounting_period,
        )

        self.assertEqual(
            request.pages,
            [
                "DBS Bank Statement "
                "Account ending in 1234"
            ],
        )

        self.assertEqual(
            len(request.candidate_requirements),
            1,
        )

        candidate = request.candidate_requirements[0]

        self.assertEqual(
            candidate.requirement_id,
            updated_requirement.requirement_id,
        )

        self.assertEqual(
            candidate.masked_account_identifier,
            "****1234",
        )

        candidate_data = candidate.model_dump()

        self.assertNotIn(
            "account_ref",
            candidate_data,
        )

    def test_analysis_request_excludes_resolved_requirements(self):
        case = self.create_llm_case()
        first = case.requirements[0]
        resolved = first.model_copy(update={"status": "accepted"})
        outstanding = first.model_copy(update={
            "requirement_id": "req_outstanding",
            "scope": first.scope.model_copy(update={
                "account_ref": "account_two",
                "masked_account_identifier": "****5678",
            }),
        })
        case = case.model_copy(update={"requirements": [resolved, outstanding]})

        request = build_document_analysis_request(
            case,
            extracted(text="Account ending in 5678"),
            None,
        )

        self.assertEqual(
            [item.requirement_id for item in request.candidate_requirements],
            ["req_outstanding"],
        )

    def test_resolver_selects_unique_bank_requirement(self):
        case = self.create_llm_case()
        first = case.requirements[0]
        second = first.model_copy(update={
            "requirement_id": "req_second",
            "scope": first.scope.model_copy(update={
                "account_ref": "account_two",
                "masked_account_identifier": "****5678",
            }),
        })
        case = case.model_copy(update={"requirements": [first, second]})
        analysis = LLMDocumentAnalysis(
            detected_type="bank_statement",
            entity_name="entity_demo",
            account_identifier="Account ending in 5678",
            detected_period="2026-07",
            coverage_start=datetime(2026, 7, 1).date(),
            coverage_end=datetime(2026, 7, 31).date(),
        )

        resolution = resolve_requirement_id(case, analysis)

        self.assertEqual(resolution.requirement_id, "req_second")
        self.assertEqual(resolution.outcome, "unique_structured_match")

    def test_resolver_does_not_guess_when_bank_candidates_are_ambiguous(self):
        case = self.create_llm_case()
        first = case.requirements[0]
        second = first.model_copy(update={
            "requirement_id": "req_second",
            "scope": first.scope.model_copy(update={"account_ref": "account_two"}),
        })
        case = case.model_copy(update={"requirements": [first, second]})
        analysis = LLMDocumentAnalysis(
            detected_type="bank_statement",
            entity_name="entity_demo",
            account_identifier="Account ending in 1234",
            detected_period="2026-07",
            coverage_start=datetime(2026, 7, 1).date(),
            coverage_end=datetime(2026, 7, 31).date(),
        )

        resolution = resolve_requirement_id(case, analysis)

        self.assertIsNone(resolution.requirement_id)
        self.assertEqual(resolution.outcome, "ambiguous")
        self.assertEqual(
            set(resolution.candidate_requirement_ids),
            {first.requirement_id, "req_second"},
        )

    def test_resolver_selects_unique_explicit_item_requirement(self):
        payload = case_request()
        payload["requirements"] = [
            {
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
            },
            {
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
                    "expected_item_refs": ["INV-002"],
                    "allow_multiple_documents": False,
                },
            },
        ]
        case = self.store.create_case(
            self.actor,
            CreateCaseRequest.model_validate(payload),
            "resolver-explicit-items",
        )
        analysis = LLMDocumentAnalysis(
            detected_type="invoice",
            entity_name="entity_demo",
            detected_period="2026-07",
            invoice_number="INV-002",
            matched_item_refs=["INV-002"],
        )

        resolution = resolve_requirement_id(case, analysis)

        self.assertEqual(
            resolution.requirement_id,
            case.requirements[1].requirement_id,
        )
        self.assertEqual(resolution.outcome, "unique_structured_match")

    def create_llm_case(self):
        payload = case_request()

        payload["requirements"][0]["scope"][
            "masked_account_identifier"
        ] = "****1234"

        return self.store.create_case(
            self.actor,
            CreateCaseRequest.model_validate(payload),
            "document-worker-llm-case",
        )
        
    def test_processor_uses_scripted_analyzer_path(self):
        llm_case = self.create_llm_case()

        requirement = llm_case.requirements[0]

        job = self.documents.upload(
            self.actor,
            llm_case.case_id,
            requirement_id=requirement.requirement_id,
            expected_state_version=llm_case.state_version,
            filename="statement.pdf",
            media_type="application/pdf",
            content=b"synthetic-llm-pdf",
            key="llm-document-1",
        )

        token = self.documents.claim(job.job_id)

        analyzer = ScriptedDocumentAnalyzer(
            LLMDocumentAnalysis(
                detected_type="bank_statement",
                entity_name="entity_demo",
                account_identifier="Account ending in 1234",
                detected_period="2026-07",
                coverage_start=datetime(2026, 7, 1).date(),
                coverage_end=datetime(2026, 7, 31).date(),
                evidence=[
                    {
                        "field": "account_identifier",
                        "page": 1,
                        "excerpt": "Account ending in 1234",
                    },
                    {
                        "field": "coverage_start",
                        "page": 1,
                        "excerpt": (
                            "Statement Period: "
                            "01 July 2026 to 31 July 2026"
                        ),
                    },
                ],
            )
        )

        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                text=(
                    "DBS Bank Statement\n"
                    "Entity ID: entity_demo\n"
                    "Account ending in 1234\n"
                    "Statement Period: "
                    "01 July 2026 to 31 July 2026"
                ),
                file_hash=calculate_file_hash(content),
            ),
            analyzer=analyzer,
        )

        result = processor.execute_claimed(
            job.job_id,
            token,
        )

        self.assertEqual(
            result.status,
            "completed",
        )

        finding = self.documents.get_finding(
            self.actor,
            llm_case.case_id,
            job.document_id,
        )

        self.assertEqual(
            finding.result,
            "satisfies",
        )

        self.assertEqual(
            finding.account_match,
            "match",
        )

        self.assertEqual(
            finding.entity_match,
            "match",
        )

        request = analyzer.last_request

        self.assertIsNotNone(request)

        assert request is not None

        candidate = request.candidate_requirements[0]

        self.assertEqual(
            candidate.masked_account_identifier,
            "****1234",
        )

        self.assertNotIn(
            "account_ref",
            candidate.model_dump(),
        )

    def test_processor_binds_unbound_document_to_unique_structured_match(self):
        llm_case = self.create_llm_case()
        first = llm_case.requirements[0]
        second = first.model_copy(update={
            "requirement_id": "req_second",
            "scope": first.scope.model_copy(update={
                "account_ref": "account_two",
                "masked_account_identifier": "****5678",
            }),
        })
        llm_case = llm_case.model_copy(update={"requirements": [first, second]})
        with self.store.write() as conn:
            conn.execute(
                update(cases)
                .where(cases.c.case_id == llm_case.case_id)
                .values(snapshot=llm_case.model_dump_json())
            )
        job = self.documents.upload(
            self.actor,
            llm_case.case_id,
            requirement_id=None,
            expected_state_version=llm_case.state_version,
            filename="statement-5678.pdf",
            media_type="application/pdf",
            content=b"synthetic-unbound-5678",
            key="llm-unbound-unique",
        )
        token = self.documents.claim(job.job_id)
        analyzer = ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
            detected_type="bank_statement",
            entity_name="entity_demo",
            account_identifier="Account ending in 5678",
            detected_period="2026-07",
            coverage_start=datetime(2026, 7, 1).date(),
            coverage_end=datetime(2026, 7, 31).date(),
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier", page=1,
                    excerpt="Account ending in 5678",
                ),
                LLMDocumentEvidence(
                    field="coverage_start", page=1,
                    excerpt="01 July 2026 to 31 July 2026",
                ),
            ],
        ))
        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                text=(
                    "DBS Bank Statement\nEntity ID: entity_demo\n"
                    "Account ending in 5678\n"
                    "01 July 2026 to 31 July 2026"
                ),
                file_hash=calculate_file_hash(content),
            ),
            analyzer=analyzer,
        )

        result = processor.execute_claimed(job.job_id, token)

        self.assertEqual(result.status, "completed")
        documents = self.documents.list_documents(
            self.actor, llm_case.case_id, None, 20
        )
        self.assertEqual(documents.items[0].requirement_id, "req_second")
        changed = self.store.get_case(self.actor, llm_case.case_id)
        self.assertEqual(changed.requirements[0].status, "missing")
        self.assertEqual(changed.requirements[1].status, "accepted")
        audit = self.store.audit_events(self.actor, llm_case.case_id, 0, 100)
        binding = next(item for item in audit.items if item.action == "bind_document_requirement")
        self.assertEqual(binding.details["match_source"], "unique_structured_match")

    def test_processor_rebases_second_email_attachment_after_first_updates_case(self):
        payload = case_request()
        payload["requirements"][0]["scope"][
            "masked_account_identifier"
        ] = "****1234"
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
            "multi-attachment-case",
        )
        bank_requirement, invoice_requirement = case.requirements
        statement_content = b"statement-attachment"
        invoice_content = b"invoice-attachment"
        statement_job = self.documents.upload(
            self.actor,
            case.case_id,
            requirement_id=None,
            expected_state_version=case.state_version,
            filename="statement.pdf",
            media_type="application/pdf",
            content=statement_content,
            key="multi-attachment-statement",
        )
        invoice_job = self.documents.upload(
            self.actor,
            case.case_id,
            requirement_id=None,
            expected_state_version=case.state_version,
            filename="invoice.pdf",
            media_type="application/pdf",
            content=invoice_content,
            key="multi-attachment-invoice",
        )
        statement_token = self.documents.claim(statement_job.job_id)
        statement_processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                text=(
                    "DBS Bank Statement\nEntity ID: entity_demo\n"
                    "Account ending in 1234\n"
                    "Statement Period: 01 July 2026 to 31 July 2026"
                ),
                file_hash=calculate_file_hash(content),
            ),
            analyzer=ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
                detected_type="bank_statement",
                entity_name="entity_demo",
                account_identifier="Account ending in 1234",
                detected_period="2026-07",
                coverage_start=datetime(2026, 7, 1).date(),
                coverage_end=datetime(2026, 7, 31).date(),
                evidence=[
                    LLMDocumentEvidence(
                        field="account_identifier", page=1,
                        excerpt="Account ending in 1234",
                    ),
                    LLMDocumentEvidence(
                        field="coverage_start", page=1,
                        excerpt="01 July 2026 to 31 July 2026",
                    ),
                ],
            )),
        )

        first = statement_processor.execute_claimed(
            statement_job.job_id, statement_token
        )

        self.assertEqual(first.status, "completed")
        after_statement = self.store.get_case(self.actor, case.case_id)
        self.assertEqual(after_statement.requirements[0].status, "accepted")
        self.assertEqual(after_statement.requirements[1].status, "missing")
        invoice_token = self.documents.claim(invoice_job.job_id)
        invoice_analyzer = ScriptedDocumentAnalyzer(LLMDocumentAnalysis(
            detected_type="invoice",
            entity_name="entity_demo",
            detected_period="2026-07",
            invoice_number="INV-001",
            matched_item_refs=["INV-001"],
            evidence=[LLMDocumentEvidence(
                field="invoice_number", page=1,
                excerpt="Invoice Number: INV-001",
            )],
        ))
        invoice_processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                text=(
                    "Invoice\nEntity ID: entity_demo\n"
                    "Invoice Number: INV-001\nInvoice Date: 15 July 2026"
                ),
                file_hash=calculate_file_hash(content),
            ),
            analyzer=invoice_analyzer,
        )

        second = invoice_processor.execute_claimed(
            invoice_job.job_id, invoice_token
        )

        self.assertEqual(second.status, "completed")
        invoice_document = self.documents.get_document(
            self.actor, case.case_id, invoice_job.document_id
        )
        self.assertEqual(invoice_document.requirement_id, invoice_requirement.requirement_id)
        self.assertEqual(invoice_document.input_state_version, after_statement.state_version)
        self.assertEqual(invoice_document.status, "processed")
        completed_case = self.store.get_case(self.actor, case.case_id)
        self.assertEqual(completed_case.requirements[1].status, "accepted")
        self.assertEqual(completed_case.readiness_status, "ready_for_confirmation")
        self.assertIsNotNone(invoice_analyzer.last_request)
        self.assertEqual(
            [item.requirement_id for item in invoice_analyzer.last_request.candidate_requirements],
            [invoice_requirement.requirement_id],
        )
        audit = self.store.audit_events(self.actor, case.case_id, 0, 100)
        refreshed = next(
            item for item in audit.items
            if item.action == "refresh_document_processing_context"
        )
        self.assertEqual(refreshed.details["document_id"], invoice_job.document_id)
        self.assertEqual(
            refreshed.details["previous_input_state_version"],
            str(case.state_version),
        )
        self.assertEqual(
            refreshed.details["current_input_state_version"],
            str(after_statement.state_version),
        )

    def test_analysis_evidence_matches_real_page(self):
        extraction = extracted(
            text="Account ending in 1234"
        )

        analysis = LLMDocumentAnalysis(
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier",
                    page=1,
                    excerpt="Account ending in 1234",
                )
            ]
        )

        self.assertTrue(
            validate_analysis_evidence(
                extraction,
                analysis,
            )
        )

    def test_analysis_evidence_rejects_invalid_page(self):
        extraction = extracted(
            text="Account ending in 1234"
        )

        analysis = LLMDocumentAnalysis(
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier",
                    page=999,
                    excerpt="Account ending in 1234",
                )
            ]
        )

        self.assertFalse(
            validate_analysis_evidence(
                extraction,
                analysis,
            )
        )

    def test_analysis_evidence_rejects_invented_excerpt(self):
        extraction = extracted(
            text="Account ending in 1234"
        )

        analysis = LLMDocumentAnalysis(
            evidence=[
                LLMDocumentEvidence(
                    field="account_identifier",
                    page=1,
                    excerpt="Account ending in 9999",
                )
            ]
        )

        self.assertFalse(
            validate_analysis_evidence(
                extraction,
                analysis,
            )
        )

    def test_processor_invalid_llm_evidence_needs_review(self):
        llm_case = self.create_llm_case()
        requirement = llm_case.requirements[0]

        job = self.documents.upload(
            self.actor,
            llm_case.case_id,
            requirement_id=requirement.requirement_id,
            expected_state_version=llm_case.state_version,
            filename="statement.pdf",
            media_type="application/pdf",
            content=b"synthetic-invalid-evidence",
            key="llm-invalid-evidence",
        )

        token = self.documents.claim(job.job_id)

        self.assertIsNotNone(token)
        assert token is not None

        analyzer = ScriptedDocumentAnalyzer(
            LLMDocumentAnalysis(
                detected_type="bank_statement",
                entity_name="entity_demo",
                account_identifier="Account ending in 1234",
                detected_period="2026-07",
                coverage_start=datetime(2026, 7, 1).date(),
                coverage_end=datetime(2026, 7, 31).date(),
                evidence=[
                    LLMDocumentEvidence(
                        field="account_identifier",
                        page=999,
                        excerpt="This evidence does not exist",
                    )
                ],
            )
        )

        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                text=(
                    "DBS Bank Statement\n"
                    "Entity ID: entity_demo\n"
                    "Account ending in 1234\n"
                    "Statement Period: "
                    "01 July 2026 to 31 July 2026"
                ),
                file_hash=calculate_file_hash(content),
            ),
            analyzer=analyzer,
        )

        result = processor.execute_claimed(
            job.job_id,
            token,
        )

        self.assertEqual(
            result.status,
            "needs_review",
        )

        finding = self.documents.get_finding(
            self.actor,
            llm_case.case_id,
            job.document_id,
        )

        self.assertIsNotNone(finding)
        assert finding is not None

        self.assertEqual(
            finding.result,
            "needs_review",
        )

        self.assertTrue(
            any(
                "evidence" in reason.lower()
                for reason in finding.uncertainty_reasons
            )
        )

        unchanged = self.store.get_case(
            self.actor,
            llm_case.case_id,
        )

        self.assertEqual(
            unchanged.requirements[0].status,
            "missing",
        )
    def test_document_provider_network_error_is_controlled_failure(self):
        llm_case = self.create_llm_case()
        requirement = llm_case.requirements[0]

        job = self.documents.upload(
            self.actor,
            llm_case.case_id,
            requirement_id=requirement.requirement_id,
            expected_state_version=llm_case.state_version,
            filename="statement.pdf",
            media_type="application/pdf",
            content=b"synthetic-provider-failure",
            key="llm-provider-failure",
        )

        token = self.documents.claim(job.job_id)

        self.assertIsNotNone(token)
        assert token is not None

        analyzer = LiveLLMDocumentAnalyzer(
            FailingDocumentProvider()
        )

        processor = DocumentProcessor(
            self.documents,
            extractor=lambda content: extracted(
                text=(
                    "DBS Bank Statement\n"
                    "Entity ID: entity_demo\n"
                    "Account ending in 1234\n"
                    "Statement Period: "
                    "01 July 2026 to 31 July 2026"
                ),
                file_hash=calculate_file_hash(content),
            ),
            analyzer=analyzer,
        )

        result = processor.execute_claimed(
            job.job_id,
            token,
        )

        self.assertEqual(
            result.status,
            "failed",
        )

        self.assertEqual(
            result.error_code,
            "DOCUMENT_PROCESSING_FAILED",
        )

        unchanged = self.store.get_case(
            self.actor,
            llm_case.case_id,
        )

        self.assertEqual(
            unchanged.requirements[0].status,
            "missing",
        )


if __name__ == "__main__":
    unittest.main()
