# Durable Activation and Worker Design

## Goal

Turn case analysis from a request-bound operation into a durable business event. An authorised manager activates a case, the API persists a `case_activated` event and queued run, and a separate worker executes the existing bounded LLM loop. Process failure must not lose the run or silently replay a possibly completed effect.

## Scope and ownership

This is a Student 1 increment. It owns activation, run queuing, worker claiming, conservative recovery, provider consistency and audit records. Document extraction, mail delivery, reminders and the dashboard remain with Students 2, 3 and 4.

The existing synchronous `POST /api/v1/cases/{case_id}/runs` route remains available for compatibility and local diagnosis. New product flows use activation and the worker.

## API and persistence

`POST /api/v1/cases/{case_id}/activate` accepts the existing `AnalyseRequest` body and an `Idempotency-Key`. It requires an authenticated manager with case access and an enabled live provider. It returns the persisted `RunRecord` with HTTP 202 while its status is `queued`; it does not call the provider in the request.

Activation writes the event, run and `activate_case` audit record in one transaction. Replaying the same actor, case, operation, key and state version returns the same run. A changed version returns `IDEMPOTENCY_CONFLICT`. A stale first request returns `STALE_STATE`, and a case with queued or running work returns `RUN_ACTIVE`.

The event record uses type `case_activated`. Activation does not change case state or imply that a message was sent.

## Worker lifecycle

`AgentRuntime.execute(actor, run_id)` claims and executes an already queued run. `AgentRuntime.analyse(...)` remains a compatibility wrapper that queues a `case_analysis_requested` run and executes it synchronously.

`AgentWorker.run_once()` performs two bounded steps:

1. Find expired running runs and use the existing conservative recovery transition. Each becomes `needs_review` with one assigned `INTERRUPTED_RUN` task. The worker does not replay inference or external effects.
2. Select at most one queued run, resolve its original actor from trusted access configuration, and execute it with the configured provider.

Multiple workers may observe the same queued run; the transactional claim ensures only one executes it. A worker configured with a different provider, model or live/mock mode resolves the claimed run to `needs_review` with `PROVIDER_CONFIGURATION_CHANGED` rather than silently changing recorded provenance.

The command-line worker supports `--once` for deployment checks and a bounded polling loop for service operation. Database, access and LLM configuration use the same environment variables as the API. It does not expose credentials or customer content in logs.

## Failure handling

- Provider failures retain the existing bounded retry and review behavior.
- Active leases are never recovered.
- Expired leases are not replayed automatically.
- Runs whose actor is absent, no longer a manager or no longer has the case's client grant are left queued and reported as an operational error; they are not executed with broader authority and do not block other eligible queued work.
- A worker iteration handles at most one queued run so shutdown and health supervision remain simple.

## Verification

Tests use file-backed SQLite and a scripted provider to prove: activation returns 202 without inference, restart preserves queued work, a worker completes the queued LLM flow, duplicate activation has one event/run, concurrent workers yield one claim, provider mismatch creates review, and expired work is recovered without replay.
