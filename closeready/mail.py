"""Mail backends. test_sink is a labeled sandbox; smtp is live transport."""
from __future__ import annotations

import imaplib
import os
import smtplib
import ssl
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .mail_messages import (
    InboundMail, build_outbound_message, new_message_id, normalize_message_id,
    parse_rfc822,
)
from .store import DomainError


@runtime_checkable
class MailBackend(Protocol):
    backend_name: str
    live: bool
    can_receive: bool

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        """Return a backend message id. Never choose the recipient."""

    def fetch_unseen(self) -> list[InboundMail]:
        """Return unseen provider messages. Empty when receive is not configured."""

    def acknowledge(self, provider_message_ids: list[str]) -> None:
        """Mark processed provider messages so they are not fetched again."""


class SandboxMailSink:
    """In-process sandbox. Messages are persisted by CommunicationStore, not SMTP."""
    backend_name = 'test_sink'
    live = False
    can_receive = False

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        if not to_email or not subject or not body:
            raise DomainError('MAIL_REJECTED', 'Sandbox send requires a recipient, subject and body.', 422)
        return 'sink_' + uuid4().hex

    def fetch_unseen(self) -> list[InboundMail]:
        return []

    def acknowledge(self, provider_message_ids: list[str]) -> None:
        return None


class TimeoutMailSink(SandboxMailSink):
    """Test double for unknown delivery. Do not use outside tests."""

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        raise TimeoutError('sandbox send timed out')


class MemoryMailBackend:
    """In-memory live-shaped double. Used by tests; does not contact a provider."""
    backend_name = 'smtp'
    live = True
    can_receive = True

    def __init__(self):
        self.sent: list[dict] = []
        self.inbox: list[InboundMail] = []
        self.acknowledged: list[str] = []

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        if not to_email or not subject or not body:
            raise DomainError('MAIL_REJECTED', 'Send requires a recipient, subject and body.', 422)
        provider_id = normalize_message_id(metadata.get('message_id')) or (
            'smtp_' + uuid4().hex)
        self.sent.append({
            'to_email': to_email, 'subject': subject, 'body': body,
            'metadata': dict(metadata), 'provider_message_id': provider_id,
        })
        return provider_id

    def fetch_unseen(self) -> list[InboundMail]:
        seen = set(self.acknowledged)
        return [item for item in self.inbox if item.provider_message_id not in seen]

    def acknowledge(self, provider_message_ids: list[str]) -> None:
        self.acknowledged.extend(provider_message_ids)


class TimeoutMailBackend(MemoryMailBackend):
    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        raise TimeoutError('smtp send timed out')


class FailingMailBackend(MemoryMailBackend):
    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        raise RuntimeError('smtp send failed')


class SmtpMailBackend:
    """Personal-mailbox SMTP send with optional IMAP receive."""
    backend_name = 'smtp'
    live = True

    def __init__(
            self, *, from_address: str, smtp_host: str, smtp_port: int,
            username: str, password: str, timeout: float = 30.0,
            security: str = 'starttls', imap_host: str | None = None,
            imap_port: int = 993, imap_username: str | None = None,
            imap_password: str | None = None, imap_folder: str = 'INBOX',
            smtp_factory=None, imap_factory=None):
        self.from_address = from_address
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.security = security
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_username = imap_username or username
        self.imap_password = imap_password or password
        self.imap_folder = imap_folder
        self.smtp_factory = smtp_factory
        self.imap_factory = imap_factory
        self._pending_uids: dict[str, bytes] = {}

    @property
    def can_receive(self) -> bool:
        return bool(self.imap_host)

    def send(self, *, to_email: str, subject: str, body: str, metadata: dict) -> str:
        if not to_email or not subject or not body:
            raise DomainError('MAIL_REJECTED', 'Send requires a recipient, subject and body.', 422)
        provider_id = normalize_message_id(metadata.get('message_id')) or new_message_id(
            self.from_address)
        message = build_outbound_message(
            from_address=self.from_address, to_email=to_email, subject=subject,
            body=body, message_id=provider_id,
            public_reference=metadata.get('public_reference'),
            in_reply_to=metadata.get('in_reply_to'),
        )
        try:
            self._deliver(message)
        except (TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError) or 'timed out' in str(exc).lower():
                raise TimeoutError('smtp send timed out') from None
            raise
        return provider_id

    def _deliver(self, message) -> None:
        factory = self.smtp_factory or self._default_smtp
        client = factory()
        try:
            if self.security == 'starttls':
                client.starttls(context=ssl.create_default_context())
            if self.username:
                client.login(self.username, self.password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass

    def _default_smtp(self):
        if self.security == 'ssl':
            return smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, timeout=self.timeout,
                context=ssl.create_default_context())
        return smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)

    def fetch_unseen(self) -> list[InboundMail]:
        if not self.can_receive:
            return []
        factory = self.imap_factory or self._default_imap
        client = factory()
        try:
            status, _ = client.select(self.imap_folder, readonly=False)
            if status != 'OK':
                raise RuntimeError('imap select failed')
            status, data = client.search(None, 'UNSEEN')
            if status != 'OK':
                return []
            uids = data[0].split() if data and data[0] else []
            messages: list[InboundMail] = []
            pending: dict[str, bytes] = {}
            for uid in uids:
                status, payload = client.fetch(uid, '(RFC822)')
                if status != 'OK' or not payload:
                    continue
                raw = _imap_rfc822(payload)
                if raw is None:
                    continue
                mailbox_uid = uid.decode() if isinstance(uid, bytes) else str(uid)
                parsed = parse_rfc822(raw, mailbox_uid=mailbox_uid)
                messages.append(parsed)
                pending[parsed.provider_message_id] = uid
            self._pending_uids = pending
            return messages
        finally:
            try:
                client.logout()
            except Exception:
                try:
                    client.shutdown()
                except Exception:
                    pass

    def _default_imap(self):
        client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=self.timeout)
        client.login(self.imap_username, self.imap_password)
        return client

    def acknowledge(self, provider_message_ids: list[str]) -> None:
        if not self.can_receive or not provider_message_ids:
            return
        uids = [self._pending_uids[mid] for mid in provider_message_ids if mid in self._pending_uids]
        if not uids:
            return
        factory = self.imap_factory or self._default_imap
        client = factory()
        try:
            client.select(self.imap_folder, readonly=False)
            for uid in uids:
                client.store(uid, '+FLAGS', '\\Seen')
        finally:
            try:
                client.logout()
            except Exception:
                pass


