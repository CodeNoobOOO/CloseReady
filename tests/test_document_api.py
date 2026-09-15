"""Authenticated multipart boundary for durable document ingestion."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from closeready.api import create_app
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
