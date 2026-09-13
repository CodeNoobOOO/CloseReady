"""Mail backends. test_sink is a labeled sandbox, not live transport evidence."""
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .store import DomainError


@runtime_checkable
class MailBackend(Protocol):
    backend_name: str
    live: bool

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        """Return a backend message id. Never choose the recipient."""


class SandboxMailSink:
    """In-process sandbox. Messages are persisted by CommunicationStore, not SMTP."""
    backend_name = 'test_sink'
    live = False

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        if not to_email or not subject or not body:
            raise DomainError('MAIL_REJECTED', 'Sandbox send requires a recipient, subject and body.', 422)
        return 'sink_' + uuid4().hex


class TimeoutMailSink(SandboxMailSink):
    """Test double for unknown delivery. Do not use outside tests."""

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        raise TimeoutError('sandbox send timed out')


def mail_backend(name: str | None) -> MailBackend | None:
    backend = (name or 'disabled').strip().lower()
    if backend in ('', 'disabled', 'off'):
        return None
    if backend == 'test_sink':
        return SandboxMailSink()
    raise RuntimeError('CLOSEREADY_MAIL_BACKEND must be disabled or test_sink; live mail is not in this increment.')
