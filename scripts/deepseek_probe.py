"""Live DeepSeek diagnostic using synthetic data; not the business agent runtime."""
import argparse
import json
import secrets
import sys
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError


class ProbeError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError('Redirect refused; credentials were not forwarded.')


def load_config(path):
    values = {}
    try:
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('\"').strip("'")
    except OSError:
        raise ProbeError('Cannot read local .env.') from None
    if not values.get('DEEPSEEK_API_KEY'):
        raise ProbeError('DEEPSEEK_API_KEY is missing.')
    base = values.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
    if base not in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'):
        raise ProbeError('Only the official DeepSeek HTTPS endpoint is allowed.')
    return base, values['DEEPSEEK_API_KEY'], values.get('DEEPSEEK_MODEL')


def validate_call(call):
    if not isinstance(call, dict) or call.get('type') != 'function' or not isinstance(call.get('id'), str) or not call['id']:
        raise ProbeError('Invalid tool envelope.')
    function = call.get('function', {})
    if not isinstance(function, dict) or function.get('name') != 'get_case_status':
        raise ProbeError('Tool is not allowlisted.')
    try:
        arguments = json.loads(function['arguments'])
    except (KeyError, TypeError, ValueError):
        raise ProbeError('Invalid tool arguments.') from None
    if arguments != {'case_id': 'case_probe'}:
        raise ProbeError('Tool arguments outside the permitted scope.')
    return call['id']


def request(base, key, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(base + path, data=data, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
        'User-Agent': 'CloseReady-Connectivity-Probe'})
    try:
        with build_opener(NoRedirect()).open(req, timeout=45) as response:
            result = json.load(response)
    except HTTPError as error:
        raise ProbeError(f'Provider HTTP {error.code}; response body suppressed.') from None
    except (URLError, TimeoutError, OSError):
        raise ProbeError('Network connection failed or timed out.') from None
    except ValueError:
        raise ProbeError('Provider response is not JSON.') from None
    if not isinstance(result, dict):
        raise ProbeError('Unexpected provider response shape.')
    return result


def message_from(response):
    try:
        choice = response['choices'][0]
        message = choice['message']
        if choice.get('finish_reason') not in ('stop', 'tool_calls') or not isinstance(message, dict):
            raise ValueError()
        return message
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProbeError('Incomplete or invalid model completion.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list-models', action='store_true', help='Only query available models; no inference.')
    parser.add_argument('--model', help='Explicit model returned by the models endpoint.')
    args = parser.parse_args()
    base, key, configured_model = load_config(Path(__file__).resolve().parents[1] / '.env')
    available = request(base, key, '/models').get('data', [])
    models = [item['id'] for item in available if isinstance(item, dict) and isinstance(item.get('id'), str)]
    if args.list_models:
        print(json.dumps({'models': models}))
        return
    model = args.model or configured_model
    if not model or model not in models:
        raise ProbeError('Choose an available model using --model or DEEPSEEK_MODEL.')
    tools = [{'type': 'function', 'function': {'name': 'get_case_status',
        'description': 'Read the status of the only authorised synthetic case case_probe.',
        'parameters': {'type': 'object', 'properties': {'case_id': {'type': 'string', 'enum': ['case_probe']}},
                       'required': ['case_id'], 'additionalProperties': False}}}]
    messages = [{'role': 'system', 'content': 'Use get_case_status before answering. After the tool result, return only a JSON object with requirement_status and verification_marker copied from that result. Do not invent the marker.'},
                {'role': 'user', 'content': 'Check synthetic case_probe using the tool.'}]
    payload = {'model': model, 'messages': messages, 'tools': tools, 'stream': False,
               'max_tokens': 512, 'thinking': {'type': 'disabled'}}
    first = request(base, key, '/chat/completions', payload)
    message = message_from(first)
    calls = message.get('tool_calls') or []
    if len(calls) != 1:
        raise ProbeError('Expected exactly one native tool call; nothing executed.')
    call_id = validate_call(calls[0])
    marker = secrets.token_hex(12)
    result = {'requirement_status': 'needs_clarification', 'verification_marker': marker}
    messages.append(message)
    messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result)})
    final = request(base, key, '/chat/completions', payload)
    answer = message_from(final)
    if answer.get('tool_calls'):
        raise ProbeError('Unexpected extra tool call; step limit reached.')
    try:
        parsed = json.loads(answer.get('content', ''))
    except (ValueError, TypeError):
        raise ProbeError('Final answer was not strict JSON.') from None
    if parsed != result:
        raise ProbeError('Model did not correctly use the tool result.')
    print(json.dumps({'provider': 'deepseek', 'model': model, 'native_tool_call': True,
                      'validated_tool': 'get_case_status', 'round_trip_passed': True,
                      'inference_requests': 2, 'usage': [first.get('usage'), final.get('usage')]}))


if __name__ == '__main__':
    try:
        main()
    except ProbeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
