"""Live synthetic API-to-LLM-to-database check; consumes provider credit, sends no mail.

Run from repository root: python -m scripts.live_case_analysis --model deepseek-flash
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient

from closeready.api import create_app
from closeready.config import AccessConfig
from closeready.provider_factory import provider_from_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    os.environ.setdefault('CLOSEREADY_LLM_ENV_FILE', '.env')
    os.environ['LLM_MODEL'] = args.model
    provider = provider_from_environment()
    root = Path('local-data') / ('live-analysis-' + uuid4().hex[:12])
    root.mkdir(parents=True)
    token = secrets.token_urlsafe(32)
    config_data = json.loads(Path('examples/server-config.json').read_text(encoding='utf-8'))
    config_data['principals'][0]['token_sha256'] = hashlib.sha256(token.encode()).hexdigest()
    access = AccessConfig.model_validate(config_data)
    payload = json.loads(Path('examples/create-case.json').read_text(encoding='utf-8'))
    payload['requirements'].append({'document_type': 'invoice', 'accounting_period': '2026-07',
        'scope': {'entity_id': 'entity_demo', 'account_ref': None, 'coverage_start': None, 'coverage_end': None},
        'completion_rule': {'kind': 'explicit_items', 'expected_item_refs': ['SYNTHETIC-INV-A17', 'SYNTHETIC-INV-B29'],
                            'allow_multiple_documents': True}})
    headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'create-live'}
    url = 'sqlite:///' + (root / 'cases.db').resolve().as_posix()
    with TestClient(create_app(url, access, provider=provider)) as client:
        created = client.post('/api/v1/cases', json=payload, headers=headers)
        if created.status_code != 201:
            raise RuntimeError('Synthetic case creation failed.')
        case = created.json()
        headers['Idempotency-Key'] = 'analyse-live'
        response = client.post('/api/v1/cases/' + case['case_id'] + '/runs',
            json={'expected_state_version': 1}, headers=headers)
        if response.status_code != 200:
            raise RuntimeError('Analysis API failed; no success claimed.')
        run = response.json()
        tasks = client.get('/api/v1/cases/' + case['case_id'] + '/review-tasks', headers=headers).json()['items']
        after = client.get('/api/v1/cases/' + case['case_id'], headers=headers).json()
        expected = {r['requirement_id'] for r in case['requirements']}
        passed = (run['status'] == 'needs_review' and run['error_code'] is None and len(tasks) == 1
            and tasks[0]['draft'] is not None and not tasks[0]['sent']
            and set(tasks[0]['requirement_ids']) == expected and after['state_version'] == 2
            and all(r['status'] == 'missing' for r in after['requirements']))
        summary = {'passed': passed, 'live': run['live'], 'provider': run['provider'], 'model': run['model'], 'run_id': run['run_id'],
            'run_status': run['status'], 'error_code': run['error_code'], 'inference_attempts': len(run['traces']),
            'usage': [t['usage'] for t in run['traces']], 'review_task_count': len(tasks),
            'mail_sent': False, 'database_path': str(root / 'cases.db')}
        (root / 'result.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
