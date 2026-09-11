# First live case-analysis loop

This increment connects the authenticated case API to a configured LLMProvider tool loop and SQLite action records. DeepSeek is the live-verified adapter; an additional compatible Chat Completions adapter is configurable. See [team provider configuration](llm-providers.md). An authorised activation persists a queued run, and a separate worker reads the checklist and can generate a guarded unsent request draft or an assigned human review task. Assigned managers can resolve communication/error tasks, and approval creates a durable reviewed outbox record. It cannot yet assess uploaded documents, interpret replies, resolve contacts, send mail, schedule follow-ups or confirm readiness.

## Enable it locally

Complete docs/backend.md first. In the server terminal, add:

```powershell
$env:CLOSEREADY_LLM_ENABLED = '1'
$env:CLOSEREADY_LLM_ENV_FILE = '.env'
$env:DEEPSEEK_MODEL = 'deepseek-flash'
.venv/Scripts/python -m uvicorn closeready.api:from_env --factory --host 127.0.0.1 --port 8000
```

Start a second terminal with the same environment and run:

```powershell
.venv/Scripts/python -m closeready.worker
```

Use `--once` to recover expired work and process at most one queued run before exiting. The continuous worker polls every two seconds by default; `--poll-seconds` accepts 0.1 through 60.

For the DeepSeek example above, keep DEEPSEEK_API_KEY in the ignored .env. For a different provider use the LLM_* settings in the provider guide. Environment variables override the same variable names in the file. The optional parser supports plain or quoted KEY=value lines, not interpolation or inline comments. Endpoint validation belongs to the selected adapter; redirects are always refused. Missing/invalid configuration with LLM_ENABLED=1 fails startup; with LLM disabled, case CRUD remains available and starting analysis returns 503. There is no production mock-provider selector or fixture fallback.

In the terminal holding your local API token and $createdCase from the backend guide:

```powershell
$currentCase = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)" -Headers $apiHeaders
$apiHeaders['Idempotency-Key'] = 'activation-demo-1'
$analysisBody = @{ expected_state_version = $currentCase.state_version } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/activate" -Headers $apiHeaders -ContentType 'application/json' -Body $analysisBody
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/runs/$($run.run_id)" -Headers $apiHeaders
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/review-tasks" -Headers $apiHeaders
```

POST /cases/{case_id}/activate returns 202 with a queued run and does not call the model in the request. The worker processes it independently, so poll GET /runs/{run_id}. Activation starts analysis for an initial request but does not send client communication. POST /cases/{case_id}/runs remains a synchronous diagnostic route returning 200 with the terminal/current run record. All paths have prefix /api/v1 and mutation requests require a manager grant plus Idempotency-Key.

## What is implemented

1. An authorised request creates a durable case_analysis_requested event and queued run. The expected version is validated; a second active run for the same case is rejected.
2. One claimant takes a 300-second lease. The runtime supplies trusted instructions and only two tools: get_case_context with no arguments, and propose_action with typed ActionContent.
3. The model must read context before proposing. Its action cannot contain server IDs or version; the application binds these to the exact returned snapshot. Scope, current version and requirement references are checked again inside the transaction.
4. request_documents/request_clarification targeting outstanding items produce an open ReviewTask with the draft, assigned_to, sent=false and reason_code=MAIL_NOT_CONFIGURED. The proposal outcome is blocked, not queued or sent. The run normally ends needs_review.
5. create_review_task stores its explanation for the reviewer. no_action is accepted only for a nonempty resolved checklist with no review blockers. All other action types and unavailable evidence/finding references are rejected. No generic state-write or sending tool exists.
6. Proposal, review task, state-version increment and audit are committed atomically. A new review blocker resets readiness to collecting without changing requirement acceptance. The tool result is returned to the model and a final acknowledgement ends the loop; free-text claims of sending have no authority and are not exposed as execution results.

