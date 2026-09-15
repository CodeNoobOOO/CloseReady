"""Customer-visible references for associating communication with internal cases."""
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .models import ContractModel, Text, Timestamp


PublicReference = Annotated[str, StringConstraints(
    strict=True,
    pattern=r'^CR-\d{4}-[A-Z2-9]{8}$',
)]


class CaseCommunicationReference(ContractModel):
    public_reference: PublicReference
    case_id: Text
    client_id: Text
    status: Literal['active', 'revoked']
    created_at: Timestamp
    created_by: Text


class CaseReferenceResolution(ContractModel):
    matched: Annotated[bool, Field(strict=True)]
    case_id: Text | None = None
    client_id: Text | None = None
    contact_id: Text | None = None
    reason_code: Literal[
        'REFERENCE_NOT_FOUND',
        'REFERENCE_REVOKED',
        'SENDER_NOT_APPROVED',
    ] | None = None

    @model_validator(mode='after')
    def consistent_result(self):
        identifiers = (self.case_id, self.client_id, self.contact_id)
        if self.matched:
            if any(value is None for value in identifiers) or self.reason_code is not None:
                raise ValueError('Matched references require case, client and contact identifiers')
        elif any(value is not None for value in identifiers) or self.reason_code is None:
            raise ValueError('Unmatched references disclose no internal identifiers')
        return self
