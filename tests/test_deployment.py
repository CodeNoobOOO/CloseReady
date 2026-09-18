"""Deployment boundary tests that do not require Docker or live credentials."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import text

from closeready.api import create_app
from closeready.config import AccessConfig


ROOT = Path(__file__).resolve().parents[1]


def access_config():
    token_hash = hashlib.sha256(b'deployment-test-token').hexdigest()
    return AccessConfig.model_validate({
        'principals': [{
            'user_id': 'deployment_test_manager',
            'token_sha256': token_hash,
            'client_ids': ['deployment_test_client'],
            'can_manage': True,
        }],
        'policies': [{
            'policy_id': 'deployment_test_policy',
            'version': 1,
            'client_ids': ['deployment_test_client'],
            'approved_by': 'deployment_test_manager',
            'approved_at': '2026-09-11T00:00:00Z',
        }],
    })


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        database = Path(self.tmp.name) / 'health.db'
        self.app = create_app('sqlite:///' + database.as_posix(), access_config())
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_health_endpoints_are_public_and_typed(self):
        live = self.client.get('/health/live')
        ready = self.client.get('/health/ready')

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json(), {'status': 'ok'})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {'status': 'ready'})
        schema = self.client.get('/openapi.json').json()
        self.assertIn('HealthStatus', schema['components']['schemas'])

    def test_readiness_failure_is_safe_and_liveness_remains_available(self):
        with self.app.state.store.engine.begin() as conn:
            conn.execute(text("CREATE TRIGGER fail_health BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT, 'SENSITIVE DATABASE DETAIL'); END"))
        ready = self.client.get('/health/ready')

        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()['error']['code'], 'NOT_READY')
        self.assertNotIn('SENSITIVE DATABASE DETAIL', ready.text)
        self.assertEqual(self.client.get('/health/live').status_code, 200)

    def test_readiness_write_probe_rolls_back_and_checks_runtime_schema(self):
        with self.app.state.store.engine.connect() as conn:
            before = conn.execute(text('SELECT COUNT(*) FROM audit_events')).scalar_one()
        self.assertEqual(self.client.get('/health/ready').status_code, 200)
        with self.app.state.store.engine.connect() as conn:
            after = conn.execute(text('SELECT COUNT(*) FROM audit_events')).scalar_one()
        self.assertEqual(after, before)

        with self.app.state.store.engine.begin() as conn:
            conn.execute(text('DROP TABLE agent_traces'))
        self.assertEqual(self.client.get('/health/ready').status_code, 503)


class DeploymentArtifactTests(unittest.TestCase):
    def test_image_runs_as_non_root_and_starts_the_api(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')

        self.assertIn('FROM python:3.11-slim', dockerfile)
        self.assertIn('USER closeready', dockerfile)
        self.assertIn('--uid 10001', dockerfile)
        self.assertIn('EXPOSE 8000', dockerfile)
        self.assertIn('closeready.api:from_env', dockerfile)
        self.assertNotIn('COPY .env', dockerfile)

    def test_compose_separates_api_and_worker_with_shared_persistence(self):
        compose = (ROOT / 'deploy/compose.yaml').read_text(encoding='utf-8')

        self.assertIn('api:', compose)
        self.assertIn('worker:', compose)
        self.assertIn('closeready.worker', compose)
        self.assertIn('/health/ready', compose)
        self.assertGreaterEqual(compose.count('closeready-data:/data'), 2)
        self.assertGreaterEqual(compose.count('server-config.json:/run/secrets/server-config.json:ro'), 2)
        self.assertGreaterEqual(compose.count('read_only: true'), 2)
        self.assertGreaterEqual(compose.count('cap_drop:'), 2)
        self.assertGreaterEqual(compose.count('user: "10001:10001"'), 2)

    def test_build_context_excludes_secrets_and_local_state(self):
        ignored = (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()

        for entry in ('.env', '.git', '.venv', 'local-data', 'deploy/runtime.env',
                      'deploy/secrets', 'deploy/restore'):
            self.assertIn(entry, ignored)

    def test_ci_runs_deterministic_checks_without_live_credentials(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')

        for command in ('pytest', 'compileall', 'pip check', 'docker build'):
            self.assertIn(command, workflow)
        self.assertNotIn('DEEPSEEK_API_KEY', workflow)
        self.assertNotIn('LLM_API_KEY', workflow)
        self.assertNotIn('scripts.live_case_analysis', workflow)


if __name__ == '__main__':
    unittest.main()
