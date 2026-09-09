# CloseReady
Agentic document collection and bookkeeping readiness for accounting firms.

## Current status

Contract-first project setup targeting a real LLM-powered business workflow. No application, live API, model integration or deployment exists yet. Fixtures support parallel development and tests; final acceptance requires real model inference, persisted state and actual restricted-recipient mail integration. Technology choices and account access must be verified before implementation.

## Start here

- [Shared contracts v0.2](docs/contracts.md): data formats, module boundaries, proposed HTTP APIs and validation rules.
- [Real LLM runtime](docs/llm-runtime.md): provider adapter, tool loop, errors, live mail and deployment requirements.
- [Business acceptance](docs/business-acceptance.md): source review, seven rubric areas and business-connected acceptance scenarios.
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
