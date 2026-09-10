# CloseReady
Agentic document collection and bookkeeping readiness for accounting firms.

## Current status

Contract-first project setup targeting a real LLM-powered business workflow. Core Python boundary models now validate cases, requirements, evidence references and action proposals; see [Python model setup and usage](docs/python-models.md). A Python live DeepSeek connectivity/tool-call probe has passed a synthetic round-trip check. No business backend, database, mail integration or deployment exists yet. Fixtures support parallel development and tests; full acceptance still requires persisted business state and actual restricted-recipient mail integration.

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

- [Shared contracts v0.3](docs/contracts.md): data formats, module boundaries, proposed HTTP APIs and validation rules.
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
