"""Canonical provider interface and Chat Completions adapters; no mock fallback."""
from dataclasses import dataclass
import json
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ProviderError(Exception):
    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code, self.retryable = code, retryable


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Completion:
    message: dict
    calls: list[ToolCall]
    usage: dict | None
    finish_reason: str
    request_id: str | None


@runtime_checkable
class LLMProvider(Protocol):
    """Adapters translate their wire format to canonical chat/tool messages.

    complete performs one request. Runtime owns retries and business authority.
    A provider must preserve tool-call IDs and their correlated results.
    """
    provider_name: str
    model: str
    live: bool

    def complete(self, messages: list, tools: list) -> Completion: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError('REDIRECT_REFUSED', False)


def parse_completion(data: dict) -> Completion:
    try:
        choice = data['choices'][0]
        message = choice['message']
        finish = choice['finish_reason']
        if message.get('refusal'):
            raise ProviderError('REFUSAL', False)
        if finish not in ('stop', 'tool_calls'):
            raise ProviderError('INCOMPLETE_OUTPUT', False)
        if message.get('role') != 'assistant':
            raise ValueError()
        calls = []
        raw_calls = message.get('tool_calls') or []
        if not isinstance(raw_calls, list) or len(raw_calls) > 8:
            raise ValueError()
        for call in raw_calls:
            func = call['function']
            if call['type'] != 'function' or not all(isinstance(v, str) and v for v in
                (call['id'], func['name'], func['arguments'])):
                raise ValueError()
            calls.append(ToolCall(call['id'], func['name'], func['arguments']))
        if len({c.call_id for c in calls}) != len(calls):
            raise ValueError()
        usage = data.get('usage')
        # Preserve supported numeric counts only; never store opaque provider blobs.
        safe_usage = None if not isinstance(usage, dict) else {
            k: v for k, v in usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
            and type(v) is int and v >= 0}
        safe_message = {'role': 'assistant', 'content': message.get('content')}
        if safe_message['content'] is not None and not isinstance(safe_message['content'], str):
            raise ValueError()
        if calls:
            safe_message['tool_calls'] = raw_calls
        return Completion(safe_message, calls, safe_usage, finish,
            data.get('id') if isinstance(data.get('id'), str) else None)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ProviderError('INVALID_RESPONSE', False) from None


class OpenAICompatibleProvider:
    """Bearer-authenticated HTTPS Chat Completions protocol, not every LLM API."""
    provider_name = 'openai_compatible'
    live = True

    def __init__(self, api_key: str, model: str, base_url: str, timeout=30,
                 max_output_tokens=1200, output_token_field='max_tokens'):
        base_url = base_url.rstrip('/')
        url = urlsplit(base_url)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or any(c.isspace() for c in base_url) or '\\' in base_url):
            raise ValueError('Endpoint must be HTTPS without embedded credentials, query or fragment.')
        try:
            url.port
        except ValueError:
            raise ValueError('Invalid endpoint port.') from None
        if not api_key.strip() or not model.strip() or not 1 <= timeout <= 30:
            raise ValueError('Model, credential and bounded timeout are required.')
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
            raise ValueError('Output token limit must be between 1 and 4096.')
        if output_token_field not in ('max_tokens', 'max_completion_tokens'):
            raise ValueError('Unsupported output token field.')
        self._key, self.model, self.base_url, self.timeout = api_key, model, base_url, timeout
        self.max_output_tokens, self.output_token_field = max_output_tokens, output_token_field

    def _payload(self, messages: list, tools: list):
        return {'model': self.model, 'messages': messages, 'tools': tools,
            self.output_token_field: self.max_output_tokens, 'stream': False}

    def complete(self, messages: list, tools: list) -> Completion:
        payload = self._payload(messages, tools)
        req = Request(self.base_url + '/chat/completions', data=json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ProviderError('RESPONSE_LIMIT', False)
                return parse_completion(json.loads(raw))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise ProviderError('AUTHENTICATION', False) from None
            if exc.code == 429:
                raise ProviderError('RATE_LIMIT', True) from None
            raise ProviderError('PROVIDER_UNAVAILABLE' if exc.code >= 500 else 'BAD_REQUEST', exc.code >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise ProviderError('NETWORK_ERROR', True) from None
        except (ValueError, UnicodeError):
            raise ProviderError('INVALID_RESPONSE', False) from None


class DeepSeekProvider(OpenAICompatibleProvider):
    provider_name = 'deepseek'

    def __init__(self, api_key: str, model: str, base_url='https://api.deepseek.com', timeout=30,
                 max_output_tokens=1200):
        if base_url.rstrip('/') not in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'):
            raise ValueError('Only the official DeepSeek HTTPS endpoint is supported.')
        super().__init__(api_key, model, base_url, timeout, max_output_tokens)

    def _payload(self, messages: list, tools: list):
        return {**super()._payload(messages, tools), 'thinking': {'type': 'disabled'}}

    @classmethod
    def from_environment(cls):
        """Compatibility entry point; refuses selection of another provider."""
        from .provider_factory import provider_from_environment
        provider = provider_from_environment()
        if not isinstance(provider, cls):
            raise RuntimeError('Use provider_from_environment for the selected provider.')
        return provider
