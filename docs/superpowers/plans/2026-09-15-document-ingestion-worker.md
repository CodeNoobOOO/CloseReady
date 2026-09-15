# Async Document Ingestion Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Queue an authorised text-PDF upload, process it durably in the existing worker service, persist its finding, and apply verified evidence to the monthly case checklist.

**Architecture:** A focused `DocumentStore` owns document BLOBs, jobs, findings, claims and application transactions. FastAPI supplies the authenticated multipart boundary; `DocumentProcessor` calls Student 2's pure extraction/assessment functions; `AgentWorker` dispatches the document handler after checking existing agent work.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, SQLAlchemy 2, SQLite, pypdf, python-multipart, pytest/unittest.

**Spec:** `docs/superpowers/specs/2026-09-15-document-ingestion-worker-design.md`

## Global Constraints

- Accept only non-empty `application/pdf` content up to 5 MiB.
- Store bytes only in the scoped SQLite document table for this increment; never derive a filesystem path from a filename.
- Model or document text never supplies authoritative case, actor, document, finding, requirement or version identifiers.
- Only an exact, current, validated `satisfies` finding can accept a requirement.
- Do not change the existing case-analysis LLM tool loop.
- Do not add OCR, mailbox polling, customer portal upload, explicit-item assessment or a document LLM loop in this increment.

---

### Task 1: Durable document contracts and upload queue

**Files:**
- Modify: `closeready/document_models.py`
- Create: `closeready/document_store.py`
- Test: `tests/test_document_store.py`
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `DocumentRecord`, `DocumentJobRecord`, `DocumentUploadRequest`, `DocumentStore.upload(...)`, `get_document(...)`, `get_job(...)`, and `get_finding(...)`.
- Consumes: `Store`, `CaseSnapshot`, Student 2's `calculate_file_hash`, and existing access/idempotency patterns.

- [x] Write tests for authorised queueing, foreign requirement rejection, media/size rejection, replay, conflicting replay, and duplicate-hash metadata.
- [x] Run `pytest tests/test_document_store.py -q` and confirm failures because the store/contracts do not exist.
- [x] Add typed records, durable document/job/extraction/finding/idempotency tables, bounded validation, scoped reads and an idempotent upload transaction.
- [x] Add `python-multipart` as a runtime dependency; add `pytest` in a separate development requirements file.
- [x] Run the full suite through `pytest` in CI so Student 2's pytest-style tests are collected.
- [x] Run `pytest tests/test_document_store.py -q` and confirm it passes.

### Task 2: Claim, recovery and document processing

**Files:**
- Modify: `closeready/document_store.py`
- Create: `closeready/document_processor.py`
- Test: `tests/test_document_worker.py`

**Interfaces:**
- Consumes: `DocumentStore.processing_context(...)`, `extract_pdf(bytes)`, `assess_document(...)`.
- Produces: `queued_candidates()`, `expired_job_candidates()`, `claim(...)`, `recover_expired_system(...)`, `complete(...)`, `fail(...)`, and `DocumentProcessor.execute_claimed(...)`.

- [x] Write tests proving exclusive claim, expired recovery, persisted extraction/finding, invalid-PDF failure, stale completion, and successful Requirement acceptance.
- [x] Run `pytest tests/test_document_worker.py -q` and confirm failures for missing processing interfaces.
- [x] Implement 300-second leases and server-side candidate/context lookup.
- [x] Implement injected extractor/assessor processing and guarded completion transaction.
- [x] Recompute `ready_for_confirmation` only when all requirements are resolved; write audits for applied and blocked outcomes.
- [x] Run `pytest tests/test_document_worker.py -q` and confirm it passes.

### Task 3: HTTP upload and observation endpoints

**Files:**
- Modify: `closeready/api.py`
- Test: `tests/test_document_api.py`

**Interfaces:**
- Consumes: `DocumentStore.upload`, `get_document`, `get_job`, and `get_finding`.
- Produces: POST `/api/v1/cases/{case_id}/documents` and three scoped GET endpoints from the spec.

- [x] Write HTTP tests for 202 queueing, authentication, stale version, invalid content type, oversize content, replay and scoped reads.
- [x] Run `pytest tests/test_document_api.py -q` and confirm 404/missing-route failures.
- [x] Construct `DocumentStore` in `create_app`, expose it in `app.state`, read at most 5 MiB plus one byte, and add typed routes.
- [x] Include document tables in readiness checks.
- [x] Run `pytest tests/test_document_api.py -q` and confirm it passes.

### Task 4: Dispatch document jobs in the existing worker service

**Files:**
- Modify: `closeready/worker.py`
- Test: `tests/test_document_worker.py`
- Modify: `tests/test_event_worker.py`

**Interfaces:**
- Consumes: `DocumentStore` candidates/claims and `DocumentProcessor.execute_claimed`.
- Produces: one `AgentWorker.run_once()` iteration that handles at most one agent run or one document job without mixing their execution logic.

- [x] Add a failing test that queues through `DocumentStore`, runs one worker iteration and observes a completed persisted finding and updated Case.
- [x] Run the focused worker tests and confirm the new test fails before dispatch exists.
- [x] Add optional document dependencies to `AgentWorker`; preserve all existing constructor behavior and process document jobs when no agent run was executed.
- [x] Construct the document dependencies in `worker_from_environment` and dispose the shared engine once.
- [x] Run `pytest tests/test_event_worker.py tests/test_document_worker.py -q` and confirm both suites pass.

### Task 5: Documentation and integrated verification

**Files:**
- Modify: `README.md`
- Modify: `docs/backend.md`
- Modify: `docs/contracts.md`
- Create: `examples/upload-document.ps1`

**Interfaces:**
- Documents the exact API, state meanings, local PowerShell demonstration and remaining limitations.

- [x] Update status text so document upload and deterministic assessment are implemented while OCR, document LLM assessment and live mailbox ingestion remain explicit gaps.
- [x] Add commands for create Case, multipart upload, poll job, read finding and confirm changed Case.
- [x] Run `python -m pytest -q`, `python -m pip check`, `python -m compileall -q closeready tests`, and `git diff --check`.
- [x] Inspect `git status` to ensure `.env`, databases, uploaded PDFs and virtual environments are absent.
