# Sandbox communication and follow-up

CloseReady keeps communication decisions separate from transport. The case-analysis agent may propose a customer-facing draft, but it cannot choose a recipient, approve its own draft or send mail. An assigned manager reviews the draft first. Approval creates a durable outbox record with `delivery_status=not_attempted`.

The current communication backend is `test_sink`. It exercises contact resolution, sending windows, idempotency, delivery state and audit records without contacting an external mail server. A successful sandbox delivery is stored in `sandbox_mailbox` and returns `live=false`; it must never be presented as evidence that a real email was sent.

## Case association contract

Every new Case receives one active customer-visible reference in the same transaction as Case creation. The value has the form `CR-2607-X7K9Q2AB`: the middle segment identifies the accounting period and the final eight characters are random. Existing databases are backfilled when the Store starts. The mapping lives in `case_communication_refs`, separately from the public `CaseSnapshot` contract.

Authenticated case users can read the reference through:

```http
GET /api/v1/cases/{case_id}/communication-reference
```

Student 3's inbound adapter must call the Store boundary:

```python
resolution = store.resolve_case_reference(public_reference, sender_email)
```

A successful `CaseReferenceResolution` contains `case_id`, `client_id` and `contact_id`. A failed result contains only `REFERENCE_NOT_FOUND`, `REFERENCE_REVOKED` or `SENDER_NOT_APPROVED` and discloses no internal identifiers. The resolver accepts case-insensitive references and email addresses, but reference knowledge alone never authorises access.

Inbound association should first use a trusted provider thread or `In-Reply-To` mapping when available, then fall back to the customer-visible reference. Student 3 owns extraction of those mail fields; Student 1 owns the reference mapping and approved-sender validation. Unmatched results must be quarantined rather than guessed from the sender alone.

## Configuration

Set this only in the ignored runtime environment file:

```ini
CLOSEREADY_MAIL_BACKEND=test_sink
```

The administrator-controlled access file must contain:

- one active `Contact` whose `client_id` matches the case;
- an approved email address and the human who approved it;
- one `CommunicationPolicy` matching the case's `policy_id` and `policy_version`;
- the sending window, timezone, reminder interval and reminder limit.

`examples/server-config.json` contains synthetic values. Its all-zero token hash is deliberately unusable and must be replaced in the ignored deployment copy. Contacts and policies are trusted configuration; the LLM cannot create or modify them.

## Outbound request flow

1. `POST /api/v1/cases/{case_id}/activate` queues case analysis.
2. The worker lets the LLM read the authorised case and propose one typed action.
3. A document request becomes an open review task and increases `state_version`. The delivery adapter can read the Case's customer-visible reference and add it deterministically to the external message.
4. `POST /api/v1/cases/{case_id}/review-decisions` lets the assigned manager approve, edit and approve, or reject the draft.
5. Approval creates one outbox item. It still has not been sent.
6. `POST /api/v1/cases/{case_id}/outbox/{outbox_id}/deliver` resolves an approved contact, rechecks the sending policy and customer-visible content, and invokes `test_sink`.
7. `GET /api/v1/cases/{case_id}/mailbox` shows the locally persisted sandbox message.

Delivery is blocked if the draft is unapproved or obsolete, the contact is missing or ambiguous, the current time is outside the approved sending window, or the request is not authorised. A timeout is recorded as `delivery_unknown` and is not automatically retried because doing so could send a duplicate message.

## Reply and reminder flow

`POST /api/v1/cases/{case_id}/replies` represents a trusted mail-ingestion boundary. The caller supplies a sender address, receipt time and body. A real mail adapter would call this endpoint only after provider authentication and case association. In the sandbox, the demonstration script calls it directly.

An active approved sender is associated with the case and increases `state_version`. An unknown sender is quarantined and creates a human review task; its content is not treated as authorised case evidence.

`POST /api/v1/cases/{case_id}/replies/{reply_id}/assess` runs the bounded reply-assessment loop. The LLM sees the reply as untrusted data and may return a typed commitment, ambiguity, dispute or waiver request. The application validates every case and requirement reference. A clear commitment records the promised time and schedules a reminder after the configured grace period. Ambiguous, disputed and waiver-related replies go to human review and never change document acceptance.

`POST /api/v1/cases/{case_id}/reminders/dispatch-due` checks due reminders against the latest checklist before sandbox delivery. It cancels reminders whose requirements have already been accepted or waived, defers work outside the sending window and enforces configured limits. This endpoint is explicit in the current increment; a production scheduler remains future work.

## Run the integrated demonstration

Start the API and worker with Docker Compose, then run:

```powershell
.\examples\run-integrated-demo.ps1 `
    -ApiToken (Get-Content .\local-data\api-token.txt -Raw).Trim() `
    -PdfPath .\output\pdf\closeready-valid-july-2026-bank-statement.pdf
```

The script creates fresh IDs and follows current `state_version` values automatically. It performs two live LLM activities: the initial document-request proposal and the customer-reply assessment. It then uploads the synthetic text PDF and polls the document worker. The expected final checklist status is `accepted` and case readiness is `ready_for_confirmation`.

The demonstration stops before reminder dispatch because the synthetic client promises a future date. The reminder remains scheduled and can be inspected through the reminders endpoint. When it later becomes due, the dispatch endpoint rechecks whether its requirement is still outstanding before deciding whether to send or cancel it.

## Current production gaps

- `test_sink` is not SMTP, Gmail or AWS SES.
- Inbound replies are posted through an API; no provider webhook or mailbox poller is connected.
- Due reminders require an explicit dispatch call; no scheduler invokes it automatically.
- Contact and policy administration use a trusted configuration file rather than an administrative UI.
- OCR, attachment ingestion from replies and human resolution of ambiguous document findings remain outside this increment.

These limits keep the hackathon demonstration honest while preserving the same guarded interfaces needed by a future live transport adapter.
