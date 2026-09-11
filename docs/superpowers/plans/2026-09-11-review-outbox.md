# Review Decisions and Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic customer-visible content controls, authenticated review resolution, and a durable approved-message outbox without implementing mail delivery.

**Architecture:** A focused content guard validates model and human drafts. `RuntimeStore` remains the sole transactional authority for review decisions, case versions, idempotency, audit, and outbox creation; FastAPI exposes scoped decision and listing endpoints. Provider-specific contacts and delivery stay outside this increment.

**Tech Stack:** Python 3.11+, Pydantic 2, FastAPI, SQLAlchemy 2, SQLite, `unittest`

**Spec:** `docs/superpowers/specs/2026-09-11-review-outbox-design.md`

## Global Constraints

- Internal IDs remain structured metadata but must not occur in customer-visible subject or body text.
- Invalid customer-visible text is rejected, never silently rewritten.
- Approval creates a durable outbox record but performs no network call and claims no delivery.
- All mutations require manager authorization, exact state version, idempotency, and an audit record.
- Case readiness remains `collecting`; this increment cannot accept documents, waive requirements, or confirm readiness.
- Existing JSON review records must remain readable without a database reset.

---

### Task 1: Customer-visible content guard

**Files:**
- Create: `closeready/content_guard.py`
- Create: `tests/test_content_guard.py`
- Modify: `closeready/runtime_store.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Consumes: `MessageDraft` from `closeready.models`
- Produces: `validate_customer_visible_draft(draft: MessageDraft) -> None`, raising `DomainError('UNSAFE_DRAFT', ..., 422)`

- [x] **Step 1: Write focused failing unit tests for the guard**

```python
class ContentGuardTests(unittest.TestCase):
    def draft(self, subject='July statement', body='Please upload the July statement.'):
        return MessageDraft(subject=subject, body=body, requirement_ids=['req_metadata_only'])

    def test_rejects_internal_identifiers_case_insensitively(self):
        for prefix in ('case_', 'REQ_', 'run_', 'review_', 'event_', 'proposal_'):
            with self.subTest(prefix=prefix), self.assertRaises(DomainError) as raised:
                validate_customer_visible_draft(self.draft(body='Reference ' + prefix + '123'))
            self.assertEqual(raised.exception.code, 'UNSAFE_DRAFT')

    def test_allows_structured_requirement_ids_when_visible_text_is_safe(self):
        validate_customer_visible_draft(self.draft())

    def test_rejects_unsafe_scheme_control_character_and_length(self):
        for draft in (
            self.draft(body='Open javascript:alert(1)'),
            self.draft(body='Bad\x00text'),
            self.draft(subject='x' * 201),
            self.draft(body='x' * 10001),
        ):
            with self.subTest(draft=draft), self.assertRaises(DomainError):
                validate_customer_visible_draft(draft)
```

- [x] **Step 2: Run the new tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_content_guard -v`

Expected: import failure because `closeready.content_guard` does not exist.

- [x] **Step 3: Implement the deterministic guard**

```python
import re
import unicodedata

from .models import MessageDraft
from .store import DomainError

INTERNAL_ID = re.compile(r'(?i)\b(?:case|req|run|review|event|proposal)_[a-z0-9]+\b')
URL_SCHEME = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*):/{2}')
DANGEROUS_SCHEME = re.compile(r'(?i)\b(?:javascript|data|file|vbscript):')


def validate_customer_visible_draft(draft: MessageDraft) -> None:
    if len(draft.subject) > 200 or len(draft.body) > 10_000:
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft exceeds the allowed length.', 422)
    visible = draft.subject + '\n' + draft.body
    if INTERNAL_ID.search(visible):
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft contains an internal identifier.', 422)
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\r\t' for char in visible):
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft contains a control character.', 422)
    if DANGEROUS_SCHEME.search(visible) or any(
        match.group(1).lower() != 'https' for match in URL_SCHEME.finditer(visible)
    ):
        raise DomainError('UNSAFE_DRAFT', 'Customer-visible draft contains a disallowed URI scheme.', 422)
```

