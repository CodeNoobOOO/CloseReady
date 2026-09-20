"""No-network tests of the live-review coordinator's application boundary."""
import unittest
from types import SimpleNamespace
from closeready.document_ai_review import AIDocumentReviewer
from closeready.llm_document_analyzer import LLMDocumentAnalysis, LLMDocumentEvidence
from closeready.models import CaseSnapshot
from test_case_api import case_request
from test_document_worker import extracted

class AIReviewTests(unittest.TestCase):
    def setUp(self):
        from closeready.case_requests import CreateCaseRequest
        r=CreateCaseRequest.model_validate(case_request())
        self.case=CaseSnapshot.model_validate({**r.model_dump(), 'case_id':'test',
            'state_version':1,'readiness_status':'collecting','policy_version':1,
            'requirements':[x.to_requirement('req') for x in r.requirements]})
        self.reviewer=AIDocumentReviewer(SimpleNamespace(live=True,model='test-model'))
    def assess(self,analysis,extraction=None):
        self.reviewer.analyzer=SimpleNamespace(analyze=lambda request:analysis)
        return self.reviewer(case=self.case,document_id='doc',extraction=extraction or extracted(),requirement_id='req')
    def test_fabricated_quote_requires_human_review(self):
        finding=self.assess(LLMDocumentAnalysis(evidence=[LLMDocumentEvidence(field='type',page=1,excerpt='fabricated')]))
        self.assertEqual(finding.result,'needs_review')
        self.assertEqual(finding.analysis_error,'AI_REVIEW_UNAVAILABLE')
    def test_provider_failure_preserves_wrong_period_issue(self):
        self.reviewer.analyzer=SimpleNamespace(analyze=lambda request:(_ for _ in ()).throw(RuntimeError('secret provider body')))
        finding=self.reviewer(case=self.case,document_id='doc',extraction=extracted('DBS Bank Statement\nStatement Period: 01 June 2026 to 30 June 2026\nEntity ID: entity_demo\nAccount Ref: account_demo'),requirement_id='req')
        self.assertEqual(finding.result,'needs_review')
        self.assertTrue(finding.issues)
        self.assertNotIn('secret',finding.model_dump_json())
    def test_uncertain_ai_result_never_auto_accepts(self):
        finding=self.assess(LLMDocumentAnalysis(detected_type='bank_statement',uncertainty_reasons=['Unclear period'],evidence=[LLMDocumentEvidence(field='type',page=1,excerpt='DBS Bank Statement')]))
        self.assertEqual(finding.result,'needs_review')
        self.assertEqual(finding.analysis_source,'live_llm')
