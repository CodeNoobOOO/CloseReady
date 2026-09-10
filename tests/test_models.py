import copy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError
from closeready.models import CaseSnapshot, EvidenceRef, ActionProposal, ActionContent

ROOT = Path(__file__).resolve().parents[1]


def fixture(prefix):
    path = next((ROOT / 'examples').glob(prefix + '*.json'))
    return json.loads(path.read_text(encoding='utf-8'))


class ContractTests(unittest.TestCase):
    def test_case_fixture_roundtrip(self):
        data = fixture('case')
        model = CaseSnapshot.model_validate(data)
        self.assertEqual(model, CaseSnapshot.model_validate_json(model.model_dump_json()))
        self.assertEqual(model.due_at.utcoffset().total_seconds(), 0)

    def test_case_rejects_invalid_business_shapes(self):
        mutations = [
            lambda d: d.update(state_version=True),
            lambda d: d.update(state_version='1'),
            lambda d: d.update(accounting_period='2026-13'),
            lambda d: d.update(timezone='Not/AZone'),
            lambda d: d.update(due_at='2026-09-15T09:00:00'),
            lambda d: d.update(unexpected=True),
            lambda d: d['requirements'][0]['scope'].update(coverage_end=None),
            lambda d: d['requirements'][0]['scope'].update(coverage_end='2026-06-01'),
            lambda d: d['requirements'][0]['completion_rule'].update(kind='explicit_items'),
            lambda d: d['requirements'][0].update(accounting_period='2026-06'),
            lambda d: d['requirements'].append(copy.deepcopy(d['requirements'][0])),
            lambda d: d.update(requirements=[], readiness_status='ready'),
            lambda d: d.update(readiness_status='ready_for_confirmation'),
            lambda d: d['requirements'][0].update(document_type='other_supporting_document'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                data = fixture('case')
                mutate(data)
                with self.assertRaises(ValidationError):
                    CaseSnapshot.model_validate(data)

    def test_explicit_items_and_other_document_configuration(self):
        data = fixture('case')
        req = data['requirements'][0]
        req.update(document_type='other_supporting_document', description='Payroll report')
        req['completion_rule'].update(kind='explicit_items', expected_item_refs=['payroll-july'])
        req['scope'].update(coverage_start=None, coverage_end=None)
        self.assertEqual(CaseSnapshot.model_validate(data).requirements[0].description, 'Payroll report')

    def test_evidence_requires_real_page_shape(self):
        for page in [0, -1, True, '1']:
            with self.subTest(page=page), self.assertRaises(ValidationError):
                EvidenceRef(document_id='doc', page=page, excerpt='source text')
        self.assertIsNone(EvidenceRef(document_id='doc', page=None, excerpt='text').page)

    def test_action_fixture_and_discriminator(self):
        data = fixture('action')
        self.assertEqual(ActionProposal.model_validate(data).action_type, 'apply_document_finding')
        for changes in [dict(action_type='waive'), dict(payload={'sql': 'UPDATE cases'}),
                        dict(finding_ids=[]), dict(expected_state_version=0)]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ActionProposal.model_validate(dict(data, **changes))

    def test_all_action_payloads(self):
        base = fixture('action')
        payloads = {
            'apply_document_finding': {'finding_id': 'finding_document_demo'},
            'record_commitment': {'finding_id': 'finding_document_demo', 'promised_at': '2026-09-11T17:00:00+08:00'},
            'request_documents': {'subject': 'Documents', 'body': 'Please upload', 'requirement_ids': base['requirement_ids']},
            'request_clarification': {'subject': 'Clarify', 'body': 'Which period?', 'requirement_ids': base['requirement_ids']},
            'schedule_reminder': {'scheduled_at': '2026-09-11T17:00:00Z', 'draft': {'subject': 'Documents', 'body': 'Please upload', 'requirement_ids': base['requirement_ids']}},
            'create_review_task': {'issue': 'Unclear account', 'evidence_refs': []},
            'no_action': {},
        }
        for action, payload in payloads.items():
            with self.subTest(action=action):
                parsed = ActionProposal.model_validate(dict(base, action_type=action, payload=payload))
                self.assertEqual(parsed.action_type, action)
        invalid = dict(base, action_type='request_documents', payload=dict(payloads['request_documents'], recipient='evil@example.com'))
        with self.assertRaises(ValidationError):
            ActionProposal.model_validate(invalid)

    def test_llm_content_cannot_supply_server_metadata(self):
        data = fixture('action')
        with self.assertRaises(ValidationError):
            ActionContent.model_validate(data)
        for key in ['proposal_id', 'run_id', 'case_id', 'expected_state_version']:
            del data[key]
        self.assertEqual(ActionContent.model_validate(data).action_type, 'apply_document_finding')

    def test_schema_exposes_action_union(self):
        schema = ActionProposal.model_json_schema()
        self.assertIn('discriminator', schema)
        self.assertEqual(len(schema['oneOf']), 7)


if __name__ == '__main__':
    unittest.main()