Default limits: six provider attempts including at most one transient retry, one invalid-output/tool repair, 1,200 output tokens per request, 30-second socket timeout, 64,000 serialized conversation characters and 48,000 context-result characters. Oversized context is rejected without silently truncating it. These character limits are not a tokenizer-based input-token budget. Socket timeout is not a hard wall-clock cancellation guarantee; the lease prevents an expired worker from committing effects. Providers returning errors never trigger a mock fallback.

A rejected call aborts the remaining calls in that assistant batch. Unknown tools and arbitrary case IDs are rejected. Stale proposals end stale with an assigned review; the runtime does not automatically apply the proposal against newer state. API/auth/provider errors use safe codes; raw provider bodies and credentials are not returned.

The persisted model_trace includes provider/model, prompt/schema version on the run, per-attempt latency, usage (null when unavailable), provider request ID, tool names/call IDs, outcome summaries and errors. Validated proposal content and draft text are stored in separate business records. Raw prompts, rejected argument bodies, final narrative and hidden reasoning are not retained. This is concise execution tracing, not a full prompt archive.

## Idempotency and recovery

Repeating the same actor/case/key/version returns the existing run without starting another completed analysis. If the first HTTP response was lost, repeating the same request may return running with the run ID; poll GET /runs/{run_id}. A queued run can be claimed by retrying the original request with its original key. Different input with the same key returns 409.

For an interrupted running process, wait until its 300-second lease expires, then explicitly recover it:

```powershell
$apiHeaders['Idempotency-Key'] = 'recover-demo-1'
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/runs/$($run.run_id)/recover" -Headers $apiHeaders
```

Recovery requires the run's configured manager with case access. The worker scans expired running work before claiming another queued run; manual recovery remains available. Recovery marks interrupted work needs_review and ensures one assigned task for that run. It does not resend, rerun inference or erase committed effects. The transition is intrinsically idempotent; repeating recovery on a terminal run has no effect. Active leases return 409. A recovered worker cannot subsequently apply a proposal with its old claim.

Review tasks expose cursor pagination (limit 1..100), reason, optional draft, resolution metadata and fixed sent=false. The assigned manager can approve, edit and approve, reject, or dismiss an operational-error task through the authenticated review-decisions endpoint. Approval creates one durable `pending_reviewed_delivery` outbox record with `delivery_status=not_attempted`; it does not resolve a recipient or send mail. New analysis with a new key is a new explicit operation and may create another review task; deduplication across distinct business events is still required before a pilot.

## Reproduce the live synthetic check

```powershell
.venv/Scripts/python -m scripts.live_case_analysis --model deepseek-flash
```

This performs real calls using the selected provider and a new synthetic case with a bank coverage requirement plus two explicit invoice items, through the ASGI API boundary. It writes a dedicated database and summary under ignored local-data/live-analysis-*. No actual HTTP server deployment or mail transmission is part of this check. The --model value must belong to your selected service.

On 2026-09-10, an initial sandbox-restricted attempt failed with NETWORK_ERROR, exhausted its two attempts and produced an assigned failure task. After network permission was granted, the live run passed: three inference attempts, 10,924 reported total tokens, one unsent draft covering both checklist requirements, state_version=2 and both requirements still missing. The draft named July statement coverage and both configured invoice references. This demonstrates one live checklist-analysis path, not document understanding accuracy or an evaluation benchmark. Later additions to task explanations/call-ID tracing were validated locally without repeating the paid inference.

Deterministic tests use an explicitly named scripted_test provider and record live=false. They test controls and failure paths, not model reasoning. Existing core tables remain at schema version 1; runtime tables are additive in this development increment. Future schema changes need a proper migration strategy before deployment.

Next integration boundaries: Student 2 supplies scoped document artifacts and DocumentAssessment; Student 3 supplies reply interpretation, approved communication policy, contact resolution and transport against the reviewed outbox; Student 4 consumes runs/review tasks/outbox alongside cases/audit and invokes review decisions. Do not bypass the central action gate when integrating these modules.
