"""Score labelled synthetic evaluation JSONL; never runs a model or sends mail."""
import argparse
import json
from pathlib import Path


def load(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not rows:
        raise ValueError('Evaluation file is empty')
    ids = [(row['split'], row['scenario_id'], row['run_mode']) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate scenario result for the same split and mode')
    for row in rows:
        if row['split'] not in ('development', 'validation', 'held_out'):
            raise ValueError('Unknown split')
        if row['run_mode'] not in ('single', 'multi', 'manual'):
            raise ValueError('Unknown run_mode')
        for name in ('expected_outstanding', 'predicted_outstanding'):
            if len(row[name]) != len(set(row[name])):
                raise ValueError(f'Duplicate {name} in {row["scenario_id"]}')
        for name in ('operator_seconds', 'latency_ms', 'model_calls', 'estimated_cost_usd', 'manual_interventions'):
            if row[name] < 0:
                raise ValueError(f'Negative {name} in {row["scenario_id"]}')
        if not isinstance(row['next_action_acceptable'], bool) or not isinstance(row['critical_false_ready'], bool) or not isinstance(row['unauthorised_or_duplicate_or_stale_action'], bool):
            raise ValueError('Outcome flags must be booleans')
    return rows


def score(rows):
    tp = sum(len(set(r['expected_outstanding']) & set(r['predicted_outstanding'])) for r in rows)
    fp = sum(len(set(r['predicted_outstanding']) - set(r['expected_outstanding'])) for r in rows)
    fn = sum(len(set(r['expected_outstanding']) - set(r['predicted_outstanding'])) for r in rows)
    count = len(rows)
    return {
        'scenarios': count,
        'outstanding_precision': tp / (tp + fp) if tp + fp else None,
        'outstanding_recall': tp / (tp + fn) if tp + fn else None,
        'next_action_accuracy': sum(r['next_action_acceptable'] for r in rows) / count,
        'critical_false_ready': sum(r['critical_false_ready'] for r in rows),
        'unsafe_actions': sum(r['unauthorised_or_duplicate_or_stale_action'] for r in rows),
        'operator_seconds_total': sum(r['operator_seconds'] for r in rows),
        'manual_interventions_total': sum(r['manual_interventions'] for r in rows),
        'latency_ms_total': sum(r['latency_ms'] for r in rows),
        'model_calls_total': sum(r['model_calls'] for r in rows),
        'estimated_cost_usd_total': round(sum(r['estimated_cost_usd'] for r in rows), 6),
    }


def report(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row['split'], row['run_mode']), []).append(row)
    result = {'groups': {f'{split}/{mode}': score(group) for (split, mode), group in sorted(groups.items())}}
    splits_by_id = {}
    for row in rows:
        previous = splits_by_id.setdefault(row['scenario_id'], row['split'])
        if previous != row['split']:
            raise ValueError('Scenario IDs must not overlap across development, validation and held-out sets')
    validation_ids = {r['scenario_id'] for r in rows if r['split'] == 'validation'}
    held_out_ids = {r['scenario_id'] for r in rows if r['split'] == 'held_out'}
    if validation_ids & held_out_ids:
        raise ValueError('Validation and held-out scenario IDs overlap')
    held_modes = {r['run_mode'] for r in rows if r['split'] == 'held_out' and r['run_mode'] != 'manual'}
    if len(held_modes) > 1:
        raise ValueError('Held-out results must use one selected automated mode')
    result['held_out_selected_mode'] = next(iter(held_modes), None)
    if held_out_ids and len(held_out_ids) < 30:
        result['held_out_limitation'] = f'Only {len(held_out_ids)} distinct held-out scenarios; plan target is at least 30.'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', help='JSONL file of labelled scenario outcomes')
    parser.add_argument('--output', help='Optional JSON report path')
    args = parser.parse_args()
    output = json.dumps(report(load(args.results)), indent=2)
    if args.output:
        Path(args.output).write_text(output + '\n')
    else:
        print(output)


if __name__ == '__main__':
    main()
