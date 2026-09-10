# CloseReady shared contracts v0.3

Status: core Python boundary models are implemented in `closeready/models.py`: CaseSnapshot, Requirement, EvidenceRef, ActionContent and ActionProposal. HTTP endpoints, persistence, assessment modules and business execution are not implemented yet. Fixtures in `examples/` are synthetic and are not model evaluation results.

The deliverable must use a real LLM API and persisted business state. Fixtures only unblock parallel development and deterministic tests; they are not the agent implementation. See [LLM runtime](llm-runtime.md) for provider integration and [business acceptance](business-acceptance.md) for source alignment and pilot gates. A standalone DeepSeek connectivity probe has passed; the business runtime remains outstanding.

## v0.3 implementation clarifications

- `description` is optional and nullable on Requirement, but required and nonblank for other_supporting_document. Its completion_rule supplies the configured acceptance rule. Existing v0.2 fixtures remain valid.
- Requirement accounting_period must equal its case period. Coverage dates must be a complete ordered pair; coverage rules require them. Coverage rules use an empty expected_item_refs list; explicit_items requires unique nonempty references.
- Unknown fields are rejected, including arbitrary recipients in message drafts. Versions and page numbers are strict positive integers (not booleans or numeric strings). Parsed timestamps require timezone information and normalize to UTC.
- ActionContent is the model-facing discriminated union, without proposal_id, run_id, case_id or expected_state_version. The application binds these trusted fields to create ActionProposal. Neither schema authenticates the caller.
- Frozen models prevent field reassignment, but nested lists remain mutable. Revalidate serialized records at trust boundaries; do not use model_construct or unvalidated model_copy for external data.
- Structural readiness checks reject empty/unresolved checklists. Database evidence, actor permissions, stale versions, independent review blockers, and actual human confirmation still require the future execution gate.

Exact payload shapes (all fields required):

| action_type | payload |
| --- | --- |
| apply_document_finding | finding_id |
| record_commitment | finding_id, promised_at |
| request_documents | subject, body, requirement_ids |
| request_clarification | subject, body, requirement_ids |
| schedule_reminder | scheduled_at, draft: {subject, body, requirement_ids} |
| create_review_task | issue, evidence_refs: list of EvidenceRef |
| no_action | {} |

Payload finding_id must be included in envelope finding_ids. Draft requirement_ids must match the action requirement_ids without duplicates. Case membership and semantic evidence support are checked by the execution gate, not these standalone records.

## Ownership

| Owner | Owns | Boundary |
| --- | --- | --- |
| Student 1 | Case APIs, persistence, runtime, policy validation and recovery | Sole application path for state changes and external-action authorisation |
| Student 2 | Upload/extraction and document assessment | Returns findings; never changes requirement status directly |
| Student 3 | Reply interpretation, reminder drafts, scheduling and mail adapter | Sending requires the shared execution gate |
| Student 4 | Dashboard, review UI and evaluation | Calls APIs; does not access database tables directly |

Internal table layouts are not public contracts. The first implementation can use one backend and in-process module calls; separate microservices are not required.

## Conventions

- JSON field names use snake_case. IDs are opaque strings.
- Accounting periods use YYYY-MM. Timestamps use ISO 8601 with timezone; persist UTC. A case also carries its business timezone.
- Required fields must be present. Explicit null means unknown or not applicable; do not use empty strings as substitutes.
- Enumerations below are case-sensitive. Do not invent new values silently.
- Server authentication determines actor and client access. A client_id supplied in a request is never proof of permission.
- state_version is a positive integer changed by relevant case mutations. Findings retain their input version; stale findings cannot be applied without reassessment.
- Adding optional fields is normally compatible. Changing required fields, enums or meanings requires an announced contract revision.

## Case snapshot

Required fields: case_id, client_id, accounting_period, timezone, state_version, readiness_status, owner_user_id, due_at, policy_id, policy_version, requirements.

Each requirement contains requirement_id, document_type, accounting_period, status, evidence_refs, scope, completion_rule, reviewer_status. Initial document types: bank_statement, invoice, receipt, other_supporting_document; the last requires a configured description and acceptance rule.

