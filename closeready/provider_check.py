"""Provider-independent native tool check using a fresh synthetic marker."""
import json
import secrets
from .llm import LLMProvider, ProviderError


def check_provider(provider: LLMProvider) -> dict:
    tools = [{'type': 'function', 'function': {'name': 'get_probe_marker',
        'description': 'Read a fresh marker. Takes no arguments; has no business side effects.',
        'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}}]
    messages = [{'role': 'system', 'content': 'Call get_probe_marker first. Then return only the exact JSON object from the tool result, without markdown.'},
        {'role': 'user', 'content': 'Read the marker using the tool.'}]
    first = provider.complete(messages, tools)
    if len(first.calls) != 1 or first.calls[0].name != 'get_probe_marker':
        raise ProviderError('NATIVE_TOOL_CHECK_FAILED', False)
    call = first.calls[0]
    try:
        if json.loads(call.arguments) != {}:
            raise ValueError()
    except (TypeError, ValueError):
        raise ProviderError('NATIVE_TOOL_CHECK_FAILED', False) from None
    result = {'marker': secrets.token_hex(16)}
    messages.extend([first.message, {'role': 'tool', 'tool_call_id': call.call_id, 'content': json.dumps(result)}])
    final = provider.complete(messages, tools)
    try:
        if final.calls or json.loads(final.message.get('content', '')) != result:
            raise ValueError()
    except (TypeError, ValueError):
        raise ProviderError('TOOL_RESULT_CHECK_FAILED', False) from None
    return {'provider': provider.provider_name, 'model': provider.model, 'live': provider.live,
        'native_tool_round_trip': True, 'inference_requests': 2, 'usage': [first.usage, final.usage]}
