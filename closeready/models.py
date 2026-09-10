"""Validated boundary records, not persistence models or execution authority.

Never treat successful parsing as permission to apply a finding or send mail.
Revalidate serialized data at every trust boundary; model_copy/model_construct
are not validation APIs.
"""
from datetime import date, datetime, timezone
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, RootModel,
    StringConstraints, model_validator,
)

Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r'\S')]
Period = Annotated[str, StringConstraints(strict=True, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


def parse_date(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise ValueError('Expected a calendar date in YYYY-MM-DD format')


def parse_timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('Expected an ISO 8601 timestamp') from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timestamp must include a timezone')
    return value.astimezone(timezone.utc)


def valid_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Unknown IANA timezone') from None
    return value


CalendarDate = Annotated[date, BeforeValidator(parse_date)]
Timestamp = Annotated[datetime, BeforeValidator(parse_timestamp)]
TimezoneName = Annotated[Text, AfterValidator(valid_timezone)]
DocumentType = Literal['bank_statement', 'invoice', 'receipt', 'other_supporting_document']
RequirementStatus = Literal['missing', 'received', 'needs_clarification', 'awaiting_review', 'accepted', 'waived']
ReviewerStatus = Literal['not_required', 'pending', 'approved', 'waived']
ReadinessStatus = Literal['collecting', 'ready_for_confirmation', 'ready']


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class EvidenceRef(ContractModel):
    document_id: Text
    page: PositiveInt | None
    excerpt: Text


class RequirementScope(ContractModel):
    entity_id: Text
    account_ref: Text | None
    coverage_start: CalendarDate | None
    coverage_end: CalendarDate | None

    @model_validator(mode='after')
    def ordered_dates(self):
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError('Coverage dates must both be supplied or both be null')
        if self.coverage_start and self.coverage_end < self.coverage_start:
            raise ValueError('Coverage end precedes start')
        return self


class CompletionRule(ContractModel):
    kind: Literal['coverage', 'explicit_items']
    expected_item_refs: list[Text]
    allow_multiple_documents: Annotated[bool, Field(strict=True)]

    @model_validator(mode='after')
    def meaningful_items(self):
        if self.kind == 'explicit_items' and not self.expected_item_refs:
            raise ValueError('Explicit items require configured item references')
        if self.kind == 'coverage' and self.expected_item_refs:
            raise ValueError('Coverage rules do not use expected item references')
        if len(set(self.expected_item_refs)) != len(self.expected_item_refs):
            raise ValueError('Expected item references must be unique')
        return self


class Requirement(ContractModel):
    requirement_id: Text
    document_type: DocumentType
    accounting_period: Period
    status: RequirementStatus
    evidence_refs: list[EvidenceRef]
    scope: RequirementScope
    completion_rule: CompletionRule
    reviewer_status: ReviewerStatus
    description: Text | None = None

    @model_validator(mode='after')
    def configured_requirement(self):
        if self.completion_rule.kind == 'coverage' and self.scope.coverage_start is None:
            raise ValueError('Coverage rules require an inclusive date interval')
        if self.document_type == 'other_supporting_document' and self.description is None:
            raise ValueError('Other supporting documents require a description')
        if self.status == 'waived' and self.reviewer_status != 'waived':
            raise ValueError('Waived requirements require a recorded reviewer waiver')
        return self


class CaseSnapshot(ContractModel):
    case_id: Text
    client_id: Text
    accounting_period: Period
    timezone: TimezoneName
    state_version: PositiveInt
    readiness_status: ReadinessStatus
    owner_user_id: Text
    due_at: Timestamp
    policy_id: Text
    policy_version: PositiveInt
    requirements: list[Requirement]

    @model_validator(mode='after')
    def consistent_snapshot(self):
        ids = [r.requirement_id for r in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError('Requirement IDs must be unique within a case')
        if any(r.accounting_period != self.accounting_period for r in self.requirements):
            raise ValueError('Requirement period must match its case')
        if self.readiness_status != 'collecting':
            if not self.requirements or any(
                r.status not in ('accepted', 'waived') or r.reviewer_status == 'pending'
                for r in self.requirements
            ):
                raise ValueError('Unresolved or empty checklist cannot be ready')
        return self


class FindingPayload(ContractModel):
    finding_id: Text


class CommitmentPayload(FindingPayload):
    promised_at: Timestamp


class MessageDraft(ContractModel):
    subject: Text
    body: Text
    requirement_ids: Annotated[list[Text], Field(min_length=1)]


class ReminderPayload(ContractModel):
    scheduled_at: Timestamp
    draft: MessageDraft


class ReviewPayload(ContractModel):
    issue: Text
    evidence_refs: list[EvidenceRef]


class EmptyPayload(ContractModel):
    pass


class ActionFields(ContractModel):
    requirement_ids: list[Text]
    finding_ids: list[Text]
    reason: Text

    @model_validator(mode='after')
    def consistent_references(self):
        for refs in (self.requirement_ids, self.finding_ids):
            if len(refs) != len(set(refs)):
                raise ValueError('Action references must be unique')
        payload = self.payload
        if isinstance(payload, FindingPayload) and payload.finding_id not in self.finding_ids:
            raise ValueError('Payload finding must occur in finding_ids')
        draft = payload.draft if isinstance(payload, ReminderPayload) else payload
        if isinstance(draft, MessageDraft) and (
            len(draft.requirement_ids) != len(set(draft.requirement_ids))
            or set(draft.requirement_ids) != set(self.requirement_ids)
        ):
            raise ValueError('Draft requirements must match action requirements')
        return self


class ApplyDocumentFinding(ActionFields):
    action_type: Literal['apply_document_finding']
    payload: FindingPayload


class RecordCommitment(ActionFields):
    action_type: Literal['record_commitment']
    payload: CommitmentPayload


class RequestDocuments(ActionFields):
    action_type: Literal['request_documents']
    payload: MessageDraft


class RequestClarification(ActionFields):
    action_type: Literal['request_clarification']
    payload: MessageDraft


class ScheduleReminder(ActionFields):
    action_type: Literal['schedule_reminder']
    payload: ReminderPayload


class CreateReviewTask(ActionFields):
    action_type: Literal['create_review_task']
    payload: ReviewPayload


class NoAction(ActionFields):
    action_type: Literal['no_action']
    payload: EmptyPayload


ContentUnion = Annotated[Union[ApplyDocumentFinding, RecordCommitment, RequestDocuments,
    RequestClarification, ScheduleReminder, CreateReviewTask, NoAction], Field(discriminator='action_type')]


class ActionContent(RootModel[ContentUnion]):
    """Model-facing action without server-assigned provenance; use .root for fields."""
    @property
    def action_type(self):
        return self.root.action_type


class ProposalEnvelope(ContractModel):
    proposal_id: Text
    run_id: Text
    case_id: Text
    expected_state_version: PositiveInt


class ApplyDocumentProposal(ApplyDocumentFinding, ProposalEnvelope):
    pass


class CommitmentProposal(RecordCommitment, ProposalEnvelope):
    pass


class RequestDocumentsProposal(RequestDocuments, ProposalEnvelope):
    pass


class ClarificationProposal(RequestClarification, ProposalEnvelope):
    pass


class ReminderProposal(ScheduleReminder, ProposalEnvelope):
    pass


class ReviewProposal(CreateReviewTask, ProposalEnvelope):
    pass


class NoActionProposal(NoAction, ProposalEnvelope):
    pass


ProposalUnion = Annotated[Union[ApplyDocumentProposal, CommitmentProposal, RequestDocumentsProposal,
    ClarificationProposal, ReminderProposal, ReviewProposal, NoActionProposal], Field(discriminator='action_type')]


class ActionProposal(RootModel[ProposalUnion]):
    """Application envelope. Parsing does not authenticate provenance or authorize execution."""
    @property
    def action_type(self):
        return self.root.action_type