scope contains entity_id, account_ref (nullable internal reference, not a raw account number), coverage_start and coverage_end (inclusive dates, or null where not applicable). completion_rule contains kind (coverage or explicit_items), expected_item_refs and allow_multiple_documents. Coverage rules require both coverage dates; explicit_items rules require a nonempty list of configured business-item references. The backend must reject a requirement with no meaningful completion rule.

For bank statements, check actual covered dates and the relevant account, not just a printed month. Multiple partial statements may jointly satisfy coverage only when their verified intervals cover the full required period without gaps. A single invoice cannot satisfy an entire month's invoice collection: use explicit expected items or individually configured requirements. Unknown missing transactions remain outside claimed completeness.

reviewer_status: not_required, pending, approved, waived. Review decisions have separate records with reviewer, reason, evidence, timestamp and case version. Unknown entity/account evidence requires review where that evidence is required; a model confidence score is not proof.

Requirement statuses: missing, received, needs_clarification, awaiting_review, accepted, waived.

Case readiness statuses: collecting, ready_for_confirmation, ready.

The application computes readiness. All required items must be accepted or human-waived, and all review blockers resolved, before ready_for_confirmation. Only an authorised reviewer can confirm ready. Material new evidence invalidates affected confirmation. Empty or unconfigured checklists are never automatically ready.

Pausing reminders is separate from document status. A promised submission date does not satisfy a requirement.

## Document assessment module — Student 2

Input: case snapshot, document_id, extracted text/evidence references and requirement_id (or null when unmatched).

Output fields: finding_id, responsibility=document_assessment, case_id, input_state_version, document_id, requirement_id, result, detected_type, detected_period, entity_match, account_match, coverage_start, coverage_end, matched_item_refs, uncertainty_reasons, evidence_refs, issues. The application attaches finding_id and the case/version envelope; the LLM supplies only the assessment content. Validate every selected document/requirement reference against authorised inputs.

result: satisfies, needs_correction, needs_review, unmatched.

entity_match: match, mismatch, unknown. A mismatch requires review. Unknown identity must not be represented as a verified match; acceptance follows the configured evidence policy.

account_match uses the same values; non-account requirements may use null. detected_type, detected_period and coverage dates may be null when not determined. matched_item_refs is a list of supported configured item references. uncertainty_reasons is a list, empty only when no uncertainty was identified. result=satisfies is an evidence proposal; only the application completion rule can mark the requirement accepted.

Evidence references contain document_id, page (positive integer or null), and excerpt. Excerpts are untrusted data. Evidence IDs must belong to the case and must be validated by the application. Extraction artifacts retain page/source mappings; verify quoted text against those artifacts and route unsupported claims for reassessment/review. Excerpt presence alone does not establish semantic correctness.

Exact duplicate hashes are checked within authorised scope; never reveal another client's matching file. Extraction failures produce an explicit failure or needs_review, not satisfies.

The single-agent baseline may call extraction tools and produce this result itself. A separate autonomous specialist loop is only part of the candidate multi-agent configuration.

## Reply assessment module — Student 3

Input: current case snapshot, reply_id, received_at, case timezone, relevant reply text and communication policy.

Output fields: finding_id, responsibility=reply_assessment, case_id, input_state_version, reply_id, intent, requirement_ids, promised_at, needs_clarification, uncertainty_reasons, evidence_excerpt. The application attaches IDs and input versions, as for document findings.

intent: submission_commitment, document_submitted, question, dispute, waiver_request, other.

promised_at is a timezone-bearing timestamp or null. Resolve relative dates using received_at and the business timezone; unclear dates require clarification. A statement that a document was sent does not prove receipt or acceptance.

Draft messages contain subject, body and requirement_ids. The application resolves approved recipients; the model does not choose arbitrary email addresses.

## Action proposals — Student 1

Common fields: proposal_id, run_id, case_id, expected_state_version, action_type, requirement_ids, finding_ids, reason, payload.

