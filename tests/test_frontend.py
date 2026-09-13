import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from closeready.api import create_app
from closeready.config import load_access_config


class FrontendTests(unittest.TestCase):
    def test_dashboard_assets_are_served_without_cors(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(f'sqlite:///{tmp}/case.db', load_access_config(root / 'examples/server-config.json'))
            with TestClient(app) as client:
                page = client.get('/app')
                script = client.get('/app/app.js')
                style = client.get('/app/style.css')
            self.assertEqual(page.status_code, 200)
            self.assertIn('CloseReady', page.text)
            self.assertIn('/api/v1', script.text)
            self.assertIn('text/javascript', script.headers['content-type'])
            self.assertIn('text/css', style.headers['content-type'])


if __name__ == '__main__':
    unittest.main()
