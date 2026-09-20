"""Authenticated multipart boundary for durable document ingestion."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from closeready.api import create_app
from closeready.document_assessment import assess_document
from closeready.document_extraction import calculate_file_hash
from closeready.document_models import DocumentExtraction, ExtractedPage
from closeready.document_processor import DocumentProcessor
from test_case_api import OTHER_TOKEN, TOKEN, access_config, case_request


class DocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = "sqlite:///" + (Path(self.tmp.name) / "document-api.db").as_posix()
        self.app = create_app(self.url, access_config())
        self.client = TestClient(self.app)
        self.client.__enter__()
        create_headers = {
            "Authorization": "Bearer " + TOKEN,
            "Idempotency-Key": "create-document-api-case",
        }
        response = self.client.post(
            "/api/v1/cases", json=case_request(), headers=create_headers
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.case = response.json()
        self.requirement_id = self.case["requirements"][0]["requirement_id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def upload(self, *, key="upload-api-1", token=TOKEN, data=None, content=b"%PDF-1.4 test", media_type="application/pdf"):
        fields = {
            "expected_state_version": str(self.case["state_version"]),
            "requirement_id": self.requirement_id,
        }
        if data:
            fields.update(data)
        return self.client.post(
            f"/api/v1/cases/{self.case['case_id']}/documents",
            headers={
                "Authorization": "Bearer " + token,
                "Idempotency-Key": key,
            },
            data=fields,
            files={"file": ("statement.pdf", content, media_type)},
        )

    def process_for_review(self, *, key='review-upload', requirement_id=None):
        current = self.client.get(
            f'/api/v1/cases/{self.case["case_id"]}',
            headers={'Authorization': 'Bearer ' + TOKEN},
        ).json()
        response = self.upload(key=key, data={
            'expected_state_version': str(current['state_version']),
            'requirement_id': requirement_id if requirement_id is not None else self.requirement_id,
        }, content=b'%PDF uncertain statement')
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        documents = self.app.state.document_store
        token = documents.claim(job['job_id'])
        extraction = DocumentExtraction(
            file_hash=calculate_file_hash(b'%PDF uncertain statement'),
            page_count=1,
            readable=True,
            pages=[ExtractedPage(page=1, text=(
                'DBS Bank Statement\nStatement Period: 01 July 2026 to 31 July 2026\n'
                'Entity ID: entity_demo'))],
        )
        result = DocumentProcessor(
            documents, extractor=lambda _content: extraction, assessor=assess_document,
        ).execute_claimed(job['job_id'], token)
        self.assertEqual(result.status, 'needs_review')
        return job

    def test_upload_returns_202_and_scoped_metadata_endpoints(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        self.assertEqual(job["status"], "queued")

        case_id = self.case["case_id"]
        document = self.client.get(
            f"/api/v1/cases/{case_id}/documents/{job['document_id']}",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        self.assertEqual(document.status_code, 200, document.text)
        self.assertNotIn("content", document.json())
        queued = self.client.get(
            f"/api/v1/cases/{case_id}/document-jobs/{job['job_id']}",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        self.assertEqual(queued.json(), job)
        finding = self.client.get(
            f"/api/v1/cases/{case_id}/documents/{job['document_id']}/finding",
            headers={"Authorization": "Bearer " + TOKEN},
        )
        self.assertEqual(finding.status_code, 404)
        self.assertEqual(finding.json()["error"]["code"], "NOT_FOUND")

        foreign = {"Authorization": "Bearer " + OTHER_TOKEN}
        self.assertEqual(
            self.client.get(
                f"/api/v1/cases/{case_id}/documents/{job['document_id']}",
                headers=foreign,
            ).status_code,
            404,
        )

    def test_document_list_and_manager_accept_reviewed_evidence(self):
        job = self.process_for_review()
        case_id = self.case['case_id']
        listed = self.client.get(
            f'/api/v1/cases/{case_id}/documents',
            headers={'Authorization': 'Bearer ' + TOKEN},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()['items'][0]['document_id'], job['document_id'])
        path = f'/api/v1/cases/{case_id}/documents/{job["document_id"]}/review-decisions'
        headers = {
            'Authorization': 'Bearer ' + TOKEN,
            'Idempotency-Key': 'accept-reviewed-document',
        }
        body = {
            'expected_state_version': self.case['state_version'],
            'decision': 'accept_for_requirement',
            'target_requirement_id': self.requirement_id,
            'reason': 'Manager verified the account manually.',
        }
        accepted = self.client.post(path, json=body, headers=headers)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()['decision'], 'accept_for_requirement')
        self.assertEqual(accepted.json()['document_status'], 'processed')
        self.assertEqual(accepted.json()['resulting_state_version'], 2)
        self.assertEqual(self.client.post(path, json=body, headers=headers).json(), accepted.json())
        changed = self.client.get(
            f'/api/v1/cases/{case_id}', headers=headers).json()
        self.assertEqual(changed['requirements'][0]['status'], 'accepted')
        self.assertEqual(changed['readiness_status'], 'ready_for_confirmation')

    def test_manager_can_reassign_or_reject_a_reviewed_document(self):
        job = self.process_for_review()
        case_id = self.case['case_id']
        path = f'/api/v1/cases/{case_id}/documents/{job["document_id"]}/review-decisions'
        reassigned = self.client.post(path, json={
            'expected_state_version': 1,
            'decision': 'reassign_for_processing',
            'target_requirement_id': self.requirement_id,
            'reason': 'Retry against the selected checklist item.',
        }, headers={
            'Authorization': 'Bearer ' + TOKEN,
            'Idempotency-Key': 'reassign-reviewed-document',
        })
        self.assertEqual(reassigned.status_code, 200, reassigned.text)
        self.assertEqual(reassigned.json()['document_status'], 'queued')
        decisions = self.client.get(
            path, headers={'Authorization': 'Bearer ' + TOKEN}).json()['items']
        self.assertEqual(decisions[0]['source_finding']['result'], 'needs_review')
        queued_job = self.client.get(
            f'/api/v1/cases/{case_id}/document-jobs/{job["job_id"]}',
            headers={'Authorization': 'Bearer ' + TOKEN},
        ).json()
        self.assertEqual(queued_job['status'], 'queued')

        second = self.process_for_review(key='reject-upload')
        current = self.client.get(
            f'/api/v1/cases/{case_id}', headers={'Authorization': 'Bearer ' + TOKEN}).json()
        rejected = self.client.post(
            f'/api/v1/cases/{case_id}/documents/{second["document_id"]}/review-decisions',
            json={
                'expected_state_version': current['state_version'],
                'decision': 'reject_document',
                'target_requirement_id': None,
                'reason': 'The document belongs to another account.',
            },
            headers={
                'Authorization': 'Bearer ' + TOKEN,
                'Idempotency-Key': 'reject-reviewed-document',
            },
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()['document_status'], 'rejected')

    def test_manager_cannot_accept_known_wrong_period(self):
        from unittest.mock import patch
        real_assess = assess_document
        def wrong_period(**kwargs):
            return real_assess(**kwargs).model_copy(update={
                'detected_period': '2026-06', 'coverage_start': __import__('datetime').date(2026,6,1),
                'coverage_end': __import__('datetime').date(2026,6,30)})
        with patch('test_document_api.assess_document', side_effect=wrong_period):
            job = self.process_for_review(key='wrong-period-review')
        response = self.client.post(
            f'/api/v1/cases/{self.case["case_id"]}/documents/{job["document_id"]}/review-decisions',
            json={'expected_state_version': 1, 'decision': 'accept_for_requirement',
                  'target_requirement_id': self.requirement_id, 'reason': 'Try to override mismatch.'},
            headers={'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'wrong-period-accept'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'DOCUMENT_REQUIREMENT_CONFLICT')

    def test_reopen_accepted_document_preserves_history_and_invalidates_ready(self):
        job = self.process_for_review()
        base = f'/api/v1/cases/{self.case["case_id"]}'
        path = base + f'/documents/{job["document_id"]}/review-decisions'
        headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'accept-before-undo'}
        accepted = self.client.post(path, json={
            'expected_state_version': 1, 'decision': 'accept_for_requirement',
            'target_requirement_id': self.requirement_id, 'reason': 'Initial manual verification.'
        }, headers=headers)
        self.assertEqual(accepted.status_code, 200)
        confirmed = self.client.post(base + '/confirm-readiness', json={
            'expected_state_version': 2, 'reason': 'Confirmed.'
        }, headers={**headers, 'Idempotency-Key': 'confirm-before-undo'})
        self.assertEqual(confirmed.status_code, 200)
        headers['Idempotency-Key'] = 'undo-document'
        body = {'expected_state_version': 3, 'decision': 'reopen_review',
                'target_requirement_id': None, 'reason': 'Recheck account evidence.'}
        result = self.client.post(path, json=body, headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['document_status'], 'needs_review')
        self.assertEqual(self.client.post(path, json=body, headers=headers).json(), result.json())
        current = self.client.get(base, headers=headers).json()
        self.assertEqual(current['readiness_status'], 'collecting')
        self.assertEqual(current['state_version'], 4)
        self.assertEqual(current['requirements'][0]['status'], 'awaiting_review')
        self.assertEqual(current['requirements'][0]['evidence_refs'], [])
        history = self.client.get(path, headers=headers).json()['items']
        self.assertCountEqual([x['decision'] for x in history], ['accept_for_requirement', 'reopen_review'])
        original = next(x for x in history if x['decision'] == 'accept_for_requirement')
        self.assertEqual(original['reason'], 'Initial manual verification.')
        self.assertTrue(original['source_finding']['evidence_refs'])
        stale = self.client.post(path, json=body, headers={**headers, 'Idempotency-Key': 'stale-undo'})
        self.assertEqual(stale.json()['error']['code'], 'STALE_STATE')
        repeated = self.client.post(path, json={**body, 'expected_state_version': 4}, headers={**headers, 'Idempotency-Key': 'repeat-undo'})
        self.assertEqual(repeated.json()['error']['code'], 'DOCUMENT_NOT_REOPENABLE')
        rejected = self.client.post(path, json={
            'expected_state_version': 4, 'decision': 'reject_document',
            'target_requirement_id': None, 'reason': 'Incorrect account.'
        }, headers={**headers, 'Idempotency-Key': 'reject-after-undo'})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(self.client.get(base, headers=headers).json()['requirements'][0]['status'], 'missing')
        self.assertEqual(len(self.client.get(path, headers=headers).json()['items']), 3)

    def test_upload_requires_authentication_and_current_case_state(self):
        path = f"/api/v1/cases/{self.case['case_id']}/documents"
        response = self.client.post(
            path,
            data={
                "expected_state_version": "1",
                "requirement_id": self.requirement_id,
            },
            files={"file": ("statement.pdf", b"%PDF", "application/pdf")},
            headers={"Idempotency-Key": "no-auth"},
        )
        self.assertEqual(response.status_code, 401)

        stale = self.upload(key="stale-api", data={"expected_state_version": "999"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "STALE_STATE")

    def test_upload_rejects_media_type_and_size_at_http_boundary(self):
        wrong_type = self.upload(key="wrong-type", media_type="text/plain")
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(
            wrong_type.json()["error"]["code"], "UNSUPPORTED_MEDIA_TYPE"
        )

        too_large = self.upload(key="too-large", content=b"x" * (5 * 1024 * 1024 + 1))
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(too_large.json()["error"]["code"], "DOCUMENT_TOO_LARGE")

    def test_upload_is_idempotent(self):
        first = self.upload()
        replay = self.upload()
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json(), first.json())
        conflict = self.upload(content=b"%PDF changed")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")


if __name__ == "__main__":
    unittest.main()