- [x] **Step 4: Run the guard tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_content_guard -v`

Expected: all guard tests pass.

- [x] **Step 5: Add a failing runtime test proving unsafe model output is not stored**

```python
def test_internal_identifier_in_model_draft_is_rejected_without_persisting_text(self):
    unsafe = self.draft()
    args = json.loads(unsafe.calls[0].arguments)
    args['action']['payload']['body'] = 'Upload requirement ' + self.case.requirements[0].requirement_id
    result = self.run_with(ScriptedProvider([
        tool('get_case_context', {}), tool('propose_action', args, 'unsafe')
    ]), max_repairs=0)
    self.assertEqual(result.error_code, 'UNSAFE_DRAFT')
    tasks = self.db.review_tasks(self.actor, self.case.case_id).items
    self.assertEqual(len(tasks), 1)
    self.assertIsNone(tasks[0].draft)
```

- [x] **Step 6: Run the runtime test and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_runtime.RuntimeTests.test_internal_identifier_in_model_draft_is_rejected_without_persisting_text -v`

Expected: failure because the unsafe draft is currently stored.

- [x] **Step 7: Call the guard inside `RuntimeStore.apply` before `_review`**

```python
if action.action_type in ('request_documents', 'request_clarification'):
    if not action.requirement_ids or not set(action.requirement_ids).issubset(outstanding):
        raise DomainError('INVALID_TOOL', 'Draft must refer only to outstanding items.', 422)
    draft = action.payload
    validate_customer_visible_draft(draft)
    code = 'MAIL_NOT_CONFIGURED'
```

- [x] **Step 8: Run runtime and guard tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_content_guard tests.test_runtime -v`

Expected: all tests pass.

Commit:

```powershell
git add closeready/content_guard.py closeready/runtime_store.py tests/test_content_guard.py tests/test_runtime.py
git commit -m "Enforce customer-visible draft safety"
```

---

### Task 2: Review lifecycle and durable outbox transaction

**Files:**
- Modify: `closeready/runtime_models.py`
- Modify: `closeready/runtime_store.py`
- Create: `tests/test_review_outbox.py`

**Interfaces:**
- Produces: `ReviewDecisionRequest`, `OutboxRecord`, `OutboxPage`
- Produces: `RuntimeStore.decide_review(actor, case_id, request, key) -> ReviewTaskRecord`
- Produces: `RuntimeStore.outbox_records(actor, case_id, cursor=None, limit=50) -> OutboxPage`

- [ ] **Step 1: Write failing schema tests**

```python
def test_edit_requires_draft_and_other_decisions_forbid_it(self):
    base = {'expected_state_version': 2, 'review_task_id': 'review_1', 'reason': 'Reviewed'}
    with self.assertRaises(ValidationError):
        ReviewDecisionRequest.model_validate({**base, 'decision': 'edit_and_approve'})
    with self.assertRaises(ValidationError):
        ReviewDecisionRequest.model_validate({**base, 'decision': 'reject_draft',
            'edited_draft': {'subject': 'Safe', 'body': 'Safe', 'requirement_ids': ['req_1']}})
```

- [ ] **Step 2: Run the schema test and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_review_outbox.ReviewOutboxTests.test_edit_requires_draft_and_other_decisions_forbid_it -v`

Expected: import failure because the review-decision models do not exist.

- [ ] **Step 3: Add lifecycle and outbox models**

```python
class ReviewDecisionRequest(ContractModel):
    expected_state_version: PositiveInt
    review_task_id: Text
    decision: Literal['approve_draft', 'edit_and_approve', 'reject_draft', 'dismiss_error']
    reason: Annotated[Text, Field(max_length=2000)]
    edited_draft: MessageDraft | None = None

    @model_validator(mode='after')
    def draft_matches_decision(self):
        if (self.decision == 'edit_and_approve') != (self.edited_draft is not None):
            raise ValueError('Edited draft is required only for edit_and_approve')
        return self


class ReviewTaskRecord(ContractModel):
    # existing fields remain
    status: Literal['open', 'resolved'] = 'open'
    resolution: Literal['approved', 'edited_and_approved', 'rejected', 'dismissed'] | None = None
    resolved_by: Text | None = None
    resolved_at: Timestamp | None = None
    resolution_reason: Text | None = None
    approved_draft: MessageDraft | None = None


class OutboxRecord(ContractModel):
    outbox_id: Text
    case_id: Text
    review_task_id: Text
    requirement_ids: list[Text]
    subject: Text
    body: Text
    status: Literal['pending_reviewed_delivery'] = 'pending_reviewed_delivery'
    recipient_contact_id: Text | None = None
    provider_message_id: Text | None = None
    created_by: Text
    created_at: Timestamp
    delivery_status: Literal['not_attempted'] = 'not_attempted'


class OutboxPage(ContractModel):
    items: list[OutboxRecord]
    next_cursor: str | None
```

