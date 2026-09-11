"""Deterministic controls for text that may become customer-visible."""
import re
import unicodedata

from .models import MessageDraft
from .store import DomainError


INTERNAL_ID = re.compile(r'(?i)\b(?:case|req|run|review|event|proposal)_[a-z0-9]+\b')
URL_SCHEME = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*):/{2}')
DANGEROUS_SCHEME = re.compile(r'(?i)\b(?:javascript|data|file|vbscript):')


def validate_customer_visible_draft(draft: MessageDraft) -> None:
    """Reject unsafe visible text without altering its business meaning."""
    if len(draft.subject) > 200 or len(draft.body) > 10_000:
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft exceeds the allowed length.', 422)
    visible = draft.subject + '\n' + draft.body
    if INTERNAL_ID.search(visible):
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft contains an internal identifier.', 422)
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\r\t' for char in visible):
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft contains a control character.', 422)
    if DANGEROUS_SCHEME.search(visible) or any(
        match.group(1).lower() != 'https' for match in URL_SCHEME.finditer(visible)
    ):
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft contains a disallowed URI scheme.', 422)
