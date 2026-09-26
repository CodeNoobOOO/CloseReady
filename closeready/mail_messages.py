"""Customer-visible mail composition and inbound RFC 822 parsing.

The LLM never generates the case reference. Application code attaches it
before transport and extracts it again on the way back in.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
from uuid import uuid4

from .models import MessageDraft
from .store import DomainError


PUBLIC_REFERENCE = re.compile(r'\bCR-\d{4}-[A-Z2-9]{8}\b', re.IGNORECASE)
SUBJECT_PREFIX = '[{ref}] '
BODY_FOOTER = '\n\nPlease include this reference in your reply: {ref}'
EMPTY_BODY = 'The client reply had no plain-text body.'


@dataclass(frozen=True)
class InboundAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class InboundMail:
    provider_message_id: str
    sender_email: str
    subject: str
    body: str
    received_at: datetime
    in_reply_to: str | None
    references: tuple[str, ...]
    attachments: tuple[InboundAttachment, ...]
    mailbox_uid: str | None = None


def normalize_message_id(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    if text.startswith('<') and text.endswith('>') and len(text) > 2:
        text = text[1:-1]
    return text or None


def format_message_id(value: str) -> str:
    normalized = normalize_message_id(value) or value
    return normalized if normalized.startswith('<') else f'<{normalized}>'


def new_message_id(from_address: str) -> str:
    domain = from_address.rsplit('@', 1)[-1] if '@' in from_address else 'mail.local'
    return f'closeready.{uuid4().hex}@{domain}'


def extract_public_reference(*parts: str | None) -> str | None:
    """Return the sole customer-visible reference, or None if missing/ambiguous."""
    found: set[str] = set()
    for part in parts:
        if not part:
            continue
        for match in PUBLIC_REFERENCE.findall(part):
            found.add(match.upper())
    if len(found) == 1:
        return found.pop()
    return None


def apply_customer_reference(subject: str, body: str, public_reference: str) -> tuple[str, str]:
    """Attach the server-issued reference without changing the approved wording."""
    token = public_reference.strip().upper()
    prefix = SUBJECT_PREFIX.format(ref=token)
    footer = BODY_FOOTER.format(ref=token)
    new_subject = subject
    new_body = body
    if token not in subject.upper() and len(prefix) + len(subject) <= 200:
        new_subject = prefix + subject
    if token not in body.upper() and len(body.rstrip() + footer) <= 10_000:
        new_body = body.rstrip() + footer
    if token not in (new_subject + '\n' + new_body).upper():
        raise DomainError(
            'UNSAFE_DRAFT',
            'Cannot attach the customer-visible reference without exceeding length limits.',
            422,
        )
    validate_composed_copy(new_subject, new_body)
    return new_subject, new_body


def validate_composed_copy(subject: str, body: str) -> None:
    from .content_guard import validate_customer_visible_draft
    validate_customer_visible_draft(MessageDraft(
        subject=subject, body=body, requirement_ids=['req_mail_placeholder']))


def thread_candidates(message: InboundMail) -> tuple[str, ...]:
    ordered: list[str] = []
    for value in (message.in_reply_to, *message.references):
        normalized = normalize_message_id(value)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


def parse_sender(from_header: str | None) -> str:
    addresses = getaddresses([from_header or ''])
    for _name, address in addresses:
        if address and '@' in address:
            return address.strip()
    return ''


def _decoded_payload(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode('utf-8', errors='replace')
        return b''
    return payload


class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self._skip += 1
            return
        if tag in ('br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'blockquote'):
            self._chunks.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head') and self._skip:
            self._skip -= 1
        if tag in ('p', 'div', 'tr', 'li', 'blockquote'):
            self._chunks.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        raw = ''.join(self._chunks).replace('\xa0', ' ')
        raw = re.sub(r'[ \t]+', ' ', raw)
        return re.sub(r'\n{3,}', '\n\n', raw).strip()


def html_to_text(value: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(value)
        parser.close()
        text = parser.text()
    except Exception:
        text = unescape(re.sub(r'<[^>]+>', ' ', value)).replace('\xa0', ' ')
    return text.strip()


def _decode_text_part(part: Message) -> str:
    payload = _decoded_payload(part)
    charset = part.get_content_charset() or 'utf-8'
    try:
        text = payload.decode(charset, errors='replace')
    except LookupError:
        text = payload.decode('utf-8', errors='replace')
    return unescape(text).replace('\xa0', ' ')


def _first_body_part(message: Message, content_type: str) -> Message | None:
    if (message.get_content_type() == content_type
            and message.get_content_disposition() != 'attachment'):
        return message
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get_content_disposition() == 'attachment':
                continue
            if part.get_content_type() == content_type:
                return part
    return None


def trim_quoted_history(text: str) -> str:
    """Keep the client reply and drop forwarded/quoted history where practical."""
    lines = text.replace('\r\n', '\n').split('\n')
    cut = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '--' or stripped == '-- ':
            cut = index
            break
        if stripped.startswith('-----Original Message-----'):
            cut = index
            break
        if stripped.lower().startswith('-----forwarded message'):
            cut = index
            break
        if re.match(r'^On .+ wrote:\s*$', stripped):
            cut = index
            break
        if (stripped.startswith('From:') and index + 1 < len(lines)
                and lines[index + 1].strip().startswith('Sent:')):
            cut = index
            break
    kept = lines[:cut]
    while kept and kept[-1].lstrip().startswith('>'):
        kept.pop()
    return '\n'.join(kept).strip()


def readable_body(message: Message) -> str:
    plain = _first_body_part(message, 'text/plain')
    if plain is not None:
        text = _decode_text_part(plain)
    else:
        html_part = _first_body_part(message, 'text/html')
        if html_part is None:
            return ''
        text = html_to_text(_decode_text_part(html_part))
    return trim_quoted_history(text)


def _is_pdf_part(part: Message, filename: str) -> bool:
    content_type = (part.get_content_type() or '').lower()
    return content_type == 'application/pdf' or filename.lower().endswith('.pdf')


def _safe_filename(raw: str | None) -> str:
    name = Path(raw or '').name.strip()
    if not name or name in {'.', '..'}:
        return 'attachment.pdf'
    return name[:255]


def _attachments(message: Message) -> tuple[InboundAttachment, ...]:
    found: list[InboundAttachment] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_maintype() == 'multipart':
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or '').lower()
        if not filename and disposition != 'attachment':
            continue
        safe_name = _safe_filename(filename)
        if not _is_pdf_part(part, safe_name):
            continue
        content = _decoded_payload(part)
        if not content:
            continue
        found.append(InboundAttachment(
            filename=safe_name if safe_name.lower().endswith('.pdf') else 'attachment.pdf',
            content_type='application/pdf',
            content=content,
        ))
    return tuple(found)


def _header_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    ids: list[str] = []
    for token in value.replace(',', ' ').split():
        normalized = normalize_message_id(token)
        if normalized and normalized not in ids:
            ids.append(normalized)
    return tuple(ids)


def parse_rfc822(raw: bytes, *, mailbox_uid: str | None = None) -> InboundMail:
    parsed = BytesParser(policy=email_policy).parsebytes(raw)
    provider_id = normalize_message_id(parsed.get('Message-ID')) or new_message_id('inbound@mail.local')
    received = parsed.get('Date')
    try:
        received_at = parsedate_to_datetime(received) if received else datetime.now(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        received_at = datetime.now(timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    subject = parsed.get('Subject') or 'No subject'
    body = readable_body(parsed) or EMPTY_BODY
    in_reply_to = _header_ids(parsed.get('In-Reply-To'))
    references = _header_ids(parsed.get('References'))
    return InboundMail(
        provider_message_id=provider_id,
        sender_email=parse_sender(parsed.get('From')),
        subject=subject,
        body=body,
        received_at=received_at.astimezone(timezone.utc),
        in_reply_to=in_reply_to[0] if in_reply_to else None,
        references=references,
        attachments=_attachments(parsed),
        mailbox_uid=mailbox_uid,
    )


def build_outbound_message(
        *, from_address: str, to_email: str, subject: str, body: str,
        message_id: str, public_reference: str | None = None,
        in_reply_to: str | None = None) -> EmailMessage:
    message = EmailMessage()
    message['From'] = from_address
    message['To'] = to_email
    message['Subject'] = subject
    message['Message-ID'] = format_message_id(message_id)
    if in_reply_to:
        message['In-Reply-To'] = format_message_id(in_reply_to)
        message['References'] = format_message_id(in_reply_to)
    if public_reference:
        message['X-CloseReady-Reference'] = public_reference
    message.set_content(body)
    return message