Add a model validator to `ReviewTaskRecord` requiring all resolution fields to be null while open and requiring resolution, actor, timestamp, and reason while resolved. `approved_draft` is required only for approved resolutions.

- [ ] **Step 4: Run schema tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_review_outbox.ReviewOutboxTests.test_edit_requires_draft_and_other_decisions_forbid_it -v`

Expected: pass.

- [ ] **Step 5: Write failing store tests for approve, edit, reject, and dismiss**

Create a real case and review task through the scripted runtime. Assert:

```python
approved = db.decide_review(actor, case.case_id, ReviewDecisionRequest(
    expected_state_version=2, review_task_id=task.review_task_id,
    decision='approve_draft', reason='Manager approved'), 'approve-1')
self.assertEqual(approved.status, 'resolved')
self.assertEqual(approved.resolution, 'approved')
self.assertEqual(store.get_case(actor, case.case_id).state_version, 3)
self.assertEqual(len(db.outbox_records(actor, case.case_id).items), 1)
self.assertEqual(db.outbox_records(actor, case.case_id).items[0].delivery_status, 'not_attempted')
```

Add equivalent cases proving edited content is stored, rejection creates no outbox, and an operational task without a draft can only be dismissed.

- [ ] **Step 6: Run store tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_review_outbox -v`

Expected: failures because tables and store methods do not exist.

- [ ] **Step 7: Add outbox and review-decision tables**

```python
outbox = Table('mail_outbox', runtime_metadata,
    Column('outbox_id', String, primary_key=True),
    Column('case_id', String, nullable=False, index=True),
    Column('review_task_id', String, nullable=False, unique=True),
    Column('record', Text, nullable=False))
review_responses = Table('review_idempotent_responses', runtime_metadata,
    Column('actor_id', String, primary_key=True),
    Column('case_id', String, primary_key=True),
    Column('key', String, primary_key=True),
    Column('request_hash', String, nullable=False),
    Column('response', Text, nullable=False))
```

- [ ] **Step 8: Implement transactional decision and scoped listing**

`decide_review` must, inside `Store.write()`:

1. scope the case through `Store._case`;
2. require `actor.can_manage` and `task.assigned_to == actor.user_id`;
3. replay an identical key before comparing the current case version;
4. reject stale versions, resolved tasks, wrong task type, and mismatched edited requirement IDs;
5. validate the final draft with `validate_customer_visible_draft`;
6. create `OutboxRecord` only for approval decisions;
7. replace the review JSON, update the case snapshot/version, write audits, and save the idempotent response in one transaction.

Map decisions exactly:

```python
resolution = {
    'approve_draft': 'approved',
    'edit_and_approve': 'edited_and_approved',
    'reject_draft': 'rejected',
    'dismiss_error': 'dismissed',
}[request.decision]
```

- [ ] **Step 9: Add failure, replay, concurrency, compatibility, and restart tests**

Tests must assert:

```text
stale version                 -> STALE_STATE, no mutation
same key and same body        -> same resolved record, one outbox row
same key and different body   -> IDEMPOTENCY_CONFLICT
second key on resolved task   -> REVIEW_ALREADY_RESOLVED
other client/task mismatch    -> NOT_FOUND
non-manager                   -> FORBIDDEN
two concurrent approvals      -> one success, one conflict, one outbox row
legacy open task JSON         -> lifecycle defaults load successfully
new Store/RuntimeStore        -> resolved task, outbox, replay survive restart
```

- [ ] **Step 10: Run all review/outbox and runtime tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_review_outbox tests.test_runtime -v`

Expected: all tests pass.

Commit:

```powershell
git add closeready/runtime_models.py closeready/runtime_store.py tests/test_review_outbox.py
git commit -m "Add review decisions and durable outbox"
```

---

### Task 3: Authenticated HTTP interfaces

**Files:**
- Modify: `closeready/api.py`
- Modify: `tests/test_review_outbox.py`
- Modify: `docs/contracts.md`

**Interfaces:**
- Produces: `POST /api/v1/cases/{case_id}/review-decisions`
- Produces: `GET /api/v1/cases/{case_id}/outbox`

- [ ] **Step 1: Write failing HTTP tests**

Use `TestClient(create_app(..., provider=ScriptedProvider(...)))` to create a case and review task, then assert:

```python
response = client.post(f'/api/v1/cases/{case_id}/review-decisions', headers={
    'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'decision-1'}, json={
    'expected_state_version': 2,
    'review_task_id': task_id,
    'decision': 'approve_draft',
    'reason': 'Approved by manager'})
