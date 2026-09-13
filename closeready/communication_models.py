"""Student 3 communication records. Parsing is not permission to send or ingest."""
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, model_validator

from .models import ContractModel, PositiveInt, ReplyAssessment, Text, Timestamp, TimezoneName

EmailAddress = Annotated[str, StringConstraints(
    strict=True, min_length=3, max_length=254,
    pattern=r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')]
SendingWindow = Annotated[str, StringConstraints(
    strict=True, pattern=r'^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$')]


class Contact(ContractModel):
    contact_id: Text
    client_id: Text
    approved_email: EmailAddress
    active: Annotated[bool, Field(strict=True)]
    approved_by: Text


class CommunicationPolicy(ContractModel):
    policy_id: Text
    version: PositiveInt
    approved_by: Text
    approved_at: Timestamp
    initial_request_enabled: Annotated[bool, Field(strict=True)]
    min_reminder_interval_hours: PositiveInt
    max_reminders_per_requirement: PositiveInt
    commitment_grace_hours: PositiveInt
    sending_window_local: SendingWindow
    timezone: TimezoneName
    escalation_owner_user_id: Text


class IngestReplyRequest(ContractModel):
    expected_state_version: PositiveInt
    sender_email: EmailAddress
    received_at: Timestamp
    body: Annotated[Text, Field(max_length=20_000)]
    provider_message_id: Text | None = None


class DeliverOutboxRequest(ContractModel):
    contact_id: Text | None = None


class AssessReplyRequest(ContractModel):
    expected_state_version: PositiveInt


class ReplyRecord(ContractModel):
    reply_id: Text
    case_id: Text
    event_id: Text
    provider_message_id: Text | None
    conversation_ref: Text | None
    sender_contact_id: Text
    received_at: Timestamp
    body: Text
    attachment_document_ids: list[Text] = Field(default_factory=list)
    quarantined: Literal[False] = False


class QuarantinedReply(ContractModel):
    reply_id: Text
    case_id: Text
    sender_email: EmailAddress
    received_at: Timestamp
    reason_code: Text
    review_task_id: Text


class CommitmentRecord(ContractModel):
    commitment_id: Text
    case_id: Text
    requirement_id: Text
    promised_at: Timestamp
    source_reply_id: Text
    finding_id: Text
    status: Literal['active', 'superseded', 'cancelled']
    created_at: Timestamp
    policy_version: PositiveInt


class ReminderRecord(ContractModel):
    reminder_id: Text
    case_id: Text
    requirement_ids: list[Text]
    scheduled_at: Timestamp
    status: Literal['scheduled', 'queued', 'sent', 'cancelled', 'failed', 'delivery_unknown']
    dedupe_key: Text
    contact_id: Text
    policy_version: PositiveInt
    source_commitment_id: Text | None
    attempt_count: Annotated[int, Field(strict=True, ge=0)]
    subject: Text
    body: Text
    provider_message_id: Text | None = None

    @model_validator(mode='after')
    def unique_requirements(self):
        if not self.requirement_ids or len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError('Reminder requirements must be unique and nonempty')
        return self


class MailboxMessage(ContractModel):
    message_id: Text
    case_id: Text
    backend: Literal['test_sink']
    live: Literal[False] = False
    to_email: EmailAddress
    subject: Text
    body: Text
    source: Literal['outbox', 'reminder']
    source_id: Text
    sent_at: Timestamp


class ReplyPage(ContractModel):
    items: list[ReplyRecord]
    next_cursor: str | None


class CommitmentPage(ContractModel):
    items: list[CommitmentRecord]
    next_cursor: str | None


class ReminderPage(ContractModel):
    items: list[ReminderRecord]
    next_cursor: str | None


class MailboxPage(ContractModel):
    items: list[MailboxMessage]
    next_cursor: str | None


class FindingPage(ContractModel):
    items: list[ReplyAssessment]
    next_cursor: str | None


class DeliveryResult(ContractModel):
    outbox_id: Text
    delivery_status: Literal['sent', 'failed', 'delivery_unknown']
    recipient_contact_id: Text | None
    provider_message_id: Text | None
    mailbox_backend: Literal['test_sink']
    live: Literal[False] = False


class IngestReplyResult(ContractModel):
    associated: Annotated[bool, Field(strict=True)]
    reply: ReplyRecord | None = None
    quarantined: QuarantinedReply | None = None


class AssessReplyResult(ContractModel):
    finding: ReplyAssessment
    commitment: CommitmentRecord | None
    reminder: ReminderRecord | None
    review_task_id: Text | None


class DispatchRemindersResult(ContractModel):
    items: list[ReminderRecord]
