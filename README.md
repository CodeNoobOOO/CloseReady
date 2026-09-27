# CloseReady
Agentic document collection and bookkeeping readiness for accounting firms.

## Current status

An authenticated FastAPI backend persists cases, deadline changes, audit records and idempotent responses in SQLite. New bank-statement requirements require an account suffix and store only the canonical masked value, never a full account number. Managers can upload bounded PDFs into a durable queue. Embedded text is preferred; image-only PDFs use a local Tesseract OCR fallback in the worker, while low-confidence or incomplete OCR remains unresolved for human review. The worker runs Student 2's document analysis, and application code matches only one authorised outstanding Requirement from the extracted type, period, entity, masked account and configured item references. One candidate is bound directly; with several candidates, only a unique structured match is bound. Zero or multiple matches remain for human review. Each configured invoice or receipt reference is stored as its own Requirement and may be accepted automatically only when the type, entity, period, exact reference and cited evidence all pass deterministic checks. Legacy multi-reference requirements and other supporting documents remain subject to human review. The application stores evidence and applies only a current, fully verified `satisfies` result through the transaction gate. Managers can list documents and resolve uncertain findings by accepting readable evidence for a selected requirement, rejecting the document, or reassigning it for processing. Accepted evidence immediately cancels related unsent reminders. A successful initial document-request send schedules the first follow-up reminder. If the client does not reply, further reminders follow `min_reminder_interval_hours`. Open overlapping reviews or an unassessed client reply persist affected reminders as `paused`; resolution resumes eligible reminders. Reminder attempts are counted separately for each Requirement, and reaching `max_reminders_per_requirement` opens an assigned `REMINDER_LIMIT` review without suppressing other eligible items in a mixed reminder. Failed or unknown delivery opens an assigned review and is not resent automatically. The Case owner or assigned reviewer can approve a new attempt after confirmed failure through a new outbox item and Idempotency-Key, or reconcile an unknown delivery as delivered, not delivered with a new attempt, or still unresolved. Retries reference the resolved delivery review, and reminder resends count toward the limit. Once every requirement is accepted or waived, a separate manager action confirms the case as `ready`. The same worker also executes queued LLM case analysis; the configurable tool loop reads the checklist and records a validated, assigned unsent draft/review task. DeepSeek has passed a live round-trip check; the organiser gateway has a dedicated Ollama-protocol adapter and requires the deployed two-request check before it is treated as verified. An assigned manager can approve, safely edit, reject or dismiss that task, and approval atomically creates a durable reviewed outbox record. With `CLOSEREADY_MAIL_BACKEND=test_sink` the outbox can be sandbox-delivered; with `smtp` a manager-approved message is sent to an approved mailbox. Client replies are associated using a stored thread mapping or the customer-visible Case reference, never by sender address alone. PDF attachments are submitted through the existing document upload API. See [backend setup](docs/backend.md), [communication](docs/communication.md), [team model configuration](docs/llm-providers.md), [live agent setup and evidence](docs/agent-runtime.md) and [Python models](docs/python-models.md). Photo upload, handwriting recognition, bounce reconciliation and deployment remain incomplete; a sandbox mailbox row is not evidence that mail was sent.

Each Case also has a server-generated customer-visible communication reference. Student 3 can use the shared resolver to associate an inbound reply only when that reference and an approved sender match; unmatched mail is never assigned by guessing from the sender alone.

## Python connectivity check

Python 3.11+; this diagnostic uses only the standard library, with no package installation required. It is not the production agent runtime. Keep DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL in the ignored root .env file. Optional DEEPSEEK_MODEL selects a model; --model overrides it. This small parser supports plain or quoted KEY=value lines, not dotenv interpolation or inline comments.

```sh
python -m unittest discover -s tests -p test_deepseek_probe.py -v
python scripts/deepseek_probe.py --list-models
python scripts/deepseek_probe.py --model deepseek-flash
```

Live commands use your DeepSeek account and inference consumes API credit. Requests are restricted to the official HTTPS host with redirects disabled. The probe permits only a read of synthetic case_probe, validates arguments, and verifies that the model returns a fresh marker supplied only through the tool result. It does not send mail or mutate cases. Errors fail explicitly without printing provider bodies or credentials; no retry or mock fallback is performed.

Verified on 2026-09-10: deepseek-flash completed two native Chat Completions requests with tool-result correlation; reported usage was 824 total tokens. This proves one connectivity/tool-use path, not business accuracy or reliability. The observed model list also included deepseek-v4-pro. The organiser's gateway remains separate and is not used by this probe.

## Start here

The dashboard is served at `/app` on the same origin. Connect with a locally configured bearer token. It supports case checklists, PDF uploads and processing, evidence review, final readiness confirmation, reviewed messages, sandbox replies, reminders, run traces and audit history. See [frontend guide](docs/frontend.md) and [evaluation protocol](docs/evaluation.md). Follow-up reminders pause for overlapping reviews or unassessed replies and resume only after the blocking condition is resolved.

- [Shared contracts v1.2](docs/contracts.md): data formats and implemented APIs.
- [Communication](docs/communication.md): sandbox and SMTP/IMAP configuration.
- [Real LLM runtime](docs/llm-runtime.md): provider adapter, tool loop, errors, live mail and deployment requirements.
- [Business acceptance](docs/business-acceptance.md): source review, seven rubric areas and business-connected acceptance scenarios.
- [Single-host deployment](deploy/README.md): container topology, health checks, secret boundary, backup, restore and rollback.
- [Synthetic examples](examples/README.md): fixtures for parallel development before integrations exist.

## Team ownership

| Student | Area |
| --- | --- |
| 1 | Core workflow, APIs, state, runtime and execution controls |
| 2 | Document upload, extraction and evidence assessments |
| 3 | Client replies, commitments, reminders and mail integration |
| 4 | Dashboard, human review, audit display and evaluation |

Use short-lived task branches from main and pull requests for integration. Announce contract changes before merging. Each owner supplies component tests and setup instructions; evaluation is shared work.

## Delivery scope

Start with a single-agent baseline. Time-box specialist-agent experiments after the baseline works, then stabilise one configuration. Both use the same business interfaces and application-level controls. No user-facing mode switch is required.

Keep real credentials, client documents and local databases out of Git. Commit only synthetic fixtures and configuration examples without secrets.
