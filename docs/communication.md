# Communication: sandbox and live mail

CloseReady keeps communication decisions separate from transport. The case-analysis agent may propose a customer-facing draft, but it cannot choose a recipient, approve its own draft or send mail. An assigned manager reviews the draft first. Approval creates a durable outbox record with `delivery_status=not_attempted`. Unapproved drafts are never sent.

Two mail backends are supported:

| `CLOSEREADY_MAIL_BACKEND` | Meaning |
| --- | --- |
| omitted / `disabled` | Outbox items stay unsent |
| `test_sink` | Labeled in-process sandbox. `live=false`. Not evidence of external delivery |
| `smtp` | Real SMTP send, optional IMAP receive. `live=true` |

Unknown backend names fail process start. Missing SMTP credentials also fail start; there is no silent fixture fallback.

## Case association contract

Every new Case receives one active customer-visible reference in the same transaction as Case creation. The value has the form `CR-2607-X7K9Q2AB`: the middle segment identifies the accounting period and the final eight characters are random. Existing databases are backfilled when the Store starts. The mapping lives in `case_communication_refs`, separately from the public `CaseSnapshot` contract.

Authenticated case users can read the reference through:

```http
GET /api/v1/cases/{case_id}/communication-reference
```

The delivery adapter attaches this reference to the **sent** subject and body. The LLM never generates or edits it. Stored outbox and reminder drafts keep the reviewed wording; only the transport copy includes the token, typically as `[CR-2607-X7K9Q2AB]` in the subject and a short footer in the body. Internal identifiers such as `case_id` and `requirement_id` are rejected in customer-visible text.

Inbound association:

1. Prefer a stored provider `Message-ID` / `In-Reply-To` / `References` mapping from a previous outbound send.
2. Otherwise extract the customer-visible reference from the subject or body and call:

```python
resolution = store.resolve_case_reference(public_reference, sender_email)
```

A successful `CaseReferenceResolution` contains `case_id`, `client_id` and `contact_id`. A failed result contains only `REFERENCE_NOT_FOUND`, `REFERENCE_REVOKED` or `SENDER_NOT_APPROVED` and discloses no internal identifiers. Sender address alone never selects a Case: one client may have several active accounting periods. Unmatched mail is quarantined for human review.

## Configuration

Set mail settings only in the ignored runtime environment file.

Sandbox:

```ini
CLOSEREADY_MAIL_BACKEND=test_sink
```

Live personal mailbox (phase 1 send, phase 2 IMAP receive):

```ini
CLOSEREADY_MAIL_BACKEND=smtp
CLOSEREADY_SMTP_FROM=your.firm@example.com
CLOSEREADY_SMTP_HOST=smtp.gmail.com
CLOSEREADY_SMTP_PORT=587
CLOSEREADY_SMTP_SECURITY=starttls
CLOSEREADY_SMTP_USERNAME=your.firm@example.com
CLOSEREADY_SMTP_PASSWORD=
CLOSEREADY_IMAP_HOST=imap.gmail.com
CLOSEREADY_IMAP_PORT=993
CLOSEREADY_IMAP_FOLDER=INBOX
CLOSEREADY_MAIL_TIMEOUT_SECONDS=30
```

Use an app password or equivalent mailbox secret. Never commit it. Put the approved personal inbox on the matching `Contact.approved_email` in the administrator access file. Gmail and similar hosts require IMAP to be enabled for reply ingestion.

The access file must also contain:

- one active `Contact` whose `client_id` matches the case;
- an approved email address and the human who approved it;
- one `CommunicationPolicy` matching the case's `policy_id` and `policy_version`;
- the sending window, timezone, reminder interval and reminder limit.

`examples/server-config.json` contains synthetic values. Its all-zero token hash is deliberately unusable and must be replaced in the ignored deployment copy. Contacts and policies are trusted configuration; the LLM cannot create or modify them.

## Outbound request flow

