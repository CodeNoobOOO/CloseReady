"""Sandbox follow-up path. Models assess; this module sends, ingests and schedules."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Column, MetaData, String, Table, Text, UniqueConstraint, insert, select, update

from .case_references import CaseCommunicationReference, CaseReferenceResolution
from .communication_models import (
    AssessReplyResult, CommitmentPage, CommitmentRecord, DeliverOutboxRequest,
    DeliveryResult, DispatchRemindersResult, FindingPage, InboundPollResult,
    InboundProcessResult, InboundQuarantinePage, InboundQuarantineRecord,
    IngestReplyRequest, IngestReplyResult, MailboxMessage, MailboxPage,
    QuarantinedReply, ReminderPage, ReminderRecord, ReplyPage, ReplyRecord,
)
from .content_guard import validate_customer_visible_draft
from .mail import MailBackend
from .mail_messages import (
    InboundMail, apply_customer_reference, extract_public_reference,
    new_message_id, normalize_message_id, thread_candidates,
)
from .models import CaseSnapshot, MessageDraft, ReplyAssessment, ReplyAssessmentContent
from .reply_assessment import assess_reply as run_reply_assessment
from .runtime_models import OutboxRecord, ReviewTaskRecord
from .runtime_store import reviews
from .store import DomainError, Store, case_communication_refs, cases, forbidden

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
outbound_threads = Table('outbound_mail_threads', communication_metadata,
    Column('provider_message_id', String, primary_key=True),
    Column('case_id', String, nullable=False, index=True),
    Column('public_reference', String, nullable=False),
    Column('contact_id', String, nullable=False),
    Column('source', String, nullable=False),
    Column('source_id', String, nullable=False))
inbound_seen = Table('inbound_mail_seen', communication_metadata,
    Column('provider_message_id', String, primary_key=True),
    Column('record', Text, nullable=False))
inbound_quarantine = Table('inbound_quarantine', communication_metadata,
    Column('quarantine_id', String, primary_key=True),
    Column('reason_code', String, nullable=False, index=True),
    Column('record', Text, nullable=False))
comm_responses = Table('communication_idempotent_responses', communication_metadata,
    Column('actor_id', String, primary_key=True), Column('operation', String, primary_key=True),
    Column('key', String, primary_key=True), Column('request_hash', String, nullable=False),
    Column('response', Text, nullable=False))


def utcnow():
    return datetime.now(timezone.utc)


MAX_INBOUND_DOCUMENT_BYTES = 5 * 1024 * 1024


def payload_digest(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class CommunicationStore:
    def __init__(self, store: Store, runtime_store, mail: MailBackend | None,
                 document_store=None):
        self.store = store
        self.runtime = runtime_store
        self.mail = mail
        self.document_store = document_store
        communication_metadata.create_all(store.engine)

    def _require_mail(self):
        if self.mail is None:
            raise DomainError('MAIL_NOT_CONFIGURED',
                'Mail backend is not enabled for this process.', 503)

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

    def _case_reminders(self, conn, case_id):
        return [ReminderRecord.model_validate_json(row['record']) for row in
            conn.execute(select(reminders).where(reminders.c.case_id == case_id)).mappings()]

    def _attempted_reminder_count(self, conn, case_id, requirement_ids):
        sent_count = 0
        latest = None
        for reminder in self._case_reminders(conn, case_id):
            if not (set(reminder.requirement_ids) & set(requirement_ids)):
                continue
            if reminder.status in ('sent', 'queued', 'delivery_unknown'):
                sent_count += 1
                if latest is None or reminder.scheduled_at >= latest.scheduled_at:
                    latest = reminder
        return sent_count, latest

    def _follow_up_paused(self, conn, case_id, requirement_ids):
        wanted = set(requirement_ids)
        for raw in conn.execute(select(reviews.c.record).where(
                reviews.c.case_id == case_id)).scalars():
            task = ReviewTaskRecord.model_validate_json(raw)
            if task.status != 'open':
                continue
            paused = set(task.requirement_ids)
            if paused & wanted:
                return True
        return False

    def _escalate_reminder_limit(self, conn, actor, case, requirement_ids):
        return self.runtime.create_policy_review(
            conn, actor, case, 'REMINDER_LIMIT',
            'Automatic reminders stopped after the configured limit.',
            requirement_ids, 'reminder-limit|' + case.case_id + '|' + ','.join(sorted(requirement_ids)))

    def _delivery_review(self, conn, actor, case, requirement_ids, code, source_id):
        reasons = {
            'REMINDER_DELIVERY_FAILED': 'Reminder delivery failed; a human must decide the next send.',
            'REMINDER_DELIVERY_UNKNOWN': 'Reminder delivery is unknown; do not automatically resend.',
            'DELIVERY_FAILED': 'Outbox delivery failed; a human must decide the next send.',
            'DELIVERY_UNKNOWN': 'Outbox delivery is unknown; do not automatically resend.',
        }
        return self.runtime.create_policy_review(
            conn, actor, case, code, reasons[code], requirement_ids, code + '|' + source_id)

    def _save_reminder(self, conn, reminder):
        conn.execute(update(reminders).where(
            reminders.c.reminder_id == reminder.reminder_id).values(
                record=reminder.model_dump_json()))

    def _pending_reminder(self, conn, case_id, requirement_ids, contact_id):
        wanted = set(requirement_ids)
        for reminder in self._case_reminders(conn, case_id):
            if reminder.status not in ('scheduled', 'paused'):
                continue
            if reminder.contact_id == contact_id and set(reminder.requirement_ids) == wanted:
                return reminder
        return None

    @staticmethod
    def _reminder_dedupe_key(case_id, requirement_ids, contact_id, scheduled_at):
        slot = scheduled_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return '|'.join([case_id, ','.join(sorted(requirement_ids)), contact_id, slot])

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

    def _case_reference(self, conn, case_id: str) -> CaseCommunicationReference:
        raw = conn.execute(select(case_communication_refs.c.record).where(
            case_communication_refs.c.case_id == case_id)).scalar_one_or_none()
        if raw is None:
            raise DomainError('NOT_FOUND', 'Case communication reference is missing.', 404)
        return CaseCommunicationReference.model_validate_json(raw)

    def _owner_actor(self, case: CaseSnapshot):
        actor = next((principal for principal in self.store.access.principals
            if principal.user_id == case.owner_user_id and principal.can_manage
            and case.client_id in principal.client_ids), None)
        if actor is None:
            raise DomainError('ACTOR_CONFIGURATION_MISSING',
                'The case owner is not an active manager in access configuration.', 503)
        return actor

    def _actor_for_case(self, case: CaseSnapshot, suggested=None):
        if suggested is not None and suggested.can_manage and case.client_id in suggested.client_ids:
            return suggested
        return self._owner_actor(case)

    def _load_case_unscoped(self, conn, case_id: str) -> CaseSnapshot | None:
        raw = conn.execute(select(cases.c.snapshot).where(
            cases.c.case_id == case_id)).scalar_one_or_none()
        if raw is None:
            return None
        return CaseSnapshot.model_validate_json(raw)

    def _composed_copy(self, conn, case_id: str, subject: str, body: str):
        reference = self._case_reference(conn, case_id)
        visible_subject, visible_body = apply_customer_reference(
            subject, body, reference.public_reference)
        return reference, visible_subject, visible_body

    def _remember_thread(self, conn, provider_id, case_id, public_reference, contact_id,
                         source, source_id):
        normalized = normalize_message_id(provider_id)
        if not normalized:
            return
        conn.execute(insert(outbound_threads).values(
            provider_message_id=normalized, case_id=case_id,
            public_reference=public_reference, contact_id=contact_id,
            source=source, source_id=source_id))

    def _thread_mapping(self, conn, provider_id: str | None):
        normalized = normalize_message_id(provider_id)
        if not normalized:
            return None
        return conn.execute(select(outbound_threads).where(
            outbound_threads.c.provider_message_id == normalized)).mappings().one_or_none()

    def _mailbox_fields(self):
        return self.mail.backend_name, bool(getattr(self.mail, 'live', False))

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
        reference, subject, body = self._composed_copy(conn, case.case_id, queued.subject, queued.body)
        message_id = new_message_id(contact.approved_email)
        try:
            provider_id = self.mail.send(to_email=contact.approved_email, subject=subject,
                body=body, metadata={'public_reference': reference.public_reference,
                    'source': 'outbox', 'source_id': queued.outbox_id,
                    'contact_id': contact.contact_id, 'message_id': message_id})
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
        backend, live = self._mailbox_fields()
        if status == 'sent':
            message = MailboxMessage(message_id=provider_id, case_id=case.case_id,
                backend=backend, live=live, to_email=contact.approved_email, subject=subject,
                body=body, source='outbox', source_id=queued.outbox_id, sent_at=utcnow())
            conn.execute(insert(mailbox).values(message_id=message.message_id, case_id=case.case_id,
                record=message.model_dump_json()))
            self._remember_thread(conn, provider_id, case.case_id, reference.public_reference,
                contact.contact_id, 'outbox', queued.outbox_id)
        elif status == 'failed':
            self._delivery_review(
                conn, actor, case, queued.requirement_ids, 'DELIVERY_FAILED', queued.outbox_id)
        elif status == 'delivery_unknown':
            self._delivery_review(
                conn, actor, case, queued.requirement_ids, 'DELIVERY_UNKNOWN', queued.outbox_id)
        self.store._audit(conn, actor, 'deliver_outbox',
            'queued' if status == 'sent' else status, status, case)
        return DeliveryResult(outbox_id=queued.outbox_id, delivery_status=status,
            recipient_contact_id=contact.contact_id, provider_message_id=provider_id,
            mailbox_backend=backend, live=live)

    def ingest_reply(self, actor, case_id, request: IngestReplyRequest, key: str,
                     *, conversation_ref=None, attachment_document_ids=None):
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
                        conversation_ref=conversation_ref, sender_contact_id=match.contact_id,
                        received_at=request.received_at, body=request.body,
                        attachment_document_ids=list(attachment_document_ids or []))
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
        created_by_requirement = {}
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
            created_by_requirement[requirement_id] = record
            self._cancel_scheduled(conn, actor, case, [requirement_id], 'commitment_reschedule')
        case = self._bump(conn, actor, case, 'record_commitment', 'record_commitment')
        for requirement_id in outstanding:
            reminder = self._schedule_follow_up(
                conn, actor, case, [requirement_id], policy,
                commitment=created_by_requirement[requirement_id])
            case = self.store._case(conn, actor, case.case_id)
        return created, reminder, case

    def _cancel_scheduled(self, conn, actor, case, requirement_ids, reason):
        rows = conn.execute(select(reminders).where(reminders.c.case_id == case.case_id)).mappings().all()
        for row in rows:
            reminder = ReminderRecord.model_validate_json(row['record'])
            if reminder.status not in ('scheduled', 'paused'):
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

    def _schedule_follow_up(self, conn, actor, case, requirement_ids, policy, commitment=None):
        outstanding = self._outstanding(case, requirement_ids)
        if not outstanding:
            return None
        if self._follow_up_paused(conn, case.case_id, outstanding):
            return None
        contact = self._resolve_contact(case)
        pending = self._pending_reminder(conn, case.case_id, outstanding, contact.contact_id)
        if pending is not None:
            return pending
        sent_count, latest_sent = self._attempted_reminder_count(conn, case.case_id, outstanding)
        if sent_count >= policy.max_reminders_per_requirement:
            self._escalate_reminder_limit(conn, actor, case, outstanding)
            return None
        if commitment is not None:
            scheduled_at = commitment.promised_at + timedelta(hours=policy.commitment_grace_hours)
        else:
            scheduled_at = utcnow() + timedelta(hours=policy.min_reminder_interval_hours)
        if latest_sent is not None:
            min_next = latest_sent.scheduled_at + timedelta(hours=policy.min_reminder_interval_hours)
            if scheduled_at < min_next:
                scheduled_at = min_next
        if not self._in_window(policy, scheduled_at):
            scheduled_at = self._next_window(policy, scheduled_at)
        draft = self._reminder_draft(case, outstanding)
        validate_customer_visible_draft(draft)
        dedupe_key = self._reminder_dedupe_key(
            case.case_id, outstanding, contact.contact_id, scheduled_at)
        existing = conn.execute(select(reminders.c.record).where(
            reminders.c.case_id == case.case_id, reminders.c.dedupe_key == dedupe_key)).scalar_one_or_none()
        if existing:
            current = ReminderRecord.model_validate_json(existing)
            if current.status != 'cancelled':
                return current
        source_id = None
        if commitment is not None:
            source_id = commitment.commitment_id
        elif latest_sent is not None:
            source_id = latest_sent.source_commitment_id
        reminder = ReminderRecord(reminder_id='reminder_' + uuid4().hex, case_id=case.case_id,
            requirement_ids=list(outstanding), scheduled_at=scheduled_at, status='scheduled',
            dedupe_key=dedupe_key, contact_id=contact.contact_id, policy_version=policy.version,
            source_commitment_id=source_id, attempt_count=0,
            subject=draft.subject, body=draft.body)
        if existing:
            conn.execute(update(reminders).where(
                reminders.c.case_id == case.case_id, reminders.c.dedupe_key == dedupe_key).values(
                    reminder_id=reminder.reminder_id, record=reminder.model_dump_json()))
        else:
            conn.execute(insert(reminders).values(reminder_id=reminder.reminder_id, case_id=case.case_id,
                dedupe_key=dedupe_key, record=reminder.model_dump_json()))
        reason = 'Follow-up scheduled from commitment.' if commitment is not None else (
            'Next automatic reminder scheduled under policy.')
        self.store._audit(conn, actor, 'schedule_reminder', 'queued', reason, case)
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
                    if reminder.status == 'paused':
                        if self._follow_up_paused(conn, case_id, reminder.requirement_ids):
                            continue
                        scheduled_at = now if self._in_window(policy, now) else self._next_window(policy, now)
                        reminder = ReminderRecord.model_validate({
                            **reminder.model_dump(mode='json'),
                            'status': 'scheduled', 'scheduled_at': scheduled_at,
                        })
                        self._save_reminder(conn, reminder)
                        self.store._audit(conn, actor, 'resume_reminder', 'queued',
                            'follow_up_resumed', case)
                    if reminder.status != 'scheduled' or reminder.scheduled_at > now:
                        continue
                    outstanding = self._outstanding(case, reminder.requirement_ids)
                    if set(outstanding) != set(reminder.requirement_ids):
                        cancelled = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                            'status': 'cancelled'})
                        self._save_reminder(conn, cancelled)
                        self.store._audit(conn, actor, 'cancel_reminder', 'executed', 'obsolete_items', case)
                        dispatched.append(cancelled)
                        continue
                    if self._follow_up_paused(conn, case_id, reminder.requirement_ids):
                        paused = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                            'status': 'paused'})
                        self._save_reminder(conn, paused)
                        self.store._audit(conn, actor, 'dispatch_reminder', 'blocked',
                            'follow_up_paused', case)
                        dispatched.append(paused)
                        continue
                    sent_count, _latest = self._attempted_reminder_count(
                        conn, case_id, reminder.requirement_ids)
                    if sent_count >= policy.max_reminders_per_requirement:
                        cancelled = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                            'status': 'cancelled'})
                        self._save_reminder(conn, cancelled)
                        self._escalate_reminder_limit(
                            conn, actor, case, reminder.requirement_ids)
                        case = self.store._case(conn, actor, case_id)
                        self.store._audit(conn, actor, 'cancel_reminder', 'executed',
                            'REMINDER_LIMIT', case)
                        dispatched.append(cancelled)
                        continue
                    if not self._in_window(policy):
                        delayed = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                            'scheduled_at': self._next_window(policy)})
                        self._save_reminder(conn, delayed)
                        dispatched.append(delayed)
                        continue
                    contact = self._resolve_contact(case, reminder.contact_id)
                    validate_customer_visible_draft(MessageDraft(
                        subject=reminder.subject, body=reminder.body,
                        requirement_ids=reminder.requirement_ids))
                    queued = ReminderRecord.model_validate({**reminder.model_dump(mode='json'),
                        'status': 'queued', 'attempt_count': reminder.attempt_count + 1})
                    reference, subject, body = self._composed_copy(
                        conn, case_id, reminder.subject, reminder.body)
                    try:
                        provider_id = self.mail.send(to_email=contact.approved_email,
                            subject=subject, body=body,
                            metadata={'public_reference': reference.public_reference,
                                      'source': 'reminder', 'source_id': reminder.reminder_id,
                                      'contact_id': contact.contact_id,
                                      'message_id': new_message_id(contact.approved_email)})
                        status = 'sent'
                    except TimeoutError:
                        provider_id, status = None, 'delivery_unknown'
                    except Exception:
                        provider_id, status = None, 'failed'
                    finished = ReminderRecord.model_validate({**queued.model_dump(mode='json'),
                        'status': status, 'provider_message_id': provider_id})
                    self._save_reminder(conn, finished)
                    backend, live = self._mailbox_fields()
                    if status == 'sent':
                        message = MailboxMessage(message_id=provider_id, case_id=case_id,
                            backend=backend, live=live, to_email=contact.approved_email,
                            subject=subject, body=body, source='reminder',
                            source_id=reminder.reminder_id, sent_at=utcnow())
                        conn.execute(insert(mailbox).values(message_id=message.message_id,
                            case_id=case_id, record=message.model_dump_json()))
                        self._remember_thread(conn, provider_id, case_id, reference.public_reference,
                            contact.contact_id, 'reminder', reminder.reminder_id)
                        case = self.store._case(conn, actor, case_id)
                        self._schedule_follow_up(
                            conn, actor, case, reminder.requirement_ids, policy)
                        case = self.store._case(conn, actor, case_id)
                    elif status == 'failed':
                        self._delivery_review(
                            conn, actor, case, reminder.requirement_ids,
                            'REMINDER_DELIVERY_FAILED', reminder.reminder_id)
                        case = self.store._case(conn, actor, case_id)
                    elif status == 'delivery_unknown':
                        self._delivery_review(
                            conn, actor, case, reminder.requirement_ids,
                            'REMINDER_DELIVERY_UNKNOWN', reminder.reminder_id)
                        case = self.store._case(conn, actor, case_id)
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

    def list_quarantine(self, actor, cursor=None, limit=50):
        if not actor.can_manage:
            raise forbidden()
        with self.store.engine.connect() as conn:
            query = select(inbound_quarantine)
            if cursor:
                query = query.where(inbound_quarantine.c.quarantine_id > cursor)
            rows = conn.execute(
                query.order_by(inbound_quarantine.c.quarantine_id).limit(limit + 1)
            ).mappings().all()
        items = [InboundQuarantineRecord.model_validate_json(row['record']) for row in rows[:limit]]
        next_cursor = rows[limit - 1]['quarantine_id'] if len(rows) > limit else None
        return InboundQuarantinePage(items=items, next_cursor=next_cursor)

    def _seen_result(self, provider_message_id: str) -> InboundProcessResult | None:
        with self.store.engine.connect() as conn:
            raw = conn.execute(select(inbound_seen.c.record).where(
                inbound_seen.c.provider_message_id == provider_message_id)).scalar_one_or_none()
        if raw is None:
            return None
        return InboundProcessResult.model_validate_json(raw)

    def _store_seen(self, provider_message_id: str, result: InboundProcessResult) -> None:
        with self.store.write() as conn:
            existing = conn.execute(select(inbound_seen.c.provider_message_id).where(
                inbound_seen.c.provider_message_id == provider_message_id)).scalar_one_or_none()
            if existing:
                return
            conn.execute(insert(inbound_seen).values(
                provider_message_id=provider_message_id, record=result.model_dump_json()))

    def _resolve_inbound(self, message: InboundMail):
        with self.store.engine.connect() as conn:
            for candidate in thread_candidates(message):
                mapping = self._thread_mapping(conn, candidate)
                if mapping is not None:
                    resolution = self.store.resolve_case_reference(
                        mapping['public_reference'], message.sender_email)
                    return resolution, mapping['public_reference']
        extracted = extract_public_reference(message.subject, message.body)
        if extracted is None:
            return CaseReferenceResolution(
                matched=False, reason_code='REFERENCE_NOT_FOUND'), None
        return self.store.resolve_case_reference(extracted, message.sender_email), extracted

    def _quarantine_inbound(self, message: InboundMail, resolution: CaseReferenceResolution,
                            public_reference, actor=None) -> InboundProcessResult:
        reason = resolution.reason_code or 'REFERENCE_NOT_FOUND'
        review_id = None
        with self.store.write() as conn:
            if public_reference and reason in ('SENDER_NOT_APPROVED', 'REFERENCE_REVOKED'):
                raw = conn.execute(select(case_communication_refs.c.record).where(
                    case_communication_refs.c.public_reference == public_reference.strip().upper()
                )).scalar_one_or_none()
                if raw is not None:
                    reference = CaseCommunicationReference.model_validate_json(raw)
                    case = self._load_case_unscoped(conn, reference.case_id)
                    if case is not None:
                        case_actor = self._actor_for_case(case, actor)
                        review = self.runtime.create_policy_review(
                            conn, case_actor, case, reason,
                            'Inbound mail could not be associated with an approved sender.',
                            [], 'inbound-quarantine|' + message.provider_message_id)
                        review_id = review.review_task_id
                        self.store._audit(conn, case_actor, 'quarantine_inbound', 'blocked',
                            reason, case)
            record = InboundQuarantineRecord(
                quarantine_id='quarantine_' + uuid4().hex,
                sender_email=message.sender_email or 'unknown',
                received_at=message.received_at, reason_code=reason,
                provider_message_id=message.provider_message_id, review_task_id=review_id)
            conn.execute(insert(inbound_quarantine).values(
                quarantine_id=record.quarantine_id, reason_code=record.reason_code,
                record=record.model_dump_json()))
            if actor is not None and review_id is None:
                self.store._audit(conn, actor, 'quarantine_inbound', 'blocked', reason)
        return InboundProcessResult(
            provider_message_id=message.provider_message_id, associated=False,
            quarantined=record)

    def _upload_inbound_pdfs(self, actor, case: CaseSnapshot, message: InboundMail) -> list[str]:
        if self.document_store is None:
            return []
        uploaded = []
        current = self.store.get_case(actor, case.case_id)
        for index, attachment in enumerate(message.attachments):
            if not attachment.content or len(attachment.content) > MAX_INBOUND_DOCUMENT_BYTES:
                with self.store.write() as conn:
                    fresh = self.store._case(conn, actor, case.case_id)
                    self.runtime.create_policy_review(
                        conn, actor, fresh, 'UNSUPPORTED_ATTACHMENT',
                        'An inbound PDF attachment was empty or larger than the upload limit.',
                        [], 'inbound-attachment|' + message.provider_message_id + '|' + str(index))
                continue
            try:
                job = self.document_store.upload(
                    actor, case.case_id, requirement_id=None,
                    expected_state_version=current.state_version,
                    filename=attachment.filename, media_type='application/pdf',
                    content=attachment.content,
                    key='inbound-pdf|' + message.provider_message_id + '|' + str(index))
            except DomainError:
                with self.store.write() as conn:
                    fresh = self.store._case(conn, actor, case.case_id)
                    self.runtime.create_policy_review(
                        conn, actor, fresh, 'UNSUPPORTED_ATTACHMENT',
                        'An inbound PDF attachment could not be stored through the document API.',
                        [], 'inbound-attachment|' + message.provider_message_id + '|' + str(index))
                continue
            uploaded.append(job.document_id)
        return uploaded

    def _ingest_resolved_reply(self, actor, case: CaseSnapshot, message: InboundMail,
                               resolution: CaseReferenceResolution, document_ids: list[str]):
        current = self.store.get_case(actor, case.case_id)
        body = message.body if len(message.body) <= 20_000 else message.body[:20_000]
        request = IngestReplyRequest.model_validate({
            'expected_state_version': current.state_version,
            'sender_email': message.sender_email,
            'received_at': message.received_at.isoformat(),
            'body': body,
            'provider_message_id': message.provider_message_id,
        })
        ingested = self.ingest_reply(
            actor, resolution.case_id, request, 'inbound|' + message.provider_message_id,
            conversation_ref=message.in_reply_to, attachment_document_ids=document_ids)
        if not ingested.associated:
            return InboundProcessResult(
                provider_message_id=message.provider_message_id, associated=False,
                quarantined=InboundQuarantineRecord(
                    quarantine_id=ingested.quarantined.reply_id,
                    sender_email=message.sender_email or 'unknown',
                    received_at=message.received_at, reason_code='SENDER_NOT_APPROVED',
                    provider_message_id=message.provider_message_id,
                    review_task_id=ingested.quarantined.review_task_id))
        return InboundProcessResult(
            provider_message_id=message.provider_message_id, associated=True,
            case_id=resolution.case_id, reply=ingested.reply, document_ids=document_ids)

    def _attach_documents_to_reply(self, actor, case_id: str, reply: ReplyRecord,
                                   document_ids: list[str]) -> ReplyRecord:
        if not document_ids:
            return reply
        updated_reply = reply.model_copy(update={
            'attachment_document_ids': list(document_ids),
        })
        with self.store.write() as conn:
            self.store._case(conn, actor, case_id)
            updated = conn.execute(update(replies).where(
                replies.c.reply_id == reply.reply_id,
                replies.c.case_id == case_id,
            ).values(record=updated_reply.model_dump_json()))
            if updated.rowcount != 1:
                raise DomainError('NOT_FOUND', 'Reply not found.', 404)
        return updated_reply

    def process_inbound(self, message: InboundMail, actor=None) -> InboundProcessResult:
        seen = self._seen_result(message.provider_message_id)
        if seen is not None:
            return seen
        resolution, public_reference = self._resolve_inbound(message)
        if not resolution.matched:
            result = self._quarantine_inbound(message, resolution, public_reference, actor)
            self._store_seen(message.provider_message_id, result)
            return result
        with self.store.engine.connect() as conn:
            case = self._load_case_unscoped(conn, resolution.case_id)
        if case is None:
            result = self._quarantine_inbound(
                message, CaseReferenceResolution(matched=False, reason_code='REFERENCE_NOT_FOUND'),
                public_reference, actor)
            self._store_seen(message.provider_message_id, result)
            return result
        case_actor = self._actor_for_case(case, actor)
        result = self._ingest_resolved_reply(case_actor, case, message, resolution, [])
        if result.associated:
            current = self.store.get_case(case_actor, case.case_id)
            document_ids = self._upload_inbound_pdfs(case_actor, current, message)
            reply = self._attach_documents_to_reply(
                case_actor, case.case_id, result.reply, document_ids)
            result = result.model_copy(update={
                'reply': reply,
                'document_ids': document_ids,
            })
        self._store_seen(message.provider_message_id, result)
        return result

    def poll_inbound(self, actor=None, key: str | None = None) -> InboundPollResult:
        self._require_mail()
        digest = payload_digest({'action': 'poll_inbound'})
        operation = 'poll_inbound'
        if actor is not None and key:
            if not actor.can_manage:
                raise forbidden()
            with self.store.engine.connect() as conn:
                replay = self._replay_json(conn, actor, operation, key, digest)
                if replay:
                    return InboundPollResult.model_validate_json(replay)
        if not getattr(self.mail, 'can_receive', False):
            result = InboundPollResult(items=[])
        else:
            messages = self.mail.fetch_unseen()
            items = [self.process_inbound(message, actor) for message in messages]
            processed = [message.provider_message_id for message in messages]
            if processed:
                self.mail.acknowledge(processed)
            result = InboundPollResult(items=items)
        if actor is not None and key:
            with self.store.write() as conn:
                self._remember(conn, actor, operation, key, digest, result)
        return result

    def dispatch_due_all(self) -> DispatchRemindersResult:
        self._require_mail()
        now = utcnow()
        with self.store.engine.connect() as conn:
            rows = conn.execute(select(reminders)).mappings().all()
            due_cases = []
            for row in rows:
                reminder = ReminderRecord.model_validate_json(row['record'])
                if reminder.case_id in due_cases:
                    continue
                if reminder.status == 'scheduled' and reminder.scheduled_at <= now:
                    due_cases.append(reminder.case_id)
                elif reminder.status == 'paused' and not self._follow_up_paused(
                        conn, reminder.case_id, reminder.requirement_ids):
                    due_cases.append(reminder.case_id)
        dispatched = []
        for case_id in due_cases:
            with self.store.engine.connect() as conn:
                case = self._load_case_unscoped(conn, case_id)
            if case is None:
                continue
            try:
                actor = self._owner_actor(case)
                result = self.dispatch_due_reminders(
                    actor, case_id, 'worker-dispatch|' + uuid4().hex)
                dispatched.extend(result.items)
            except DomainError:
                continue
        return DispatchRemindersResult(items=dispatched)
