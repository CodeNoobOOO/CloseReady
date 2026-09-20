# Development pack verification — 2026-09-20

These are component integration checks on the local integration code, based on commit 32700f6 plus the document-review/AI changes delivered alongside this report. Use the commit containing this report to reproduce; this is not validation of 32700f6 alone. Temporary databases were created and removed; no production cases, held-out inputs or real email transport were used.

| Scenario | Rules + API/document processor | Live deepseek-flash |
| --- | --- | --- |
| dev_001 Complete July statement | PASS: eligible for confirmation | BLOCKED_AI: AI_REVIEW_UNAVAILABLE, sent to review |
| dev_002 Wrong month | PASS: period conflict retained | BLOCKED_AI: AI_REVIEW_UNAVAILABLE; period issue retained |
| dev_003 Partial month | PASS: missing coverage retained | BLOCKED_AI: AI_REVIEW_UNAVAILABLE; coverage issue retained |
| dev_004 Duplicate partial PDF | PASS: duplicate tracked, no false completion | BLOCKED_AI: AI_REVIEW_UNAVAILABLE, no acceptance |
| dev_005 Next-week promise | NOT_RUN: requires real interpretation | PASS: clarification required, promised_at null, no commitment/reminder, review created |
| dev_006 Invoice instead of statement | PASS: wrong type unresolved | PASS: needs_correction, bank statement still outstanding |

The five PDF fixtures are readable and match their scenario labels. All case requests pass schema validation. The live document coordinator currently collapses errors into AI_REVIEW_UNAVAILABLE; the exact cause for scenarios 001–004 remains undiagnosed. Do not attribute it to the provider or fixture without further investigation. These failures are actionable development results, not passing AI accuracy results.

The reply check uses trusted sandbox ingestion with received_at and a controlled communication clock. It does not exercise outbound approval/delivery, email thread matching, browser interactions or a full client round trip. No complete end-to-end or business-efficiency claim is supported. Recorded elapsed seconds include harness overhead and are not operator time or model-only latency. Cost and full model-call metrics were not collected.

## Reproduce from the repository root

Install requirements-dev.txt in the project virtual environment. The verifier automatically provisions synthetic users/policies/contacts in a temporary database; you do not need to edit your real server-config.json. It only reads evaluation/development.

```sh
.venv/bin/python scripts/verify_development.py --output local-data/evaluation/dev-rules.json
CLOSEREADY_LLM_ENV_FILE=.env .venv/bin/python scripts/verify_development.py --live --output local-data/evaluation/dev-live.json
```

The second command uses the configured real provider and incurs API cost. Keep the original verification-rules.json and verification-live.json as first-run evidence; write reruns to new files. Never include .env or real tokens in a shared package.

Share as a **development test pack with known AI failures**, not as a fully passing acceptance suite. The current integration source must also be available to teammates to reproduce the live document coordinator. Held-out remains separate and unexecuted.