def _imap_rfc822(payload) -> bytes | None:
    for item in payload:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _required(environ, key: str) -> str:
    value = (environ.get(key) or '').strip()
    if not value:
        raise RuntimeError(f'{key} is required when CLOSEREADY_MAIL_BACKEND=smtp.')
    return value


def _optional(environ, key: str, default: str | None = None) -> str | None:
    value = environ.get(key)
    if value is None:
        return default
    stripped = str(value).strip()
    return stripped if stripped else default


def smtp_backend_from_environ(environ) -> SmtpMailBackend:
    from_address = _required(environ, 'CLOSEREADY_SMTP_FROM')
    host = _required(environ, 'CLOSEREADY_SMTP_HOST')
    username = _required(environ, 'CLOSEREADY_SMTP_USERNAME')
    password = _required(environ, 'CLOSEREADY_SMTP_PASSWORD')
    port_text = _optional(environ, 'CLOSEREADY_SMTP_PORT', '587') or '587'
    timeout_text = _optional(environ, 'CLOSEREADY_MAIL_TIMEOUT_SECONDS', '30') or '30'
    try:
        port = int(port_text)
        timeout = float(timeout_text)
    except ValueError as exc:
        raise RuntimeError('SMTP port and timeout must be numbers.') from exc
    security = (_optional(environ, 'CLOSEREADY_SMTP_SECURITY', 'starttls') or 'starttls').lower()
    if security not in ('starttls', 'ssl', 'none'):
        raise RuntimeError('CLOSEREADY_SMTP_SECURITY must be starttls, ssl or none.')
    if security == 'none' and port == 465:
        security = 'ssl'
    imap_host = _optional(environ, 'CLOSEREADY_IMAP_HOST')
    imap_port_text = _optional(environ, 'CLOSEREADY_IMAP_PORT', '993') or '993'
    try:
        imap_port = int(imap_port_text)
    except ValueError as exc:
        raise RuntimeError('IMAP port must be a number.') from exc
    return SmtpMailBackend(
        from_address=from_address, smtp_host=host, smtp_port=port,
        username=username, password=password, timeout=timeout, security=security,
        imap_host=imap_host, imap_port=imap_port,
        imap_username=_optional(environ, 'CLOSEREADY_IMAP_USERNAME', username),
        imap_password=_optional(environ, 'CLOSEREADY_IMAP_PASSWORD', password),
        imap_folder=_optional(environ, 'CLOSEREADY_IMAP_FOLDER', 'INBOX') or 'INBOX',
    )


def mail_backend(name: str | None, environ=None) -> MailBackend | None:
    backend = (name or 'disabled').strip().lower()
    env = environ if environ is not None else os.environ
    if backend in ('', 'disabled', 'off'):
        return None
    if backend == 'test_sink':
        return SandboxMailSink()
    if backend == 'smtp':
        return smtp_backend_from_environ(env)
    raise RuntimeError(
        'CLOSEREADY_MAIL_BACKEND must be disabled, test_sink or smtp.')
