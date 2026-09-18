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
from closeready.document_processor import DocumentProcessor
from closeready.document_store import DocumentStore, document_jobs
from closeready.runtime_store import RuntimeStore
from closeready.store import DomainError, Store
from closeready.worker import AgentWorker
from test_case_api import access_config, case_request


GOOD_TEXT = """DBS Bank Statement
Statement Period: 01 July 2026 to 31 July 2026
Entity ID: entity_demo
Account Ref: account_demo
"""


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


if __name__ == "__main__":
    unittest.main()
