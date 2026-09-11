"""Scripted provider tests prove controls, not LLM performance. No live calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update, text

from test_case_api import access_config, case_request, TOKEN, OTHER_TOKEN
from closeready.api import create_app
from closeready.case_requests import CreateCaseRequest, ChangeDeadlineRequest
from closeready.llm import Completion, ProviderError, ToolCall
from closeready.runtime import AgentRuntime
from closeready.runtime_store import RuntimeStore, runs
from closeready.store import Store, DomainError
from closeready.runtime_models import ProposeArgs


def tool(name, args, call_id='call-1'):
    return Completion(message={'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]},
        calls=[ToolCall(call_id=call_id, name=name, arguments=json.dumps(args))],
        usage={'prompt_tokens': 10, 'completion_tokens': 5}, finish_reason='tool_calls', request_id='test-request')


def final():
    return Completion(message={'role': 'assistant', 'content': 'Task recorded.'}, calls=[], usage=None,
        finish_reason='stop', request_id='test-request')


class ScriptedProvider:
    provider_name = 'scripted_test'
    model = 'scripted_test'
    live = False

    def __init__(self, steps):
        self.steps, self.messages = iter(steps), []

    def complete(self, messages, tools):
        self.messages.append(json.loads(json.dumps(messages)))
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'runtime.db').as_posix()
        self.store = Store(self.url, access_config())
        self.actor = access_config().principals[0]
        self.case = self.store.create_case(self.actor, CreateCaseRequest.model_validate(case_request()), 'case-1')
        self.db = RuntimeStore(self.store)

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def draft(self, requirement_id=None):
        rid = requirement_id or self.case.requirements[0].requirement_id
        return tool('propose_action', {'action': {'action_type': 'request_documents',
            'requirement_ids': [rid], 'finding_ids': [], 'reason': 'July statement is missing.',
            'payload': {'subject': 'July statement', 'body': 'Please upload the complete July statement.', 'requirement_ids': [rid]}}}, 'call-2')

    def run_with(self, provider, key='run-1', **kwargs):
        version = self.store.get_case(self.actor, self.case.case_id).state_version
        return AgentRuntime(self.db, provider, **kwargs).analyse(self.actor, self.case.case_id, version, key)

    def test_draft_is_persisted_unsent_and_actual_result_returned_to_model(self):
        provider = ScriptedProvider([tool('get_case_context', {}), self.draft(), final()])
        result = self.run_with(provider)
        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(result.provider, 'scripted_test')
        tasks = self.db.review_tasks(self.actor, self.case.case_id).items
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].reason_code, 'MAIL_NOT_CONFIGURED')
        self.assertEqual(tasks[0].assigned_to, self.case.owner_user_id)
        self.assertEqual(tasks[0].draft.subject, 'July statement')
        self.assertFalse(tasks[0].sent)
        outcome = json.loads(provider.messages[-1][-1]['content'])
        self.assertEqual(outcome['outcome'], 'blocked')
        self.assertEqual(outcome['review_task_id'], tasks[0].review_task_id)
        case = self.store.get_case(self.actor, self.case.case_id)
        self.assertEqual(case.requirements[0].status, 'missing')
        self.assertEqual(case.readiness_status, 'collecting')
        self.assertEqual(case.state_version, 2)
        self.assertEqual(len(self.db.get_run(self.actor, result.run_id).traces), 3)

    def test_same_key_replays_without_calling_provider_again(self):
        provider = ScriptedProvider([tool('get_case_context', {}), self.draft(), final()])
        runtime = AgentRuntime(self.db, provider)
        first = runtime.analyse(self.actor, self.case.case_id, 1, 'same')
        second = runtime.analyse(self.actor, self.case.case_id, 1, 'same')
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(provider.messages), 3)

    def test_cross_case_reference_cannot_create_a_draft(self):
        provider = ScriptedProvider([tool('get_case_context', {}), self.draft('foreign-requirement'), final()])
        result = self.run_with(provider, max_steps=3, max_repairs=0)
        self.assertEqual(result.status, 'needs_review')
        self.assertTrue(all(t.draft is None for t in self.db.review_tasks(self.actor, self.case.case_id).items))
        self.assertEqual(self.store.get_case(self.actor, self.case.case_id).requirements[0].status, 'missing')

    def test_internal_identifier_in_model_draft_is_rejected_without_persisting_text(self):
        rid = self.case.requirements[0].requirement_id
        unsafe = tool('propose_action', {'action': {'action_type': 'request_documents',
            'requirement_ids': [rid], 'finding_ids': [], 'reason': 'July statement is missing.',
            'payload': {'subject': 'July statement', 'body': 'Upload requirement ' + rid,
                        'requirement_ids': [rid]}}}, 'unsafe')
        result = self.run_with(ScriptedProvider([tool('get_case_context', {}), unsafe, final()]), max_repairs=0)
        self.assertEqual(result.error_code, 'UNSAFE_DRAFT')
        tasks = self.db.review_tasks(self.actor, self.case.case_id).items
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0].draft)

    def test_changed_version_blocks_stale_model_action(self):
        def change_then_propose(messages):
            self.store.change_deadline(self.actor, self.case.case_id, ChangeDeadlineRequest(
                expected_state_version=1, due_at='2026-10-01T00:00:00Z', reason='Manager correction'), 'change')
            return self.draft()
        result = self.run_with(ScriptedProvider([tool('get_case_context', {}), change_then_propose]))
        self.assertEqual(result.status, 'stale')
        self.assertTrue(all(t.draft is None for t in self.db.review_tasks(self.actor, self.case.case_id).items))

    def test_provider_failure_is_not_mocked_and_has_assigned_review(self):
        result = self.run_with(ScriptedProvider([ProviderError('AUTHENTICATION', False)]))
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_code, 'AUTHENTICATION')
        self.assertEqual(self.db.review_tasks(self.actor, self.case.case_id).items[0].assigned_to, self.actor.user_id)

    def test_tool_loop_has_finite_limit(self):
        provider = ScriptedProvider([tool('get_case_context', {}, str(i)) for i in range(3)])
        result = self.run_with(provider, max_steps=3)
        self.assertEqual(result.error_code, 'STEP_LIMIT')
        self.assertEqual(len(provider.messages), 3)

    def test_transient_retry_counts_against_request_budget(self):
        provider = ScriptedProvider([ProviderError('RATE_LIMIT', True), ProviderError('RATE_LIMIT', True)])
        result = self.run_with(provider)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(provider.messages), 2)
        self.assertEqual(len(result.traces), 2)

    def test_no_action_cannot_hide_missing_documents(self):
        response = tool('propose_action', {'action': {'action_type': 'no_action',
            'requirement_ids': [], 'finding_ids': [], 'reason': 'All fine', 'payload': {}}})
        result = self.run_with(ScriptedProvider([tool('get_case_context', {}), response]), max_repairs=0)
        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(result.error_code, 'INVALID_TOOL')

    def test_human_review_retains_actionable_explanation(self):
        response = tool('propose_action', {'action': {'action_type': 'create_review_task',
            'requirement_ids': [self.case.requirements[0].requirement_id], 'finding_ids': [],
            'reason': 'Clarification needed.', 'payload': {'issue': 'Confirm which account the client should submit.', 'evidence_refs': []}}})
        self.run_with(ScriptedProvider([tool('get_case_context', {}), response, final()]))
        task = self.db.review_tasks(self.actor, self.case.case_id).items[0]
        self.assertEqual(task.reason, 'Confirm which account the client should submit.')

    def test_bad_tool_then_draft_in_same_batch_must_not_apply_draft(self):
        bad = tool('send_email', {})
        good = self.draft()
        batch = Completion(message={'role': 'assistant', 'content': None,
            'tool_calls': bad.message['tool_calls'] + good.message['tool_calls']},
            calls=bad.calls + good.calls, usage=None, finish_reason='tool_calls', request_id=None)
        result = self.run_with(ScriptedProvider([tool('get_case_context', {}), batch]), max_repairs=0)
        self.assertEqual(result.status, 'needs_review')
        self.assertTrue(all(t.draft is None for t in self.db.review_tasks(self.actor, self.case.case_id).items))

    def test_tool_scope_is_bound_and_unknown_tool_is_rejected(self):
        for response in [tool('get_case_context', {'case_id': 'other'}), tool('send_email', {'to': 'arbitrary@example.com'})]:
            with self.subTest(response=response.calls[0].name):
                result = self.run_with(ScriptedProvider([response]), key=response.calls[0].name, max_repairs=0)
                self.assertEqual(result.status, 'needs_review')
                self.assertEqual(result.error_code, 'INVALID_TOOL')

    def test_expired_claim_is_recovered_without_replaying_effects(self):
        run = self.db.start(self.actor, self.case.case_id, 1, 'claim', 'scripted_test', 'scripted_test', False)
        claim = self.db.claim(self.actor, run.run_id)
        with self.store.write() as conn:
            conn.execute(update(runs).where(runs.c.run_id == run.run_id).values(lease_until=0))
        recovered = self.db.recover(self.actor, run.run_id)
        self.assertEqual(recovered.status, 'needs_review')
        self.assertEqual(recovered.error_code, 'INTERRUPTED_RUN')
        self.assertEqual(len(self.db.review_tasks(self.actor, self.case.case_id).items), 1)
        with self.assertRaises(DomainError):
            self.db.apply(self.actor, run.run_id, claim,
                ProposeArgs.model_validate_json(self.draft().calls[0].arguments).action, 1)

    def test_review_proposal_and_case_update_roll_back_if_audit_fails(self):
        run = self.db.start(self.actor, self.case.case_id, 1, 'atomic', 'scripted_test', 'scripted_test', False)
        claim = self.db.claim(self.actor, run.run_id)
        with self.store.write() as conn:
            conn.execute(text("CREATE TRIGGER fail_runtime_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT, 'injected failure'); END"))
        from sqlalchemy.exc import SQLAlchemyError
        with self.assertRaises(SQLAlchemyError):
            self.db.apply(self.actor, run.run_id, claim,
                ProposeArgs.model_validate_json(self.draft().calls[0].arguments).action, 1)
        self.assertEqual(self.db.review_tasks(self.actor, self.case.case_id).items, [])
        self.assertEqual(self.store.get_case(self.actor, self.case.case_id).state_version, 1)
        with self.store.write() as conn:
            conn.execute(text('DROP TRIGGER fail_runtime_audit'))
        result = self.db.apply(self.actor, run.run_id, claim,
            ProposeArgs.model_validate_json(self.draft().calls[0].arguments).action, 1)
        self.assertEqual(result['code'], 'MAIL_NOT_CONFIGURED')

    def test_run_and_task_survive_new_database_connection(self):
        run = self.run_with(ScriptedProvider([tool('get_case_context', {}), self.draft(), final()]))
        restarted_store = Store(self.url, access_config())
        try:
            restarted = RuntimeStore(restarted_store)
            self.assertEqual(restarted.get_run(self.actor, run.run_id).status, 'needs_review')
            self.assertEqual(len(restarted.get_run(self.actor, run.run_id).traces), 3)
            self.assertEqual(len(restarted.review_tasks(self.actor, self.case.case_id).items), 1)
        finally:
            restarted_store.engine.dispose()
        self.assertEqual(self.db.recover(self.actor, run.run_id).status, 'needs_review')
        self.assertEqual(len(self.db.review_tasks(self.actor, self.case.case_id).items), 1)

    def test_runtime_http_access_and_disabled_provider(self):
        provider = ScriptedProvider([tool('get_case_context', {}), self.draft(), final()])
        with TestClient(create_app(self.url, access_config(), provider=provider)) as client:
            headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'api-run'}
            result = client.post('/api/v1/cases/' + self.case.case_id + '/runs', json={'expected_state_version': 1}, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            path = '/api/v1/runs/' + result.json()['run_id']
            self.assertEqual(client.get(path, headers=headers).status_code, 200)
            self.assertEqual(client.get(path, headers={'Authorization': 'Bearer ' + OTHER_TOKEN}).status_code, 404)
        with TestClient(create_app(self.url, access_config())) as client:
            result = client.post('/api/v1/cases/' + self.case.case_id + '/runs', json={'expected_state_version': 2}, headers=headers)
            self.assertEqual(result.status_code, 503)


if __name__ == '__main__':
    unittest.main()
