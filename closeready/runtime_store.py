"""Durable analysis runs and the limited, transactional first action gate."""
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, UniqueConstraint, insert, select, update

from .models import ActionProposal, CaseSnapshot
from .runtime_models import ReviewTaskPage, ReviewTaskRecord, RunRecord
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
traces = Table('agent_traces', runtime_metadata,
    Column('run_id', String, primary_key=True), Column('step', Integer, primary_key=True), Column('record', Text, nullable=False))


def now():
    return datetime.now(timezone.utc).isoformat()


class RuntimeStore:
    def __init__(self, store: Store):
        self.store = store
        runtime_metadata.create_all(store.engine)

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

    def review_tasks(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            query = select(reviews).where(reviews.c.case_id == case_id)
            if cursor:
                query = query.where(reviews.c.review_task_id > cursor)
            rows = conn.execute(query.order_by(reviews.c.review_task_id).limit(limit + 1)).mappings().all()
        return ReviewTaskPage(items=[ReviewTaskRecord.model_validate_json(r['record']) for r in rows[:limit]],
            next_cursor=rows[limit - 1]['review_task_id'] if len(rows) > limit else None)

    def start(self, actor, case_id, version, key, provider, model, live):
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
                'type': 'case_analysis_requested', 'occurred_at': now(), 'case_id': case_id,
                'actor_user_id': actor.user_id, 'expected_state_version': version})))
            conn.execute(insert(runs).values(run_id=run_id, case_id=case_id, actor_id=actor.user_id,
                key=key, expected_version=version, status='queued', record=record.model_dump_json()))
            self.store._audit(conn, actor, 'request_case_analysis', 'queued', 'Analysis event recorded.',
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
                draft, code = action.payload, 'MAIL_NOT_CONFIGURED'
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