self.assertEqual(response.status_code, 200, response.text)
self.assertEqual(response.json()['resolution'], 'approved')
listed = client.get(f'/api/v1/cases/{case_id}/outbox', headers={
    'Authorization': 'Bearer ' + TOKEN})
self.assertEqual(listed.status_code, 200)
self.assertEqual(len(listed.json()['items']), 1)
```

Also assert 401 without authentication, 404 for another client's case, 403 for a read-only principal, 422 for invalid bodies, and 409 for stale/idempotency conflicts.

- [ ] **Step 2: Run HTTP tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_review_outbox.ReviewOutboxHttpTests -v`

Expected: 404 because the new routes do not exist.

- [ ] **Step 3: Add the two API routes**

```python
@app.post('/api/v1/cases/{case_id}/review-decisions', response_model=ReviewTaskRecord)
def decide_review(case_id: str, body: ReviewDecisionRequest, actor: Actor, key: Key):
    return runtime_store.decide_review(actor, case_id, body, key)

@app.get('/api/v1/cases/{case_id}/outbox', response_model=OutboxPage)
def list_outbox(case_id: str, actor: Actor,
                cursor: Annotated[str | None, Query(max_length=128)] = None,
                limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return runtime_store.outbox_records(actor, case_id, cursor, limit)
```

- [ ] **Step 4: Run HTTP and full API tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_review_outbox tests.test_case_api tests.test_runtime -v`

Expected: all tests pass.

- [ ] **Step 5: Update the shared contract with implemented status**

Document exact request/response fields, lifecycle values, authorization, idempotency, state-version behavior, outbox non-delivery semantics, and the Student 3/4 consumption boundary. Remove statements that review tasks are read-only or fixed to `open` where they are superseded.

- [ ] **Step 6: Run docs diff check and commit**

Run: `git diff --check`

Expected: no whitespace errors.

Commit:

```powershell
git add closeready/api.py tests/test_review_outbox.py docs/contracts.md
git commit -m "Expose review and outbox APIs"
```

---

### Task 4: Operator examples and complete verification

**Files:**
- Modify: `docs/backend.md`
- Modify: `docs/agent-runtime.md`
- Modify: `README.md`
- Create: `examples/review-decision.json`
- Modify: `examples/README.md`

**Interfaces:**
- Consumes: the implemented HTTP routes
- Produces: copyable local demonstration instructions for Student 3, Student 4, and judges

- [ ] **Step 1: Add a synthetic approval example**

```json
{
  "expected_state_version": 2,
  "review_task_id": "replace-with-returned-review-task-id",
  "decision": "approve_draft",
  "reason": "Reviewed and approved by the assigned account manager"
}
```

Mark the IDs and version as runtime placeholders in prose; the fixture is illustrative and must not be posted unchanged.

- [ ] **Step 2: Document the end-to-end local flow**

Add PowerShell examples that obtain the current case version and open task ID, post an approval with a fresh idempotency key, list outbox records, and verify `delivery_status=not_attempted`. State explicitly that no email was sent and that Student 3 must resolve a trusted contact and implement delivery separately.

- [ ] **Step 3: Update status summaries**

Update README and runtime documentation to say review resolution and durable approved outbox are implemented, while contact resolution, mail delivery, reply ingestion, reminder scheduling, and dashboard remain outstanding.

- [ ] **Step 4: Run complete local verification**

Run:

```powershell
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m unittest discover -s tests
git diff --check
git status --short
```

Expected: no broken requirements, all tests pass, no diff whitespace errors, and only intended files are modified.

- [ ] **Step 5: Review the branch diff and commit documentation**

Run: `git diff origin/main --stat`

Commit:

```powershell
git add README.md docs/backend.md docs/agent-runtime.md examples/review-decision.json examples/README.md
git commit -m "Document reviewed outbox workflow"
```

- [ ] **Step 6: Final branch verification**

Run:

```powershell
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m unittest discover -s tests
git diff --check origin/main
git status --short
```

Expected: all checks pass and the working tree is clean.
