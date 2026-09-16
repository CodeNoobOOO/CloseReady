"""Sandbox follow-up path. Models assess; this module sends, ingests and schedules."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Column, MetaData, String, Table, Text, UniqueConstraint, insert, select, update

from .communication_models import (
    AssessReplyResult, CommitmentPage, CommitmentRecord, DeliverOutboxRequest,
    DeliveryResult, DispatchRemindersResult, FindingPage, IngestReplyRequest,
    IngestReplyResult, MailboxMessage, MailboxPage, QuarantinedReply, ReminderPage,
    ReminderRecord, ReplyPage, ReplyRecord,
)
from .content_guard import validate_customer_visible_draft
from .mail import MailBackend
from .models import CaseSnapshot, MessageDraft, ReplyAssessment, ReplyAssessmentContent
from .reply_assessment import assess_reply as run_reply_assessment
from .runtime_models import OutboxRecord
from .store import DomainError, Store, cases, forbidden

communication_metadata = MetaData()
replies = Table('client_replies', communication_metadata,
    Column('reply_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('record', Text, nullable=False))
quarantine = Table('quarantined_replies', communication_metadata,
    Column('reply_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('record', Text, nullable=False))
findings = Table('reply_findings', communication_metadata,
    Column('finding_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('reply_id', String, nullable=False), Column('record', Text, nullable=False))
commitments = Table('commitments', communication_metadata,
    Column('commitment_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('record', Text, nullable=False))
reminders = Table('reminders', communication_metadata,
    Column('reminder_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('dedupe_key', String, nullable=False), Column('record', Text, nullable=False),
    UniqueConstraint('case_id', 'dedupe_key'))
mailbox = Table('sandbox_mailbox', communication_metadata,
    Column('message_id', String, primary_key=True), Column('case_id', String, nullable=False, index=True),
    Column('record', Text, nullable=False))
comm_responses = Table('communication_idempotent_responses', communication_metadata,
    Column('actor_id', String, primary_key=True), Column('operation', String, primary_key=True),
    Column('key', String, primary_key=True), Column('request_hash', String, nullable=False),
    Column('response', Text, nullable=False))


def utcnow():
    return datetime.now(timezone.utc)


def payload_digest(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class CommunicationStore:
    def __init__(self, store: Store, runtime_store, mail: MailBackend | None):
        self.store = store
        self.runtime = runtime_store
        self.mail = mail
        communication_metadata.create_all(store.engine)

    def _require_mail(self):
        if self.mail is None or getattr(self.mail, 'live', False):
            raise DomainError('MAIL_NOT_CONFIGURED',
                'Sandbox mail (test_sink) is not enabled for this process.', 503)

    def _policy(self, case):
        policy = self.store.access.communication_policy(case.policy_id, case.policy_version)
        if policy is None:
            raise DomainError('POLICY_NOT_CONFIGURED',
                'Communication policy is not configured for this case.', 503)
        return policy

    def _active_contacts(self, client_id):
        return self.store.access.contacts_for(client_id, active_only=True)

    def _resolve_contact(self, case, contact_id=None):
        contacts = self._active_contacts(case.client_id)
        if contact_id:
            match = next((c for c in contacts if c.contact_id == contact_id), None)
            if match is None:
                raise DomainError('CONTACT_UNRESOLVED',
                    'Recipient is not an active approved contact for this client.', 422)
            return match
        if len(contacts) == 1:
            return contacts[0]
        if not contacts:
            raise DomainError('CONTACT_UNRESOLVED',
                'No active approved contact is configured for this client.', 422)
        raise DomainError('CONTACT_AMBIGUOUS',
            'Select an approved contact_id; more than one is active.', 422)

    def _in_window(self, policy, when=None):
        moment = (when or utcnow()).astimezone(ZoneInfo(policy.timezone))
        start_text, end_text = policy.sending_window_local.split('-')
        start = datetime.strptime(start_text, '%H:%M').time()
        end = datetime.strptime(end_text, '%H:%M').time()
        current = moment.time()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def _next_window(self, policy, when=None):
        moment = (when or utcnow()).astimezone(ZoneInfo(policy.timezone))
        start_text, _end = policy.sending_window_local.split('-')
        start = datetime.strptime(start_text, '%H:%M').time()
        candidate = moment.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if candidate <= moment:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def _outstanding(self, case, requirement_ids):
        known = {r.requirement_id: r for r in case.requirements}
        if not set(requirement_ids).issubset(known):
            raise DomainError('INVALID_TOOL', 'Requirement is not on this case.', 422)
        return [rid for rid in requirement_ids if known[rid].status not in ('accepted', 'waived')]

    def _bump(self, conn, actor, case, reason, action, outcome='executed'):
        changed = CaseSnapshot.model_validate({**case.model_dump(mode='json'),
            'state_version': case.state_version + 1, 'readiness_status': 'collecting'})
        updated = conn.execute(update(cases).where(cases.c.case_id == case.case_id,
            cases.c.state_version == case.state_version).values(
                state_version=changed.state_version, snapshot=changed.model_dump_json()))
        if updated.rowcount != 1:
            raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
        self.store._audit(conn, actor, action, outcome, reason, changed,
            old=case.state_version, new=changed.state_version)
        return changed

    def _replay_json(self, conn, actor, operation, key, digest):
        row = conn.execute(select(comm_responses).where(
            comm_responses.c.actor_id == actor.user_id, comm_responses.c.operation == operation,
            comm_responses.c.key == key)).mappings().one_or_none()
        if row is None:
            return None
        if row['request_hash'] != digest:
            raise DomainError('IDEMPOTENCY_CONFLICT', 'Key was already used with different input.', 409)
        return row['response']

    def _remember(self, conn, actor, operation, key, digest, result):
        conn.execute(insert(comm_responses).values(actor_id=actor.user_id, operation=operation,
            key=key, request_hash=digest, response=result.model_dump_json()))

    def _page(self, conn, table, case_id, cursor, limit, model, id_column):
        query = select(table).where(table.c.case_id == case_id)
        if cursor:
            query = query.where(id_column > cursor)
        rows = conn.execute(query.order_by(id_column).limit(limit + 1)).mappings().all()
        items = [model.model_validate_json(row['record']) for row in rows[:limit]]
        next_cursor = rows[limit - 1][id_column.name] if len(rows) > limit else None
        return items, next_cursor

    def list_replies(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            items, next_cursor = self._page(conn, replies, case_id, cursor, limit, ReplyRecord, replies.c.reply_id)
        return ReplyPage(items=items, next_cursor=next_cursor)

    def list_commitments(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            items, next_cursor = self._page(
                conn, commitments, case_id, cursor, limit, CommitmentRecord, commitments.c.commitment_id)
        return CommitmentPage(items=items, next_cursor=next_cursor)

    def list_reminders(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            items, next_cursor = self._page(
                conn, reminders, case_id, cursor, limit, ReminderRecord, reminders.c.reminder_id)
        return ReminderPage(items=items, next_cursor=next_cursor)

    def list_findings(self, actor, case_id, cursor=None, limit=50):
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            items, next_cursor = self._page(
                conn, findings, case_id, cursor, limit, ReplyAssessment, findings.c.finding_id)
        return FindingPage(items=items, next_cursor=next_cursor)

    def list_mailbox(self, actor, case_id, cursor=None, limit=50):
        self._require_mail()
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            items, next_cursor = self._page(
                conn, mailbox, case_id, cursor, limit, MailboxMessage, mailbox.c.message_id)
        return MailboxPage(items=items, next_cursor=next_cursor)

    def deliver_outbox(self, actor, case_id, outbox_id, request: DeliverOutboxRequest, key: str):
        self._require_mail()
        error, result = None, None
        digest = payload_digest(request.model_dump(mode='json'))
        operation = 'deliver_outbox:' + outbox_id
        with self.store.write() as conn:
            try:
                case = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                replay = self._replay_json(conn, actor, operation, key, digest)
                if replay:
                    return DeliveryResult.model_validate_json(replay)
                policy = self._policy(case)
                queued = self.runtime.load_outbox(conn, actor, case_id, outbox_id)
                if queued.delivery_status != 'not_attempted':
                    raise DomainError('ALREADY_DELIVERED', 'This outbox item already has a delivery attempt.', 409)
                outstanding = self._outstanding(case, queued.requirement_ids)
                if set(outstanding) != set(queued.requirement_ids):
                    raise DomainError('OBSOLETE_DRAFT',
                        'One or more drafted items are no longer outstanding; regenerate the message.', 409)
                contact = self._resolve_contact(case, request.contact_id)
                if not self._in_window(policy):
                    raise DomainError('OUTSIDE_SENDING_WINDOW',
                        'Sending is suppressed outside the approved window.', 409)
                validate_customer_visible_draft(MessageDraft(
                    subject=queued.subject, body=queued.body, requirement_ids=queued.requirement_ids))
                claimed = OutboxRecord.model_validate({**queued.model_dump(mode='json'),
                    'recipient_contact_id': contact.contact_id, 'delivery_status': 'queued'})
                self.runtime.save_outbox(conn, claimed)
                result = self._send_outbox(conn, actor, case, claimed, contact)
                self._remember(conn, actor, operation, key, digest, result)
            except DomainError as exc:
                error = exc
                self.store._audit(conn, actor, 'deliver_outbox', 'blocked', exc.code)
        if error:
            raise error
        return result

    def _send_outbox(self, conn, actor, case, queued, contact):
        try:
            provider_id = self.mail.send(to_email=contact.approved_email, subject=queued.subject,
                body=queued.body, metadata={'case_id': case.case_id, 'source': 'outbox',
                    'source_id': queued.outbox_id, 'contact_id': contact.contact_id})
            status = 'sent'
        except TimeoutError:
            provider_id, status = None, 'delivery_unknown'
        except DomainError:
            raise
        except Exception:
            provider_id, status = None, 'failed'
        sent = OutboxRecord.model_validate({**queued.model_dump(mode='json'),
            'recipient_contact_id': contact.contact_id, 'provider_message_id': provider_id,
            'delivery_status': status})
        self.runtime.save_outbox(conn, sent)
        if status == 'sent':
            message = MailboxMessage(message_id=provider_id, case_id=case.case_id,
                backend='test_sink', to_email=contact.approved_email, subject=queued.subject,
                body=queued.body, source='outbox', source_id=queued.outbox_id, sent_at=utcnow())
            conn.execute(insert(mailbox).values(message_id=message.message_id, case_id=case.case_id,
                record=message.model_dump_json()))
        self.store._audit(conn, actor, 'deliver_outbox',
            'queued' if status == 'sent' else status, status, case)
        return DeliveryResult(outbox_id=queued.outbox_id, delivery_status=status,
            recipient_contact_id=contact.contact_id, provider_message_id=provider_id,
            mailbox_backend='test_sink')

    def ingest_reply(self, actor, case_id, request: IngestReplyRequest, key: str):
        error, result = None, None
        digest = payload_digest(request.model_dump(mode='json'))
        operation = 'ingest_reply:' + case_id
        with self.store.write() as conn:
            try:
                case = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                replay = self._replay_json(conn, actor, operation, key, digest)
                if replay:
                    return IngestReplyResult.model_validate_json(replay)
                if case.state_version != request.expected_state_version:
                    raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
                match = next((c for c in self._active_contacts(case.client_id)
                              if c.approved_email.lower() == request.sender_email.lower()), None)
                if match is None:
                    review = self.runtime.create_policy_review(conn, actor, case, 'UNKNOWN_SENDER',
                        'Inbound mail was not from an approved contact for this client.',
                        [], 'unknown-sender|' + key)
                    quarantined = QuarantinedReply(reply_id='reply_' + uuid4().hex, case_id=case_id,
                        sender_email=request.sender_email, received_at=request.received_at,
                        reason_code='UNKNOWN_SENDER', review_task_id=review.review_task_id)
                    conn.execute(insert(quarantine).values(reply_id=quarantined.reply_id, case_id=case_id,
                        record=quarantined.model_dump_json()))
                    result = IngestReplyResult(associated=False, quarantined=quarantined)
                else:
                    record = ReplyRecord(reply_id='reply_' + uuid4().hex, case_id=case_id,
                        event_id='event_' + uuid4().hex, provider_message_id=request.provider_message_id,
                        conversation_ref=None, sender_contact_id=match.contact_id,
                        received_at=request.received_at, body=request.body)
                    conn.execute(insert(replies).values(reply_id=record.reply_id, case_id=case_id,
                        record=record.model_dump_json()))
                    self._bump(conn, actor, case, 'client_reply_received', 'ingest_reply')
                    result = IngestReplyResult(associated=True, reply=record)
                self._remember(conn, actor, operation, key, digest, result)
            except DomainError as exc:
                error = exc
                self.store._audit(conn, actor, 'ingest_reply',
                    'stale' if exc.code == 'STALE_STATE' else 'blocked', exc.code)
        if error:
            raise error
        return result

    def assess_reply(self, actor, case_id, reply_id, expected_version, key, provider):
        if provider is None:
            raise DomainError('LLM_UNAVAILABLE', 'Reply assessment requires a configured LLM provider.', 503)
        digest = payload_digest({'expected_state_version': expected_version, 'reply_id': reply_id})
        operation = 'assess_reply:' + reply_id
        with self.store.engine.connect() as conn:
            replay = self._replay_json(conn, actor, operation, key, digest)
            if replay:
                return AssessReplyResult.model_validate_json(replay)
            case = self.store._case(conn, actor, case_id)
            if not actor.can_manage:
                raise forbidden()
            raw = conn.execute(select(replies.c.record).where(
                replies.c.reply_id == reply_id, replies.c.case_id == case_id)).scalar_one_or_none()
            if raw is None:
                raise DomainError('NOT_FOUND', 'Reply not found.', 404)
            reply = ReplyRecord.model_validate_json(raw)
        content = run_reply_assessment(provider, case, reply)
        return self.apply_reply_assessment(actor, case_id, reply_id, content, expected_version, key)

    def apply_reply_assessment(self, actor, case_id, reply_id, content: ReplyAssessmentContent,
                               expected_version, key):
        error, result = None, None
        digest = payload_digest({'expected_state_version': expected_version, 'reply_id': reply_id})
        operation = 'assess_reply:' + reply_id
        with self.store.write() as conn:
            try:
                case = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                replay = self._replay_json(conn, actor, operation, key, digest)
                if replay:
                    return AssessReplyResult.model_validate_json(replay)
                if case.state_version != expected_version:
                    raise DomainError('STALE_STATE', 'Reload case before retrying.', 409)
                raw = conn.execute(select(replies.c.record).where(
                    replies.c.reply_id == reply_id, replies.c.case_id == case_id)).scalar_one_or_none()
                if raw is None:
                    raise DomainError('NOT_FOUND', 'Reply not found.', 404)
                reply = ReplyRecord.model_validate_json(raw)
                if conn.execute(select(findings.c.finding_id).where(
                        findings.c.reply_id == reply_id)).first():
                    raise DomainError('ALREADY_ASSESSED', 'This reply already has an assessment.', 409)
                if not set(content.requirement_ids).issubset(
                        {r.requirement_id for r in case.requirements}):
                    raise DomainError('INVALID_TOOL',
                        'Assessment refers to a requirement outside this case.', 422)
                finding = ReplyAssessment.model_validate({
                    **content.model_dump(mode='json'),
                    'finding_id': 'finding_' + uuid4().hex, 'case_id': case_id,
                    'input_state_version': case.state_version, 'reply_id': reply_id})
                conn.execute(insert(findings).values(finding_id=finding.finding_id, case_id=case_id,
                    reply_id=reply_id, record=finding.model_dump_json()))
                commitment = reminder = review_id = None
                policy = self.store.access.communication_policy(case.policy_id, case.policy_version)
                review_code = review_reason = None
                if finding.intent in ('dispute', 'waiver_request'):
                    review_code, review_reason = 'REPLY_REQUIRES_REVIEW', finding.intent
                elif finding.intent == 'submission_commitment':
                    if finding.needs_clarification or finding.promised_at is None:
                        review_code, review_reason = 'AMBIGUOUS_COMMITMENT', 'Promised date is unclear.'
                    elif policy is None:
                        raise DomainError('POLICY_NOT_CONFIGURED',
                            'Communication policy is not configured for this case.', 503)
                    elif finding.promised_at > case.due_at:
                        review_code = 'COMMITMENT_AFTER_DEADLINE'
                        review_reason = 'Promised date is after the case deadline.'
                    else:
                        commitment, reminder, case = self._record_commitment(
                            conn, actor, case, finding, reply, policy)
                elif finding.needs_clarification:
                    review_code, review_reason = 'REPLY_NEEDS_CLARIFICATION', 'Reply requires clarification.'
                if review_code:
                    review = self.runtime.create_policy_review(conn, actor, case, review_code,
                        review_reason, finding.requirement_ids, 'reply-review|' + reply_id)
                    review_id = review.review_task_id
                elif commitment is None:
                    self._bump(conn, actor, case, finding.intent, 'assess_reply')
                result = AssessReplyResult(finding=finding, commitment=commitment,
                    reminder=reminder, review_task_id=review_id)
                self._remember(conn, actor, operation, key, digest, result)
            except DomainError as exc:
                error = exc
                self.store._audit(conn, actor, 'assess_reply',
                    'stale' if exc.code == 'STALE_STATE' else 'blocked', exc.code)
        if error:
            raise error
        return result

    def _record_commitment(self, conn, actor, case, finding, reply, policy):
        requirement_ids = finding.requirement_ids or [
            r.requirement_id for r in case.requirements if r.status not in ('accepted', 'waived')]
        outstanding = self._outstanding(case, requirement_ids)
        if not outstanding:
            raise DomainError('INVALID_TOOL', 'Commitment must refer to an outstanding requirement.', 422)
        created = None
        reminder = None
        previous = [CommitmentRecord.model_validate_json(row['record']) for row in
            conn.execute(select(commitments).where(commitments.c.case_id == case.case_id)).mappings()]
        for requirement_id in outstanding:
            for current in previous:
                if current.requirement_id == requirement_id and current.status == 'active':
                    superseded = CommitmentRecord.model_validate({**current.model_dump(mode='json'),
                        'status': 'superseded'})
                    conn.execute(update(commitments).where(
                        commitments.c.commitment_id == current.commitment_id).values(
                            record=superseded.model_dump_json()))
            record = CommitmentRecord(commitment_id='commitment_' + uuid4().hex, case_id=case.case_id,
                requirement_id=requirement_id, promised_at=finding.promised_at,
                source_reply_id=reply.reply_id, finding_id=finding.finding_id,
                status='active', created_at=utcnow(), policy_version=policy.version)
            conn.execute(insert(commitments).values(commitment_id=record.commitment_id,
                case_id=case.case_id, record=record.model_dump_json()))
            created = record
            self._cancel_scheduled(conn, actor, case, [requirement_id], 'commitment_reschedule')
            reminder = self._schedule_follow_up(conn, actor, case, [requirement_id], record, policy)
        case = self._bump(conn, actor, case, 'record_commitment', 'record_commitment')
        return created, reminder, case

    def _cancel_scheduled(self, conn, actor, case, requirement_ids, reason):
        rows = conn.execute(select(reminders).where(reminders.c.case_id == case.case_id)).mappings().all()
        for row in rows:
            reminder = ReminderRecord.model_validate_json(row['record'])
            if reminder.status != 'scheduled':
                continue
            if set(reminder.requirement_ids) & set(requirement_ids):
                cancelled = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                    'status': 'cancelled'})
                conn.execute(update(reminders).where(
                    reminders.c.reminder_id == reminder.reminder_id).values(
                        record=cancelled.model_dump_json()))
                self.store._audit(conn, actor, 'cancel_reminder', 'executed', reason, case)

    def cancel_scheduled_for_resolved(
            self, conn, actor, case, requirement_ids):
        """Cancel related unsent reminders inside the caller's case transaction."""
        self._cancel_scheduled(
            conn, actor, case, requirement_ids, 'requirement_resolved')

    def _schedule_follow_up(self, conn, actor, case, requirement_ids, commitment, policy):
        contact = self._resolve_contact(case)
        sent_count = 0
        latest_sent = None
        for row in conn.execute(select(reminders).where(
                reminders.c.case_id == case.case_id)).mappings():
            reminder = ReminderRecord.model_validate_json(row['record'])
            overlapping = set(reminder.requirement_ids) & set(requirement_ids)
            if overlapping and reminder.status in ('sent', 'queued', 'delivery_unknown'):
                sent_count += 1
                latest_sent = reminder
        if sent_count >= policy.max_reminders_per_requirement:
            self.runtime.create_policy_review(conn, actor, case, 'REMINDER_LIMIT',
                'Automatic reminders stopped after the configured limit.',
                requirement_ids, 'reminder-limit|' + commitment.commitment_id)
            return None
        scheduled_at = commitment.promised_at + timedelta(hours=policy.commitment_grace_hours)
        if latest_sent is not None:
            min_next = latest_sent.scheduled_at + timedelta(hours=policy.min_reminder_interval_hours)
            if scheduled_at < min_next:
                scheduled_at = min_next
        if not self._in_window(policy, scheduled_at):
            scheduled_at = self._next_window(policy, scheduled_at)
        draft = self._reminder_draft(case, requirement_ids)
        validate_customer_visible_draft(draft)
        day = scheduled_at.astimezone(timezone.utc).date().isoformat()
        dedupe_key = '|'.join([case.case_id, ','.join(sorted(requirement_ids)), day, contact.contact_id])
        existing = conn.execute(select(reminders.c.record).where(
            reminders.c.case_id == case.case_id, reminders.c.dedupe_key == dedupe_key)).scalar_one_or_none()
        if existing:
            current = ReminderRecord.model_validate_json(existing)
            if current.status != 'cancelled':
                return current
        reminder = ReminderRecord(reminder_id='reminder_' + uuid4().hex, case_id=case.case_id,
            requirement_ids=list(requirement_ids), scheduled_at=scheduled_at, status='scheduled',
            dedupe_key=dedupe_key, contact_id=contact.contact_id, policy_version=policy.version,
            source_commitment_id=commitment.commitment_id, attempt_count=0,
            subject=draft.subject, body=draft.body)
        if existing:
            conn.execute(update(reminders).where(
                reminders.c.case_id == case.case_id, reminders.c.dedupe_key == dedupe_key).values(
                    reminder_id=reminder.reminder_id, record=reminder.model_dump_json()))
        else:
            conn.execute(insert(reminders).values(reminder_id=reminder.reminder_id, case_id=case.case_id,
                dedupe_key=dedupe_key, record=reminder.model_dump_json()))
        self.store._audit(conn, actor, 'schedule_reminder', 'queued', 'Follow-up scheduled from commitment.', case)
        return reminder

    def _reminder_draft(self, case, requirement_ids):
        labels = []
        kept = []
        for requirement in case.requirements:
            if requirement.requirement_id not in requirement_ids:
                continue
            if requirement.status in ('accepted', 'waived'):
                continue
            kept.append(requirement.requirement_id)
            label = requirement.document_type.replace('_', ' ')
            if requirement.description:
                label += ' (' + requirement.description + ')'
            labels.append(label + ' for ' + requirement.accounting_period)
        if not labels:
            raise DomainError('OBSOLETE_DRAFT', 'No outstanding items remain for a reminder.', 409)
        return MessageDraft(subject='Reminder: outstanding bookkeeping documents',
            body='Please send the outstanding items: ' + '; '.join(labels) + '.',
            requirement_ids=kept)

    def dispatch_due_reminders(self, actor, case_id, key: str):
        self._require_mail()
        error, result = None, None
        digest = payload_digest({'case_id': case_id, 'action': 'dispatch_due'})
        operation = 'dispatch_reminders:' + case_id
        with self.store.write() as conn:
            try:
                case = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                replay = self._replay_json(conn, actor, operation, key, digest)
                if replay:
                    return DispatchRemindersResult.model_validate_json(replay)
                policy = self._policy(case)
                dispatched = []
                now = utcnow()
                for row in conn.execute(select(reminders).where(
                        reminders.c.case_id == case_id)).mappings():
                    reminder = ReminderRecord.model_validate_json(row['record'])
                    if reminder.status != 'scheduled' or reminder.scheduled_at > now:
                        continue
                    outstanding = self._outstanding(case, reminder.requirement_ids)
                    if set(outstanding) != set(reminder.requirement_ids):
                        cancelled = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                            'status': 'cancelled'})
                        conn.execute(update(reminders).where(
                            reminders.c.reminder_id == reminder.reminder_id).values(
                                record=cancelled.model_dump_json()))
                        self.store._audit(conn, actor, 'cancel_reminder', 'executed', 'obsolete_items', case)
                        dispatched.append(cancelled)
                        continue
                    if not self._in_window(policy):
                        delayed = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                            'scheduled_at': self._next_window(policy)})
                        conn.execute(update(reminders).where(
                            reminders.c.reminder_id == reminder.reminder_id).values(
                                record=delayed.model_dump_json()))
                        dispatched.append(delayed)
                        continue
                    contact = self._resolve_contact(case, reminder.contact_id)
                    validate_customer_visible_draft(MessageDraft(
                        subject=reminder.subject, body=reminder.body,
                        requirement_ids=reminder.requirement_ids))
                    queued = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                        'status': 'queued', 'attempt_count': reminder.attempt_count + 1})
                    try:
                        provider_id = self.mail.send(to_email=contact.approved_email,
                            subject=reminder.subject, body=reminder.body,
                            metadata={'case_id': case_id, 'source': 'reminder',
                                      'source_id': reminder.reminder_id})
                        status = 'sent'
                    except TimeoutError:
                        provider_id, status = None, 'delivery_unknown'
                    except Exception:
                        provider_id, status = None, 'failed'
                    finished = ReminderRecord.model_validate({**queued.model_dump(mode='json'),
                        'status': status, 'provider_message_id': provider_id})
                    conn.execute(update(reminders).where(
                        reminders.c.reminder_id == reminder.reminder_id).values(
                            record=finished.model_dump_json()))
                    if status == 'sent':
                        message = MailboxMessage(message_id=provider_id, case_id=case_id,
                            backend='test_sink', to_email=contact.approved_email,
                            subject=reminder.subject, body=reminder.body, source='reminder',
                            source_id=reminder.reminder_id, sent_at=utcnow())
                        conn.execute(insert(mailbox).values(message_id=message.message_id,
                            case_id=case_id, record=message.model_dump_json()))
                    self.store._audit(conn, actor, 'dispatch_reminder',
                        'queued' if status == 'sent' else status, status, case)
                    dispatched.append(finished)
                result = DispatchRemindersResult(items=dispatched)
                self._remember(conn, actor, operation, key, digest, result)
            except DomainError as exc:
                error = exc
                self.store._audit(conn, actor, 'dispatch_reminders', 'blocked', exc.code)
        if error:
            raise error
        return result
