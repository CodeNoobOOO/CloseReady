# AI contributor instructions

## Read before implementation

Read README.md, docs/contracts.md, docs/llm-runtime.md and docs/business-acceptance.md, then inspect existing code. Examples in examples/ are synthetic fixtures, not implemented endpoints or real inference results.

## Scope and ownership

- Student 1: case state, backend APIs, runtime, action validation and recovery.
- Student 2: upload/extraction, document assessments and evidence.
- Student 3: replies, commitments, reminders and email integration.
- Student 4: frontend, human review, audit display and evaluation infrastructure.

Follow the current user's assigned task. Keep changes focused; explain cross-module changes and avoid unrelated rewrites. Do not assume a technology stack is selected until it is documented or confirmed by the user.

## Contracts and safety

Use the documented field names, states and module boundaries. Changes to shared interfaces must update contracts, affected examples and tests together, and explain compatibility impacts in the PR.

Use persistent case state as business truth. Model outputs propose actions; application code enforces scope, evidence, state versions, policy, idempotency and human approval. Never bypass these checks to make a demo pass.

The deliverable requires actual LLM API integration. Clearly label mocks and test sinks; never silently substitute them for a failed live integration or claim they prove business-connected functionality.

Never commit credentials, real client documents, private account instructions or local databases. Do not log secrets or grant tools arbitrary filesystem, SQL or network authority.

## Validation and collaboration

For implementation changes, run relevant happy-path and failure tests. For documentation/fixture changes, check consistency, JSON parsing and Git whitespace checks. Report what was and was not tested; do not invent passing results.

Use task branches and PRs. Do not commit, push or merge without user authorisation. Do not amend or overwrite other contributors' work without an explicit need and authorisation.

Keep README setup/status accurate as implementation arrives. Record actual provider/framework choices, required non-secret configuration and reproducible checks.
