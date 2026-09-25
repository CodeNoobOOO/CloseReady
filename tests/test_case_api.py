"""Business-boundary tests with real transactions in temporary SQLite files."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

from closeready.api import create_app, from_env
from closeready.config import AccessConfig
from closeready.case_requests import CreateCaseRequest
from closeready.models import CaseSnapshot
from closeready.store import cases
from closeready.runtime_store import reviews
from sqlalchemy import insert, update

TOKEN = 'synthetic-test-token-never-use-in-deployment'
OTHER_TOKEN = 'other-synthetic-test-token-never-use'
ROOT = Path(__file__).resolve().parents[1]


def access_config():
    return AccessConfig.model_validate({
        'principals': [
            {'user_id': 'user_manager_demo', 'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
             'client_ids': ['client_demo'], 'can_manage': True},
            {'user_id': 'other', 'token_sha256': hashlib.sha256(OTHER_TOKEN.encode()).hexdigest(),
             'client_ids': ['client_other'], 'can_manage': True},
        ],
        'policies': [{'policy_id': 'policy_demo', 'version': 1, 'client_ids': ['client_demo'],
                      'approved_by': 'user_manager_demo', 'approved_at': '2026-09-10T00:00:00Z'}],
    })


def case_request():
    data = json.loads((ROOT / 'examples/case-snapshot.json').read_text(encoding='utf-8'))
    for key in ('case_id', 'state_version', 'readiness_status', 'policy_version'):
        del data[key]
    for req in data['requirements']:
        for key in ('requirement_id', 'status', 'evidence_refs', 'reviewer_status'):
            del req[key]
    return data


class CaseApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'cases.db').as_posix()
        self.app = create_app(self.url, access_config())
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'create-1'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def create(self):
        response = self.client.post('/api/v1/cases', json=case_request(), headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def update(self, case_id, key='update-1', version=1):
        return self.client.patch('/api/v1/cases/' + case_id + '/deadline',
            json={'expected_state_version': version, 'due_at': '2026-09-20T09:00:00+08:00', 'reason': 'Client approved extension'},
            headers=dict(self.headers, **{'Idempotency-Key': key}))

    def mark_ready_for_confirmation(self, case):
        requirement = dict(case['requirements'][0])
        requirement.update(
            status='accepted',
            reviewer_status='approved',
            evidence_refs=[{
                'document_id': 'document_test',
                'page': 1,
                'excerpt': 'Synthetic verified evidence.',
            }],
        )
        ready = CaseSnapshot.model_validate({
            **case,
            'state_version': case['state_version'] + 1,
            'readiness_status': 'ready_for_confirmation',
            'requirements': [requirement],
        })
        with self.app.state.store.engine.begin() as conn:
            conn.execute(update(cases).where(cases.c.case_id == case['case_id']).values(
                state_version=ready.state_version,
                snapshot=ready.model_dump_json(),
            ))
        return ready.model_dump(mode='json')

    def test_create_read_and_server_owned_initial_state(self):
        case = self.create()
        self.assertEqual(case['state_version'], 1)
        self.assertEqual(case['readiness_status'], 'collecting')
        self.assertEqual(case['requirements'][0]['status'], 'missing')
        self.assertEqual(case['requirements'][0]['evidence_refs'], [])
        self.assertTrue(case['requirements'][0]['requirement_id'])
        self.assertEqual(case['policy_version'], 1)
        got = self.client.get('/api/v1/cases/' + case['case_id'], headers=self.headers)
        self.assertEqual(got.json(), case)

    def test_create_bank_requirement_requires_account_suffix(self):
        payload = case_request()
        payload['requirements'][0]['scope'].pop('masked_account_identifier', None)

        response = self.client.post(
            '/api/v1/cases', json=payload, headers=self.headers)

        self.assertEqual(response.status_code, 422, response.text)

    def test_create_bank_requirement_requires_internal_account_reference(self):
        payload = case_request()
        payload['requirements'][0]['scope']['account_ref'] = None

        response = self.client.post(
            '/api/v1/cases', json=payload, headers=self.headers)

        self.assertEqual(response.status_code, 422, response.text)

    def test_create_bank_requirement_normalizes_four_digit_account_suffix(self):
        payload = case_request()
        payload['requirements'][0]['scope']['masked_account_identifier'] = '1234'

        response = self.client.post(
            '/api/v1/cases', json=payload, headers=self.headers)

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            response.json()['requirements'][0]['scope']['masked_account_identifier'],
            '****1234',
        )

    def test_create_case_rejects_duplicate_internal_bank_account_references(self):
        payload = case_request()
        second = copy.deepcopy(payload['requirements'][0])
        second['scope']['masked_account_identifier'] = '****5678'
        payload['requirements'].append(second)

        response = self.client.post(
            '/api/v1/cases', json=payload, headers=self.headers)

        self.assertEqual(response.status_code, 422, response.text)

    def test_manager_confirms_computed_readiness_with_audited_idempotent_transition(self):
        case = self.mark_ready_for_confirmation(self.create())
        path = '/api/v1/cases/' + case['case_id'] + '/confirm-readiness'
        headers = dict(self.headers, **{'Idempotency-Key': 'confirm-ready-1'})
        body = {
            'expected_state_version': case['state_version'],
            'reason': 'Manager verified the supporting documents.',
        }

        response = self.client.post(path, json=body, headers=headers)

        self.assertEqual(response.status_code, 200, response.text)
        confirmed = response.json()
        self.assertEqual(confirmed['readiness_status'], 'ready')
        self.assertEqual(confirmed['state_version'], case['state_version'] + 1)
        self.assertEqual(self.client.post(path, json=body, headers=headers).json(), confirmed)
        audit = self.client.get(
            '/api/v1/cases/' + case['case_id'] + '/audit-events', headers=self.headers
        ).json()['items']
        self.assertEqual(audit[-1]['action'], 'confirm_readiness')
        self.assertEqual(audit[-1]['reason'], body['reason'])

    def test_reopen_preserves_evidence_is_idempotent_and_allows_reconfirmation(self):
        case = self.mark_ready_for_confirmation(self.create())
        base = '/api/v1/cases/' + case['case_id']
        confirmed = self.client.post(base + '/confirm-readiness', json={
            'expected_state_version': case['state_version'], 'reason': 'Reviewed.'
        }, headers=dict(self.headers, **{'Idempotency-Key': 'confirm'})).json()
        headers = dict(self.headers, **{'Idempotency-Key': 'reopen'})
        body = {'expected_state_version': confirmed['state_version'], 'reason': 'Confirmation was premature.'}
        response = self.client.post(base + '/reopen', json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        reopened = response.json()
        self.assertEqual(reopened['readiness_status'], 'ready_for_confirmation')
        self.assertEqual(reopened['requirements'], confirmed['requirements'])
        self.assertEqual(reopened['state_version'], confirmed['state_version'] + 1)
        self.assertEqual(self.client.post(base + '/reopen', json=body, headers=headers).json(), reopened)
        events = self.client.get(base + '/audit-events', headers=headers).json()['items']
        self.assertEqual([e['action'] for e in events].count('reopen_case'), 1)
        self.assertEqual(events[-1]['reason'], body['reason'])
        self.assertEqual(self.client.post(base + '/reopen', json={**body, 'reason': 'Changed'}, headers=headers).status_code, 409)
        again = self.client.post(base + '/confirm-readiness', json={
            'expected_state_version': reopened['state_version'], 'reason': 'Checked again.'
        }, headers=dict(self.headers, **{'Idempotency-Key': 'confirm-again'}))
        self.assertEqual(again.json()['readiness_status'], 'ready')

    def test_read_only_manager_cannot_reopen(self):
        case = self.mark_ready_for_confirmation(self.create())
        base = '/api/v1/cases/' + case['case_id']
        confirmed = self.client.post(base + '/confirm-readiness', json={
            'expected_state_version': case['state_version'], 'reason': 'Reviewed.'
        }, headers=dict(self.headers, **{'Idempotency-Key': 'confirm-readonly'})).json()
        access = access_config()
        access = access.model_copy(update={'principals': [
            p.model_copy(update={'can_manage': False}) for p in access.principals
        ]})
        with TestClient(create_app(self.url, access)) as reader:
            response = reader.post(base + '/reopen', json={
                'expected_state_version': confirmed['state_version'], 'reason': 'Recheck.'
            }, headers=dict(self.headers, **{'Idempotency-Key': 'readonly-reopen'}))
            self.assertEqual(response.status_code, 403)
            self.assertEqual(reader.get(base, headers=self.headers).json(), confirmed)

    def test_reopen_rejects_invalid_state_version_reason_and_scope(self):
        case = self.create()
        path = '/api/v1/cases/' + case['case_id'] + '/reopen'
        body = {'expected_state_version': 1, 'reason': 'Recheck.'}
        headers = dict(self.headers, **{'Idempotency-Key': 'reopen-invalid'})
        self.assertEqual(self.client.post(path, json=body, headers=headers).json()['error']['code'], 'CASE_NOT_READY')
        self.assertEqual(self.client.post(path, json={**body, 'expected_state_version': 99}, headers=headers).json()['error']['code'], 'STALE_STATE')
        self.assertEqual(self.client.post(path, json={**body, 'reason': '   '}, headers=headers).status_code, 422)
        self.assertEqual(self.client.post(path, json=body).status_code, 401)
        self.assertEqual(self.client.post(path, json=body, headers={**headers, 'Authorization': 'Bearer ' + OTHER_TOKEN}).status_code, 404)

    def test_readiness_confirmation_rejects_collecting_and_stale_cases(self):
        collecting = self.create()
        path = '/api/v1/cases/' + collecting['case_id'] + '/confirm-readiness'
        blocked = self.client.post(path, json={
            'expected_state_version': collecting['state_version'],
            'reason': 'Attempt before evidence is complete.',
        }, headers=dict(self.headers, **{'Idempotency-Key': 'confirm-too-soon'}))
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()['error']['code'], 'CASE_NOT_CONFIRMABLE')

        ready = self.mark_ready_for_confirmation(collecting)
        stale = self.client.post(path, json={
            'expected_state_version': ready['state_version'] - 1,
            'reason': 'Stale browser state.',
        }, headers=dict(self.headers, **{'Idempotency-Key': 'confirm-stale'}))
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'STALE_STATE')

    def test_readiness_confirmation_rejects_an_open_operational_review(self):
        case = self.mark_ready_for_confirmation(self.create())
        with self.app.state.store.engine.begin() as conn:
            conn.execute(insert(reviews).values(
                review_task_id='review_open_test',
                case_id=case['case_id'],
                run_id='run_open_test',
                record=json.dumps({'status': 'open'}),
            ))
        response = self.client.post(
            '/api/v1/cases/' + case['case_id'] + '/confirm-readiness',
            json={
                'expected_state_version': case['state_version'],
                'reason': 'Attempt while review is open.',
            },
            headers=dict(self.headers, **{'Idempotency-Key': 'confirm-open-review'}),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'CASE_NOT_CONFIRMABLE')

    def test_authentication_and_cross_client_access(self):
        case = self.create()
        path = '/api/v1/cases/' + case['case_id']
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer wrong'}).status_code, 401)
        other = {'Authorization': 'Bearer ' + OTHER_TOKEN}
        self.assertEqual(self.client.get(path, headers=other).status_code, 404)
        self.assertEqual(self.client.get(path + '/audit-events', headers=other).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/cases', headers=other).json()['items'], [])
        self.assertEqual(self.client.post('/api/v1/cases', headers=dict(other, **{'Idempotency-Key': 'x'}), json=case_request()).status_code, 403)

    def test_cannot_overpost_authority_or_create_empty_case(self):
        for field, value in [('readiness_status', 'ready'), ('state_version', 10), ('policy_version', 2)]:
            data = case_request()
            data[field] = value
            with self.subTest(field=field):
                self.assertEqual(self.client.post('/api/v1/cases', headers=self.headers, json=data).status_code, 422)
        for field, value in [('status', 'accepted'), ('requirement_id', 'invented'), ('evidence_refs', [])]:
            data = case_request()
            data['requirements'][0][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.client.post('/api/v1/cases', headers=self.headers, json=data).status_code, 422)
        data = case_request()
        data['requirements'] = []
        self.assertEqual(self.client.post('/api/v1/cases', headers=self.headers, json=data).status_code, 422)

    def test_owner_and_policy_require_authorised_configuration(self):
        for changes in ({'owner_user_id': 'other'}, {'policy_id': 'unknown'}):
            with self.subTest(changes=changes):
                response = self.client.post('/api/v1/cases', headers=self.headers, json=dict(case_request(), **changes))
                self.assertEqual(response.status_code, 403)

    def test_idempotency_replays_after_update_and_rejects_key_reuse(self):
        case = self.create()
        changed = self.update(case['case_id'])
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['state_version'], 2)
        self.assertEqual(self.create(), case)  # Replay original, not current state.
        data = case_request()
        data['due_at'] = '2026-10-01T00:00:00Z'
        conflict = self.client.post('/api/v1/cases', headers=self.headers, json=data)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.update(case['case_id']).json(), changed.json())

    def test_stale_write_is_rejected_and_audited(self):
        case = self.create()
        self.assertEqual(self.update(case['case_id']).status_code, 200)
        stale = self.update(case['case_id'], key='stale')
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'STALE_STATE')
        path = '/api/v1/cases/' + case['case_id']
        self.assertEqual(self.client.get(path, headers=self.headers).json()['state_version'], 2)
        audit = self.client.get(path + '/audit-events', headers=self.headers).json()['items']
        self.assertEqual([a['outcome'] for a in audit], ['executed', 'executed', 'stale'])
        self.assertEqual(audit[1]['old_state_version'], 1)
        self.assertEqual(audit[1]['new_state_version'], 2)
        self.assertEqual(audit[1]['actor_user_id'], 'user_manager_demo')

    def test_restart_preserves_case_audit_and_idempotency(self):
        case = self.create()
        self.assertEqual(self.update(case['case_id']).status_code, 200)
        with TestClient(create_app(self.url, access_config())) as restarted:
            path = '/api/v1/cases/' + case['case_id']
            self.assertEqual(restarted.get(path, headers=self.headers).json()['state_version'], 2)
            self.assertEqual(len(restarted.get(path + '/audit-events', headers=self.headers).json()['items']), 2)
            self.assertEqual(restarted.post('/api/v1/cases', json=case_request(), headers=self.headers).json(), case)

    def test_audit_insert_failure_rolls_back_update_and_replay_record(self):
        case = self.create()
        with self.app.state.store.engine.begin() as conn:
            conn.execute(text("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT, 'test audit failure'); END"))
        response = self.update(case['case_id'])
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('test audit failure', response.text)
        path = '/api/v1/cases/' + case['case_id']
        self.assertEqual(self.client.get(path, headers=self.headers).json()['state_version'], 1)
        with self.app.state.store.engine.begin() as conn:
            conn.execute(text('DROP TRIGGER fail_audit'))
        self.assertEqual(self.update(case['case_id']).json()['state_version'], 2)

    def test_concurrent_updates_only_one_succeeds(self):
        case = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda key: self.update(case['case_id'], key=key).status_code, ['a', 'b']))
        self.assertEqual(sorted(results), [200, 409])

    def test_list_pagination_and_input_errors(self):
        fixed_ids = [
            type('FixedUuid', (), {'hex': value})()
            for value in ('0' * 32, '1' * 32, 'f' * 32, 'e' * 32)
        ]
        with patch('closeready.store.uuid4', side_effect=fixed_ids):
            older = self.create()
            self.headers['Idempotency-Key'] = 'create-2'
            newer = self.create()
        first = self.client.get('/api/v1/cases?limit=1', headers=self.headers).json()
        second = self.client.get('/api/v1/cases', params={'limit': 1, 'cursor': first['next_cursor']}, headers=self.headers).json()
        self.assertEqual(len(first['items']), 1)
        self.assertEqual(len(second['items']), 1)
        self.assertEqual(first['items'][0]['case_id'], newer['case_id'])
        self.assertEqual(second['items'][0]['case_id'], older['case_id'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.client.get('/api/v1/cases?limit=1001', headers=self.headers).status_code, 422)
        missing_key = self.client.post('/api/v1/cases', headers={'Authorization': 'Bearer ' + TOKEN}, json=case_request())
        self.assertEqual(missing_key.status_code, 422)
        self.assertIn('request_id', missing_key.json())

    def test_validation_does_not_echo_sensitive_input(self):
        data = case_request()
        data['unexpected_secret'] = 'DO-NOT-ECHO-ME'
        response = self.client.post('/api/v1/cases', headers=self.headers, json=data)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('DO-NOT-ECHO-ME', response.text)

    def test_read_only_actor_cannot_mutate_even_with_known_key(self):
        case = self.create()
        config = access_config().model_dump()
        config['principals'][0]['can_manage'] = False
        with TestClient(create_app(self.url, AccessConfig.model_validate(config))) as reader:
            self.assertEqual(reader.get('/api/v1/cases/' + case['case_id'], headers=self.headers).status_code, 200)
            self.assertEqual(reader.post('/api/v1/cases', json=case_request(), headers=self.headers).status_code, 403)
            response = reader.patch('/api/v1/cases/' + case['case_id'] + '/deadline',
                json={'expected_state_version': 1, 'due_at': '2026-10-01T00:00:00Z', 'reason': 'Attempt'}, headers=self.headers)
            self.assertEqual(response.status_code, 403)

    def test_concurrent_creation_with_same_key_has_one_effect(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.client.post('/api/v1/cases', json=case_request(), headers=self.headers), range(2)))
        self.assertEqual([r.status_code for r in results], [201, 201])
        self.assertEqual(results[0].json(), results[1].json())
        self.assertEqual(len(self.client.get('/api/v1/cases', headers=self.headers).json()['items']), 1)

    def test_audit_pagination_and_openapi(self):
        case = self.create()
        self.update(case['case_id'])
        path = '/api/v1/cases/' + case['case_id'] + '/audit-events'
        first = self.client.get(path, params={'limit': 1}, headers=self.headers).json()
        second = self.client.get(path, params={'limit': 1, 'cursor': first['next_cursor']}, headers=self.headers).json()
        self.assertEqual(first['items'][0]['action'], 'create_case')
        self.assertEqual(second['items'][0]['action'], 'change_deadline')
        self.assertIsNone(second['next_cursor'])
        spec = self.client.get('/openapi.json')
        self.assertEqual(spec.status_code, 200)
        self.assertIn('HTTPBearer', spec.json()['components']['securitySchemes'])

    def test_startup_requires_explicit_configuration(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(RuntimeError):
            from_env()

    def test_committed_setup_examples_match_executable_contracts(self):
        example = CreateCaseRequest.model_validate_json((ROOT / 'examples/create-case.json').read_text(encoding='utf-8'))
        self.assertEqual(example.client_id, 'client_demo')
        config = AccessConfig.model_validate_json((ROOT / 'examples/server-config.json').read_text(encoding='utf-8'))
        self.assertIsNone(config.authenticate('0000000000000000000000000000000000000000000000000000000000000000'))
        response = self.client.post('/api/v1/cases', headers=self.headers, json=example.model_dump(mode='json'))
        self.assertEqual(response.status_code, 201)

    def test_policy_scope_and_persisted_version(self):
        case = self.create()
        config = access_config().model_dump()
        config['policies'][0]['version'] = 2
        config['policies'][0]['client_ids'] = frozenset({'client_other'})
        with TestClient(create_app(self.url, AccessConfig.model_validate(config))) as changed:
            result = changed.get('/api/v1/cases/' + case['case_id'], headers=self.headers)
            self.assertEqual(result.json()['policy_version'], 1)
            response = changed.post('/api/v1/cases', json=case_request(),
                headers=dict(self.headers, **{'Idempotency-Key': 'new-policy-scope'}))
            self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
