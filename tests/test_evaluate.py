import tempfile
import unittest
from pathlib import Path

from scripts.evaluate import load, report, score


def row(scenario_id='a', split='validation', mode='single'):
    return dict(scenario_id=scenario_id, split=split, run_mode=mode,
                expected_outstanding=['bank'], predicted_outstanding=['bank', 'invoice'],
                next_action_acceptable=True, critical_false_ready=False,
                unauthorised_or_duplicate_or_stale_action=False,
                operator_seconds=30, manual_interventions=1, latency_ms=100,
                model_calls=1, estimated_cost_usd=.001)


class EvaluationTests(unittest.TestCase):
    def test_precision_recall_and_cost(self):
        result = score([row()])
        self.assertEqual(result['outstanding_precision'], .5)
        self.assertEqual(result['outstanding_recall'], 1)
        self.assertEqual(result['estimated_cost_usd_total'], .001)

    def test_held_out_mode_cannot_change(self):
        with self.assertRaises(ValueError):
            report([row('a', 'held_out', 'single'), row('b', 'held_out', 'multi')])

    def test_matching_scenario_across_modes_is_allowed(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'results.jsonl'
            path.write_text('\n'.join(json.dumps(row(mode=mode)) for mode in ('single', 'multi', 'manual')))
            results = report(load(path))
            self.assertEqual(len(results['groups']), 3)

    def test_development_cannot_leak_into_held_out(self):
        with self.assertRaises(ValueError):
            report([row('same', 'development'), row('same', 'held_out')])

    def test_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'results.jsonl'
            import json
            path.write_text(json.dumps(row()) + '\n' + json.dumps(row()) + '\n')
            with self.assertRaises(ValueError):
                load(path)


if __name__ == '__main__':
    unittest.main()
