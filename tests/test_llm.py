"""Adapter shape/error tests; live evidence comes from the separate smoke command."""
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from closeready.llm import DeepSeekProvider, ProviderError, parse_completion


class ProviderTests(unittest.TestCase):
    def test_native_call_and_usage_are_parsed(self):
        parsed = parse_completion({'id': 'provider-1', 'choices': [{'finish_reason': 'tool_calls',
            'message': {'role': 'assistant', 'content': None, 'reasoning_content': 'do not persist this',
                'tool_calls': [{'id': 'call-1', 'type': 'function',
                    'function': {'name': 'get_case_context', 'arguments': '{}'}}]}}],
            'usage': {'prompt_tokens': 15, 'completion_tokens': 8, 'private_blob': 'secret'}})
        self.assertEqual(parsed.calls[0].call_id, 'call-1')
        self.assertEqual(parsed.calls[0].arguments, '{}')
        self.assertEqual(parsed.usage, {'prompt_tokens': 15, 'completion_tokens': 8})
        self.assertNotIn('reasoning_content', parsed.message)

    def test_malformed_truncated_or_refused_output_is_explicit(self):
        for data in [{}, {'choices': []}, {'choices': [{'finish_reason': 'length', 'message': {}}]},
                     {'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'no'}}]}]:
            with self.subTest(data=data), self.assertRaises(ProviderError):
                parse_completion(data)

    def test_endpoint_is_restricted(self):
        with self.assertRaises(ValueError):
            DeepSeekProvider('secret', 'model', 'https://example.com')

    def test_http_errors_are_safe_and_classified(self):
        for status, code, retry in [(401, 'AUTHENTICATION', False), (429, 'RATE_LIMIT', True),
                                    (500, 'PROVIDER_UNAVAILABLE', True), (400, 'BAD_REQUEST', False)]:
            with self.subTest(status=status), patch('closeready.llm.build_opener') as opener:
                opener.return_value.open.side_effect = HTTPError('https://api.deepseek.com', status, 'sensitive-body', {}, None)
                with self.assertRaises(ProviderError) as caught:
                    DeepSeekProvider('secret', 'model').complete([], [])
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retry)
                self.assertNotIn('sensitive-body', str(caught.exception))
