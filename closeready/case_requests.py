"""Public requests omit IDs, approvals and state owned by the server."""
import re
from typing import Annotated
from pydantic import Field, model_validator
from .models import (
    CaseSnapshot, CaseTitle, CompletionRule, ContractModel, DocumentType, Period,
    PositiveInt, Requirement, RequirementScope, Text, Timestamp, TimezoneName,
)


class RequirementDefinition(ContractModel):
    document_type: DocumentType
    accounting_period: Period
    scope: RequirementScope
    completion_rule: CompletionRule
    description: Text | None = None

    @model_validator(mode='before')
    @classmethod
    def normalize_bank_account_suffix(cls, value):
        if not isinstance(value, dict) or value.get('document_type') != 'bank_statement':
            return value
        scope = value.get('scope')
        if not isinstance(scope, dict):
            return value
        supplied = scope.get('masked_account_identifier')
        if isinstance(supplied, str) and re.fullmatch(r'\d{4}', supplied.strip()):
            normalized = dict(value)
            normalized_scope = dict(scope)
            normalized_scope['masked_account_identifier'] = '****' + supplied.strip()
            normalized['scope'] = normalized_scope
            return normalized
        return value

    @model_validator(mode='after')
    def validate_configuration(self):
        if self.document_type == 'bank_statement':
            if not self.scope.account_ref:
                raise ValueError(
                    'Bank statement requirements need a case-local account reference')
            masked = self.scope.masked_account_identifier
            if not isinstance(masked, str) or not re.fullmatch(r'\*{4}\d{4}', masked):
                raise ValueError(
                    'Bank statement requirements need a masked account identifier '
                    'containing only the final four digits')
        self.to_requirement('validation-only')
        return self

    def to_requirement(self, requirement_id: str) -> Requirement:
        return Requirement.model_validate({**self.model_dump(),
            'requirement_id': requirement_id, 'status': 'missing',
            'reviewer_status': 'not_required', 'evidence_refs': []})


class CreateCaseRequest(ContractModel):
    title: CaseTitle
    client_id: Text
    accounting_period: Period
    timezone: TimezoneName
    owner_user_id: Text
    due_at: Timestamp
    policy_id: Text
    requirements: Annotated[list[RequirementDefinition], Field(min_length=1, max_length=200)]

    @model_validator(mode='after')
    def same_period(self):
        if any(r.accounting_period != self.accounting_period for r in self.requirements):
            raise ValueError('Requirement period must match the case')
        bank_refs = [
            r.scope.account_ref.casefold()
            for r in self.requirements
            if r.document_type == 'bank_statement' and r.scope.account_ref is not None
        ]
        if len(bank_refs) != len(set(bank_refs)):
            raise ValueError('Bank account references must be unique within the case')
        return self


class ChangeDeadlineRequest(ContractModel):
    expected_state_version: PositiveInt
    due_at: Timestamp
    reason: Annotated[Text, Field(max_length=2000)]


class ConfirmReadinessRequest(ContractModel):
    expected_state_version: PositiveInt
    reason: Annotated[Text, Field(max_length=2000)]


class ReopenCaseRequest(ContractModel):
    expected_state_version: PositiveInt
    reason: Annotated[Text, Field(max_length=2000)]


class CasePage(ContractModel):
    items: list[CaseSnapshot]
    next_cursor: str | None


class AuditEvent(ContractModel):
    audit_id: PositiveInt
    case_id: Text | None
    event_id: Text | None
    run_id: Text | None
    actor_user_id: Text
    action: Text
    outcome: Text
    reason: Text
    occurred_at: Timestamp
    old_state_version: PositiveInt | None
    new_state_version: PositiveInt | None
    policy_id: Text | None
    policy_version: PositiveInt | None
    details: dict[str, str | list[str] | None] = Field(default_factory=dict)


class AuditPage(ContractModel):
    items: list[AuditEvent]
    next_cursor: str | None
