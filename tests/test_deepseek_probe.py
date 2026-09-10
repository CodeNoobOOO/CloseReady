import unittest
from scripts.deepseek_probe import ProbeError, validate_call, load_config
from pathlib import Path
from tempfile import TemporaryDirectory


class ProbeTests(unittest.TestCase):
    def call(self, name='get_case_status', arguments='{"case_id":"case_probe"}'):
        return {'id': 'call_1', 'type': 'function', 'function': {'name': name, 'arguments': arguments}}

    def test_valid_call(self):
        self.assertEqual(validate_call(self.call()), 'call_1')

    def test_reject_unsafe_calls(self):
        for call in [self.call('send_email'), self.call(arguments='{"case_id":"other"}'),
                     self.call(arguments='{"case_id":"case_probe","extra":true}'),
                     self.call(arguments='not json'), self.call(arguments='[]')]:
            with self.subTest(call=call), self.assertRaises(ProbeError):
                validate_call(call)

    def test_config_rejects_other_destination_and_hides_key(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('DEEPSEEK_API_KEY=secret-test\nDEEPSEEK_BASE_URL=https://example.com\n')
            with self.assertRaises(ProbeError) as error:
                load_config(path)
            self.assertNotIn('secret-test', str(error.exception))


if __name__ == '__main__':
    unittest.main()
