"""SQLite transactions for cases, audit records and idempotent responses."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import (
    Column, Integer, MetaData, String, Table, Text as SQLText,
    create_engine, insert, select, update,
)
from sqlalchemy.engine import make_url

from .case_requests import AuditPage, CasePage, ChangeDeadlineRequest, CreateCaseRequest
from .config import AccessConfig, Principal
from .models import CaseSnapshot

metadata = MetaData()
cases = Table('cases', metadata,
    Column('case_id', String, primary_key=True),
    Column('client_id', String, nullable=False, index=True),
    Column('state_version', Integer, nullable=False),
    Column('snapshot', SQLText, nullable=False),
    Column('policy_binding', SQLText, nullable=False))
audit = Table('audit_events', metadata,
    Column('audit_id', Integer, primary_key=True, autoincrement=True),
    Column('case_id', String, nullable=True, index=True),
    Column('record', SQLText, nullable=False))
responses = Table('idempotent_responses', metadata,
    Column('actor_id', String, primary_key=True),
    Column('operation', String, primary_key=True),
    Column('key', String, primary_key=True),
    Column('request_hash', String, nullable=False),
    Column('response', SQLText, nullable=False))
schema = Table('schema_version', metadata, Column('version', Integer, primary_key=True))


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def forbidden():
    return DomainError('FORBIDDEN', 'Action is not permitted for this actor.', 403)


class Store:
    def __init__(self, database_url: str, access: AccessConfig):
        url = make_url(database_url)
        if url.drivername != 'sqlite' or not url.database or url.database == ':memory:':
            raise RuntimeError('This increment requires a file-backed sqlite:/// database URL.')
        self.engine = create_engine(database_url, connect_args={'check_same_thread': False, 'timeout': 10})
        self.access = access
        metadata.create_all(self.engine)
        with self.write() as conn:
            versions = list(conn.execute(select(schema.c.version)).scalars())
            if not versions:
                conn.execute(insert(schema).values(version=1))
            elif versions != [1]:
                raise RuntimeError('Unsupported database schema version; migration required.')

    @contextmanager
    def write(self):
        # SQLite obtains the write reservation before reading a version or key.
        with self.engine.connect() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _case(self, conn, actor: Principal, case_id: str) -> CaseSnapshot:
        raw = conn.execute(select(cases.c.snapshot).where(
            cases.c.case_id == case_id, cases.c.client_id.in_(actor.client_ids))).scalar_one_or_none()
        if raw is None:
            raise DomainError('NOT_FOUND', 'Case not found.', 404)
        return CaseSnapshot.model_validate_json(raw)

    def get_case(self, actor: Principal, case_id: str) -> CaseSnapshot:
        with self.engine.connect() as conn:
            return self._case(conn, actor, case_id)

    def list_cases(self, actor: Principal, cursor: str | None, limit: int) -> CasePage:
        query = select(cases.c.snapshot).where(cases.c.client_id.in_(actor.client_ids))
        if cursor is not None:
            query = query.where(cases.c.case_id > cursor)
        with self.engine.connect() as conn:
            rows = conn.execute(query.order_by(cases.c.case_id).limit(limit + 1)).scalars().all()
        items = [CaseSnapshot.model_validate_json(row) for row in rows[:limit]]
        return CasePage(items=items, next_cursor=items[-1].case_id if len(rows) > limit else None)

    def audit_events(self, actor: Principal, case_id: str, cursor: int, limit: int) -> AuditPage:
        with self.engine.connect() as conn:
            self._case(conn, actor, case_id)
            rows = conn.execute(select(audit).where(audit.c.case_id == case_id, audit.c.audit_id > cursor)
                .order_by(audit.c.audit_id).limit(limit + 1)).mappings().all()
        items = [dict(json.loads(row['record']), audit_id=row['audit_id']) for row in rows[:limit]]
        return AuditPage(items=items, next_cursor=str(rows[limit - 1]['audit_id']) if len(rows) > limit else None)

    def _audit(self, conn, actor, action, outcome, reason, case=None, old=None, new=None, event_id=None, run_id=None):
        record = {'case_id': case.case_id if case else None, 'event_id': event_id, 'run_id': run_id,
            'actor_user_id': actor.user_id, 'action': action, 'outcome': outcome, 'reason': reason,
            'occurred_at': datetime.now(timezone.utc).isoformat(),
            'old_state_version': old, 'new_state_version': new,
            'policy_id': case.policy_id if case else None,
            'policy_version': case.policy_version if case else None}
        conn.execute(insert(audit).values(case_id=record['case_id'], record=json.dumps(record)))

    def _replay(self, conn, actor, operation, key, digest):
        row = conn.execute(select(responses).where(responses.c.actor_id == actor.user_id,
            responses.c.operation == operation, responses.c.key == key)).mappings().one_or_none()
        if row:
            if row['request_hash'] != digest:
                raise DomainError('IDEMPOTENCY_CONFLICT', 'Key was already used with different input.', 409)
            return CaseSnapshot.model_validate_json(row['response'])
        return None

    def _remember(self, conn, actor, operation, key, digest, result):
        conn.execute(insert(responses).values(actor_id=actor.user_id, operation=operation,
            key=key, request_hash=digest, response=result.model_dump_json()))

    @staticmethod
    def _digest(request):
        canonical = json.dumps(request.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def create_case(self, actor: Principal, request: CreateCaseRequest, key: str) -> CaseSnapshot:
        error = None
        with self.write() as conn:
            try:
                if not actor.can_manage or request.client_id not in actor.client_ids:
                    raise forbidden()
                # Authorisation always precedes replay, even after access revocation.
                result = self._replay(conn, actor, 'create_case', key, self._digest(request))
                if result:
                    return result
                owner = next((p for p in self.access.principals if p.user_id == request.owner_user_id
                    and p.can_manage and request.client_id in p.client_ids), None)
                policy = next((p for p in self.access.policies if p.policy_id == request.policy_id
                    and request.client_id in p.client_ids), None)
                if owner is None or policy is None:
                    raise forbidden()
                data = request.model_dump(exclude={'requirements'})
                result = CaseSnapshot.model_validate({**data, 'case_id': 'case_' + uuid4().hex,
                    'state_version': 1, 'readiness_status': 'collecting', 'policy_version': policy.version,
                    'requirements': [r.to_requirement('req_' + uuid4().hex) for r in request.requirements]})
                conn.execute(insert(cases).values(case_id=result.case_id, client_id=result.client_id,
                    state_version=1, snapshot=result.model_dump_json(), policy_binding=policy.model_dump_json()))
                self._audit(conn, actor, 'create_case', 'executed', 'Case created.', result, new=1)
                self._remember(conn, actor, 'create_case', key, self._digest(request), result)
            except DomainError as exc:
                error = exc
                self._audit(conn, actor, 'create_case', 'blocked', exc.code)
        if error:
            raise error
        return result

    def change_deadline(self, actor: Principal, case_id: str, request: ChangeDeadlineRequest, key: str) -> CaseSnapshot:
        error, current = None, None
        operation = 'change_deadline:' + case_id
        with self.write() as conn:
            try:
                current = self._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                result = self._replay(conn, actor, operation, key, self._digest(request))
                if result:
                    return result
                if current.state_version != request.expected_state_version:
                    raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
                data = current.model_dump()
                data.update(due_at=request.due_at, state_version=current.state_version + 1)
                result = CaseSnapshot.model_validate(data)
                changed = conn.execute(update(cases).where(cases.c.case_id == case_id,
                    cases.c.state_version == request.expected_state_version).values(
                        snapshot=result.model_dump_json(), state_version=result.state_version))
                if changed.rowcount != 1:
                    raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
                self._audit(conn, actor, 'change_deadline', 'executed', request.reason,
                    result, old=current.state_version, new=result.state_version)
                self._remember(conn, actor, operation, key, self._digest(request), result)
            except DomainError as exc:
                error = exc
                self._audit(conn, actor, 'change_deadline', 'stale' if exc.code == 'STALE_STATE' else 'blocked',
                    exc.code, current, old=current.state_version if current else None)
        if error:
            raise error
        return result
