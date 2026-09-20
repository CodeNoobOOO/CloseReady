"""Live document first-pass analysis, with conservative application checks."""
from .document_assessment import assess_document, assess_llm_analysis
from .llm_document_analyzer import (
    CandidateRequirement, DocumentAnalysisRequest, LiveLLMDocumentAnalyzer,
)


class AIDocumentReviewer:
    def __init__(self, provider):
        if not provider.live:
            raise ValueError('AI document review requires a live provider.')
        self.provider = provider
        self.analyzer = LiveLLMDocumentAnalyzer(provider)

    def __call__(self, *, case, document_id, extraction, requirement_id, duplicate=False):
        rule = assess_document(case=case, document_id=document_id, extraction=extraction,
                               requirement_id=requirement_id, duplicate=duplicate)
        metadata = {'analysis_source': 'live_llm', 'analysis_model': self.provider.model}
        candidates = [r for r in case.requirements
                      if r.status not in ('accepted', 'waived')
                      and (requirement_id is None or r.requirement_id == requirement_id)]
        try:
            if not extraction.readable or not candidates:
                raise ValueError('No readable evidence or eligible requirement')
            if sum(len(p.text) for p in extraction.pages) > 60000:
                raise ValueError('Document exceeds analysis context limit')
            analysis = self.analyzer.analyze(DocumentAnalysisRequest(
                pages=[p.text for p in extraction.pages], accounting_period=case.accounting_period,
                candidate_requirements=[CandidateRequirement(
                    requirement_id=r.requirement_id, document_type=r.document_type,
                    accounting_period=r.accounting_period, entity_name=r.scope.entity_id,
                    masked_account_identifier=r.scope.masked_account_identifier,
                    coverage_start=r.scope.coverage_start, coverage_end=r.scope.coverage_end,
                    expected_item_refs=r.completion_rule.expected_item_refs,
                ) for r in candidates],
            ))
            # A model quote must occur on the claimed page; fabricated evidence never passes.
            pages = {p.page: ' '.join(p.text.split()) for p in extraction.pages}
            if not analysis.evidence or any(
                not ' '.join(e.excerpt.split()) or
                ' '.join(e.excerpt.split()) not in pages.get(e.page, '')
                for e in analysis.evidence
            ):
                raise ValueError('Ungrounded evidence')
            finding = assess_llm_analysis(case=case, document_id=document_id,
                analysis=analysis, requirement_id=requirement_id, duplicate=duplicate)
        except Exception:
            # Preserve deterministic issues, but never silently accept after failed inference.
            return rule.model_copy(update={**metadata, 'result': 'needs_review',
                'analysis_error': 'AI_REVIEW_UNAVAILABLE',
                'uncertainty_reasons': list(dict.fromkeys(rule.uncertainty_reasons +
                    ['AI review could not be verified. Human review is required.']))})
        issues = list(dict.fromkeys(rule.issues + finding.issues))
        uncertainty = list(dict.fromkeys(rule.uncertainty_reasons + finding.uncertainty_reasons))
        # LLM inference cannot override a known rule conflict or unresolved identity.
        result = finding.result
        if rule.result == 'needs_correction':
            result = 'needs_correction'
        elif result == 'satisfies' and rule.result != 'satisfies':
            result = 'needs_review'
        if result == 'satisfies' and (issues or uncertainty):
            result = 'needs_review'
        if result == 'satisfies':
            # Retain the independently verified identity and coverage for the existing gate.
            return rule.model_copy(update={**metadata, 'issues': issues,
                                           'uncertainty_reasons': uncertainty})
        return finding.model_copy(update={**metadata, 'result': result,
            'issues': issues, 'uncertainty_reasons': uncertainty,
            'evidence_refs': finding.evidence_refs or rule.evidence_refs})
