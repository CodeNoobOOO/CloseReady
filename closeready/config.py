"""Administrator-controlled access grants. Never accepted from HTTP callers."""
import hashlib
import hmac
from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator
from .models import ContractModel, PositiveInt, Text, Timestamp


class Principal(ContractModel):
    user_id: Text
    token_sha256: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]
    client_ids: frozenset[Text]
    can_manage: Annotated[bool, Field(strict=True)]


class PolicyBinding(ContractModel):
    """Approved version identity only; communication settings are a later module."""
    policy_id: Text
    version: PositiveInt
    client_ids: frozenset[Text]
    approved_by: Text
    approved_at: Timestamp


class AccessConfig(ContractModel):
    principals: Annotated[tuple[Principal, ...], Field(min_length=1)]
    policies: Annotated[tuple[PolicyBinding, ...], Field(min_length=1)]

    @model_validator(mode='after')
    def unique_configuration(self):
        for values in ([p.user_id for p in self.principals],
                       [p.token_sha256 for p in self.principals],
                       [p.policy_id for p in self.policies]):
            if len(values) != len(set(values)):
                raise ValueError('Duplicate configuration identity')
        return self

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
