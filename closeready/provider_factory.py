"""Server-side provider selection. No credentials cross from one vendor to another."""
import os
from pathlib import Path
from .llm import DeepSeekProvider, LLMProvider, OllamaGatewayProvider, OpenAICompatibleProvider

KEYS = ('LLM_PROVIDER', 'LLM_API_KEY', 'LLM_MODEL', 'LLM_BASE_URL',
    'LLM_TIMEOUT_SECONDS', 'LLM_MAX_OUTPUT_TOKENS', 'LLM_OUTPUT_TOKEN_FIELD',
    'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_BASE_URL')


def provider_from_environment() -> LLMProvider:
    values = {}
    path = os.environ.get('CLOSEREADY_LLM_ENV_FILE')
    if path:
        try:
            for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    if key.strip() in KEYS:
                        values[key.strip()] = value.strip().strip('\"').strip("'")
        except OSError:
            raise RuntimeError('Cannot read configured LLM environment file.') from None
    for key in KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    name = values.get('LLM_PROVIDER', 'deepseek')
    try:
        timeout = int(values.get('LLM_TIMEOUT_SECONDS', '30'))
        output = int(values.get('LLM_MAX_OUTPUT_TOKENS', '1200'))
        token_field = values.get('LLM_OUTPUT_TOKEN_FIELD', 'max_tokens')
        if name == 'deepseek':
            if token_field != 'max_tokens':
                raise ValueError('DeepSeek uses max_tokens in this adapter.')
            return DeepSeekProvider(
                values.get('LLM_API_KEY', values.get('DEEPSEEK_API_KEY', '')),
                values.get('LLM_MODEL', values.get('DEEPSEEK_MODEL', '')),
                values.get('LLM_BASE_URL', values.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')),
                timeout, output)
        if name == 'openai_compatible':
            # Deliberately no DEEPSEEK_* fallback in this branch.
            return OpenAICompatibleProvider(values.get('LLM_API_KEY', ''), values.get('LLM_MODEL', ''),
                values.get('LLM_BASE_URL', ''), timeout, output, token_field)
        if name == 'ollama_gateway':
            if token_field != 'max_tokens':
                raise ValueError('Ollama gateway uses num_predict through LLM_MAX_OUTPUT_TOKENS.')
            return OllamaGatewayProvider(values.get('LLM_API_KEY', ''), values.get('LLM_MODEL', ''),
                values.get('LLM_BASE_URL', ''), timeout, output)
        raise ValueError('Unknown provider identifier.')
    except (TypeError, ValueError):
        raise RuntimeError('LLM configuration is missing, invalid or unsupported; see docs/llm-providers.md.') from None