Allowed model proposals: apply_document_finding, record_commitment, request_documents, request_clarification, schedule_reminder, create_review_task, no_action.

- apply_document_finding: payload identifies document finding; the application maps validated evidence to permitted status changes.
- record_commitment: payload identifies reply finding and promised_at.
- request_documents: payload contains subject/body draft and requirement IDs for an initial request or permitted follow-up. Application resolves recipients and upload links, checks timing and creates an outbox item.
- request_clarification: payload contains a draft; timing and delivery policy still apply.
- schedule_reminder: payload contains scheduled_at and a draft.
- create_review_task: payload contains issue and supporting references.
- no_action: payload is an empty object; reason explains waiting or lack of pending work.

Models cannot waive requirements, approve themselves, confirm readiness or execute arbitrary database operations. Human decisions use authenticated review endpoints.

Validate scope, evidence, permissions and expected version before committing. Persist state changes, audit entries and pending external actions atomically. Reject conflicting versions and reload state. Execute multiple proposals sequentially against current state, never silently reuse an obsolete version.

The application issues proposal_id/run_id and binds proposals to the loaded snapshot. A model cannot manufacture authorisation metadata. A single validated transaction may apply a finding, recompute readiness and cancel related reminders as one business operation. Before another model decision, reload state and supply actual execution outcomes to the model.

Acceptance automatically cancels unsent reminders for resolved items; mixed-item drafts must be regenerated or reviewed to exclude resolved items. Recording a commitment reschedules follow-up under policy without changing document acceptance. Review pauses stop external follow-up for affected items. Resolving a review task records its outcome and recomputes readiness; accepted/waived items may not leave an obsolete blocker indefinitely.

Execution outcomes: executed, queued, blocked, stale, failed. queued means persisted for later delivery, not sent.

## Events, runs and reminders

Events: case_activated, document_uploaded, document_extraction_completed, client_reply_received, reminder_due, reviewer_decision. Persist event_id, case_id, type, occurred_at, payload and dedupe_key. Case creation alone does not send: an authorised activation starts the first document request. Document upload starts extraction; the runtime must not reason over an unfinished extraction artifact.

Run fields: run_id, event_id, case_id, run_mode, start_state_version, status, started_at, finished_at.

run_mode: single or multi; fixed for a run. Run status: queued, running, completed, needs_review, failed, stale. Waiting for a client ends the run and schedules a future event.

Reminder status: scheduled, queued, sent, cancelled, failed, delivery_unknown. Track requirement IDs, scheduled time and dedupe key. Recheck outstanding items, reviewer pauses, commitments, recipients and delivery history before sending.

Use persisted outbox actions with unique dedupe keys. An email timeout may mean delivery succeeded: mark delivery_unknown and reconcile with the provider or a human before resending. Do not claim exactly-once delivery without provider support.

Each audit entry records event/run IDs, actor, action, outcome, evidence or policy references, timestamp and old/new versions where applicable. Include denied actions; exclude secrets.

## Operational business records

- Policy: policy_id, version, approved_by, approved_at, initial_request_enabled, min_reminder_interval_hours, max_reminders_per_requirement, commitment_grace_hours, sending_window_local, timezone, escalation_owner_user_id. These are firm-approved settings, not invented by the LLM. Suppress messages outside the sending window and persist the next due time. A commitment beyond the business deadline creates an internal review rather than silently extending the deadline.
- Contact: contact_id, client_id, approved_email, active, approved_by. A sender match alone is insufficient to override case association or authorise a waiver.
- Document: document_id, case_id, file_hash, storage_ref, original_filename, content_type, extraction_status, extracted_artifact_ref, uploaded_by, uploaded_at. Storage refs are application-managed, not arbitrary paths or URLs supplied to tools.
- Reply: reply_id, case_id, provider_message_id, conversation_ref, sender_contact_id, received_at, body_ref, attachment_document_ids. Unknown or ambiguous associations are quarantined for review before the agent sees another client's context.
- ReviewTask: review_task_id, case_id, requirement_ids, reason_code, evidence_refs, assigned_to, status (open/resolved), resolution, created_at, resolved_at. Exhausted retries and escalation thresholds must create an assigned task, not only a log line.
- Reminder: reminder_id, case_id, requirement_ids, scheduled_at, status, dedupe_key, contact_id, policy_version, source_commitment_id (nullable), attempt_count. The resolved recipient is recorded in restricted execution history.

