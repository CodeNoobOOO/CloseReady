"""Customer-visible case-reference boundary tests."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from closeready.api import create_app
from closeready.config import AccessConfig
from sqlalchemy import delete

from closeready.store import Store, case_communication_refs
from test_case_api import OTHER_TOKEN, TOKEN, access_config, case_request


def reference_config():
    data = access_config().model_dump(mode='json')
    data['contacts'] = [{
        'contact_id': 'contact_demo',
        'client_id': 'client_demo',
        'approved_email': 'client@example.test',
        'active': True,
        'approved_by': 'user_manager_demo',
    }]
    return AccessConfig.model_validate(data)


class CaseReferenceHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'references.db').as_posix()
        self.config = reference_config()
        self.client = TestClient(create_app(self.url, self.config))
        self.client.__enter__()
        self.headers = {
            'Authorization': 'Bearer ' + TOKEN,
            'Idempotency-Key': 'reference-case-1',
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def create_case(self, key='reference-case-1'):
        response = self.client.post('/api/v1/cases', json=case_request(), headers={
            **self.headers, 'Idempotency-Key': key,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_new_case_has_stable_scoped_customer_reference(self):
        case = self.create_case()
        path = f"/api/v1/cases/{case['case_id']}/communication-reference"

        response = self.client.get(path, headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertRegex(record['public_reference'], r'^CR-2607-[A-Z2-9]{8}$')
        self.assertEqual(record['case_id'], case['case_id'])
        self.assertEqual(record['client_id'], 'client_demo')
        self.assertEqual(record['status'], 'active')
        with TestClient(create_app(self.url, self.config)) as restarted:
            self.assertEqual(restarted.get(path, headers=self.headers).json(), record)

        other = {'Authorization': 'Bearer ' + OTHER_TOKEN}
        self.assertEqual(self.client.get(path, headers=other).status_code, 404)

    def test_each_case_gets_a_distinct_reference(self):
        first = self.create_case('reference-case-a')
        second = self.create_case('reference-case-b')
        first_ref = self.client.get(
            f"/api/v1/cases/{first['case_id']}/communication-reference",
            headers=self.headers,
        ).json()['public_reference']
        second_ref = self.client.get(
            f"/api/v1/cases/{second['case_id']}/communication-reference",
            headers=self.headers,
        ).json()['public_reference']
        self.assertNotEqual(first_ref, second_ref)

    def test_restart_backfills_a_case_without_a_reference(self):
        case = self.create_case('reference-legacy')
        with self.client.app.state.store.write() as conn:
            conn.execute(delete(case_communication_refs).where(
                case_communication_refs.c.case_id == case['case_id'],
            ))

        with TestClient(create_app(self.url, self.config)) as restarted:
            response = restarted.get(
                f"/api/v1/cases/{case['case_id']}/communication-reference",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertRegex(response.json()['public_reference'], r'^CR-2607-[A-Z2-9]{8}$')
        self.assertEqual(response.json()['created_by'], 'system:migration')


class CaseReferenceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'resolution.db').as_posix()
        self.config = reference_config()
        self.store = Store(self.url, self.config)
        self.actor = self.config.principals[0]
        from closeready.case_requests import CreateCaseRequest
        self.case = self.store.create_case(
            self.actor,
            CreateCaseRequest.model_validate(case_request()),
            'resolution-case',
        )
        self.reference = self.store.case_communication_reference(
            self.actor, self.case.case_id,
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def test_reference_and_approved_sender_resolve_case(self):
        result = self.store.resolve_case_reference(
            self.reference.public_reference.lower(),
            'CLIENT@EXAMPLE.TEST',
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.case_id, self.case.case_id)
        self.assertEqual(result.client_id, 'client_demo')
        self.assertEqual(result.contact_id, 'contact_demo')
        self.assertIsNone(result.reason_code)

    def test_sender_mismatch_and_unknown_reference_disclose_no_case(self):
        mismatch = self.store.resolve_case_reference(
            self.reference.public_reference,
            'stranger@example.test',
        )
        self.assertFalse(mismatch.matched)
        self.assertEqual(mismatch.reason_code, 'SENDER_NOT_APPROVED')
        self.assertIsNone(mismatch.case_id)
        self.assertIsNone(mismatch.client_id)

        unknown = self.store.resolve_case_reference(
            'CR-2607-ZZZZZZZZ',
            'client@example.test',
        )
        self.assertFalse(unknown.matched)
        self.assertEqual(unknown.reason_code, 'REFERENCE_NOT_FOUND')
        self.assertIsNone(unknown.case_id)


if __name__ == '__main__':
    unittest.main()
