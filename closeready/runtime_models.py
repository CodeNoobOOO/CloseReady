from typing import Annotated, Literal
from pydantic import Field, model_validator
from .models import ActionContent, ContractModel, MessageDraft, PositiveInt, Text, Timestamp


class AnalyseRequest(ContractModel):
    expected_state_version: PositiveInt


class ContextArgs(ContractModel):
    pass


class ProposeArgs(ContractModel):
    action: ActionContent


class Trace(ContractModel):
    step: int
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int] | None
    provider_request_id: str | None
    tool_names: list[str]
    outcomes: list[str]
    error_code: str | None
    tool_call_ids: list[str] = Field(default_factory=list)


class RunRecord(ContractModel):
    run_id: Text
    event_id: Text
    case_id: Text
    run_mode: Literal['single'] = 'single'
    start_state_version: PositiveInt
    status: Literal['queued', 'running', 'completed', 'needs_review', 'failed', 'stale']
    started_at: Timestamp | None
    finished_at: Timestamp | None
    provider: Text
    model: Text
    live: bool
    prompt_version: str = 'case-analysis-v1'
    schema_version: str = '0.5'
    error_code: str | None = None
    traces: list[Trace] = Field(default_factory=list)


class ReviewTaskRecord(ContractModel):
    review_task_id: Text
    case_id: Text
    run_id: Text
    requirement_ids: list[Text]
    reason_code: Text
    reason: Text = 'Inspect the run error and case before taking action.'
    assigned_to: Text
    status: Literal['open', 'resolved'] = 'open'
    draft: MessageDraft | None = None
    sent: Literal[False] = False
    created_at: Timestamp
    resolution: Literal['approved', 'edited_and_approved', 'rejected', 'dismissed'] | None = None
    resolved_by: Text | None = None
    resolved_at: Timestamp | None = None
    resolution_reason: Text | None = None
    approved_draft: MessageDraft | None = None

    @model_validator(mode='after')
    def consistent_resolution(self):
        resolution_fields = (self.resolution, self.resolved_by, self.resolved_at, self.resolution_reason)
        if self.status == 'open':
            if any(value is not None for value in resolution_fields) or self.approved_draft is not None:
                raise ValueError('Open review tasks cannot contain resolution data')
            return self
        if any(value is None for value in resolution_fields):
            raise ValueError('Resolved review tasks require complete resolution data')
        approved = self.resolution in ('approved', 'edited_and_approved')
        if approved != (self.approved_draft is not None):
            raise ValueError('Approved resolutions require the final approved draft')
        return self


class ReviewTaskPage(ContractModel):
    items: list[ReviewTaskRecord]
    next_cursor: str | None


class ReviewDecisionRequest(ContractModel):
    expected_state_version: PositiveInt
    review_task_id: Text
    decision: Literal['approve_draft', 'edit_and_approve', 'reject_draft', 'dismiss_error']
    reason: Annotated[Text, Field(max_length=2000)]
    edited_draft: MessageDraft | None = None

    @model_validator(mode='after')
    def draft_matches_decision(self):
        if (self.decision == 'edit_and_approve') != (self.edited_draft is not None):
            raise ValueError('Edited draft is required only for edit_and_approve')
        return self


class OutboxRecord(ContractModel):
    outbox_id: Text
    case_id: Text
    review_task_id: Text
    requirement_ids: list[Text]
    subject: Text
    body: Text
    status: Literal['pending_reviewed_delivery'] = 'pending_reviewed_delivery'
    recipient_contact_id: Text | None = None
    provider_message_id: Text | None = None
    created_by: Text
    created_at: Timestamp
    delivery_status: Literal['not_attempted', 'queued', 'sent', 'failed', 'delivery_unknown'] = 'not_attempted'


class OutboxPage(ContractModel):
    items: list[OutboxRecord]
    next_cursor: str | None
