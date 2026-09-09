# Real LLM runtime and integration contract

Status: implementation design, not implemented or live-tested. Owner: Student 1. Source intent: Problem.docx, judging rubric.docx, plan b.docx and the four-student Plan B execution plan.

## Required outcome

The baseline must call an authenticated external LLM API to interpret unfamiliar document evidence and client replies, select permitted tools, and choose the next business action. A scripted if/else demo or a fixture replay is not an accepted implementation. Deterministic rules enforce authority and completeness; they do not replace semantic interpretation.

Student 1 owns the provider adapter and bounded tool loop. Student 2 supplies extraction, evidence mappings and document-assessment prompts/contracts. Student 3 supplies reply/drafting prompts, communication tools and mail integration. In baseline mode there is one agent control loop; specialist reasoning loops are enabled only in the candidate multi-agent mode.

## Configuration and provider adapter

Separate runtime environment (development/test/pilot), agent mode (single/multi), LLM backend (live/mock) and mail backend (provider/test_sink). Multi-agent is not synonymous with live operation. Pilot startup must refuse mock inference or a test-only mail sink; missing credentials must fail explicitly, never silently load fixture responses.

Required configuration: provider identifier, model identifier, approved region/base endpoint where relevant, secret reference, request timeout, maximum model steps, maximum repair attempts, transient retry limit, context/output token budgets and run budget. Validate provider availability using an actual tool-call round trip. Record selected service/framework/model and supported capabilities before implementing provider-specific behaviour.

Provider adapter input: trusted instructions, role-labelled conversation, server-generated tool definitions, output schema and configured limits. Output: assistant text, tool calls with call IDs/name/arguments, usage where available, provider request ID, finish status and model identifier. Provider credentials are obtained server-side; neither the browser nor the model receives secrets.

Return explicit errors for authentication, rate limit, timeout, unavailable service, invalid output and refusal. Retry transient failures within configured limits; do not retry bad credentials as if transient. Schema repair may return validation errors to the model within a bounded attempt count. Exhaustion creates a review task and failed/needs_review run. Usage unavailable means null, not zero. Any cost estimate records the rate assumptions and is labelled estimated.

## Tool loop

1. Claim a durable event; establish the authenticated scope, fixed run mode, policy version and latest case snapshot.
2. Wait for extraction completion where required. Load only case-scoped document artifacts, relevant replies, commitments and action history within the context budget. Partial/truncated evidence must be flagged, not treated as complete.
3. Call the live model with trusted task instructions and schema-defined tools. Document/email content remains untrusted tool data; do not promote it into system instructions.
4. Validate requested tool name and arguments against a server allowlist and schema. Recheck scope on every tool call; do not expose general SQL, shell, arbitrary network access or provider credentials.
5. Execute read tools or gated action tools. Return actual structured results with matching tool-call IDs to the model. Assistant text that says 'sent' is not a send result.
6. For a state-changing proposal, validate evidence, policy and expected version; commit through the application transaction and outbox. The model never performs raw writes or sends.
7. Reload state after mutation and decide whether more work is justified. A stale snapshot causes bounded reassessment, not automatic overwriting. Avoid replaying completed effects on retry.
8. End on completed work, a persisted scheduled follow-up, assigned human review, or exhausted limits. Preserve an audit record and next action. Waiting for a client is a persisted schedule, not an LLM process sleeping for days.

Initial semantic decisions are document matching, evidence adequacy, reply intent, commitment interpretation and contextual message drafting. Calendar eligibility, authorisation, dedupe and readiness are application rules.

## Initial model-facing tools

| Tool | Purpose | Execution authority |
| --- | --- | --- |
| get_case_context | Retrieve requirements, policy, commitments and relevant history | Case-scoped read |
| get_document_evidence | Retrieve extraction pages by authorised document ID | Case-scoped read; no arbitrary URL |
| get_reply_evidence | Retrieve relevant conversation and attachment references | Case-scoped read |
| submit_document_assessment | Validate and persist model finding with server-bound provenance | Evidence record only; does not accept requirement |
| submit_reply_assessment | Validate and persist reply finding | Evidence record only; does not waive or accept |
| propose_action | Request a typed business action from contracts.md | Full application gate; result returned to model |

Implement these with executable typed request/response schemas in the selected language, not free-form dictionaries. Action payloads must be discriminated by action_type. The current Markdown and fixtures are design inputs, not runtime schema validation.

## Live mail and uploaded documents

For business-connected acceptance, send to approved test recipients through a real mail integration and ingest actual replies/attachments through an authenticated connector. Record provider message IDs, thread association, delivery outcome and bounce/failed states. A sandbox can restrict recipients while still using actual transport. A local mail sink is useful for tests but is not proof of real delivery.

Persist files privately; validate size/type, reject unsupported or encrypted documents explicitly, and isolate extraction with resource limits. Do not execute macros or follow document instructions/links. Validate upload-session scope and sender/thread association before exposing case context. A mailbox address match does not authorise requirement waivers.

Before sending, serialize dispatch decisions with relevant case mutations or use an equivalent guarded dispatch protocol. Recheck status and mark the action claimed. Evidence arriving after provider handoff cannot recall the message reliably; record the ordering and suppress subsequent obsolete reminders. Unknown delivery outcomes require reconciliation. Exactly-once external delivery is not assumed.

## Deployment and readiness checks

The four-person plan assigns AWS Lightsail deployment to Student 1; it does not establish model API access. Keep that deployment target pending account verification. Database and file storage must survive process restarts; a scheduler/worker must recover due tasks after restart. Include authenticated access, TLS for the deployed service, protected secrets, health checks and a documented backup/restore procedure before a controlled pilot.

Real API connectivity alone is insufficient for production readiness. A controlled pilot additionally needs firm-approved requirements, contacts, follow-up policies, authorised data handling and named human owners. Enterprise onboarding, accounting posting and full reconciliation remain out of scope.

## Observability and acceptance

Record run/event IDs, model/provider and prompt version, schema/policy versions, actual tool calls/results, latency, usage, finish/error reason and state transitions. Store concise evidence-based explanations, not hidden chain-of-thought. Redact sensitive content and never log API keys.

Live acceptance must use fresh document/reply variations beyond the committed fixtures and show an actual provider request, tool result, committed case update and a real test-mail round trip. Test provider failure, invalid output, refusal, hostile evidence and restart recovery. Keep mock test results separate from live results.
