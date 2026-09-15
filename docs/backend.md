# Case API: local development

The backend provides an authenticated FastAPI application with file-backed SQLite storage through SQLAlchemy. A manager can persist a bounded text-based PDF and its processing job before the existing worker extracts or assesses it. A current finding with verified type, coverage, entity, account and evidence can update the bound requirement through an optimistic transaction; all other results remain unresolved. An authorised activation also persists a queued LLM run for the same separately supervised worker. Assigned managers can resolve communication draft tasks; approving a draft creates a durable reviewed outbox record. Activation does not itself communicate with a client, resolve a recipient, send mail or confirm readiness.

## Setup on Windows

Run from the repository root. Install Python 3.11+ and then:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
```

Create a local access configuration. The committed example is synthetic and has no usable token. The following generates a random API token in a shell variable and puts only its SHA-256 hash into an ignored local file. Run the generation once for this local configuration; keep the terminal open for the request examples. Repeating it rotates access for this user.

```powershell
New-Item -ItemType Directory -Force local-data | Out-Null
$apiToken = .venv/Scripts/python -c "import secrets; print(secrets.token_urlsafe(32))"
$hashProvider = [System.Security.Cryptography.SHA256]::Create()
$tokenHash = [BitConverter]::ToString($hashProvider.ComputeHash([Text.Encoding]::UTF8.GetBytes($apiToken))).Replace('-', '').ToLowerInvariant()
$hashProvider.Dispose()
$accessConfig = Get-Content examples/server-config.json -Raw | ConvertFrom-Json
$accessConfig.principals[0].token_sha256 = $tokenHash
$accessConfig | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 local-data/server-config.json
```

In a second terminal, from the same repository root:

```powershell
$env:CLOSEREADY_ACCESS_CONFIG = 'local-data/server-config.json'
$env:CLOSEREADY_DATABASE_URL = 'sqlite:///local-data/closeready.db'
.venv/Scripts/python -m uvicorn closeready.api:from_env --factory --host 127.0.0.1 --port 8000
```

The application reads these two process environment variables; it does not automatically load `.env`. The parent directory of the database must exist. Missing or invalid access configuration stops startup; there is no default credential. The LLM provider's API key is separate and cannot authenticate to this API.

Open http://127.0.0.1:8000/docs for API schemas. Use the Authorize button with the local API token if trying requests there. Documentation contains no case data; business endpoints require authentication. CORS is not enabled yet; use a same-origin frontend proxy for browser integration.

Service supervisors may call `GET /health/live` and `GET /health/ready` without authentication. These endpoints return only process/storage status, never case or provider information. Readiness validates all application tables and performs a rollback-only write probe without calling the LLM.

## Try the complete persistence path

In the original terminal holding `$apiToken`:

```powershell
$apiHeaders = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = 'create-demo-1' }
$createdCase = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/cases' -Headers $apiHeaders -ContentType 'application/json' -Body (Get-Content examples/create-case.json -Raw)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)" -Headers $apiHeaders

