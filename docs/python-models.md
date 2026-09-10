# Using the shared Python models

Run from the repository root with Python 3.11+. On Windows:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m unittest discover -s tests -v
```

On Linux/Lightsail use `.venv/bin/python` instead. No API key or network inference is needed for model tests. This phase installs Pydantic and IANA timezone data; it does not install a web server or database.

```python
from pathlib import Path
from closeready.models import CaseSnapshot, ActionContent, ActionProposal

case = CaseSnapshot.model_validate_json(
    Path('examples/case-snapshot.json').read_text(encoding='utf-8')
)
print(case.requirements[0].requirement_id)

# The LLM returns content only. These are illustrative values, not a live call.
content = ActionContent.model_validate({
    'action_type': 'no_action', 'requirement_ids': [], 'finding_ids': [],
    'reason': 'Waiting for evidence.', 'payload': {},
})

# Only server code supplies this envelope from the authenticated run/snapshot.
proposal = ActionProposal.model_validate({
    **content.model_dump(mode='json'),
    'proposal_id': 'server-issued-proposal', 'run_id': 'server-issued-run',
    'case_id': case.case_id, 'expected_state_version': case.state_version,
})
print(proposal.root.reason)
print(proposal.model_dump_json())  # Flat JSON: there is no "root" wrapper on the wire.
```

`model_validate()` accepts Python dictionaries; `model_validate_json()` accepts JSON. Catch `pydantic.ValidationError` at the API/tool boundary and return a safe field-level error. Avoid logging raw validation input because it can contain client data. Validation is not execution authorization.

Student 2 imports EvidenceRef and reads CaseSnapshot/Requirement. Student 3 uses MessageDraft and the action content variants. Student 4 can consume JSON Schema and the synthetic JSON examples. DocumentAssessment and ReplyAssessment are still documented contracts, not implemented Python models in this increment.

Generate standard JSON Schema directly, for example:

```python
import json
from closeready.models import CaseSnapshot, Requirement, EvidenceRef, ActionContent, ActionProposal

for model in (CaseSnapshot, Requirement, EvidenceRef, ActionContent, ActionProposal):
    print(json.dumps(model.model_json_schema(), indent=2))
```

These schemas describe the shared boundary, not provider-specific tool parameters. A provider adapter may need to translate unsupported JSON Schema features; do not assume a provider accepts the entire discriminated union unchanged.

Dates use YYYY-MM-DD; timestamps include a timezone and normalize to UTC. All declared nullable fields remain required unless explicitly given a default (currently Requirement.description). Unknown fields and unrecognized actions are rejected. Model output cannot include server provenance fields in ActionContent.

The models validate structure and local consistency. They do not look up evidence ownership, verify quotations, authenticate reviewers, calculate covered intervals, enforce send policies or persist changes. Those checks belong to the next workflow implementation.
