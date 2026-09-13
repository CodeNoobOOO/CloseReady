# CloseReady
Agentic document collection and bookkeeping readiness for accounting firms.

## Current status

An authenticated FastAPI backend persists cases, deadline changes, audit records and idempotent responses in SQLite. An authorised activation queues a durable event and run for a separately supervised worker; the configurable LLM loop reads the checklist and records a validated, assigned unsent draft/review task. DeepSeek has been live-tested. An assigned manager can approve, safely edit, reject or dismiss that task, and approval atomically creates a durable reviewed outbox record. With `CLOSEREADY_MAIL_BACKEND=test_sink` and administrator contacts, Student 3 can sandbox-deliver that outbox item, ingest a trusted reply, assess it, record a commitment and schedule or cancel reminders. The sandbox is labeled and is not live mail. See [backend setup](docs/backend.md), [sandbox communication](docs/communication.md), [team model configuration](docs/llm-providers.md), [live agent setup and evidence](docs/agent-runtime.md) and [Python models](docs/python-models.md). Document assessment, live mail transport and deployment remain unimplemented; a sandbox mailbox row is not evidence that mail was sent.

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

- [Shared contracts v0.8](docs/contracts.md): data formats, module boundaries, implemented/proposed HTTP APIs and validation rules.
- [Sandbox communication](docs/communication.md): approved contacts, test_sink mailbox, reply ingest, commitments and reminders.
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
