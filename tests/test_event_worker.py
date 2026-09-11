"""Durable activation and worker tests use a real file-backed SQLite database."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from closeready.api import create_app
from closeready.case_requests import CreateCaseRequest
from closeready.config import AccessConfig
from closeready.llm import Completion, ToolCall
from closeready.runtime import AgentRuntime
from closeready.runtime_store import RuntimeStore, events, runs
from closeready.store import Store


TOKEN = 'event-worker-synthetic-token'


def access_config():
    return AccessConfig.model_validate({
        'principals': [{
            'user_id': 'user_manager_demo',
            'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
            'client_ids': ['client_demo'],
            'can_manage': True,
        }],
        'policies': [{
            'policy_id': 'policy_demo',
            'version': 1,
            'client_ids': ['client_demo'],
            'approved_by': 'user_manager_demo',
            'approved_at': '2026-09-10T00:00:00Z',
        }],
    })


def case_request():
    return {
        'client_id': 'client_demo',
        'accounting_period': '2026-07',
        'timezone': 'Asia/Singapore',
        'owner_user_id': 'user_manager_demo',
        'due_at': '2026-09-15T01:00:00Z',
        'policy_id': 'policy_demo',
        'requirements': [{
            'document_type': 'bank_statement',
            'accounting_period': '2026-07',
            'scope': {
                'entity_id': 'entity_demo',
                'account_ref': 'account_demo',
                'coverage_start': '2026-07-01',
                'coverage_end': '2026-07-31',
            },
            'completion_rule': {
                'kind': 'coverage',
                'expected_item_refs': [],
                'allow_multiple_documents': True,
            },
        }],
    }


class NeverCalledProvider:
    provider_name = 'scripted_test'
    model = 'scripted_test'
    live = True

    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        raise AssertionError('Activation must not call the provider in the HTTP request.')


def tool(name, args, call_id):
    arguments = json.dumps(args)
    return Completion(
        message={'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': call_id,
            'type': 'function',
            'function': {'name': name, 'arguments': arguments},
        }]},
        calls=[ToolCall(call_id, name, arguments)],
        usage=None,
        finish_reason='tool_calls',
        request_id='scripted-request',
    )


def final():
    return Completion(
        message={'role': 'assistant', 'content': 'Recorded.'},
        calls=[], usage=None, finish_reason='stop', request_id='scripted-request')


class ScriptedProvider:
    provider_name = 'scripted_test'
    model = 'scripted_test'
    live = False

    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return next(self.steps)


class ActivationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'activation.db').as_posix()
        self.provider = NeverCalledProvider()
        self.app = create_app(self.url, access_config(), provider=self.provider)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'create-case'}
        response = self.client.post('/api/v1/cases', headers=self.headers, json=case_request())
        self.assertEqual(response.status_code, 201, response.text)
        self.case = response.json()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def activate(self, key='activate-1', version=None):
        headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': key}
        body = {'expected_state_version': version or self.case['state_version']}
        return self.client.post(
            f"/api/v1/cases/{self.case['case_id']}/activate", headers=headers, json=body)

    def test_activation_returns_queued_run_without_calling_provider(self):
        response = self.activate()
        self.assertEqual(response.status_code, 202, response.text)
        record = response.json()
        self.assertEqual(record['status'], 'queued')
        self.assertIsNone(record['started_at'])
        self.assertEqual(self.provider.calls, 0)

        with self.app.state.store.engine.connect() as conn:
            event = json.loads(conn.execute(select(events.c.record)).scalar_one())
            stored_status = conn.execute(select(runs.c.status)).scalar_one()
        self.assertEqual(event['type'], 'case_activated')
        self.assertEqual(stored_status, 'queued')

    def test_activation_replay_has_one_event_and_one_run(self):
        first = self.activate()
        second = self.activate()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()['run_id'], second.json()['run_id'])
        with self.app.state.store.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(events.c.event_id)).all()), 1)
            self.assertEqual(len(conn.execute(select(runs.c.run_id)).all()), 1)

    def test_activation_key_cannot_collide_with_legacy_run_key(self):
        legacy = self.app.state.runtime_store.start(
            access_config().principals[0], self.case['case_id'], self.case['state_version'],
            'activate:collision', self.provider.provider_name, self.provider.model, self.provider.live)
        with self.app.state.store.write() as conn:
            record = json.loads(conn.execute(select(runs.c.record).where(
                runs.c.run_id == legacy.run_id)).scalar_one())
            record.update(status='completed', finished_at='2026-09-11T00:00:00Z')
            conn.execute(runs.update().where(runs.c.run_id == legacy.run_id).values(
                status='completed', record=json.dumps(record)))

        activated = self.activate(key='collision')

        self.assertEqual(activated.status_code, 202, activated.text)
        self.assertNotEqual(activated.json()['run_id'], legacy.run_id)
        with self.app.state.store.engine.connect() as conn:
            event_types = [json.loads(raw)['type'] for raw in
                conn.execute(select(events.c.record)).scalars()]
        self.assertEqual(event_types, ['case_analysis_requested', 'case_activated'])

    def test_activation_requires_live_configuration_manager_and_current_state(self):
        with TestClient(create_app(self.url, access_config())) as disabled:
            response = disabled.post(
                f"/api/v1/cases/{self.case['case_id']}/activate",
                headers={'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'disabled'},
                json={'expected_state_version': self.case['state_version']},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'LLM_UNAVAILABLE')

        non_live = NeverCalledProvider()
        non_live.live = False
        with TestClient(create_app(self.url, access_config(), provider=non_live)) as mock_app:
            mock_response = mock_app.post(
                f"/api/v1/cases/{self.case['case_id']}/activate",
                headers={'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'mock'},
                json={'expected_state_version': self.case['state_version']},
            )
        self.assertEqual(mock_response.status_code, 503)
        self.assertEqual(mock_response.json()['error']['code'], 'LIVE_LLM_REQUIRED')

        read_only_data = access_config().model_dump()
        read_only_data['principals'][0]['can_manage'] = False
        with TestClient(create_app(
                self.url, AccessConfig.model_validate(read_only_data), provider=self.provider)) as read_only:
            forbidden = read_only.post(
                f"/api/v1/cases/{self.case['case_id']}/activate",
                headers={'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'read-only'},
                json={'expected_state_version': self.case['state_version']},
            )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()['error']['code'], 'FORBIDDEN')

        stale = self.activate(key='stale', version=99)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'STALE_STATE')


class _RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'queued.db').as_posix()
        self.store = Store(self.url, access_config())
        self.actor = access_config().principals[0]
        self.case = self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(case_request()), 'create-case')
        self.runtime_store = RuntimeStore(self.store)

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def provider(self):
        requirement_id = self.case.requirements[0].requirement_id
        return ScriptedProvider([
            tool('get_case_context', {}, 'context'),
            tool('propose_action', {'action': {
                'action_type': 'request_documents',
                'requirement_ids': [requirement_id],
                'finding_ids': [],
                'reason': 'The statement is missing.',
                'payload': {
                    'subject': 'July bank statement',
                    'body': 'Please upload the complete July bank statement.',
                    'requirement_ids': [requirement_id],
                },
            }}, 'proposal'),
            final(),
        ])


class QueuedRuntimeTests(_RuntimeFixture):
    def test_execute_processes_an_existing_queued_run(self):
        provider = self.provider()
        queued = self.runtime_store.start(
            self.actor, self.case.case_id, 1, 'activate:one',
            provider.provider_name, provider.model, provider.live,
            event_type='case_activated', audit_action='activate_case')

        result = AgentRuntime(self.runtime_store, provider).execute(self.actor, queued.run_id)

        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(provider.calls, 3)
        self.assertEqual(len(self.runtime_store.review_tasks(
            self.actor, self.case.case_id).items), 1)

    def test_execute_refuses_changed_provider_configuration(self):
        queued = self.runtime_store.start(
            self.actor, self.case.case_id, 1, 'activate:mismatch',
            'original-provider', 'original-model', True,
            event_type='case_activated', audit_action='activate_case')
        provider = self.provider()

        result = AgentRuntime(self.runtime_store, provider).execute(self.actor, queued.run_id)

        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(result.error_code, 'PROVIDER_CONFIGURATION_CHANGED')
        self.assertEqual(provider.calls, 0)


class WorkerTests(_RuntimeFixture):
    def worker(self, provider):
        from closeready.worker import AgentWorker
        return AgentWorker(self.runtime_store, provider, access_config())

    def queue(self, provider, key='activate:worker'):
        return self.runtime_store.start(
            self.actor, self.case.case_id, 1, key,
            provider.provider_name, provider.model, provider.live,
            event_type='case_activated', audit_action='activate_case')

    def test_worker_executes_a_queued_run_after_store_restart(self):
        provider = self.provider()
        queued = self.queue(provider)
        self.store.engine.dispose()
        self.store = Store(self.url, access_config())
        self.runtime_store = RuntimeStore(self.store)

        result = self.worker(provider).run_once()

        self.assertEqual(result.run_id, queued.run_id)
        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(provider.calls, 3)

    def test_worker_recovers_expired_run_without_replaying_provider(self):
        provider = self.provider()
        queued = self.queue(provider)
        self.runtime_store.claim(self.actor, queued.run_id)
        with self.store.write() as conn:
            conn.execute(runs.update().where(runs.c.run_id == queued.run_id).values(lease_until=0))

        result = self.worker(provider).run_once()

        self.assertIsNone(result)
        recovered = self.runtime_store.get_run(self.actor, queued.run_id)
        self.assertEqual(recovered.status, 'needs_review')
        self.assertEqual(recovered.error_code, 'INTERRUPTED_RUN')
        self.assertEqual(provider.calls, 0)

    def test_two_workers_execute_one_claim_once(self):
        provider = self.provider()
        queued = self.queue(provider)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.worker(provider).run_once(), range(2)))

        self.assertEqual(provider.calls, 3)
        self.assertEqual(
            self.runtime_store.get_run(self.actor, queued.run_id).status, 'needs_review')
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_worker_does_not_execute_run_for_removed_actor(self):
        provider = self.provider()
        queued = self.queue(provider)
        other_access = AccessConfig.model_validate({
            'principals': [{
                'user_id': 'different-manager',
                'token_sha256': hashlib.sha256(b'different-token').hexdigest(),
                'client_ids': ['client_other'],
                'can_manage': True,
            }],
            'policies': [{
                'policy_id': 'other-policy',
                'version': 1,
                'client_ids': ['client_other'],
                'approved_by': 'different-manager',
                'approved_at': '2026-09-10T00:00:00Z',
            }],
        })
        from closeready.worker import AgentWorker

        with self.assertLogs('closeready.worker', level='ERROR') as captured:
            result = AgentWorker(self.runtime_store, provider, other_access).run_once()

        self.assertIsNone(result)
        self.assertTrue(any('ACTOR_CONFIGURATION_MISSING' in line for line in captured.output))
        with self.store.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(runs.c.status).where(
                    runs.c.run_id == queued.run_id)).scalar_one(), 'queued')
        self.assertEqual(provider.calls, 0)

    def test_worker_skips_actor_after_client_grant_is_revoked(self):
        provider = self.provider()
        queued = self.queue(provider)
        revoked_data = access_config().model_dump()
        revoked_data['principals'][0]['client_ids'] = ['client_other']
        revoked = AccessConfig.model_validate(revoked_data)
        self.store.engine.dispose()
        self.store = Store(self.url, revoked)
        self.runtime_store = RuntimeStore(self.store)
        from closeready.worker import AgentWorker

        with self.assertLogs('closeready.worker', level='ERROR') as captured:
            result = AgentWorker(self.runtime_store, provider, revoked).run_once()

        self.assertIsNone(result)
        self.assertTrue(any('ACTOR_CONFIGURATION_MISSING' in line for line in captured.output))
        with self.store.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(runs.c.status).where(
                    runs.c.run_id == queued.run_id)).scalar_one(), 'queued')
        self.assertEqual(provider.calls, 0)

    def test_removed_actor_does_not_block_expired_run_recovery(self):
        provider = self.provider()
        queued = self.queue(provider)
        self.runtime_store.claim(self.actor, queued.run_id)
        with self.store.write() as conn:
            conn.execute(runs.update().where(runs.c.run_id == queued.run_id).values(lease_until=0))
        other_access = AccessConfig.model_validate({
            'principals': [{
                'user_id': 'different-manager',
                'token_sha256': hashlib.sha256(b'different-token').hexdigest(),
                'client_ids': ['client_other'],
                'can_manage': True,
            }],
            'policies': [{
                'policy_id': 'other-policy',
                'version': 1,
                'client_ids': ['client_other'],
                'approved_by': 'different-manager',
                'approved_at': '2026-09-10T00:00:00Z',
            }],
        })
        from closeready.worker import AgentWorker

        result = AgentWorker(self.runtime_store, provider, other_access).run_once()

        self.assertIsNone(result)
        recovered = self.runtime_store.get_run(self.actor, queued.run_id)
        self.assertEqual(recovered.status, 'needs_review')
        self.assertEqual(recovered.error_code, 'INTERRUPTED_RUN')
        self.assertEqual(provider.calls, 0)

    def test_worker_poll_interval_is_bounded(self):
        from argparse import ArgumentTypeError
        from closeready.worker import polling_seconds

        self.assertEqual(polling_seconds('2.5'), 2.5)
        for invalid in ('0', '61', 'not-a-number'):
            with self.subTest(invalid=invalid), self.assertRaises(ArgumentTypeError):
                polling_seconds(invalid)


if __name__ == '__main__':
    unittest.main()
