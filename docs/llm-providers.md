# Shared LLM interface for the team

Business code depends on `LLMProvider.complete(messages, tools) -> Completion`, not DeepSeek. The API's environment factory selects an adapter at startup. Each developer can choose their own local provider without changing CaseSnapshot, ActionContent, findings contracts or the central execution gate.

Frontend/upload/database work does not require an LLM key. AI coding tools are also unrelated to the model used by the running application. Keep provider credentials in a local ignored file or server environment, never in shared Python code or the browser.

## Configuration

The API still requires CLOSEREADY_LLM_ENABLED=1 to enable inference. Set CLOSEREADY_LLM_ENV_FILE=.env to load a local file, or supply the values as process environment variables. The check scripts default to reading .env; the server does not silently load it. Restart the server after a configuration change.

| Setting | Meaning |
| --- | --- |
| LLM_PROVIDER | deepseek, openai_compatible or ollama_gateway; defaults to deepseek for existing users |
| LLM_API_KEY | Credential for the selected endpoint |
| LLM_MODEL | Exact model identifier supplied by that service |
| LLM_BASE_URL | Provider base URL; Chat Completions adapters append `/chat/completions`, while `ollama_gateway` appends `/api/chat` |
| LLM_TIMEOUT_SECONDS | Integer 1..30; default 30 |
| LLM_MAX_OUTPUT_TOKENS | Integer 1..4096; default 1200 |
| LLM_OUTPUT_TOKEN_FIELD | max_tokens (default), or max_completion_tokens for the compatible adapter |

Existing `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` and `DEEPSEEK_BASE_URL` remain supported only when provider=deepseek. For DeepSeek, canonical LLM_* values take precedence over their DEEPSEEK_* aliases; an explicitly empty canonical value is an error, not a fallback. For each exact variable name, the process environment overrides the local file. Avoid mixing the two naming schemes in new configurations.

### Existing DeepSeek user

No local .env changes are required if DEEPSEEK_API_KEY and DEEPSEEK_MODEL are already set. The adapter restricts the endpoint to api.deepseek.com (with optional /v1) and adds its own `thinking: {type: disabled}` parameter. It uses max_tokens. The legacy `DeepSeekProvider.from_environment()` entry point remains available but refuses another provider selection.

### Compatible service

Set the following in your own ignored .env, using that service's actual values:

```dotenv
LLM_PROVIDER=openai_compatible
LLM_API_KEY=your-own-key
LLM_MODEL=your-model-id
LLM_BASE_URL=https://your-approved-service.example/v1
LLM_OUTPUT_TOKEN_FIELD=max_tokens
```

The example domain is a placeholder, not a working service. `LLM_BASE_URL` is a base URL, not the complete /chat/completions URL. All four settings are required for this provider. The factory never borrows DEEPSEEK_API_KEY when selecting a compatible service. It also never retries using a different provider/key after configuration failure.

The compatible adapter sends Bearer authentication, model, messages, tools, stream=false and the configured output-limit field. It omits DeepSeek's thinking parameter. It expects native function tool calls, correlated role=tool messages and a Chat Completions response shape. It does not translate every vendor's native API, implement cloud request signing, streaming, vision inputs or provider-specific reasoning continuation blocks. Use a provider mode supporting this non-streaming tool conversation, or implement a dedicated adapter.

### NUS-ISS organiser gateway

The organiser gateway exposes Ollama's chat protocol rather than OpenAI Chat Completions. Configure it explicitly; do not select `openai_compatible` for this endpoint:

```dotenv
LLM_PROVIDER=ollama_gateway
LLM_API_KEY=your-team-key
LLM_MODEL=global.anthropic.claude-sonnet-4-5-20250929-v1:0
LLM_BASE_URL=https://api.softwaresystems.app
LLM_OUTPUT_TOKEN_FIELD=max_tokens
```

This adapter posts to `/api/chat`, authenticates with `X-API-Key`, translates canonical assistant/tool-result messages to Ollama messages, and converts Ollama tool calls back into the canonical runtime format. `LLM_MAX_OUTPUT_TOKENS` becomes Ollama `options.num_predict`; `LLM_OUTPUT_TOKEN_FIELD` must remain `max_tokens`. A direct synthetic request on 2026-09-27 confirmed that the public gateway returned a structured first-turn `message.tool_calls`; run the shared two-request check below in each deployed environment to verify the full tool-result round trip.

Endpoints must use HTTPS and cannot contain embedded username/password, query credentials or fragments. Redirects are refused. Unlike the DeepSeek adapter, the compatible adapter accepts the hostname explicitly configured by the server administrator; it is not an HTTP/model-controlled URL. This is an intentional extension for team-owned services, not a general-purpose network tool. Limit deployment egress to your selected service when provisioning the server.

## Two levels of verification

First, check actual native tool support:

```powershell
.venv/Scripts/python -m scripts.check_llm
```

Optional `--model your-model-id` overrides LLM_MODEL for that command only. The check performs at most two real requests, calls a local read-only synthetic tool, and requires the model to return a random marker generated after its first response. Plain text pretending to call a tool fails. It consumes API credit but creates no business case or email.

Then verify the actual application schemas and persistence path:

```powershell
.venv/Scripts/python -m scripts.live_case_analysis --model your-model-id
```

This uses the same configured factory and full case-analysis gate. A basic tool check passing does not prove support for the application's nested/discriminated schema or model decision quality. The case check writes synthetic data under ignored local-data and records provider/model with the result. Review its result before using a new service for integration.

Verified in this workspace: DeepSeek passed the live case-analysis check earlier, and the refactored shared factory/native-tool check passed on 2026-09-10 with deepseek-flash (two requests, 720 reported total tokens). On 2026-09-27 the organiser gateway returned a structured native tool call from a direct first-turn probe. Its full tool-result round trip and business-runtime path remain unverified until the deployed shared checks pass. The compatible and organiser adapters have deterministic request/configuration tests. Do not advertise an incomplete probe as full verification.

The original scripts/deepseek_probe.py remains a legacy DeepSeek-only diagnostic. Prefer the shared check for new team configurations.

## Adding a different protocol

1. Implement LLMProvider in a dedicated adapter module. Expose provider_name, model and live=True.
2. Translate canonical role-labelled messages and function definitions into that provider's format. Translate its response back to ToolCall/Completion, preserving tool-call/result correlation. Do not pass provider-specific response blocks through to the core runtime.
3. Return actual usage when available, otherwise null; classify failures as ProviderError with a safe code and retryable flag. Do not expose response bodies or keys. Runtime owns retry budgets and action authority.
4. Register an explicit provider identifier in provider_factory.py with its own credential/endpoint validation. Do not add caller-supplied provider selection to business APIs.
5. Add adapter wire/error tests, run the shared native check and case check, and record actual results. Never modify domain states to accommodate a provider failure.

The protocol currently uses a canonical Chat Completions-style message representation internally; a native-protocol adapter must translate it. This keeps the runtime independent of HTTP/vendor details without claiming all provider features are interchangeable.

Student 2 and Student 3 should consume this interface when adding assessment/drafting inference; they should return the agreed typed findings/actions and let the central backend apply them. Local experimentation with different models is allowed. For the final assessment, select and record one fully integrated configuration and run the shared evaluations on that configuration.