1. `POST /api/v1/cases/{case_id}/activate` queues case analysis.
2. The worker lets the LLM read the authorised case and propose one typed action.
3. A document request becomes an open review task and increases `state_version`.
4. `POST /api/v1/cases/{case_id}/review-decisions` lets the assigned manager approve, edit and approve, or reject the draft.
5. Approval creates one outbox item. It still has not been sent.
6. `POST /api/v1/cases/{case_id}/outbox/{outbox_id}/deliver` resolves an approved contact, rechecks outstanding items, the sending window and the customer-visible guard, attaches the Case reference, and invokes the configured backend.
7. `GET /api/v1/cases/{case_id}/mailbox` lists the locally persisted delivery copy. `live=true` means SMTP was used; it is still not a bounce receipt.

Delivery is blocked if the draft is unapproved or obsolete, the contact is missing or ambiguous, the current time is outside the approved sending window, or the request is not authorised. A timeout is recorded as `delivery_status=delivery_unknown` and opens an assigned review; it is not automatically retried. Other transport errors are `failed` and also open a review.

## Reply, attachment and reminder flow

`POST /api/v1/cases/{case_id}/replies` remains the trusted ingest boundary used by the sandbox demo. It still requires an approved sender for that known Case.

Live inbound uses the authenticated poller instead of an unauthenticated webhook:

```http
POST /api/v1/inbound-mail/poll
GET  /api/v1/inbound-mail/quarantine
```

The poller fetches unseen IMAP messages (or the test double's inbox), associates them as above, persists an approved reply, and submits PDF attachments through the existing document store used by:

```http
POST /api/v1/cases/{case_id}/documents
```

Non-PDF parts are ignored. Empty or oversized PDFs open a human review rather than guessing a requirement. Duplicate provider message IDs are idempotent.

`POST /api/v1/cases/{case_id}/replies/{reply_id}/assess` is unchanged: the LLM may record a commitment; the application schedules a reminder after policy checks.

`POST /api/v1/cases/{case_id}/reminders/dispatch-due` rechecks outstanding items, duplicate keys, interval/limit policy, open overlapping reviews and the sending window, then sends due reminders through the same backend. After a successful send it schedules the next chase reminder under `min_reminder_interval_hours`, or opens a `REMINDER_LIMIT` review and stops when `max_reminders_per_requirement` is reached. Open dispute, waiver, clarification or limit reviews pause affected follow-up as `paused` so the worker does not keep a due reminder. Failed or unknown reminder delivery opens an assigned review and does not schedule the next chase. Outcomes are `sent`, `failed` or `delivery_unknown`. Obsolete mixed-item reminders are cancelled rather than sent. When the worker is idle and mail is configured, it also polls inbound mail and dispatches due reminders.

## Run the integrated sandbox demonstration

Start the API and worker with Docker Compose, then run:

```powershell
.\examples\run-integrated-demo.ps1 `
    -ApiToken (Get-Content .\local-data\api-token.txt -Raw).Trim() `
    -PdfPath .\output\pdf\closeready-valid-july-2026-bank-statement.pdf
```

That script still uses `test_sink`. It never sends external email.

## Prove a real mailbox round trip

1. Put your personal address on the demo contact and enable `CLOSEREADY_MAIL_BACKEND=smtp` with the SMTP/IMAP settings above.
2. Create a case, approve the agent draft, then `POST .../outbox/{outbox_id}/deliver` with that `contact_id`.
3. Confirm the message arrived, including the `CR-YYMM-...` reference.
4. Reply from the same approved address, optionally attaching a text PDF, keeping the reference or using In-Reply-To.
5. `POST /api/v1/inbound-mail/poll` and inspect the associated reply, uploaded document job and any quarantine rows.

## Remaining production gaps

- Personal SMTP/IMAP is not a dedicated transactional provider (SES, a firm mail gateway, bounce webhooks).
- Due reminders can be dispatched by the worker or the explicit HTTP route; there is no separate multi-host scheduler.
- Contact and policy administration still use a trusted configuration file rather than an administrative UI.
- OCR and human resolution of ambiguous document findings remain Student 2/4 work.

A `test_sink` mailbox row is still not evidence that mail was sent. An SMTP `delivery_status=sent` means the provider accepted the message, not that the recipient read it.
