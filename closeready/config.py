"""Administrator-controlled access grants. Never accepted from HTTP callers."""
import hashlib
import hmac
from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator
from .communication_models import CommunicationPolicy, Contact
from .models import ContractModel, PositiveInt, Text, Timestamp


class Principal(ContractModel):
    user_id: Text
    token_sha256: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]
    client_ids: frozenset[Text]
    can_manage: Annotated[bool, Field(strict=True)]


class PolicyBinding(ContractModel):
    """Approved version identity. Reminder/send settings live on CommunicationPolicy."""
    policy_id: Text
    version: PositiveInt
    client_ids: frozenset[Text]
    approved_by: Text
    approved_at: Timestamp


class AccessConfig(ContractModel):
    principals: Annotated[tuple[Principal, ...], Field(min_length=1)]
    policies: Annotated[tuple[PolicyBinding, ...], Field(min_length=1)]
    contacts: tuple[Contact, ...] = ()
    communication_policies: tuple[CommunicationPolicy, ...] = ()

    @model_validator(mode='after')
    def unique_configuration(self):
        for values in ([p.user_id for p in self.principals],
                       [p.token_sha256 for p in self.principals],
                       [p.policy_id for p in self.policies],
                       [c.contact_id for c in self.contacts]):
            if len(values) != len(set(values)):
                raise ValueError('Duplicate configuration identity')
        policy_versions = [(p.policy_id, p.version) for p in self.communication_policies]
        if len(policy_versions) != len(set(policy_versions)):
            raise ValueError('Duplicate communication policy version')
        emails = [(c.client_id, c.approved_email.lower()) for c in self.contacts]
        if len(emails) != len(set(emails)):
            raise ValueError('Duplicate approved email for a client')
        return self

    def contacts_for(self, client_id: str, *, active_only=True) -> tuple[Contact, ...]:
        return tuple(c for c in self.contacts
                     if c.client_id == client_id and (c.active or not active_only))

    def communication_policy(self, policy_id: str, version: int) -> CommunicationPolicy | None:
        return next((p for p in self.communication_policies
                     if p.policy_id == policy_id and p.version == version), None)

    def authenticate(self, token: str) -> Principal | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for principal in self.principals:
            if hmac.compare_digest(principal.token_sha256, digest):
                return principal
        return None


def load_access_config(path: str) -> AccessConfig:
    try:
        return AccessConfig.model_validate_json(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise RuntimeError('Access configuration is missing or invalid; see docs/backend.md.') from None
