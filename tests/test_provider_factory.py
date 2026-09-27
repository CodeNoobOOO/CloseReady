import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from closeready.llm import DeepSeekProvider, LLMProvider, OllamaGatewayProvider, OpenAICompatibleProvider
from closeready.provider_factory import provider_from_environment


class ProviderConfigurationTests(unittest.TestCase):
    def test_existing_deepseek_configuration_still_works(self):
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'legacy-key', 'DEEPSEEK_MODEL': 'legacy-model'}, clear=True):
            provider = provider_from_environment()
        self.assertIsInstance(provider, DeepSeekProvider)
        self.assertIsInstance(provider, LLMProvider)
        self.assertEqual(provider.model, 'legacy-model')

    def test_generic_factory_selects_independent_configuration(self):
        with patch.dict('os.environ', {'LLM_PROVIDER': 'openai_compatible', 'LLM_API_KEY': 'generic-key',
            'LLM_MODEL': 'team-model', 'LLM_BASE_URL': 'https://llm.example.com/v1',
            'DEEPSEEK_API_KEY': 'must-not-use-this'}, clear=True):
            provider = provider_from_environment()
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertEqual(provider.provider_name, 'openai_compatible')
        self.assertEqual(provider.model, 'team-model')

    def test_generic_never_inherits_legacy_key(self):
        with patch.dict('os.environ', {'LLM_PROVIDER': 'openai_compatible', 'LLM_MODEL': 'team-model',
            'LLM_BASE_URL': 'https://llm.example.com/v1', 'DEEPSEEK_API_KEY': 'do-not-leak'}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                provider_from_environment()
        self.assertNotIn('do-not-leak', str(caught.exception))

    def test_factory_selects_ollama_gateway_without_legacy_key_fallback(self):
        with patch.dict('os.environ', {'LLM_PROVIDER': 'ollama_gateway',
            'LLM_API_KEY': 'gateway-key', 'LLM_MODEL': 'team-model',
            'LLM_BASE_URL': 'https://api.softwaresystems.app',
            'DEEPSEEK_API_KEY': 'must-not-use-this'}, clear=True):
            provider = provider_from_environment()
        self.assertIsInstance(provider, OllamaGatewayProvider)
        self.assertIsInstance(provider, LLMProvider)
        self.assertEqual(provider.provider_name, 'ollama_gateway')
        self.assertEqual(provider.model, 'team-model')

    def test_environment_overrides_file_and_unknown_provider_fails(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'config.env'
            path.write_text('LLM_PROVIDER=deepseek\nLLM_MODEL=file-model\nLLM_API_KEY=file-key\n', encoding='utf-8')
            with patch.dict('os.environ', {'CLOSEREADY_LLM_ENV_FILE': str(path), 'LLM_MODEL': 'env-model'}, clear=True):
                self.assertEqual(provider_from_environment().model, 'env-model')
            with patch.dict('os.environ', {'LLM_PROVIDER': 'not-supported'}, clear=True):
                with self.assertRaises(RuntimeError):
                    provider_from_environment()

    def test_endpoint_and_bounds_are_validated(self):
        for url in ['http://example.com/v1', 'https://user:password@example.com/v1',
                    'https://example.com/v1?key=secret', 'https://example.com/v1#fragment', 'https://']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                OpenAICompatibleProvider('key', 'model', url)
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider('key', 'model', 'https://example.com', timeout=1000)

    def test_wire_payload_does_not_leak_deepseek_parameters_or_key(self):
        for provider, wants_thinking, key in [
            (DeepSeekProvider('deepseek-key', 'model'), True, 'deepseek-key'),
            (OpenAICompatibleProvider('generic-key', 'model', 'https://llm.example.com/v1'), False, 'generic-key')]:
            with self.subTest(provider=provider.provider_name), patch('closeready.llm.build_opener') as opener:
                opener.return_value.open.return_value = BytesIO(json.dumps({
                    'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}]}).encode())
                provider.complete([{'role': 'user', 'content': 'hello'}], [])
                request = opener.return_value.open.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual('thinking' in body, wants_thinking)
                self.assertEqual(request.get_header('Authorization'), 'Bearer ' + key)
                self.assertNotIn(key, str(body))
                self.assertEqual(request.full_url, provider.base_url + '/chat/completions')

    def test_output_limit_field_is_explicit(self):
        with patch.dict('os.environ', {'LLM_PROVIDER': 'openai_compatible', 'LLM_API_KEY': 'key',
            'LLM_MODEL': 'model', 'LLM_BASE_URL': 'https://llm.example.com/v1',
            'LLM_OUTPUT_TOKEN_FIELD': 'max_completion_tokens', 'LLM_MAX_OUTPUT_TOKENS': '900'}, clear=True):
            provider = provider_from_environment()
        with patch('closeready.llm.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(json.dumps({
                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}]}).encode())
            provider.complete([], [])
            body = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(body['max_completion_tokens'], 900)
        self.assertNotIn('max_tokens', body)


if __name__ == '__main__':
    unittest.main()
