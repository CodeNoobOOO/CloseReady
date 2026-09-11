# Review Decisions and Outbox Design

## Purpose

CloseReady currently stores an LLM-generated document-request draft as an open review task, but the task cannot be resolved and the draft cannot progress safely toward delivery. This increment adds the Student 1 application boundary for customer-visible content validation, authenticated review decisions, and a durable mail outbox. It does not send email, ingest replies, or build the dashboard.

## Ownership boundary

- Student 1 owns the schemas, authenticated review-decision API, state/version checks, customer-visible content guard, outbox authorization, persistence, idempotency, and audit records.
- Student 3 owns contact provisioning, communication-policy content, the provider-specific mail adapter, delivery attempts, provider identifiers, reply ingestion, and reminder scheduling. Student 3 must use the outbox boundary rather than sending directly from model output.
- Student 4 owns the dashboard and review UI and consumes only authenticated HTTP APIs.
- Internal `case_id`, `review_task_id`, and `requirement_id` values remain in structured records for authorization, correlation, and audit. They must not occur in customer-visible subject or body text.

## Scope

This increment supports review tasks that contain a document-request or clarification draft and operational-error review tasks that have no draft.

An authorized manager can:

1. approve an unchanged safe draft;
2. edit and approve a safe draft;
3. reject a draft;
4. dismiss an operational-error task.

Approving a draft creates one durable outbox record in the same transaction that resolves the review task and increments the case version. It does not contact a mail provider. Rejecting or dismissing resolves the task without creating an outbox record. Review decisions are terminal in this increment and cannot be changed.

Requirement acceptance, waiver, document review, readiness confirmation, contact administration, and sending are outside this increment.

## Customer-visible content guard

The guard validates `MessageDraft.subject` and `MessageDraft.body` before a model-produced draft is stored and again before an approved draft is placed in the outbox. It rejects:

- internal identifier tokens with the prefixes `case_`, `req_`, `run_`, `review_`, `event_`, or `proposal_`, case-insensitively;
- URI schemes other than `https:` and `mailto:` in visible text;
- Unicode control characters other than newline, carriage return, and tab;
- a subject longer than 200 characters or a body longer than 10,000 characters.

The application does not silently redact invalid text because removal can alter meaning. A model-produced invalid draft is rejected as `UNSAFE_DRAFT`; the bounded runtime may use its existing single invalid-tool repair attempt. If repair is exhausted, the existing failure path creates an assigned review task without the unsafe draft. A human-edited invalid draft receives HTTP 422 and remains open.

The guard is a deterministic last-mile control, not a complete data-loss-prevention system. Client names, approved recipient addresses, tone, and safe business labels will come from Student 3's trusted communication configuration. The model never chooses the recipient.

## Records

### Review task

`ReviewTaskRecord` is extended with:

- `status`: `open` or `resolved`;
- `resolution`: null while open, otherwise `approved`, `edited_and_approved`, `rejected`, or `dismissed`;
- `resolved_by`: authenticated user ID or null;
- `resolved_at`: UTC timestamp or null;
- `resolution_reason`: reviewer-supplied text or null;
- `approved_draft`: the guarded final draft or null.

Existing stored records are normalized when read by supplying null lifecycle fields and treating the existing `open` value as open. This additive compatibility avoids a database reset for current local data.

### Review decision request

`ReviewDecisionRequest` contains:

- `expected_state_version`: positive integer;
- `review_task_id`: non-empty identifier;
- `decision`: `approve_draft`, `edit_and_approve`, `reject_draft`, or `dismiss_error`;
- `reason`: non-empty reviewer explanation;
- `edited_draft`: required only for `edit_and_approve` and forbidden otherwise.

`approve_draft` and `edit_and_approve` require a review task with a draft. `reject_draft` requires a draft. `dismiss_error` requires a task without a draft. Edited draft requirement IDs must exactly match the task requirement IDs.

### Outbox record

`OutboxRecord` contains:

- `outbox_id`, `case_id`, `review_task_id`;
- structured `requirement_ids`;
- guarded `subject` and `body`;
- `status`: fixed to `pending_reviewed_delivery` for this increment;
- `recipient_contact_id`: null until Student 3 resolves an approved contact;
- `provider_message_id`: null;
- `created_by`, `created_at`;
- `delivery_status`: fixed to `not_attempted`.

The record deliberately contains no arbitrary email address and no send method. Student 3 will extend the delivery lifecycle through a separate design while preserving the immutable approved content or recording a new approval when content changes.

## HTTP interface

### Resolve a review task

`POST /api/v1/cases/{case_id}/review-decisions`

Requirements:

- bearer-authenticated manager with access to the case;
- `Idempotency-Key` header;
- exact `expected_state_version`;
- open task belonging to the case and assigned to that manager.

Returns the resolved `ReviewTaskRecord`. Repeating the same actor, case, key, and body returns the original response. Reusing the key with different input returns 409. A stale version, resolved task, wrong assignment, or task/case mismatch returns 409 or 404 without exposing another client's data.

### List outbox records

`GET /api/v1/cases/{case_id}/outbox?cursor=&limit=`

Returns a scoped page for the authenticated case. This read endpoint is sufficient for Student 3 integration and Student 4 display; write/delivery endpoints are not part of this increment.

## Transaction and state behavior

For approval, one database transaction:

1. authenticates and scopes the case/task;
2. validates the expected case version and terminal status;
3. selects and validates the final draft;
4. marks the task resolved;
5. inserts exactly one outbox record linked to the task;
6. increments `state_version` once;
7. writes the idempotency response and audit events.

Reject and dismiss perform steps 1, 2, 4, 6, and 7. Readiness remains `collecting`; resolving a communication or operational task cannot accept a document or confirm readiness.

Concurrent decisions serialize through the existing SQLite write boundary. Only the first valid decision commits. An idempotent replay returns the original result even if the case has since changed.

## Audit behavior

Every successful decision records `resolve_review_task` with the resolution and old/new state version. Approval additionally records `queue_reviewed_outbox` with `outcome=queued`. Audit text contains safe reason codes rather than full customer message bodies or credentials.

Denied and stale decisions follow the existing mutation-audit policy where a scoped case is known. No network delivery is claimed by this increment.

## Testing and acceptance

Deterministic tests must prove:

- each unsafe identifier prefix is rejected in subject and body;
- approved metadata still retains internal requirement IDs;
- invalid visible URLs/control characters/lengths are rejected;
- unsafe model drafts never persist as customer-visible drafts;
- approve and edit-and-approve create exactly one pending outbox record;
- reject and dismiss create no outbox record;
- decision/task-type mismatches fail without mutation;
- stale, unauthorized, cross-case, duplicate, and idempotency-conflict decisions are safe;
- concurrent decisions commit only once;
- existing open review records remain readable;
- state version and audit transitions match the committed decision;
- restarting the API preserves resolved tasks, outbox records, and idempotent responses.

The feature is complete when Student 4 can resolve a task through the HTTP API and Student 3 can list a durable approved outbox record without accessing SQLite directly. Actual delivery remains explicitly `not_attempted`.
