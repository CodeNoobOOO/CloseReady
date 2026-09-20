# Development and held-out scenario packs

Repository sharing note: the development pack and verifier are shared with the integration code. Held-out fixtures and their SHA256SUMS are retained separately by the evaluation owner and are intentionally not included in this branch upload. References to held_out below describe that separate local pack.

All clients, documents and messages are synthetic. These are test inputs and expected outcomes, **not passing test results**. The pack contains text-based PDFs, matching source text, case creation requests and reply inputs. No scenarios have been run against an application or model as part of preparing these packs.

## Development — repeat freely

Open `development/scenarios.jsonl` for complete instructions and expected outcomes.

| ID | Scenario | Expected result |
| --- | --- | --- |
| dev_001 | Correct complete July statement | Evidence accepted after verification; eligible for final confirmation, never automatically finally ready |
| dev_002 | June statement for July requirement | Period conflict; requirement unresolved; request correction or human review |
| dev_003 | July 1–15 statement | Remaining coverage unresolved; request missing dates |
| dev_004 | Upload the identical partial PDF twice | Duplicate does not complete coverage or trigger duplicate effects |
| dev_005 | “I will submit … next week” | Clarify the date or review ambiguity; never invent an exact timestamp |
| dev_006 | Invoice instead of bank statement | Bank statement stays outstanding; request correct document or human review |

Use each folder's `case.json` to create a new case through the API. Provision its client/owner/policy in an isolated test access configuration first. Upload the provided PDF; for duplicates upload the same bytes twice using distinct request keys. A reply requires an authorised synthetic contact and an established outbound context. `.invalid` email addresses are intentionally non-deliverable: use sandbox transport. Never use these cases with real SMTP.

For relative dates, use the scenario's specified clock and timezone. Running an old reply against today's clock invalidates the timing experiment. Do not modify the computer clock; use an isolated clock-controlled evaluation harness. These fixtures do not yet provide that harness.

After a failure, change implementation/prompt/rules and rerun a fresh copy. Record first-run and post-fix results separately. A known implementation limitation is still a failure against the intended outcome, not a reason to change the label to match the implementation.

## Held-out — final evaluation only

`held_out/scenarios.jsonl` contains 30 cases across the same six categories, using separate client/entity/account identifiers and August periods. Coverage windows, commitment expressions and wrong-document/identity inputs include variations. Some cases differ primarily in identity; this is a small controlled functional suite, not evidence of broad real-world generalisation. There are no OCR or real-mail cases in this pack.

The expected-outcome manifest is for the final evaluator. Keep it out of model prompts and day-to-day development tasks. Ideally a teammate owns this folder and runs final evaluation. Fixture authors have seen the inputs, so this is a frozen internal holdout, not an independently blind benchmark.

Before running any held-out case, record the selected commit, architecture/run mode, provider/model, prompts, business rules, tool configuration and pricing assumptions in `final-configuration.json` (copy the example). Selection uses development only in this two-set workflow. Do not use held-out results to choose architecture, tune prompts or adjust rules. This two-set process supersedes the earlier three-set execution plan; development comparisons should be reported as exploratory, not independent architecture validation.

Run the selected configuration once per case and use an independent fresh case copy for the matched manual baseline. Keep first-run failures. If held-out findings inform a fix, the original set is no longer an untouched final test for that fix; report that limitation and commission a new holdout for a new final claim.

`held_out/SHA256SUMS` freezes the scenario and fixture bytes. Verify checksums before final evaluation. Do not regenerate this file to hide fixture changes.

## Recording results

Use `scripts/evaluate.py` for measured result JSONL. Map `expected.outstanding` to `expected_outstanding` and record actual predictions separately. Use the same scenario ID for matched automated/manual results. Keep results outside these fixture folders, for example under ignored `local-data/evaluation/`.

Record actual operator seconds, manual interventions, latency, model calls, token usage, price basis and estimated cost. Do not fill unknown values with zero. Include actual provider/model and whether inference was live. A ready checkbox or an automated test pass is not an evaluation result. Judge `next_action_acceptable` against the scenario's accepted actions, and report critical false-ready and unsafe effects even if a later retry succeeds.

No architecture has been selected and no held-out scores are claimed by this pack.
