# Case API: local development

The backend provides an authenticated FastAPI application with file-backed SQLite storage through SQLAlchemy. An optional [live analysis loop](agent-runtime.md) now calls DeepSeek and stores unsent draft/review tasks. It does not activate client communication, send mail, accept documents or confirm readiness.

## Setup on Windows

Run from the repository root. Install Python 3.11+ and then:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m unittest discover -s tests -v
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

Stop and restart the server using the same database URL: the case, version, audit history and successful idempotent responses remain. Repeating an identical mutation with its original key returns the original snapshot without another mutation. To see a stale rejection, repeat the deadline request with a new key and the old expected_state_version=1.

## Available routes

| Route | Purpose | Access |
| --- | --- | --- |
| POST /api/v1/cases | Create a configured checklist; 201 snapshot | Manager with client grant; valid owner and policy |
| GET /api/v1/cases | Scoped page; items, next_cursor | Any actor with client grant |
| GET /api/v1/cases/{case_id} | Current snapshot | Actor with client grant |
| PATCH /api/v1/cases/{case_id}/deadline | Audited deadline change; 200 snapshot | Manager with client grant |
| GET /api/v1/cases/{case_id}/audit-events | Scoped audit page | Actor with client grant |

List endpoints accept limit=1..100 (default 50). Pass next_cursor back unchanged. Case cursors are case IDs sorted lexically; audit cursors are increasing audit IDs. New insertions before a case cursor may require a fresh listing. Mutations require an Idempotency-Key of 1..128 letters, digits or `._:-`. Keys are scoped by actor and operation (including the case for deadline updates). Replays preserve the original response, which may be older than the current case; GET the case for current state.

CreateCaseRequest rejects caller-provided IDs, version, policy_version, readiness, requirement status, reviewer status and evidence. IDs are server-generated. Requirements start missing, with no evidence; readiness starts collecting. Empty checklists are rejected. Requirements may use the same document type for distinct configured accounts or items. There is currently no uniqueness restriction on client/period; an idempotency key prevents accidental request replay, not all duplicate business configuration.

Deadline updates accept only expected_state_version, due_at and reason. They increment the version even when the timestamp is unchanged. They do not alter evidence/requirement status or authorize an LLM deadline extension. This route must be extended to reconcile scheduled work before reminders/activation are implemented.

Errors use the shared error envelope and a generated request_id, also returned as X-Request-ID. Unknown and inaccessible cases both return 404. Schema failures return 422 without echoing input. Storage failures return 503; retry the same payload/key. No automatic write retry occurs inside this API.

## Access and transaction boundary

Configure one random high-entropy token per principal, stored only as its SHA-256 hash in the server access file. SHA-256 here is for random bearer tokens, not human passwords. can_manage=false grants read access within configured client_ids. Owners must be configured managers with a grant to that client. Each policy binding explicitly allows client IDs, records an approved version identity and is captured in the database when a case is created. These bindings are not the full reminder policy implementation and cannot enable sending.

Configuration is administrator-owned and loaded at startup; restart after rotating tokens or changing grants. Case records retain their original policy version. Revoking access prevents idempotent response replay for that client. HTTP callers cannot edit access grants or policy bindings. The repository assumes Principal objects came from this trusted authentication layer; it is not an untrusted tool entry point.

SQLite BEGIN IMMEDIATE serializes writers. Version comparison, snapshot update, successful audit and idempotent response commit together. A failed audit insert rolls back the snapshot and replay record. Denied/stale business mutations record a separate outcome without changing the case version. Denials with no accessible case have a null case_id and are retained internally, not exposed through another client's case audit route. HTTP authentication/schema rejections are not persisted in the business audit table in this increment.

Schema version 1 initializes a new database; future migrations require an explicit migration implementation. This increment has no migration, retention, backup or recovery administration commands. JSON snapshots are internal storage, not a public database interface. Read/write through the API rather than sharing the SQLite file with group members.

## Scope and deployment limits

Tests use real file-backed SQLite transactions and the ASGI HTTP boundary, including restart/reopen, concurrent writes, rollback, idempotency and access denial. They do not prove deployed network access, LLM business accuracy or delivery behavior.

For Lightsail, use `.venv/bin/python` and an absolute persistent database path. This API has not been deployed: keep local development bound to loopback until TLS, secret provisioning, access logging, resource limits, backups and deployment tests are in place. The analysis runtime adds durable events and a limited action gate; a complete runtime still needs evidence ownership/verification, human review resolution, reminder reconciliation, mail integration and asynchronous worker recovery.
