"""Review decisions authorize durable work; they never send mail."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.exc import SQLAlchemyError

from closeready.case_requests import CreateCaseRequest
from closeready.llm import ProviderError
from closeready.runtime import AgentRuntime
from closeready.runtime_models import ReviewDecisionRequest, ReviewTaskRecord
from closeready.runtime_store import RuntimeStore, reviews
from closeready.store import DomainError, Store
from closeready.config import Principal
from test_case_api import access_config, case_request
from test_runtime import ScriptedProvider, final, tool


class ReviewOutboxSchemaTests(unittest.TestCase):
    def base(self):
        return {
            'expected_state_version': 2,
            'review_task_id': 'review_1',
            'reason': 'Reviewed by the assigned manager.',
        }

    def test_edit_requires_draft_and_other_decisions_forbid_it(self):
        draft = {'subject': 'July statement', 'body': 'Please upload the July statement.',
                 'requirement_ids': ['req_1']}
        with self.assertRaises(ValidationError):
            ReviewDecisionRequest.model_validate({**self.base(), 'decision': 'edit_and_approve'})
        with self.assertRaises(ValidationError):
            ReviewDecisionRequest.model_validate({**self.base(), 'decision': 'reject_draft',
                                                   'edited_draft': draft})
        valid = ReviewDecisionRequest.model_validate({**self.base(), 'decision': 'edit_and_approve',
                                                      'edited_draft': draft})
        self.assertEqual(valid.edited_draft.subject, 'July statement')

    def test_open_review_records_remain_backward_compatible(self):
        legacy = {
            'review_task_id': 'review_legacy', 'case_id': 'case_legacy', 'run_id': 'run_legacy',
            'requirement_ids': [], 'reason_code': 'BAD_REQUEST',
            'reason': 'Inspect the run error and case before taking action.',
            'assigned_to': 'manager', 'status': 'open', 'draft': None, 'sent': False,
            'created_at': '2026-09-11T00:00:00Z',
        }
        task = ReviewTaskRecord.model_validate(legacy)
        self.assertIsNone(task.resolution)
        self.assertIsNone(task.resolved_at)


class ReviewOutboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'review.db').as_posix()
        self.store = Store(self.url, access_config())
        self.actor = access_config().principals[0]
        self.case = self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(case_request()), 'case-review')
        self.db = RuntimeStore(self.store)

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def create_draft_task(self):
        rid = self.case.requirements[0].requirement_id
        draft = tool('propose_action', {'action': {'action_type': 'request_documents',
            'requirement_ids': [rid], 'finding_ids': [], 'reason': 'The statement is missing.',
            'payload': {'subject': 'July statement', 'body': 'Please upload the complete July statement.',
                        'requirement_ids': [rid]}}}, 'draft')
        AgentRuntime(self.db, ScriptedProvider([tool('get_case_context', {}), draft, final()])).analyse(
            self.actor, self.case.case_id, 1, 'draft-run')
        return self.db.review_tasks(self.actor, self.case.case_id).items[0]

    def create_error_task(self):
        AgentRuntime(self.db, ScriptedProvider([ProviderError('AUTHENTICATION', False)])).analyse(
            self.actor, self.case.case_id, 1, 'error-run')
        return self.db.review_tasks(self.actor, self.case.case_id).items[0]

    def request(self, task, decision='approve_draft', **changes):
        data = {'expected_state_version': 2, 'review_task_id': task.review_task_id,
                'decision': decision, 'reason': 'Reviewed by the assigned manager.'}
        data.update(changes)
        return ReviewDecisionRequest.model_validate(data)

    def test_approve_resolves_task_and_creates_one_unsent_outbox_record(self):
        task = self.create_draft_task()
        resolved = self.db.decide_review(
            self.actor, self.case.case_id, self.request(task), 'approve-1')
        self.assertEqual(resolved.status, 'resolved')
        self.assertEqual(resolved.resolution, 'approved')
        self.assertEqual(resolved.resolved_by, self.actor.user_id)
        self.assertEqual(resolved.approved_draft, task.draft)
        self.assertEqual(self.store.get_case(self.actor, self.case.case_id).state_version, 3)
        queued = self.db.outbox_records(self.actor, self.case.case_id).items
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].review_task_id, task.review_task_id)
        self.assertEqual(queued[0].requirement_ids, task.requirement_ids)
        self.assertEqual(queued[0].delivery_status, 'not_attempted')
        self.assertIsNone(queued[0].recipient_contact_id)
        self.assertIsNone(queued[0].provider_message_id)

    def test_edit_and_approve_uses_guarded_reviewer_content(self):
        task = self.create_draft_task()
        edited = {'subject': 'Required July bank statement',
                  'body': 'Please provide the complete statement for July.',
                  'requirement_ids': task.requirement_ids}
        resolved = self.db.decide_review(self.actor, self.case.case_id,
            self.request(task, 'edit_and_approve', edited_draft=edited), 'edit-1')
        self.assertEqual(resolved.resolution, 'edited_and_approved')
        self.assertEqual(resolved.approved_draft.body, edited['body'])
        self.assertEqual(self.db.outbox_records(self.actor, self.case.case_id).items[0].body, edited['body'])

    def test_reject_draft_resolves_without_outbox(self):
        task = self.create_draft_task()
        resolved = self.db.decide_review(self.actor, self.case.case_id,
            self.request(task, 'reject_draft'), 'reject-1')
        self.assertEqual(resolved.resolution, 'rejected')
        self.assertEqual(self.db.outbox_records(self.actor, self.case.case_id).items, [])

    def test_dismiss_operational_error_resolves_without_outbox(self):
        task = self.create_error_task()
        resolved = self.db.decide_review(self.actor, self.case.case_id,
            self.request(task, 'dismiss_error'), 'dismiss-1')
        self.assertEqual(resolved.resolution, 'dismissed')
        self.assertEqual(self.db.outbox_records(self.actor, self.case.case_id).items, [])

    def test_decision_type_and_edited_requirement_mismatches_do_not_mutate(self):
        task = self.create_draft_task()
        invalid = [
            self.request(task, 'dismiss_error'),
            self.request(task, 'edit_and_approve', edited_draft={
                'subject': 'Safe', 'body': 'Safe body', 'requirement_ids': ['req_foreign']}),
        ]
        for index, request in enumerate(invalid):
            with self.subTest(decision=request.decision), self.assertRaises(DomainError):
                self.db.decide_review(self.actor, self.case.case_id, request, 'invalid-' + str(index))
        current = self.db.review_tasks(self.actor, self.case.case_id).items[0]
        self.assertEqual(current.status, 'open')
        self.assertEqual(self.store.get_case(self.actor, self.case.case_id).state_version, 2)
        self.assertEqual(self.db.outbox_records(self.actor, self.case.case_id).items, [])

    def test_unsafe_human_edit_is_rejected_without_mutation(self):
        task = self.create_draft_task()
        request = self.request(task, 'edit_and_approve', edited_draft={
            'subject': 'Safe', 'body': 'Internal ' + task.requirement_ids[0],
            'requirement_ids': task.requirement_ids})
        with self.assertRaises(DomainError) as raised:
            self.db.decide_review(self.actor, self.case.case_id, request, 'unsafe-edit')
        self.assertEqual(raised.exception.code, 'UNSAFE_DRAFT')
        self.assertEqual(self.store.get_case(self.actor, self.case.case_id).state_version, 2)
        self.assertEqual(self.db.outbox_records(self.actor, self.case.case_id).items, [])

    def test_replay_is_stable_and_key_reuse_is_rejected(self):
        task = self.create_draft_task()
        request = self.request(task)
        first = self.db.decide_review(self.actor, self.case.case_id, request, 'stable-key')
        replay = self.db.decide_review(self.actor, self.case.case_id, request, 'stable-key')
        self.assertEqual(replay, first)
        self.assertEqual(len(self.db.outbox_records(self.actor, self.case.case_id).items), 1)
        changed = self.request(task, reason='A different explanation.')
        with self.assertRaises(DomainError) as raised:
            self.db.decide_review(self.actor, self.case.case_id, changed, 'stable-key')
        self.assertEqual(raised.exception.code, 'IDEMPOTENCY_CONFLICT')

    def test_stale_and_second_decisions_are_rejected(self):
        task = self.create_draft_task()
        stale = self.request(task, expected_state_version=1)
        with self.assertRaises(DomainError) as raised:
            self.db.decide_review(self.actor, self.case.case_id, stale, 'stale')
        self.assertEqual(raised.exception.code, 'STALE_STATE')
        self.db.decide_review(self.actor, self.case.case_id, self.request(task), 'first')
        with self.assertRaises(DomainError) as raised:
            self.db.decide_review(self.actor, self.case.case_id,
                                  self.request(task, expected_state_version=3), 'second')
        self.assertEqual(raised.exception.code, 'REVIEW_ALREADY_RESOLVED')

    def test_concurrent_decisions_commit_once(self):
        task = self.create_draft_task()
        request = self.request(task)

        def decide(key):
            try:
                return self.db.decide_review(self.actor, self.case.case_id, request, key)
            except DomainError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ('concurrent-a', 'concurrent-b')))
        self.assertEqual(sum(isinstance(result, ReviewTaskRecord) for result in results), 1)
        self.assertIn('REVIEW_ALREADY_RESOLVED', results)
        self.assertEqual(len(self.db.outbox_records(self.actor, self.case.case_id).items), 1)

    def test_legacy_record_and_restart_preserve_resolution_outbox_and_replay(self):
        task = self.create_draft_task()
        with self.store.write() as conn:
            raw = json.loads(conn.execute(
                reviews.select().with_only_columns(reviews.c.record).where(
                    reviews.c.review_task_id == task.review_task_id)).scalar_one())
            for key in ('resolution', 'resolved_by', 'resolved_at', 'resolution_reason', 'approved_draft'):
                raw.pop(key, None)
            conn.execute(update(reviews).where(reviews.c.review_task_id == task.review_task_id).values(
                record=json.dumps(raw)))
        legacy = self.db.review_tasks(self.actor, self.case.case_id).items[0]
        request = self.request(legacy)
        first = self.db.decide_review(self.actor, self.case.case_id, request, 'restart-key')
        restarted_store = Store(self.url, access_config())
        try:
            restarted = RuntimeStore(restarted_store)
            self.assertEqual(restarted.review_tasks(self.actor, self.case.case_id).items[0], first)
            self.assertEqual(len(restarted.outbox_records(self.actor, self.case.case_id).items), 1)
            self.assertEqual(restarted.decide_review(
                self.actor, self.case.case_id, request, 'restart-key'), first)
        finally:
            restarted_store.engine.dispose()

    def test_non_manager_and_cross_case_task_cannot_decide(self):
        task = self.create_draft_task()
        reader = Principal(user_id='reader', token_sha256='0' * 64,
            client_ids=frozenset({'client_demo'}), can_manage=False)
        with self.assertRaises(DomainError) as raised:
            self.db.decide_review(reader, self.case.case_id, self.request(task), 'reader')
        self.assertEqual(raised.exception.code, 'FORBIDDEN')

        second = self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(case_request()), 'case-second')
        wrong_case_request = self.request(task, expected_state_version=second.state_version)
        with self.assertRaises(DomainError) as raised:
            self.db.decide_review(self.actor, second.case_id, wrong_case_request, 'wrong-case')
        self.assertEqual(raised.exception.code, 'NOT_FOUND')

    def test_approval_records_resolution_and_outbox_audits(self):
        task = self.create_draft_task()
        self.db.decide_review(self.actor, self.case.case_id, self.request(task), 'audited')
        events = self.store.audit_events(self.actor, self.case.case_id, 0, 100).items
        self.assertEqual([event.action for event in events[-2:]],
                         ['resolve_review_task', 'queue_reviewed_outbox'])
        self.assertEqual(events[-2].old_state_version, 2)
        self.assertEqual(events[-2].new_state_version, 3)
        self.assertEqual(events[-1].outcome, 'queued')

    def test_approval_rolls_back_if_audit_write_fails(self):
        task = self.create_draft_task()
        with self.store.write() as conn:
            conn.execute(text("CREATE TRIGGER fail_review_audit BEFORE INSERT ON audit_events "
                              "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"))
        with self.assertRaises(SQLAlchemyError):
            self.db.decide_review(self.actor, self.case.case_id, self.request(task), 'rollback')
        self.assertEqual(self.store.get_case(self.actor, self.case.case_id).state_version, 2)
        self.assertEqual(self.db.review_tasks(self.actor, self.case.case_id).items[0].status, 'open')
        self.assertEqual(self.db.outbox_records(self.actor, self.case.case_id).items, [])


if __name__ == '__main__':
    unittest.main()
