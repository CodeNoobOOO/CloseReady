# Async Document Ingestion Worker Design

**Status:** Implemented and locally verified on 2026-09-15.

## Goal

Allow an authorised client manager to attach a text-based PDF to one requirement in a monthly case, return immediately with a durable queued job, process the PDF in the existing worker service, persist the extraction and `DocumentFinding`, and apply a verified successful finding to the case checklist.

## Scope

This increment accepts only `application/pdf`, requires a non-empty file no larger than 5 MiB, and assumes text can be extracted by `pypdf`. It uses Student 2's deterministic extraction and assessment functions. OCR, live mailbox polling, customer portal upload, document-assessment LLM tools, explicit-item completeness, and object storage are outside this increment.

The existing case-analysis LLM loop remains unchanged. The same worker process dispatches both queued case-analysis runs and queued document jobs through separate handlers.

## API

`POST /api/v1/cases/{case_id}/documents` is manager-only and requires bearer authentication and `Idempotency-Key`. Multipart fields are `file`, `requirement_id`, and `expected_state_version`. A successful request returns HTTP 202 with a `DocumentJobRecord`. Replaying the same key and identical content returns the same record; reuse with different content returns `IDEMPOTENCY_CONFLICT`.

`GET /api/v1/cases/{case_id}/documents/{document_id}` returns document metadata without file bytes. `GET /api/v1/cases/{case_id}/document-jobs/{job_id}` exposes durable processing state. `GET /api/v1/cases/{case_id}/documents/{document_id}/finding` returns the persisted finding or 404.

## Persistence

SQLite stores five document-domain tables:

- `documents`: scoped metadata, SHA-256, and PDF bytes.
- `document_jobs`: queued/running/completed/needs_review/failed/stale state, actor, claim lease, attempts, and error.
- `document_extractions`: one immutable typed extraction artifact per processed document.
- `document_findings`: one immutable typed finding per document.
- `document_upload_responses`: request digest and response for idempotent upload replay.

The database is the hand-off boundary between API and worker. File bytes are never returned by an API and are not written to an arbitrary filesystem path. The 5 MiB limit keeps SQLite suitable for this demonstration; production deployment should replace the BLOB with encrypted object storage.

## Processing and State Authority

The worker claims one job for 300 seconds, loads the current case and PDF through the server-owned binding, calls `extract_pdf`, and calls `assess_document`. Duplicate status is derived by checking another document with the same case and file hash. Extraction and inference-like assessment happen outside the write transaction; audit records identify the system document worker while the upload record retains the authorised manager.

Completion opens a new write transaction and verifies the claim token, case scope, document/requirement bindings, and exact `input_state_version`. A stale finding cannot change the case. The application persists the finding before applying effects.

For this constrained slice:

- `satisfies`: add validated evidence to the bound requirement, set it to `accepted`, keep `reviewer_status=not_required`, increment case version, and set readiness to `ready_for_confirmation` only when every requirement is accepted or waived.
- `needs_correction`: retain the case checklist state and finish the job as `needs_review`.
- `needs_review`: retain the case checklist state and finish the job as `needs_review`.
- `unmatched`: retain the case checklist state and finish the job as `needs_review`.

Every upload and applied or blocked result writes an audit record. Existing reminder dispatch already rechecks outstanding requirements before any sandbox send and cancels an obsolete reminder. Immediate cancellation at finding-application time is deferred until a public Student 3 integration method exists.

## Recovery and Errors

A claim prevents concurrent processing. Expired document claims are returned to `queued` because extraction has no external side effect and completion is one guarded transaction. Invalid PDFs finish `failed` with `INVALID_PDF`; unexpected processing errors finish `failed` with `DOCUMENT_PROCESSING_FAILED`. A state change between context load and completion finishes `stale` and does not mutate the checklist.

## Security and Guardrails

Only managers scoped to the case client may upload or read document records. The server binds case, document, finding, actor, and version identifiers. Filenames are metadata only and never become paths. PDF content is untrusted data, has a strict type and size boundary, and is never interpreted as an instruction. Document results cannot bypass the application-owned completion transaction.

## Verification

Tests cover authentication/scope, media type and size limits, idempotency, duplicate hashes, durable job claiming/recovery, invalid extraction, stale completion, successful evidence application, case version/readiness changes, HTTP responses, worker dispatch, restart persistence, and regression of the full existing suite.