Owner and deadline enable overdue sorting and escalation. Clients can have multiple accounts, periods and requirements; do not hard-code a single July bank statement.

## Frontend HTTP contract — Student 4

All routes below have prefix /api/v1. They are planned, not yet available.

| Method and route | Purpose | Response |
| --- | --- | --- |
| GET /cases | List authorised cases; optional cursor | 200: items and next_cursor |
| POST /cases | Create a client-period case from explicit requirements | 201: case snapshot |
| GET /cases/{case_id} | Case details | 200: case snapshot |
| POST /cases/{case_id}/activate | Authorised initial-request activation with expected_state_version | 202: event_id, run_id |
| POST /cases/{case_id}/documents | Multipart file upload with optional requirement_id | 202: document_id, event_id, run_id |
| GET /runs/{run_id} | Poll processing | 200: run record |
| GET /cases/{case_id}/findings | Evidence assessments | 200: items and next_cursor |
| GET /cases/{case_id}/review-tasks | Pending and resolved review tasks | 200: items and next_cursor |
| POST /cases/{case_id}/review-decisions | Authenticated reviewer decision | 200: updated case snapshot |
| POST /cases/{case_id}/confirm-readiness | Human readiness confirmation | 200: updated case snapshot |
| GET /cases/{case_id}/audit-events | Audit history | 200: items and next_cursor |
| GET /cases/{case_id}/reminders | Scheduled and completed follow-up | 200: items and next_cursor |
| GET /cases/{case_id}/commitments | Client commitments | 200: items and next_cursor |

Case creation takes client_id, accounting_period, timezone, owner_user_id, due_at, policy_id and requirement definitions. Validate owner access and persist the selected policy version. The backend supplies case IDs, versions and initial statuses. Do not accept caller-supplied readiness or reviewer approval.

Document upload reserves the event/run records and returns 202 only after durable file registration. Run status remains queued until extraction succeeds or a failure is recorded. A scoped client upload session must bind the authorised client/case server-side and cannot grant review permissions. Contact/policy provisioning may use an administrator-managed seed/import in the MVP; its trusted configuration is not editable by the model.

Review body: expected_state_version, review_task_id (nullable), requirement_id, decision, reason, evidence_refs. decision: accept, request_correction, waive, pause_followup, resume_followup. Final confirmation body: expected_state_version and reason. Resolve the referenced task when its issue is addressed; unresolved independent blockers still prevent confirmation.

Mutation requests carry an Idempotency-Key header. Same key and payload replay the recorded response; the same key with different payload is rejected. Keys are scoped to authenticated actor and operation.

Errors use {"error":{"code":"STALE_STATE","message":"Reload case before retrying.","retryable":false},"request_id":"request_demo"}. HTTP codes: 401 unauthenticated, 403 forbidden action, 404 absent or inaccessible resource, 409 version/idempotency conflict, 422 invalid input, 503 unavailable service.

Only trusted mail ingestion creates reply events after sender/case association checks. Do not expose an unauthenticated endpoint accepting arbitrary case IDs as replies.

## First acceptance scenarios

1. June statement for July returns needs_correction and does not satisfy July.
2. A clear submission commitment changes reminder timing, not requirement acceptance.
3. Corrected evidence resolves the relevant requirement and cancels obsolete reminders.
4. A stale finding cannot overwrite a newer human decision.
5. Duplicate events cannot create duplicate outbox actions.
6. An ambiguous delivery result is not blindly retried.
7. Cross-client evidence and model-proposed waivers are rejected.
8. A checklist with no configured requirements cannot become ready.

Each owner supplies component tests. Student 4 aggregates evaluation; final human confirmation is excluded from routine autonomous-handling steps that the system is expected to automate.
