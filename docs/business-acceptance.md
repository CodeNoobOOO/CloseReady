# Business alignment and acceptance review

Reviewed against the local Problem.docx, judging rubric.docx, plan b.docx and CloseReady_Team_of_4_Plan_of_Action_PlanB.docx. Source files remain unchanged. This document records design requirements, not completed functionality or test results.

## Review conclusion

The original v0.1 correctly separated model judgement from application authority, but was not sufficient to guide business-connected implementation. It omitted a concrete live LLM loop, first-request activation, coverage/item completeness, operational policy records and live integration acceptance. v0.2 addresses those design gaps. Executable schemas, backend code, provider selection, credentials and deployment remain outstanding.

The central outcome is fewer manual document-follow-up cycles and accurate client-period readiness. A bank-statement demo is a first scenario, not the entire business model. Requirements must support relevant entities/accounts, full statement coverage, expected items and correction history. A document's displayed month alone is insufficient proof of completeness.

## Rubric evidence to produce

| Rubric | Required implementation/evaluation evidence | Primary owner |
| --- | --- | --- |
| Goal and scope | Client-period checklist, owner/deadline, missing-item explanations and timed manual baseline | S1 + S4 |
| Architecture and reasoning loop | Live LLM tool loop, committed state, event-driven resumption and bounded stops | S1 |
| Tool use and integration | Executable typed schemas, actual extraction and real test-mail round trip | S1 + S2 + S3 |
| Autonomy and human oversight | Routine automatic correction/follow-up; assigned review tasks; authenticated waivers/confirmation | S1 + S3 + S4 |
| Safety and guardrails | Server-side scope checks, injection tests, credential isolation, stale/dedupe and delivery reconciliation | S1 + S2 + S3 |
| Observability and evaluation | Provider/tool traces, frozen held-out set, documented failures, cost/latency and operator-time results | All; S4 aggregates |
| Platform and tooling | Named actual provider/framework, bounded runtime integration and deployed recovery demonstration | S1 |

## Mandatory end-to-end checks

1. Activate a configured case with no uploads. The live agent identifies outstanding items and a permitted initial request is actually delivered to an approved test inbox.
2. Upload a fresh wrong-period or partial-period statement. The model identifies evidence; full-period completeness rules keep the requirement unresolved and a specific correction request is generated.
3. Supply two verified partial statements covering the full required interval. Apply the configured multi-document policy; gaps or wrong accounts prevent acceptance.
4. Submit one invoice when two explicit items are required. The remaining item stays missing; a generic monthly label cannot hide the gap.
5. Ingest an actual reply and its attachments. A clear commitment adjusts timing, an ambiguous date gets clarification, and a disputed requirement gets an assigned review task.
6. Correct the missing evidence before dispatch. Obsolete pending reminders are cancelled; mixed-item messages exclude resolved items.
7. Reach the configured no-response limit. Persist an assigned escalation and stop prohibited further automatic reminders.
8. Confirm readiness as an authorised human. Material new evidence reopens assessment without erasing historical approval.
9. Simulate provider failure, unknown mail delivery and worker restart. Preserve state, bound retries and recover/reconcile without duplicate effects.
10. Attempt cross-client access, unauthorised approval, stale writes and prompt injection. Deny prohibited effects and retain audit evidence.

## Evaluation rules

Use at least 30 held-out scenarios as proposed in plan b.docx; the four-person plan's shorter minimum-suite list is not a replacement for that target. Reserve validation scenarios for architecture selection. Report at least 90% precision/recall for outstanding items, at least 90% appropriate next actions, at least 80% correct autonomous handling of routine scenarios, at least 30% lower active operator time, zero critical false-ready/unauthorised/duplicate/stale effects in the defined tests, and complete action traceability as targets, never as achieved claims.

A routine scenario ends at eligible-for-confirmation or a correctly scheduled wait; mandatory final human confirmation is outside that autonomous segment. Unnecessary escalation fails the autonomous criterion. Full operator-time measurement includes final review, edits and recovery. Report sample counts, first-run failures and post-fix results.

Use synthetic or authorised anonymised data with real model calls. Do not infer real monthly-close acceleration from simulated time. Benchmark mode and provider usage must be visible in results.

## Delivery priorities

1. Freeze the v0.2 boundary formats and implement executable schemas with fixtures and validation tests.
2. Select the backend/runtime and verify the real LLM API tool-call round trip. Do not build an entire mock-only application first.
3. Implement durable case/event/action handling and connect extraction plus actual restricted-recipient email.
4. Prove the first complete business loop, then expand exception handling and evaluate optional specialists.
5. Deploy and run held-out/live integration checks before inviting a controlled business pilot.

The plan allows a sandbox fallback when time is short. That fallback can demonstrate workflow but must be labelled as such; it does not meet the user's business-connected acceptance gate. Do not silently weaken acceptance to match an incomplete implementation.
