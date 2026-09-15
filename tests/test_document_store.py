"""Durable document upload queue tests at the business boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from closeready.case_requests import CreateCaseRequest
from closeready.config import Principal
from closeready.document_store import MAX_DOCUMENT_BYTES, DocumentStore
from closeready.store import DomainError, Store
from test_case_api import access_config, case_request


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = "sqlite:///" + (Path(self.tmp.name) / "documents.db").as_posix()
        self.store = Store(self.url, access_config())
        self.documents = DocumentStore(self.store)
        self.actor = access_config().principals[0]
        self.case = self.store.create_case(
            self.actor,
            CreateCaseRequest.model_validate(case_request()),
            "create-document-case",
        )
        self.requirement_id = self.case.requirements[0].requirement_id

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def upload(self, *, key="upload-1", content=b"%PDF-1.4 test", **changes):
        values = {
            "actor": self.actor,
            "case_id": self.case.case_id,
            "requirement_id": self.requirement_id,
            "expected_state_version": self.case.state_version,
            "filename": "july-statement.pdf",
            "media_type": "application/pdf",
            "content": content,
            "key": key,
        }
        values.update(changes)
        return self.documents.upload(**values)

    def test_authorised_upload_queues_durable_job_without_changing_case(self):
        job = self.upload()

        self.assertEqual(job.case_id, self.case.case_id)
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempt_count, 0)
        document = self.documents.get_document(self.actor, self.case.case_id, job.document_id)
        self.assertEqual(document.requirement_id, self.requirement_id)
        self.assertEqual(document.original_filename, "july-statement.pdf")
        self.assertEqual(document.media_type, "application/pdf")
        self.assertEqual(document.size_bytes, len(b"%PDF-1.4 test"))
        self.assertEqual(document.status, "queued")
        self.assertFalse(hasattr(document, "content"))
        self.assertEqual(
            self.store.get_case(self.actor, self.case.case_id).state_version,
            self.case.state_version,
        )

        restarted = DocumentStore(Store(self.url, access_config()))
        persisted = restarted.get_job(self.actor, self.case.case_id, job.job_id)
        self.assertEqual(persisted, job)
        restarted.store.engine.dispose()

    def test_requirement_must_belong_to_case_and_state_must_be_current(self):
        with self.assertRaises(DomainError) as unknown:
            self.upload(requirement_id="req_from_another_case")
        self.assertEqual(unknown.exception.code, "INVALID_REQUIREMENT")

        with self.assertRaises(DomainError) as stale:
            self.upload(key="stale-upload", expected_state_version=999)
        self.assertEqual(stale.exception.code, "STALE_STATE")

    def test_upload_requires_manager_with_case_access(self):
        reader = Principal(
            user_id="reader",
            token_sha256="0" * 64,
            client_ids=frozenset({self.case.client_id}),
            can_manage=False,
        )
        with self.assertRaises(DomainError) as denied:
            self.upload(actor=reader)
        self.assertEqual(denied.exception.code, "FORBIDDEN")

        foreign = access_config().principals[1]
        with self.assertRaises(DomainError) as hidden:
            self.upload(actor=foreign)
        self.assertEqual(hidden.exception.code, "NOT_FOUND")
        with self.assertRaises(DomainError) as hidden_invalid:
            self.upload(actor=foreign, media_type="text/plain", content=b"")
        self.assertEqual(hidden_invalid.exception.code, "NOT_FOUND")

    def test_upload_rejects_wrong_media_empty_and_oversize_content(self):
        with self.assertRaises(DomainError) as media:
            self.upload(media_type="text/plain")
        self.assertEqual(media.exception.code, "UNSUPPORTED_MEDIA_TYPE")
        self.assertEqual(media.exception.status, 415)

        with self.assertRaises(DomainError) as empty:
            self.upload(key="empty", content=b"")
        self.assertEqual(empty.exception.code, "INVALID_DOCUMENT")

        with self.assertRaises(DomainError) as large:
            self.upload(key="large", content=b"x" * (MAX_DOCUMENT_BYTES + 1))
        self.assertEqual(large.exception.code, "DOCUMENT_TOO_LARGE")
        self.assertEqual(large.exception.status, 413)

    def test_idempotency_replays_same_upload_and_rejects_changed_input(self):
        first = self.upload()
        replay = self.upload()
        self.assertEqual(replay, first)

        with self.assertRaises(DomainError) as conflict:
            self.upload(content=b"%PDF-1.4 different")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_same_content_is_recorded_as_duplicate_with_a_separate_job(self):
        first = self.upload()
        second = self.upload(key="upload-2")

        self.assertNotEqual(second.document_id, first.document_id)
        duplicate = self.documents.get_document(
            self.actor, self.case.case_id, second.document_id
        )
        self.assertEqual(duplicate.duplicate_of_document_id, first.document_id)

    def test_finding_is_absent_before_processing(self):
        job = self.upload()
        self.assertIsNone(
            self.documents.get_finding(self.actor, self.case.case_id, job.document_id)
        )


if __name__ == "__main__":
    unittest.main()
