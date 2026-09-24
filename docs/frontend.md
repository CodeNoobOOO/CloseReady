# Frontend integration

The dashboard runs at `/app` on the API origin. API and document worker must both be running. Browser access uses the existing administrator-provisioned bearer token; it is kept in session storage and cleared by Disconnect. Access grants, recipients and mail policy remain server controlled.

## Start on macOS or Linux

From the repository root, after configuring `.env` and `local-data/server-config.json`:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
export CLOSEREADY_ACCESS_CONFIG=local-data/server-config.json
export CLOSEREADY_DATABASE_URL=sqlite:///local-data/closeready.db
export CLOSEREADY_MAIL_BACKEND=test_sink
export CLOSEREADY_LLM_ENABLED=1
export CLOSEREADY_LLM_ENV_FILE=.env
.venv/bin/python -m uvicorn closeready.api:from_env --factory --host 127.0.0.1 --port 8000
```

In another terminal, set the same variables and run `.venv/bin/python -m closeready.worker`. Stop each foreground process with Ctrl+C. The database survives shutdown. For processes started in the background during local development, stop the PIDs recorded in `local-data/backend.pid` and `local-data/worker.pid`, after checking they still identify the expected processes.

## Manager workflow

1. Connect, select a client-period case, or choose New case. Pressing Enter in the token field connects. Cases are listed newest-created first. New case requires authorised client, owner and policy IDs. Enter one or more bank accounts separated by commas, using either `1234` or `operating:1234`; each becomes a separate full-month bank-statement Requirement and only the masked value is shown later. Labels allow two accounts with the same last four digits to remain separate, while matching such an ambiguous document still requires human review. Invoices, receipts and other supporting documents use explicit required item references. Other supporting documents require a description.
2. Read the outstanding count, next reminder, owner, case reference and next step. Four document categories are always visible. Unconfigured categories are neutral; missing/correction items are red; only accepted evidence has a green tick. Received, pending and waived items are separately labelled.
3. Upload one text-based PDF up to 5 MiB, optionally binding it to a checklist requirement. The worker processes durable jobs; queued/processing documents refresh automatically. Scans cannot be OCR-assessed in this increment. Inspect the result and evidence rather than treating upload success as acceptance.
4. Open a finding to see detected period, identity matches, coverage, issues, uncertainty, page evidence and review history. Fetch the original PDF from `GET /api/v1/cases/{case_id}/documents/{document_id}/content` with the current bearer token and render it from a temporary browser Blob URL; never place the token in a URL or expose a public document link. Close or Escape returns to the case without opening the decision form; only Make decision proceeds. Documents in `needs_review` can be accepted for a chosen requirement, rejected or reassigned for processing, with a mandatory reason. Application gates decide whether the action is permitted.
5. Generate AI follow-up to enqueue an analysis. The action uses the configured LLM and may consume API credit. Review, edit or reject the resulting draft. Delivering an approved message is a separate action: `test_sink` is simulated; `smtp` is real transport. This local setup keeps `test_sink` enabled.
6. Record a trusted reply from an approved sender and assess it. Unknown senders are quarantined. Review commitments, reminders and findings. Promises beyond the deadline create a review; changing the deadline requires an explicit manager action and does not itself mean the old reply has been reassessed.
7. When requirements and reviews are resolved, Confirm ready asks for a final human reason. The button is disabled while the case is not confirmable; the backend independently enforces all readiness checks.
8. Audit history includes explicit document-decision labels, filenames, reasons, actors, timestamps, policy and state versions. Run traces show mode, model, tool outcomes, latency and reported usage. Missing trace retrieval is shown as an error rather than silently hidden.
9. Rejecting a document offers `Reject only` and `Reject and prepare correction email`. The latter queues an LLM run using the saved rejection finding and manager reason; it prepares a human-review draft and never sends directly.

## Concurrency and failures

Case navigation uses a generation counter so late responses cannot replace the current case. Disconnect clears case state and invalidates pending fetches. Mutation buttons are locked while a request is in flight. On a network/server failure, Retry uncertain request reuses the exact body and idempotency key in the current tab; no automatic mutation retry occurs. Reloading or disconnecting loses this in-memory retry context, so inspect the case/audit before issuing another operation. Stale writes are rejected by the backend and the UI reloads state.

## Verification and limitations

Run `.venv/bin/python -m pytest -q` and `node --test tests/frontend.test.cjs` (Node 22+). The latter covers case races, disconnect, mutation retry identity, keyboard form actions, explicit document-review navigation and checklist/readiness display. CI runs both.

The integrated backend is `integration/student2-student3` at `4d670f5`. Its document worker uses deterministic assessment; the separate LLM document-analysis feature branch has not been integrated. Follow-up pause/resume has no public route yet. The UI does not bypass these missing capabilities or claim OCR, live email verification, multi-agent evaluation or a finished submission video/write-up. Actual business accuracy and held-out results remain separate evaluation work.

## Integration verification — 2026-09-18

Local API and worker checks used three new synthetic cases, preserving the original demo: wrong-period evidence remained missing with `needs_correction`; uncertain identity required human review and became accepted only after a recorded decision; complete evidence became `ready_for_confirmation` and then `ready` after manager confirmation. These are functional checks, not held-out business evaluation results.

An isolated browser test database verified creating a case with all four requirement types, cancelling an empty required reply form, opening document evidence, submitting a reasoned document acceptance, and final readiness confirmation with visible audit reasons. No real email or new LLM inference was used for these checks.

Confirmed cases expose **Undo ready confirmation**. A manager must provide a reason; Cancel leaves the case unchanged. Successful undo returns the case to pending final confirmation, preserving accepted checklist evidence and both audit events. This does not reject a document or restart reminders.

### Reopen regression — 2026-09-19

An isolated browser case passed document acceptance → confirm (version 3) → cancel undo with no change → reasoned undo (version 4) → confirm again (version 5). Accepted evidence survived reopening; the original confirmation and new reopen reason remained visible in the audit timeline. API tests also cover scope, authentication, read-only permissions, blank reasons, stale versions and idempotent replay. Live LLM calls and real email delivery were not rerun for this change.