$apiHeaders['Idempotency-Key'] = 'deadline-demo-1'
$deadlineBody = @{ expected_state_version = 1; due_at = '2026-09-20T09:00:00+08:00'; reason = 'Manager approved a revised collection deadline.' } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/deadline" -Headers $apiHeaders -ContentType 'application/json' -Body $deadlineBody
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/audit-events" -Headers $apiHeaders
```

## Review a draft and create a durable outbox record

After a live or scripted analysis has created a safe draft, always reload the case and open tasks rather than copying a stale version or ID:

```powershell
$currentCase = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)" -Headers $apiHeaders
$openTask = (Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/review-tasks" -Headers $apiHeaders).items |
    Where-Object { $_.status -eq 'open' -and $null -ne $_.draft } |
    Select-Object -First 1

$apiHeaders['Idempotency-Key'] = 'approve-draft-demo-1'
$decisionBody = @{
    expected_state_version = $currentCase.state_version
    review_task_id = $openTask.review_task_id
    decision = 'approve_draft'
    reason = 'Reviewed and approved by the assigned account manager.'
} | ConvertTo-Json

$resolved = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/review-decisions" `
    -Headers $apiHeaders -ContentType 'application/json' -Body $decisionBody
$outboxPage = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/cases/$($createdCase.case_id)/outbox" -Headers $apiHeaders
$resolved | ConvertTo-Json -Depth 12
$outboxPage | ConvertTo-Json -Depth 12
```

The resolved task records the human decision. The outbox item has `status=pending_reviewed_delivery` and starts with `delivery_status=not_attempted` and no recipient. That is not a send. To exercise the Week 1 sandbox path, set `CLOSEREADY_MAIL_BACKEND=test_sink`, restart the API, and follow [sandbox communication](communication.md). Use `edit_and_approve` with `edited_draft`, `reject_draft` for a task containing a draft, or `dismiss_error` for an operational task without a draft. Decisions are terminal and require the exact current case version.

Stop and restart the server using the same database URL: the case, version, audit history and successful idempotent responses remain. Repeating an identical mutation with its original key returns the original snapshot without another mutation. To see a stale rejection, repeat the deadline request with a new key and the old expected_state_version=1.

## Upload and process a text-based PDF

With the API running, start the worker in another terminal using the same access configuration and database. Set the LLM provider variables described in [agent runtime](agent-runtime.md); the worker handles both LLM runs and document jobs, one durable item per iteration.

```powershell
$env:CLOSEREADY_ACCESS_CONFIG = 'local-data/server-config.json'
$env:CLOSEREADY_DATABASE_URL = 'sqlite:///local-data/closeready.db'
$env:CLOSEREADY_LLM_ENABLED = '1'
.venv/Scripts/python -m closeready.worker --poll-seconds 1
```

Then run `examples/upload-document.ps1` from the API terminal. Supply the current case and requirement IDs returned by the API; these server-owned IDs are never taken from PDF text.

```powershell
./examples/upload-document.ps1 -ApiToken $apiToken -CaseId $createdCase.case_id `
    -RequirementId $createdCase.requirements[0].requirement_id `
    -ExpectedStateVersion $createdCase.state_version -PdfPath 'C:/temp/july-statement.pdf'
```

The script queues the upload, polls its job and reads the finding and current Case. A `completed` job means the application accepted verified evidence. `needs_review`, `failed` and `stale` leave the requirement unresolved. This increment accepts only non-empty `application/pdf` uploads up to 5 MiB and only extracts embedded text; scanned PDFs require future OCR and manual review.

## Available routes

| Route | Purpose | Access |
| --- | --- | --- |
| GET /health/live | Process liveness; no business data | Public for service supervision |
| GET /health/ready | Database/schema readiness; no LLM call | Public for service supervision |
| POST /api/v1/cases | Create a configured checklist; 201 snapshot | Manager with client grant; valid owner and policy |
| GET /api/v1/cases | Scoped page; items, next_cursor | Any actor with client grant |
| GET /api/v1/cases/{case_id} | Current snapshot | Actor with client grant |
| GET /api/v1/cases/{case_id}/communication-reference | Customer-visible mail reference | Actor with client grant |
| PATCH /api/v1/cases/{case_id}/deadline | Audited deadline change; 200 snapshot | Manager with client grant |
| GET /api/v1/cases/{case_id}/audit-events | Scoped audit page | Actor with client grant |
| POST /api/v1/cases/{case_id}/documents | Persist a text-PDF and queue processing; 202 job | Manager with client grant |
| GET /api/v1/cases/{case_id}/documents/{document_id} | Document metadata without file bytes | Actor with client grant |
| GET /api/v1/cases/{case_id}/document-jobs/{job_id} | Poll durable processing status | Actor with client grant |
| GET /api/v1/cases/{case_id}/documents/{document_id}/finding | Read a completed assessment | Actor with client grant |
| POST /api/v1/cases/{case_id}/activate | Persist case_activated event and queued run; 202 | Manager with client grant; configured provider |
| GET /api/v1/cases/{case_id}/review-tasks | Open and resolved review tasks | Actor with client grant |
| POST /api/v1/cases/{case_id}/review-decisions | Resolve assigned draft/error review; may create reviewed outbox | Assigned manager |
| GET /api/v1/cases/{case_id}/outbox | Scoped reviewed messages | Actor with client grant |
| POST /api/v1/cases/{case_id}/outbox/{outbox_id}/deliver | Sandbox delivery of an approved outbox item | Assigned manager; `test_sink` |
| GET /api/v1/cases/{case_id}/mailbox | Labeled sandbox messages; `live=false` | Actor with client grant; `test_sink` |
| POST /api/v1/cases/{case_id}/replies | Trusted reply ingest | Manager with client grant |
| GET /api/v1/cases/{case_id}/replies | Associated replies | Actor with client grant |
| POST /api/v1/cases/{case_id}/replies/{reply_id}/assess | Reply assessment + commitment/reminder effects | Manager; LLM enabled |
| GET /api/v1/cases/{case_id}/findings | Reply assessments | Actor with client grant |
| GET /api/v1/cases/{case_id}/commitments | Recorded commitments | Actor with client grant |
| GET /api/v1/cases/{case_id}/reminders | Follow-up schedule | Actor with client grant |
| POST /api/v1/cases/{case_id}/reminders/dispatch-due | Dispatch due sandbox reminders | Manager; `test_sink` |

List endpoints accept limit=1..100 (default 50). Pass next_cursor back unchanged. Case cursors are case IDs sorted lexically; audit cursors are increasing audit IDs. New insertions before a case cursor may require a fresh listing. Mutations require an Idempotency-Key of 1..128 letters, digits or `._:-`. Document upload also requires multipart fields `file`, `expected_state_version` and optional `requirement_id`. Keys are scoped by actor and operation. Replays preserve the original response, which may be older than the current case; GET the case for current state.

CreateCaseRequest rejects caller-provided IDs, version, policy_version, readiness, requirement status, reviewer status and evidence. IDs are server-generated. Requirements start missing, with no evidence; readiness starts collecting. Empty checklists are rejected. Requirements may use the same document type for distinct configured accounts or items. There is currently no uniqueness restriction on client/period; an idempotency key prevents accidental request replay, not all duplicate business configuration.

Deadline updates accept only expected_state_version, due_at and reason. They increment the version even when the timestamp is unchanged. They do not alter evidence/requirement status or authorize an LLM deadline extension. This route must be extended to reconcile scheduled work before reminders/activation are implemented.

Errors use the shared error envelope and a generated request_id, also returned as X-Request-ID. Unknown and inaccessible cases both return 404. Schema failures return 422 without echoing input. Storage failures return 503; retry the same payload/key. No automatic write retry occurs inside this API.

## Access and transaction boundary

Configure one random high-entropy token per principal, stored only as its SHA-256 hash in the server access file. SHA-256 here is for random bearer tokens, not human passwords. can_manage=false grants read access within configured client_ids. Owners must be configured managers with a grant to that client. Each policy binding explicitly allows client IDs, records an approved version identity and is captured in the database when a case is created. These bindings are not editable by HTTP callers. Reminder intervals, sending windows and approved recipients live on `communication_policies` and `contacts` in the same access file; see [sandbox communication](communication.md). Without those records and `CLOSEREADY_MAIL_BACKEND=test_sink`, approval still cannot send.

Configuration is administrator-owned and loaded at startup; restart after rotating tokens or changing grants. Case records retain their original policy version. Revoking access prevents idempotent response replay for that client. HTTP callers cannot edit access grants or policy bindings. The repository assumes Principal objects came from this trusted authentication layer; it is not an untrusted tool entry point.

SQLite BEGIN IMMEDIATE serializes writers. Version comparison, snapshot update, successful audit and idempotent response commit together. A failed audit insert rolls back the snapshot and replay record. Denied/stale business mutations record a separate outcome without changing the case version. Denials with no accessible case have a null case_id and are retained internally, not exposed through another client's case audit route. HTTP authentication/schema rejections are not persisted in the business audit table in this increment.

Schema version 1 initializes a new database; future migrations require an explicit migration implementation. This increment has no migration or retention administration commands. The [single-host deployment runbook](../deploy/README.md) documents an operator-controlled SQLite backup and restore drill. JSON snapshots are internal storage, not a public database interface. Read/write through the API rather than sharing the SQLite file with group members.

## Scope and deployment limits

Tests use real file-backed SQLite transactions and the ASGI HTTP boundary, including restart/reopen, concurrent writes, rollback, idempotency and access denial. They do not prove deployed network access, LLM business accuracy or delivery behavior.

The repository now includes a non-root image and a single-host Compose topology that runs the API and `python -m closeready.worker` as separately supervised services against one persistent volume. See the [deployment runbook](../deploy/README.md). The application has not yet been deployed to Lightsail: external assessment still requires TLS termination, firewall rules, host secret provisioning, encrypted off-host backups and a deployed restart test. The runtime now queues and recovers analysis and document work, stores deterministic PDF findings and can sandbox-deliver a reviewed request when `test_sink` is enabled. A complete business workflow still needs OCR and richer document rules, document-review resolution, live mail transport and an actual Lightsail deployment.
