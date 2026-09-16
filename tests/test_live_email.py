"""Live SMTP/IMAP mail adapter tests. Provider doubles never contact a network."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from closeready.api import create_app
from closeready.case_requests import CreateCaseRequest
from closeready.communication_models import DeliverOutboxRequest
from closeready.communication_store import CommunicationStore
from closeready.document_store import DocumentStore
from closeready.mail import (
    FailingMailBackend, MemoryMailBackend, SandboxMailSink, SmtpMailBackend,
    TimeoutMailBackend, mail_backend,
)
from closeready.mail_messages import (
    InboundAttachment, InboundMail, apply_customer_reference, build_outbound_message,
    extract_public_reference, parse_rfc822,
)
from closeready.runtime import AgentRuntime
from closeready.runtime_models import ReviewDecisionRequest
from closeready.runtime_store import RuntimeStore
from closeready.store import DomainError, Store
from closeready.content_guard import validate_customer_visible_draft
from closeready.models import MessageDraft
from test_case_api import TOKEN, case_request
from test_communication import communication_config
from test_runtime import ScriptedProvider, final, tool


def inbound(**changes):
    data = {
        'provider_message_id': 'inbound-msg-1',
        'sender_email': 'client@example.test',
        'subject': 'Documents',
        'body': 'I will send the July statement tomorrow.',
        'received_at': datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc),
        'in_reply_to': None,
        'references': (),
        'attachments': (),
    }
    data.update(changes)
    return InboundMail(**data)


class MailFactoryTests(unittest.TestCase):
    def test_disabled_and_test_sink_remain_available(self):
        self.assertIsNone(mail_backend(None))
        self.assertIsNone(mail_backend('disabled'))
        sink = mail_backend('test_sink')
        self.assertIsInstance(sink, SandboxMailSink)
        self.assertFalse(sink.live)

    def test_unknown_backend_fails_startup(self):
        with self.assertRaises(RuntimeError):
            mail_backend('gmail')

    def test_smtp_backend_requires_nonsecret_settings(self):
        with self.assertRaises(RuntimeError) as raised:
            mail_backend('smtp', environ={'CLOSEREADY_SMTP_HOST': 'smtp.example.test'})
        self.assertIn('CLOSEREADY_SMTP_FROM', str(raised.exception))
        self.assertNotIn('password', str(raised.exception).lower())

        backend = mail_backend('smtp', environ={
            'CLOSEREADY_SMTP_FROM': 'firm@example.test',
            'CLOSEREADY_SMTP_HOST': 'smtp.example.test',
            'CLOSEREADY_SMTP_USERNAME': 'firm@example.test',
            'CLOSEREADY_SMTP_PASSWORD': 'not-a-real-password',
            'CLOSEREADY_IMAP_HOST': 'imap.example.test',
        })
        self.assertTrue(backend.live)
        self.assertEqual(backend.backend_name, 'smtp')
        self.assertTrue(backend.can_receive)


class MailMessageTests(unittest.TestCase):
    def test_reference_is_attached_deterministically_and_extracted(self):
        subject, body = apply_customer_reference(
            'July statement', 'Please upload the complete July statement.',
            'CR-2607-SA48HA7B')
        self.assertTrue(subject.startswith('[CR-2607-SA48HA7B]'))
        self.assertIn('CR-2607-SA48HA7B', body)
        self.assertEqual(extract_public_reference(subject, body), 'CR-2607-SA48HA7B')
        again_subject, again_body = apply_customer_reference(subject, body, 'CR-2607-SA48HA7B')
        self.assertEqual(again_subject, subject)
        self.assertEqual(again_body, body)
        validate_customer_visible_draft(MessageDraft(
            subject=subject, body=body, requirement_ids=['req_metadata_only']))

    def test_ambiguous_or_missing_references_are_not_guessed(self):
        self.assertIsNone(extract_public_reference('Hello', 'No token here'))
        self.assertIsNone(extract_public_reference(
            '[CR-2607-AAAAAAAA] one', 'Also CR-2607-BBBBBBBB'))

    def test_rfc822_round_trip_extracts_pdf_and_thread_headers(self):
        message = build_outbound_message(
            from_address='firm@example.test', to_email='client@example.test',
            subject='[CR-2607-SA48HA7B] July statement',
            body='Please reply with the statement.',
            message_id='closeready.abc@example.test',
            public_reference='CR-2607-SA48HA7B')
        raw_outbound = message.as_bytes()
        parsed_out = parse_rfc822(raw_outbound)
        self.assertEqual(parsed_out.subject, '[CR-2607-SA48HA7B] July statement')
        self.assertNotIn('case_', parsed_out.body)

        reply = EmailMessage()
        reply['From'] = 'Client <client@example.test>'
        reply['To'] = 'firm@example.test'
        reply['Subject'] = 'Re: [CR-2607-SA48HA7B] July statement'
        reply['Message-ID'] = '<reply-1@example.test>'
        reply['In-Reply-To'] = '<closeready.abc@example.test>'
        reply.set_content('Here is the statement.')
        reply.add_attachment(
            b'%PDF-1.4 synthetic', maintype='application', subtype='pdf',
            filename='july-statement.pdf')
        parsed = parse_rfc822(reply.as_bytes())
        self.assertEqual(parsed.sender_email, 'client@example.test')
        self.assertEqual(parsed.in_reply_to, 'closeready.abc@example.test')
        self.assertEqual(len(parsed.attachments), 1)
        self.assertEqual(parsed.attachments[0].filename, 'july-statement.pdf')
        self.assertTrue(parsed.attachments[0].content.startswith(b'%PDF'))


class SmtpTransportTests(unittest.TestCase):
    def test_smtp_send_uses_injected_client_and_omits_internal_ids(self):
        sent = []

        class FakeSMTP:
            def starttls(self, context=None):
                return None

            def login(self, username, password):
                self.username = username

            def send_message(self, message):
                sent.append(message)

            def quit(self):
                return None

        backend = SmtpMailBackend(
            from_address='firm@example.test', smtp_host='smtp.example.test',
            smtp_port=587, username='firm@example.test', password='secret',
            smtp_factory=FakeSMTP)
        provider_id = backend.send(
            to_email='client@example.test',
            subject='[CR-2607-SA48HA7B] July statement',
            body='Please upload the statement for CR-2607-SA48HA7B.',
            metadata={'public_reference': 'CR-2607-SA48HA7B',
                      'message_id': 'closeready.demo@example.test'})
        self.assertEqual(provider_id, 'closeready.demo@example.test')
        self.assertEqual(len(sent), 1)
        rendered = sent[0].as_string()
        self.assertIn('CR-2607-SA48HA7B', rendered)
        self.assertNotIn('case_', rendered)
        self.assertNotIn('req_', rendered)
        self.assertEqual(sent[0]['X-CloseReady-Reference'], 'CR-2607-SA48HA7B')


class LiveCommunicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'live-mail.db').as_posix()
        self.access = communication_config()
        self.store = Store(self.url, self.access)
        self.actor = self.access.principals[0]
        self.case = self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(case_request()), 'live-case')
        self.runtime = RuntimeStore(self.store)
        self.mail = MemoryMailBackend()
        self.db = CommunicationStore(self.store, self.runtime, self.mail)
        self.db.document_store = DocumentStore(
            self.store, on_requirements_resolved=self.db.cancel_scheduled_for_resolved)
        self.reference = self.store.case_communication_reference(
            self.actor, self.case.case_id).public_reference

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def requirement_id(self):
        return self.store.get_case(self.actor, self.case.case_id).requirements[0].requirement_id

    def approve_draft(self):
        rid = self.requirement_id()
        draft = tool('propose_action', {'action': {'action_type': 'request_documents',
            'requirement_ids': [rid], 'finding_ids': [], 'reason': 'The statement is missing.',
            'payload': {'subject': 'July statement',
                        'body': 'Please upload the complete July statement.',
                        'requirement_ids': [rid]}}}, 'draft')
        AgentRuntime(self.runtime, ScriptedProvider(
            [tool('get_case_context', {}), draft, final()])).analyse(
                self.actor, self.case.case_id, 1, 'draft-run')
        task = self.runtime.review_tasks(self.actor, self.case.case_id).items[0]
        self.runtime.decide_review(self.actor, self.case.case_id, ReviewDecisionRequest.model_validate({
            'expected_state_version': 2, 'review_task_id': task.review_task_id,
            'decision': 'approve_draft', 'reason': 'Reviewed by the assigned manager.'}), 'approve-1')
        return self.runtime.outbox_records(self.actor, self.case.case_id).items[0]

    def test_live_backend_sends_approved_outbox_with_case_reference(self):
        queued = self.approve_draft()
        result = self.db.deliver_outbox(self.actor, self.case.case_id, queued.outbox_id,
            DeliverOutboxRequest(), 'live-deliver')
        self.assertEqual(result.delivery_status, 'sent')
        self.assertTrue(result.live)
        self.assertEqual(result.mailbox_backend, 'smtp')
        self.assertEqual(len(self.mail.sent), 1)
        sent = self.mail.sent[0]
        self.assertEqual(sent['to_email'], 'client@example.test')
        self.assertIn(self.reference, sent['subject'])
        self.assertIn(self.reference, sent['body'])
        self.assertNotIn(self.case.case_id, sent['subject'] + sent['body'])
        self.assertNotIn(self.requirement_id(), sent['subject'] + sent['body'])
        stored = self.runtime.outbox_records(self.actor, self.case.case_id).items[0]
        self.assertNotIn(self.reference, stored.subject)
        mailbox = self.db.list_mailbox(self.actor, self.case.case_id).items[0]
        self.assertTrue(mailbox.live)
        self.assertIn(self.reference, mailbox.subject)

    def test_unapproved_draft_is_not_sent(self):
        rid = self.requirement_id()
        draft = tool('propose_action', {'action': {'action_type': 'request_documents',
            'requirement_ids': [rid], 'finding_ids': [], 'reason': 'The statement is missing.',
            'payload': {'subject': 'July statement',
                        'body': 'Please upload the complete July statement.',
                        'requirement_ids': [rid]}}}, 'draft')
        AgentRuntime(self.runtime, ScriptedProvider(
            [tool('get_case_context', {}), draft, final()])).analyse(
                self.actor, self.case.case_id, 1, 'unapproved-run')
        with self.assertRaises(DomainError):
            self.db.deliver_outbox(self.actor, self.case.case_id, 'outbox_missing',
                DeliverOutboxRequest(), 'no-outbox')
        self.assertEqual(self.mail.sent, [])

    def test_timeout_and_failure_are_recorded_without_retry(self):
        queued = self.approve_draft()
        timed = CommunicationStore(self.store, self.runtime, TimeoutMailBackend())
        result = timed.deliver_outbox(self.actor, self.case.case_id, queued.outbox_id,
            DeliverOutboxRequest(), 'timeout-live')
        self.assertEqual(result.delivery_status, 'delivery_unknown')
        self.assertTrue(result.live)
        with self.assertRaises(DomainError) as raised:
            timed.deliver_outbox(self.actor, self.case.case_id, queued.outbox_id,
                DeliverOutboxRequest(), 'timeout-live-2')
        self.assertEqual(raised.exception.code, 'ALREADY_DELIVERED')

    def test_failed_send_is_recorded(self):
        queued = self.approve_draft()
        failing = CommunicationStore(self.store, self.runtime, FailingMailBackend())
        result = failing.deliver_outbox(self.actor, self.case.case_id, queued.outbox_id,
            DeliverOutboxRequest(), 'fail-live')
        self.assertEqual(result.delivery_status, 'failed')
        self.assertIsNone(result.provider_message_id)

    def test_inbound_uses_visible_reference_not_sender_alone(self):
        associated = self.db.process_inbound(inbound(
            subject=f'Re: [{self.reference}] July statement',
            body=f'I will send it. {self.reference}'))
        self.assertTrue(associated.associated)
        self.assertEqual(associated.case_id, self.case.case_id)
        self.assertEqual(associated.reply.sender_contact_id, 'contact_demo')
        self.assertEqual(len(self.db.list_replies(self.actor, self.case.case_id).items), 1)

        unknown_case = self.db.process_inbound(inbound(
            provider_message_id='inbound-sender-only',
            subject='July statement', body='I will send it tomorrow.'))
        self.assertFalse(unknown_case.associated)
        self.assertIsNone(unknown_case.case_id)
        self.assertEqual(unknown_case.quarantined.reason_code, 'REFERENCE_NOT_FOUND')

    def test_unknown_sender_is_quarantined_without_exposing_case_id(self):
        result = self.db.process_inbound(inbound(
            provider_message_id='inbound-stranger',
            sender_email='stranger@example.test',
            subject=f'[{self.reference}] July',
            body=f'Reply for {self.reference}'))
        self.assertFalse(result.associated)
        self.assertIsNone(result.case_id)
        self.assertEqual(result.quarantined.reason_code, 'SENDER_NOT_APPROVED')
        self.assertEqual(self.db.list_replies(self.actor, self.case.case_id).items, [])
        quarantined = self.db.list_quarantine(self.actor).items
        self.assertEqual(quarantined[0].reason_code, 'SENDER_NOT_APPROVED')
        self.assertNotIn('case_id', quarantined[0].model_dump())

    def test_thread_mapping_is_preferred_over_a_second_reference(self):
        queued = self.approve_draft()
        delivered = self.db.deliver_outbox(self.actor, self.case.case_id, queued.outbox_id,
            DeliverOutboxRequest(), 'thread-deliver')
        other = self.store.create_case(
            self.actor, CreateCaseRequest.model_validate(case_request()), 'live-case-2')
        other_ref = self.store.case_communication_reference(
            self.actor, other.case_id).public_reference
        result = self.db.process_inbound(inbound(
            provider_message_id='inbound-thread',
            subject=f'Re: [{other_ref}] later period',
            body=f'This body mentions {other_ref}',
            in_reply_to=delivered.provider_message_id))
        self.assertTrue(result.associated)
        self.assertEqual(result.case_id, self.case.case_id)
        self.assertNotEqual(result.case_id, other.case_id)

    def test_pdf_attachment_is_submitted_through_document_store(self):
        result = self.db.process_inbound(inbound(
            provider_message_id='inbound-pdf',
            subject=f'[{self.reference}] statement',
            body=f'Attached. {self.reference}',
            attachments=(InboundAttachment(
                filename='july-statement.pdf', content_type='application/pdf',
                content=b'%PDF-1.4 synthetic statement'),)))
        self.assertTrue(result.associated)
        self.assertEqual(len(result.document_ids), 1)
        self.assertEqual(result.reply.attachment_document_ids, result.document_ids)
        document = self.db.document_store.get_document(
            self.actor, self.case.case_id, result.document_ids[0])
        self.assertEqual(document.original_filename, 'july-statement.pdf')
        self.assertEqual(document.media_type, 'application/pdf')

    def test_duplicate_inbound_message_is_idempotent(self):
        message = inbound(subject=f'[{self.reference}] hi', body=self.reference)
        first = self.db.process_inbound(message)
        second = self.db.process_inbound(message)
        self.assertEqual(first.reply.reply_id, second.reply.reply_id)
        self.assertEqual(len(self.db.list_replies(self.actor, self.case.case_id).items), 1)

    def test_due_reminder_dispatch_records_live_statuses(self):
        from closeready.communication_models import IngestReplyRequest
        from closeready.models import ReplyAssessmentContent
        ingested = self.db.ingest_reply(self.actor, self.case.case_id, IngestReplyRequest.model_validate({
            'expected_state_version': self.store.get_case(self.actor, self.case.case_id).state_version,
            'sender_email': 'client@example.test',
            'received_at': '2026-09-11T09:00:00+08:00',
            'body': 'I will send the July statement on 11 September 2026 by 5 pm.',
        }), 'live-reply')
        version = self.store.get_case(self.actor, self.case.case_id).state_version
        applied = self.db.apply_reply_assessment(
            self.actor, self.case.case_id, ingested.reply.reply_id,
            ReplyAssessmentContent.model_validate({
                'intent': 'submission_commitment',
                'requirement_ids': [self.requirement_id()],
                'promised_at': '2026-09-11T17:00:00+08:00',
                'needs_clarification': False,
                'uncertainty_reasons': [],
                'evidence_excerpt': 'I will send the July statement.',
            }), version, 'live-assess')
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        from closeready.communication_store import reminders
        import json
        with self.store.write() as conn:
            conn.execute(update(reminders).where(
                reminders.c.reminder_id == applied.reminder.reminder_id).values(
                    record=json.dumps({**applied.reminder.model_dump(mode='json'),
                                       'scheduled_at': past})))
        dispatched = self.db.dispatch_due_reminders(self.actor, self.case.case_id, 'live-dispatch')
        self.assertEqual(dispatched.items[0].status, 'sent')
        self.assertIn(self.reference, self.mail.sent[-1]['subject'])
        self.assertNotIn(self.requirement_id(), self.mail.sent[-1]['body'])


class LiveMailHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.url = 'sqlite:///' + (Path(self.tmp.name) / 'live-http.db').as_posix()
        self.mail = MemoryMailBackend()
        self.client = TestClient(create_app(
            self.url, communication_config(), mail=self.mail))
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'http-live-case'}
        created = self.client.post('/api/v1/cases', json=case_request(), headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.case = created.json()
        self.reference = self.client.get(
            '/api/v1/cases/' + self.case['case_id'] + '/communication-reference',
            headers=self.headers).json()['public_reference']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_poll_ingests_reference_matched_reply(self):
        self.mail.inbox.append(inbound(
            provider_message_id='poll-1',
            subject=f'[{self.reference}] reply',
            body=f'Sending shortly. {self.reference}'))
        polled = self.client.post('/api/v1/inbound-mail/poll',
            headers=dict(self.headers, **{'Idempotency-Key': 'poll-1'}))
        self.assertEqual(polled.status_code, 200, polled.text)
        item = polled.json()['items'][0]
        self.assertTrue(item['associated'])
        self.assertEqual(item['case_id'], self.case['case_id'])
        replay = self.client.post('/api/v1/inbound-mail/poll',
            headers=dict(self.headers, **{'Idempotency-Key': 'poll-1'}))
        self.assertEqual(replay.json()['items'][0]['reply']['reply_id'], item['reply']['reply_id'])

    def test_poll_quarantine_list_hides_unmatched_case_ids(self):
        self.mail.inbox.append(inbound(
            provider_message_id='poll-unknown',
            sender_email='stranger@example.test',
            subject='Hello', body='No reference here'))
        polled = self.client.post('/api/v1/inbound-mail/poll',
            headers=dict(self.headers, **{'Idempotency-Key': 'poll-unknown'}))
        self.assertFalse(polled.json()['items'][0]['associated'])
        self.assertIsNone(polled.json()['items'][0]['case_id'])
        listed = self.client.get('/api/v1/inbound-mail/quarantine', headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()['items'][0]['reason_code'], 'REFERENCE_NOT_FOUND')
        self.assertNotIn('case_id', listed.json()['items'][0])


if __name__ == '__main__':
    unittest.main()
