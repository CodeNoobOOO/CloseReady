# First live case-analysis runtime

Implement inline on feat/core-workflow; preserve the uncommitted case API increment. No commit/push is authorised for this continuation.

Goal: a real DeepSeek bounded native tool loop reads a persisted case and proposes an action. Application code checks actor scope, references and the exact snapshot version, then records a review task or a justified no_action. No mail transport or document/reply assessment is available yet, so requests become explicitly unsent review tasks. Unsupported business actions are blocked, not simulated.

- [x] Write failing tests with explicit scripted provider doubles (separate from live tests): valid draft, unsafe reference, stale result, unavailable provider, step limit, replay, lease recovery and scoped HTTP reads.
- [x] Implement closeready/llm.py: bounded HTTPS adapter, official endpoint only, redirect refusal, typed response, safe classified errors and actual usage. No fixture fallback. Set finite timeout and output limits; transient retry handled within runtime call budget.
- [x] Implement runtime_models.py and runtime_store.py: additive event/run/proposal/trace/review tables, atomic gate, durable idempotent event creation, expiring claim, explicit expired-run recovery, assigned review on failure. Scope reads and writes to the initiating authorised actor/client.
- [x] Implement runtime.py: server-bound get_case_context and propose_action tools; require read before propose; return actual tool results; bounded steps and invalid-output repairs; persist trace summaries without raw prompts or hidden reasoning.
- [x] Add authenticated synchronous POST /cases/{id}/runs, GET /runs/{id}, POST /runs/{id}/recover and GET /cases/{id}/review-tasks. Do not reuse /activate because this analysis does not send the initial request. Existing case API works with LLM disabled; starting a run fails explicitly when unavailable.
- [x] Add real synthetic validation script and perform a small live DeepSeek run with the configured key. Verify persisted task, trace and unchanged document readiness. Report live result separately from scripted tests.
- [x] Update docs/contracts/setup/status, run complete tests and diff checks. Future scope: document evidence, reply interpretation, full communication policy, outbox/transport, async scheduling, review resolution and deployment.
