import json
import unittest
from closeready.llm import ProviderError, parse_completion
from closeready.provider_check import check_provider


class CheckDouble:
    provider_name, model, live = 'scripted_test', 'test-model', False

    def __init__(self, native=True, copy_result=True, fenced_result=False):
        self.native, self.copy_result = native, copy_result
        self.fenced_result, self.count = fenced_result, 0

    def complete(self, messages, tools):
        self.count += 1
        if self.count == 1 and self.native:
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'check-call', 'type': 'function', 'function': {'name': 'get_probe_marker', 'arguments': '{}'}}]}
            finish = 'tool_calls'
        else:
            content = messages[-1]['content'] if self.copy_result else '{"marker":"invented"}'
            if self.fenced_result:
                content = f'The marker is:\n\n```json\n{content}\n```'
            msg, finish = {'role': 'assistant', 'content': content}, 'stop'
        return parse_completion({'choices': [{'finish_reason': finish, 'message': msg}]})


class ProviderCheckTests(unittest.TestCase):
    def test_native_round_trip_returns_check_result(self):
        provider = CheckDouble()
        result = check_provider(provider)
        self.assertEqual(provider.count, 2)
        self.assertTrue(result['native_tool_round_trip'])
        self.assertEqual(result['provider'], 'scripted_test')
        self.assertFalse(result['live'])

    def test_text_only_completion_is_not_tool_support(self):
        with self.assertRaises(ProviderError):
            check_provider(CheckDouble(native=False))

    def test_invented_tool_result_fails(self):
        with self.assertRaises(ProviderError):
            check_provider(CheckDouble(copy_result=False))

    def test_fenced_tool_result_still_proves_native_round_trip(self):
        result = check_provider(CheckDouble(fenced_result=True))
        self.assertTrue(result['native_tool_round_trip'])
