from typing import Literal
from pydantic import Field
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
    status: Literal['open'] = 'open'
    draft: MessageDraft | None = None
    sent: Literal[False] = False
    created_at: Timestamp


class ReviewTaskPage(ContractModel):
    items: list[ReviewTaskRecord]
    next_cursor: str | None
