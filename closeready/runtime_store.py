"""Durable analysis runs and the limited, transactional first action gate."""
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, UniqueConstraint, insert, select, update

from .content_guard import validate_customer_visible_draft
from .models import ActionProposal, CaseSnapshot
from .runtime_models import (
    OutboxPage, OutboxRecord, ReviewDecisionRequest, ReviewTaskPage,
    ReviewTaskRecord, RunRecord,
)
from .config import Principal
from .store import DomainError, Store, cases, forbidden

runtime_metadata = MetaData()
runs = Table('agent_runs', runtime_metadata,
    Column('run_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('actor_id', String, nullable=False), Column('key', String, nullable=False),
    Column('expected_version', Integer, nullable=False), Column('status', String, nullable=False),
    Column('claim_token', String), Column('lease_until', Integer), Column('record', Text, nullable=False),
    Column('decision', Text), UniqueConstraint('actor_id', 'case_id', 'key'))
events = Table('agent_events', runtime_metadata,
    Column('event_id', String, primary_key=True), Column('case_id', String, nullable=False),
    Column('record', Text, nullable=False))
proposals = Table('agent_proposals', runtime_metadata,
    Column('proposal_id', String, primary_key=True), Column('run_id', String, nullable=False, unique=True),
    Column('record', Text, nullable=False), Column('outcome', Text, nullable=False))
reviews = Table('review_tasks', runtime_metadata,
    Column('review_task_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('run_id', String, nullable=False, unique=True), Column('record', Text, nullable=False))
outbox = Table('mail_outbox', runtime_metadata,
    Column('outbox_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('review_task_id', String, nullable=False, unique=True), Column('record', Text, nullable=False))
review_responses = Table('review_idempotent_responses', runtime_metadata,
    Column('actor_id', String, primary_key=True), Column('case_id', String, primary_key=True),
    Column('key', String, primary_key=True), Column('request_hash', String, nullable=False),
    Column('response', Text, nullable=False))
traces = Table('agent_traces', runtime_metadata,
    Column('run_id', String, primary_key=True), Column('step', Integer, primary_key=True), Column('record', Text, nullable=False))


def now():
    return datetime.now(timezone.utc).isoformat()


class RuntimeStore:
    def __init__(self, store: Store):
        self.store = store
        runtime_metadata.create_all(store.engine)

    def check_ready(self):
        self.store.check_ready(runtime_metadata.tables)

    def _row(self, conn, actor, run_id):
        row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one_or_none()
        if row is None:
            raise DomainError('NOT_FOUND', 'Run not found.', 404)
        self.store._case(conn, actor, row['case_id'])
        return row

    def _record(self, conn, row):
        data = json.loads(row['record'])
        data['traces'] = [json.loads(r) for r in conn.execute(select(traces.c.record)
            .where(traces.c.run_id == row['run_id']).order_by(traces.c.step)).scalars()]
        return RunRecord.model_validate(data)

    def get_run(self, actor, run_id):
        with self.store.engine.connect() as conn:
            return self._record(conn, self._row(conn, actor, run_id))

    def expired_run_candidates(self):
        with self.store.engine.connect() as conn:
            rows = conn.execute(select(runs.c.run_id, runs.c.actor_id).where(
                runs.c.status == 'running',
                runs.c.lease_until <= int(time.time())).order_by(runs.c.run_id)).all()
        return [(row.run_id, row.actor_id) for row in rows]

    def queued_candidates(self):
        with self.store.engine.connect() as conn:
            rows = conn.execute(select(
                runs.c.run_id, runs.c.actor_id, cases.c.client_id
            ).select_from(runs.join(cases, runs.c.case_id == cases.c.case_id)).where(
                runs.c.status == 'queued').order_by(runs.c.run_id)).all()
        return [(row.run_id, row.actor_id, row.client_id) for row in rows]

    def recover_expired_system(self, run_id):
        with self.store.write() as conn:
            row = conn.execute(select(runs).where(
                runs.c.run_id == run_id)).mappings().one_or_none()
            if row is None:
                return None
            if row['status'] != 'running':
                return self._record(conn, row)
            if row['lease_until'] > time.time():
                raise DomainError('RUN_ACTIVE', 'Run still has an active claim.', 409)
            raw_case = conn.execute(select(cases.c.snapshot).where(
                cases.c.case_id == row['case_id'])).scalar_one_or_none()
            if raw_case is None:
                raise DomainError('NOT_FOUND', 'Case not found.', 404)
            case = CaseSnapshot.model_validate_json(raw_case)
            system_actor = Principal(user_id='system_worker', token_sha256='0' * 64,
                client_ids=frozenset({case.client_id}), can_manage=True)
            self._review(conn, system_actor, row, case, 'INTERRUPTED_RUN', [])
            self._set(conn, row, 'needs_review', 'INTERRUPTED_RUN',
                claim_token=None, lease_until=None)
            changed = conn.execute(select(runs).where(
                runs.c.run_id == run_id)).mappings().one()
            return self._record(conn, changed)

    def review_tasks(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            query = select(reviews).where(reviews.c.case_id == case_id)
            if cursor:
                query = query.where(reviews.c.review_task_id > cursor)
            rows = conn.execute(query.order_by(reviews.c.review_task_id).limit(limit + 1)).mappings().all()
        return ReviewTaskPage(items=[ReviewTaskRecord.model_validate_json(r['record']) for r in rows[:limit]],
            next_cursor=rows[limit - 1]['review_task_id'] if len(rows) > limit else None)

    def outbox_records(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            query = select(outbox).where(outbox.c.case_id == case_id)
            if cursor:
                query = query.where(outbox.c.outbox_id > cursor)
            rows = conn.execute(query.order_by(outbox.c.outbox_id).limit(limit + 1)).mappings().all()
        return OutboxPage(items=[OutboxRecord.model_validate_json(row['record']) for row in rows[:limit]],
            next_cursor=rows[limit - 1]['outbox_id'] if len(rows) > limit else None)

    def load_outbox(self, conn, actor, case_id, outbox_id):
        self.store._case(conn, actor, case_id)
        raw = conn.execute(select(outbox.c.record).where(
            outbox.c.outbox_id == outbox_id, outbox.c.case_id == case_id)).scalar_one_or_none()
        if raw is None:
            raise DomainError('NOT_FOUND', 'Outbox record not found.', 404)
        return OutboxRecord.model_validate_json(raw)

    def save_outbox(self, conn, record):
        conn.execute(update(outbox).where(outbox.c.outbox_id == record.outbox_id).values(
            record=record.model_dump_json()))

    def create_policy_review(self, conn, actor, case, reason_code, reason, requirement_ids, key):
        """Assigned review from communication policy. Completes without calling a provider."""
        existing = conn.execute(select(runs).where(runs.c.actor_id == actor.user_id,
            runs.c.case_id == case.case_id, runs.c.key == key)).mappings().one_or_none()
        if existing:
            raw = conn.execute(select(reviews.c.record).where(
                reviews.c.run_id == existing['run_id'])).scalar_one_or_none()
            if raw:
                return ReviewTaskRecord.model_validate_json(raw)
        run_id, event_id = 'run_' + uuid4().hex, 'event_' + uuid4().hex
        record = RunRecord(run_id=run_id, event_id=event_id, case_id=case.case_id,
            start_state_version=case.state_version, status='completed',
            started_at=now(), finished_at=now(), provider='communication_policy',
            model='none', live=False, prompt_version='n/a', schema_version='0.8',
            error_code=reason_code)
        conn.execute(insert(events).values(event_id=event_id, case_id=case.case_id, record=json.dumps({
            'type': 'communication_policy_review', 'occurred_at': now(), 'case_id': case.case_id,
            'actor_user_id': actor.user_id, 'reason_code': reason_code})))
        conn.execute(insert(runs).values(run_id=run_id, case_id=case.case_id, actor_id=actor.user_id,
            key=key, expected_version=case.state_version, status='completed',
            record=record.model_dump_json()))
        row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one()
        return self._review(conn, actor, row, case, reason_code, requirement_ids, reason=reason)

    @staticmethod
    def _decision_digest(request):
        canonical = json.dumps(request.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def decide_review(self, actor, case_id: str, request: ReviewDecisionRequest, key: str):
        error, current = None, None
        digest = self._decision_digest(request)
        with self.store.write() as conn:
            try:
                current = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                replay = conn.execute(select(review_responses).where(
                    review_responses.c.actor_id == actor.user_id,
                    review_responses.c.case_id == case_id,
                    review_responses.c.key == key)).mappings().one_or_none()
                if replay:
                    if replay['request_hash'] != digest:
                        raise DomainError('IDEMPOTENCY_CONFLICT', 'Key was already used with different input.', 409)
                    return ReviewTaskRecord.model_validate_json(replay['response'])
                raw = conn.execute(select(reviews.c.record).where(
                    reviews.c.review_task_id == request.review_task_id,
                    reviews.c.case_id == case_id)).scalar_one_or_none()
                if raw is None:
                    raise DomainError('NOT_FOUND', 'Review task not found.', 404)
                task = ReviewTaskRecord.model_validate_json(raw)
                if task.assigned_to != actor.user_id:
                    raise forbidden()
                if task.status != 'open':
                    raise DomainError('REVIEW_ALREADY_RESOLVED', 'Review task is already resolved.', 409)
                if current.state_version != request.expected_state_version:
                    raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
                draft_decisions = {'approve_draft', 'edit_and_approve', 'reject_draft'}
                if (request.decision in draft_decisions) != (task.draft is not None):
                    raise DomainError('INVALID_REVIEW_DECISION', 'Decision does not match the review task type.', 422)
                final_draft = None
                if request.decision == 'approve_draft':
                    final_draft = task.draft
                elif request.decision == 'edit_and_approve':
                    final_draft = request.edited_draft
                    if (len(final_draft.requirement_ids) != len(task.requirement_ids)
                            or set(final_draft.requirement_ids) != set(task.requirement_ids)):
                        raise DomainError('INVALID_REVIEW_DECISION',
                            'Edited draft requirements must match the review task.', 422)
                if final_draft:
                    validate_customer_visible_draft(final_draft)
                resolution = {'approve_draft': 'approved',
                    'edit_and_approve': 'edited_and_approved', 'reject_draft': 'rejected',
                    'dismiss_error': 'dismissed'}[request.decision]
                resolved_at = now()
                resolved = ReviewTaskRecord.model_validate({**task.model_dump(mode='json'),
                    'status': 'resolved', 'resolution': resolution, 'resolved_by': actor.user_id,
                    'resolved_at': resolved_at, 'resolution_reason': request.reason,
                    'approved_draft': final_draft.model_dump(mode='json') if final_draft else None})
                queued = None
                if final_draft:
                    queued = OutboxRecord(outbox_id='outbox_' + uuid4().hex, case_id=case_id,
                        review_task_id=task.review_task_id, requirement_ids=task.requirement_ids,
                        subject=final_draft.subject, body=final_draft.body,
                        created_by=actor.user_id, created_at=resolved_at)
                    conn.execute(insert(outbox).values(outbox_id=queued.outbox_id, case_id=case_id,
                        review_task_id=task.review_task_id, record=queued.model_dump_json()))
                changed = CaseSnapshot.model_validate({**current.model_dump(mode='json'),
                    'state_version': current.state_version + 1, 'readiness_status': 'collecting'})
                updated = conn.execute(update(cases).where(cases.c.case_id == case_id,
                    cases.c.state_version == current.state_version).values(
                        state_version=changed.state_version, snapshot=changed.model_dump_json()))
                if updated.rowcount != 1:
                    raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
                conn.execute(update(reviews).where(reviews.c.review_task_id == task.review_task_id).values(
                    record=resolved.model_dump_json()))
                self.store._audit(conn, actor, 'resolve_review_task', 'executed', resolution,
                    changed, old=current.state_version, new=changed.state_version, run_id=task.run_id)
                if queued:
                    self.store._audit(conn, actor, 'queue_reviewed_outbox', 'queued',
                        'REVIEWED_DELIVERY_PENDING', changed, old=changed.state_version,
                        new=changed.state_version, run_id=task.run_id)
                conn.execute(insert(review_responses).values(actor_id=actor.user_id, case_id=case_id,
                    key=key, request_hash=digest, response=resolved.model_dump_json()))
            except DomainError as exc:
                error = exc
                self.store._audit(conn, actor, 'resolve_review_task',
                    'stale' if exc.code == 'STALE_STATE' else 'blocked', exc.code, current,
                    old=current.state_version if current else None)
        if error:
            raise error
        return resolved

    def start(self, actor, case_id, version, key, provider, model, live,
              event_type='case_analysis_requested', audit_action='request_case_analysis'):
        with self.store.write() as conn:
            case = self.store._case(conn, actor, case_id)
            if not actor.can_manage:
                raise forbidden()
            existing = conn.execute(select(runs).where(runs.c.actor_id == actor.user_id,
                runs.c.case_id == case_id, runs.c.key == key)).mappings().one_or_none()
            if existing:
                if existing['expected_version'] != version:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 'Key was used with different input.', 409)
                return self._record(conn, existing)
            if case.state_version != version:
                raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
            active = conn.execute(select(runs.c.run_id).where(runs.c.case_id == case_id,
                runs.c.status.in_(['queued', 'running']))).first()
            if active:
                raise DomainError('RUN_ACTIVE', 'An analysis is active; inspect or recover that run first.', 409)
            run_id, event_id = 'run_' + uuid4().hex, 'event_' + uuid4().hex
            record = RunRecord(run_id=run_id, event_id=event_id, case_id=case_id,
                start_state_version=version, status='queued', started_at=None, finished_at=None,
                provider=provider, model=model, live=live)
            conn.execute(insert(events).values(event_id=event_id, case_id=case_id, record=json.dumps({
                'type': event_type, 'occurred_at': now(), 'case_id': case_id,
                'actor_user_id': actor.user_id, 'expected_state_version': version})))
            conn.execute(insert(runs).values(run_id=run_id, case_id=case_id, actor_id=actor.user_id,
                key=key, expected_version=version, status='queued', record=record.model_dump_json()))
            self.store._audit(conn, actor, audit_action, 'queued', 'Analysis event recorded.',
                case, old=version, new=version, event_id=event_id, run_id=run_id)
        return record

    def _set(self, conn, row, status, error=None, **extra):
        data = json.loads(row['record'])
        data.update(status=status, error_code=error)
        if status == 'running':
            data['started_at'] = now()
        if status in ('completed', 'needs_review', 'failed', 'stale'):
            data['finished_at'] = now()
        conn.execute(update(runs).where(runs.c.run_id == row['run_id']).values(
            status=status, record=json.dumps(data), **extra))

    def claim(self, actor, run_id):
        with self.store.write() as conn:
            row = self._row(conn, actor, run_id)
            if not actor.can_manage or row['actor_id'] != actor.user_id:
                raise forbidden()
            if row['status'] != 'queued':
                return None
            token = uuid4().hex
            self._set(conn, row, 'running', claim_token=token, lease_until=int(time.time()) + 300)
            return token

    def _claimed(self, conn, actor, run_id, token):
        row = self._row(conn, actor, run_id)
        if not actor.can_manage or row['actor_id'] != actor.user_id:
            raise forbidden()
        if row['status'] != 'running' or row['claim_token'] != token or row['lease_until'] <= time.time():
            raise DomainError('CLAIM_LOST', 'Run claim expired or was recovered.', 409)
        return row

    def context(self, actor, run_id, token):
        with self.store.engine.connect() as conn:
            row = self._claimed(conn, actor, run_id, token)
            case = self.store._case(conn, actor, row['case_id'])
            return case

    def trace(self, actor, run_id, token, trace):
        with self.store.write() as conn:
            self._claimed(conn, actor, run_id, token)
            conn.execute(insert(traces).values(run_id=run_id, step=trace.step, record=trace.model_dump_json()))

    def _review(self, conn, actor, row, case, code, requirement_ids, draft=None, reason=None):
        existing = conn.execute(select(reviews.c.record).where(reviews.c.run_id == row['run_id'])).scalar_one_or_none()
        if existing:
            return ReviewTaskRecord.model_validate_json(existing)
        task = ReviewTaskRecord(review_task_id='review_' + uuid4().hex, case_id=case.case_id,
            run_id=row['run_id'], requirement_ids=requirement_ids, reason_code=code,
            assigned_to=case.owner_user_id, draft=draft, created_at=now(),
            reason=reason or 'Inspect the run error and case before taking action.')
        conn.execute(insert(reviews).values(review_task_id=task.review_task_id, case_id=case.case_id,
            run_id=row['run_id'], record=task.model_dump_json()))
        data = case.model_dump()
        # A new unresolved human task invalidates readiness confirmation.
        data.update(state_version=case.state_version + 1, readiness_status='collecting')
        changed = CaseSnapshot.model_validate(data)
        conn.execute(update(cases).where(cases.c.case_id == case.case_id).values(
            state_version=changed.state_version, snapshot=changed.model_dump_json()))
        self.store._audit(conn, actor, 'create_review_task', 'executed', code, changed,
            old=case.state_version, new=changed.state_version,
            event_id=json.loads(row['record'])['event_id'], run_id=row['run_id'])
        return task

    def apply(self, actor, run_id, token, content, loaded_version):
        with self.store.write() as conn:
            row = self._claimed(conn, actor, run_id, token)
            if row['decision']:
                raise DomainError('ALREADY_DECIDED', 'This run already recorded its action.', 409)
            case = self.store._case(conn, actor, row['case_id'])
            if case.state_version != loaded_version:
                raise DomainError('STALE_STATE', 'Case changed after tool read.', 409)
            proposal = ActionProposal.model_validate({**content.model_dump(mode='json'),
                'proposal_id': 'proposal_' + uuid4().hex, 'run_id': run_id,
                'case_id': case.case_id, 'expected_state_version': loaded_version})
            action = proposal.root
            known = {r.requirement_id: r for r in case.requirements}
            if not set(action.requirement_ids).issubset(known) or action.finding_ids:
                raise DomainError('INVALID_TOOL', 'Unsupported references.', 422)
            outstanding = {r.requirement_id for r in case.requirements if r.status not in ('accepted', 'waived')}
            draft = None
            if action.action_type in ('request_documents', 'request_clarification'):
                if not action.requirement_ids or not set(action.requirement_ids).issubset(outstanding):
                    raise DomainError('INVALID_TOOL', 'Draft must refer only to outstanding items.', 422)
                draft = action.payload
                validate_customer_visible_draft(draft)
                code = 'MAIL_NOT_CONFIGURED'
            elif action.action_type == 'create_review_task':
                if action.payload.evidence_refs:
                    raise DomainError('INVALID_TOOL', 'Document evidence storage is not available yet.', 422)
                code = 'AGENT_REQUESTED_REVIEW'
            elif action.action_type == 'no_action':
                pending_review = conn.execute(select(reviews.c.review_task_id).where(reviews.c.case_id == case.case_id)).first()
                if outstanding or not case.requirements or pending_review or any(r.reviewer_status == 'pending' for r in case.requirements):
                    raise DomainError('INVALID_TOOL', 'Unresolved work requires a review or document request.', 422)
                code = None
            else:
                raise DomainError('UNSUPPORTED_ACTION', 'This business action is not implemented yet.', 422)
            explanation = action.payload.issue if action.action_type == 'create_review_task' else action.reason
            task = self._review(conn, actor, row, case, code, action.requirement_ids, draft, explanation) if code else None
            outcome = {'outcome': 'blocked' if draft else 'executed',
                'code': code, 'review_task_id': task.review_task_id if task else None, 'sent': False}
            conn.execute(insert(proposals).values(proposal_id=action.proposal_id, run_id=run_id,
                record=proposal.model_dump_json(), outcome=json.dumps(outcome)))
            conn.execute(update(runs).where(runs.c.run_id == run_id).values(decision=json.dumps(outcome)))
            self.store._audit(conn, actor, action.action_type, outcome['outcome'], code or 'No action required.',
                case, old=loaded_version, new=loaded_version + (1 if task else 0),
                event_id=json.loads(row['record'])['event_id'], run_id=run_id)
            return outcome

    def finish(self, actor, run_id, token, status, code=None):
        with self.store.write() as conn:
            row = self._claimed(conn, actor, run_id, token)
            case = self.store._case(conn, actor, row['case_id'])
            if code:
                self._review(conn, actor, row, case, code, [])
            self._set(conn, row, status, code)
            self.store._audit(conn, actor, 'finish_analysis', status, code or 'Bounded analysis finished.',
                case, event_id=json.loads(row['record'])['event_id'], run_id=run_id)
        return self.get_run(actor, run_id)

    def recover(self, actor, run_id):
        with self.store.write() as conn:
            row = self._row(conn, actor, run_id)
            if not actor.can_manage:
                raise forbidden()
            if row['status'] == 'running':
                if row['lease_until'] > time.time():
                    raise DomainError('RUN_ACTIVE', 'Run still has an active claim.', 409)
                case = self.store._case(conn, actor, row['case_id'])
                self._review(conn, actor, row, case, 'INTERRUPTED_RUN', [])
                self._set(conn, row, 'needs_review', 'INTERRUPTED_RUN', claim_token=None)
            elif row['status'] == 'queued':
                # Explicit recovery abandons queued analysis rather than silently replaying inference.
                case = self.store._case(conn, actor, row['case_id'])
                self._review(conn, actor, row, case, 'INTERRUPTED_RUN', [])
                self._set(conn, row, 'needs_review', 'INTERRUPTED_RUN')
        return self.get_run(actor, run_id)
