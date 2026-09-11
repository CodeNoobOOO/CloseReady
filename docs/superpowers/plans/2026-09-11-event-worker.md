# Durable Activation and Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist case activation and execute queued LLM runs through a restart-safe worker.

**Architecture:** FastAPI queues an activation event and returns immediately. A separate worker resolves the original actor from trusted configuration, recovers expired claims conservatively, and executes one transactionally claimed run through the existing bounded runtime.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLAlchemy, SQLite, unittest.

**Spec:** `docs/superpowers/specs/2026-09-11-event-worker-design.md`

## Global Constraints

- Model output never receives database, filesystem, credential or arbitrary network authority.
- Activation never claims that a customer message was delivered.
- Existing routes and stored run records remain readable.
- Expired claimed work goes to human review instead of automatic replay.
- All writes preserve case scope, idempotency and audit records.

---

### Task 1: Queue activation through HTTP

**Files:**
- Modify: `closeready/runtime_store.py`
- Modify: `closeready/api.py`
- Test: `tests/test_event_worker.py`

**Interfaces:**
- Produces: `RuntimeStore.start(..., event_type, audit_action)` and `POST /api/v1/cases/{case_id}/activate`.

- [x] Write a failing HTTP test proving activation returns 202, records `case_activated`, remains queued and does not call the provider.
- [x] Run the focused test and confirm the route is missing.
- [x] Generalise run creation without changing the legacy synchronous route.
- [x] Add the authenticated activation route and run the focused tests.
- [x] Add idempotency, stale-state, disabled-provider and access tests.

### Task 2: Execute an existing queued run

**Files:**
- Modify: `closeready/runtime.py`
- Test: `tests/test_event_worker.py`

**Interfaces:**
- Produces: `AgentRuntime.execute(actor, run_id) -> RunRecord`.

- [x] Write failing tests for queued execution and provider configuration mismatch.
- [x] Run them and confirm `execute` is missing.
- [x] Extract existing loop execution from `analyse` and enforce recorded provider provenance.
- [x] Run focused runtime and worker tests.

### Task 3: Recover and claim work from a service loop

**Files:**
- Modify: `closeready/runtime_store.py`
- Create: `closeready/worker.py`
- Test: `tests/test_event_worker.py`

**Interfaces:**
- Produces: `RuntimeStore.expired_run_candidates()`, `RuntimeStore.queued_candidates()`, `RuntimeStore.recover_expired_system()`, `AgentWorker.run_once()` and `python -m closeready.worker`.

- [x] Write failing tests for restart processing, two-worker claim safety, expired-run recovery and removed actors.
- [x] Run them and confirm the worker does not exist.
- [x] Add read-only candidate discovery and a single-iteration worker.
- [x] Add the environment-backed CLI polling loop with bounded delay validation.
- [x] Run focused tests and existing runtime tests.

### Task 4: Document and verify the deployment boundary

**Files:**
- Modify: `README.md`
- Modify: `docs/backend.md`
- Modify: `docs/agent-runtime.md`
- Modify: `docs/business-acceptance.md`

**Interfaces:**
- Documents the activation response, API/worker commands, recovery semantics and remaining integration work.

- [x] Update implementation status and local commands without claiming deployment or delivery.
- [x] Run the complete unit suite and dependency check.
- [x] Run `git diff --check` and inspect the final diff.
