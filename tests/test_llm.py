"""Adapter shape/error tests; live evidence comes from the separate smoke command."""
import json
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from closeready.llm import DeepSeekProvider, OllamaGatewayProvider, ProviderError, parse_completion
from closeready.provider_check import check_provider


class ProviderTests(unittest.TestCase):
    def test_ollama_gateway_native_tool_call_is_translated_to_canonical_completion(self):
        response = {'model': 'model', 'message': {'role': 'assistant', 'content': '',
            'tool_calls': [{'function': {'name': 'get_case_context', 'arguments': {}}}]},
            'done': True, 'done_reason': 'tool_calls', 'prompt_eval_count': 15, 'eval_count': 8}
        tools = [{'type': 'function', 'function': {'name': 'get_case_context',
            'description': 'Read context.', 'parameters': {'type': 'object', 'properties': {}}}}]
        with patch('closeready.llm.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(json.dumps(response).encode())
            completion = OllamaGatewayProvider('gateway-key', 'model',
                'https://api.softwaresystems.app').complete(
                    [{'role': 'user', 'content': 'Read the case.'}], tools)
        request = opener.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://api.softwaresystems.app/api/chat')
        self.assertEqual(request.get_header('X-api-key'), 'gateway-key')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(body['options'], {'num_predict': 1200})
        self.assertEqual(body['tools'], tools)
        self.assertEqual(completion.calls[0].name, 'get_case_context')
        self.assertEqual(completion.calls[0].arguments, '{}')
        self.assertTrue(completion.calls[0].call_id)
        self.assertEqual(completion.finish_reason, 'tool_calls')
        self.assertEqual(completion.usage,
            {'prompt_tokens': 15, 'completion_tokens': 8, 'total_tokens': 23})

    def test_ollama_gateway_translates_canonical_tool_result_for_second_turn(self):
        provider = OllamaGatewayProvider('gateway-key', 'model', 'https://api.softwaresystems.app')
        messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'internal-call-1',
                'type': 'function', 'function': {'name': 'get_case_context', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'internal-call-1', 'content': '{"case":"demo"}'},
        ]
        with patch('closeready.llm.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(json.dumps({'model': 'model',
                'message': {'role': 'assistant', 'content': 'done'}, 'done': True,
                'done_reason': 'stop'}).encode())
            completion = provider.complete(messages, [])
        body = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(body['messages'][0], {'role': 'assistant', 'content': '',
            'tool_calls': [{'function': {'name': 'get_case_context', 'arguments': {}}}]})
        self.assertEqual(body['messages'][1], {'role': 'tool',
            'tool_name': 'get_case_context', 'content': '{"case":"demo"}'})
        self.assertEqual(completion.message, {'role': 'assistant', 'content': 'done'})

    def test_ollama_gateway_passes_two_request_native_tool_round_trip(self):
        first = {'model': 'model', 'message': {'role': 'assistant', 'content': '',
            'tool_calls': [{'function': {'name': 'get_probe_marker', 'arguments': {}}}]},
            'done': True, 'done_reason': 'tool_calls'}

        class SequencedOpener:
            def __init__(self):
                self.requests = []

            def open(inner_self, request, timeout):
                inner_self.requests.append(request)
                if len(inner_self.requests) == 1:
                    return BytesIO(json.dumps(first).encode())
                body = json.loads(request.data)
                marker = json.loads(body['messages'][-1]['content'])
                return BytesIO(json.dumps({'model': 'model', 'message': {
                    'role': 'assistant', 'content': json.dumps(marker)},
                    'done': True, 'done_reason': 'stop'}).encode())

        opener = SequencedOpener()
        with patch('closeready.llm.build_opener', return_value=opener):
            result = check_provider(OllamaGatewayProvider(
                'gateway-key', 'model', 'https://api.softwaresystems.app'))
        self.assertEqual(len(opener.requests), 2)
        second = json.loads(opener.requests[1].data)
        self.assertEqual(second['messages'][-1]['role'], 'tool')
        self.assertEqual(second['messages'][-1]['tool_name'], 'get_probe_marker')
        self.assertTrue(result['native_tool_round_trip'])

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
