---
icon: lucide/cpu
---

# Model Configuration

The `models` section names the AI providers and model IDs that agents, teams, and the router use.
This page covers model fields, provider credentials, provider-specific setup, request tuning, and switching models from chat.
The same model can have different IDs on different providers; check the [OpenAI](https://developers.openai.com/api/docs/models), [Claude](https://platform.claude.com/docs/en/models/overview), [Gemini](https://ai.google.dev/gemini-api/docs/models), and [OpenRouter](https://openrouter.ai/api/v1/models) catalogs.

## Supported Providers

- `anthropic` - Claude models (Anthropic)
- `bedrock_claude` - Anthropic Claude models on Amazon Bedrock
- `azure` - OpenAI models through Azure OpenAI deployments
- `openai` - GPT models and OpenAI-compatible endpoints
- `codex` or `openai_codex` - OpenAI models available through a local Codex CLI ChatGPT login
- `kimi` or `kimi_code` - Kimi models available through a local Kimi Code CLI login
- `google` or `gemini` - Google Gemini models
- `vertexai_claude` - Anthropic Claude models on Google Vertex AI
- `ollama` - Local models via Ollama
- `llama_cpp` - Local models through an OpenAI-compatible llama.cpp server
- `groq` - Groq-hosted models (fast inference)
- `openrouter` - OpenRouter-hosted models (access to many providers)
- `cerebras` - Cerebras-hosted models
- `deepseek` - DeepSeek models
- `zai` - Z.ai GLM models
- `synthetic` - Built-in Lorem Ipsum model for local conversations and load generation

To generate a starter config for one provider, use `mindroom config init --provider <preset>` (see [Getting Started](../getting-started.md)).

## Model Config Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | Yes | - | The AI provider (see supported providers above) |
| `id` | Yes | - | Model ID specific to the provider; for Azure OpenAI, the deployment name |
| `display_name` | No | `null` | Friendly model name shown in clients |
| `icon` | No | `null` | Image path relative to the config file's directory, or a Matrix `mxc://` URI |
| `api` | No | `null` | `responses` or `chat_completions`; valid only with `provider: openai` (see [OpenAI API Models](#openai-api-models)); unset selects automatically |
| `host` | No | `null` | Host URL for self-hosted models (e.g., Ollama) |
| `api_key` | No | `null` | Model-specific API key used instead of the provider's shared key; equivalent to `extra_kwargs.api_key` |
| `extra_kwargs` | No | `null` | Additional provider-specific parameters (see [Extra Kwargs](#extra-kwargs)) |
| `context_window` | No | `null` | Provider context window in tokens, at least 1; budgets history replay and compaction (see [Context Window](history.md#context-window)); a `compaction.model` or `compaction.fallback_model` needs its own value |
| `stream_idle_timeout_seconds` | No | `null` | Seconds a streamed request may go without a provider event before it counts as stalled, at least 0; `0` disables the limit (see [Stalled Streams](#stalled-streams)) |

The mapping key, such as `default` below, is the stable name agents, teams, the router, and commands use.
`display_name` and `icon` only change how clients show the model and are never sent to the provider:

```yaml
models:
  default:
    provider: openai
    id: gpt-6-astra
    display_name: Quick helper
    icon: icons/helper.png
```

A local `icon` must be a PNG, JPEG, WebP, or GIF image of at most 1 MiB inside the config file's directory; absolute paths fail validation.
A missing, unreadable, unsupported, or out-of-directory image falls back to the client's provider icon and does not prevent startup.
External image URLs are not fetched.
Blank `display_name` or `icon` values count as unset.

## Configuration Examples

```yaml
models:
  # Anthropic Claude
  sonnet:
    provider: anthropic
    id: claude-sonnet-5-5
    context_window: 1000000

  haiku:
    provider: anthropic
    id: claude-haiku-4-5
    context_window: 200000

  # OpenAI
  gpt:
    provider: openai
    id: gpt-6-astra
    context_window: 1050000

  # Google Gemini (both 'google' and 'gemini' work as provider names)
  gemini:
    provider: google
    id: gemini-3.8-flash
    context_window: 1048576

  # Anthropic Claude on Vertex AI
  vertex_claude:
    provider: vertexai_claude
    id: claude-sonnet-5-5
    extra_kwargs:
      project_id: your-gcp-project
      region: global

  # Local via Ollama
  local:
    provider: ollama
    id: qwen3.8:27b
    context_window: 256000
    host: http://localhost:11434

  # OpenRouter (access to many model providers)
  openrouter:
    provider: openrouter
    id: anthropic/claude-sonnet-5.5

  # Groq (fast inference; Qwen3.8 is a preview model)
  groq:
    provider: groq
    id: qwen/qwen3.8-27b
    context_window: 131042

  # Cerebras (65,536 tokens on the free tier; paid plans support 131,072)
  cerebras:
    provider: cerebras
    id: qwen-3.8-27b
    context_window: 65536

  # DeepSeek
  deepseek:
    provider: deepseek
    id: deepseek-v4-pro
    context_window: 1048576

  # Z.ai GLM Coding Plan (OpenAI Chat Completions endpoint)
  glm:
    provider: zai
    id: glm-5.3
    context_window: 1048576
    stream_idle_timeout_seconds: 300  # hosted service behind a custom endpoint; see Stalled Streams
    extra_kwargs:
      base_url: https://api.z.ai/api/coding/paas/v4

  # Custom OpenAI-compatible endpoint (e.g., vLLM, llama.cpp server)
  custom:
    provider: openai
    id: my-model
    extra_kwargs:
      base_url: http://localhost:8080/v1
```

Azure, Bedrock, Codex, and Kimi examples are in their own sections below.
Claude Fable 5.1 uses `claude-fable-5-1` on Anthropic, `anthropic.claude-fable-5-1` on Bedrock, and `anthropic/claude-fable-5.1` on OpenRouter.
Claude Opus 5.5 and Sonnet 5.5 use `claude-opus-5-5` and `claude-sonnet-5-5` on Anthropic and Vertex AI, `anthropic.claude-opus-5-5` and `anthropic.claude-sonnet-5-5` on Bedrock, and `anthropic/claude-opus-5.5` and `anthropic/claude-sonnet-5.5` on OpenRouter.
For the direct DeepSeek API, use `deepseek-flash` for V4.1 Flash or `deepseek-v4-pro` for Pro; the older `deepseek-v4-flash` name is a [temporary compatibility route to V4.1 Flash](https://api-docs.deepseek.com/updates/#date-2026-09-10).
OpenRouter uses the distinct ID `deepseek/deepseek-v4.1-flash`.
[GLM 5.3](https://docs.z.ai/guides/llm/glm-5.3) is available through the GLM Coding Plan endpoint shown above.
The [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B) and each hosting provider document their own context limits.

### Claude 5.5 and Fable 5.1 Limits

Fable 5.1, Opus 5.5, and Sonnet 5.5 follow the same [tool-choice rules](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide): `auto` and `none` are allowed, while forcing `any` or a named tool returns an error.
They reject `thinking: {type: disabled}` and manual `budget_tokens`; lower effort with `extra_kwargs.output_config: {effort: low}` instead, or use `extra_kwargs.thinking: {type: between_tools}` on [Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5#turn-off-up-front-thinking).
With adaptive thinking, the notes these models write between tool calls are hidden, so responses show tool traces without that narration; Sonnet 5.5's `between_tools` setting returns those notes as text.

## Environment Variables

Provider credentials come from environment variables, stored credentials, CLI logins, or a model-specific key.
A model uses the key saved for it in the dashboard's **Models** editor first, then `api_key` or `extra_kwargs.api_key` from its config, then the provider's shared key from the environment or credential store.
Setting both `api_key` and `extra_kwargs.api_key` is a validation error, and a blank key counts as unset.
A model whose `extra_kwargs` authenticate another way (`auth_token` for `anthropic`, `azure_ad_token` or `azure_ad_token_provider` for `azure`, or `vertexai: true` for `google` and `gemini`) never receives the shared key, although the provider SDK may still read `ANTHROPIC_API_KEY` or `AZURE_OPENAI_API_KEY` from the process environment.
`codex`, `kimi`, `bedrock_claude`, `vertexai_claude`, and `synthetic` models authenticate without an API key and ignore a configured one.

Shared API keys are read from these variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
# Optional, only when overriding the default Azure OpenAI API version:
# AZURE_OPENAI_API_VERSION=2024-10-21
# Optional Azure deployment for the client:
# AZURE_OPENAI_DEPLOYMENT=...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
CEREBRAS_API_KEY=...
DEEPSEEK_API_KEY=...
ZAI_API_KEY=...
```

For Amazon Bedrock Claude, use standard AWS credential resolution:

```bash
AWS_REGION=us-east-1          # AWS_DEFAULT_REGION is used when AWS_REGION is unset
# Optional static credentials:
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
# Optional local profile instead:
AWS_PROFILE=...
```

Without static credentials or a profile, Bedrock uses the runtime IAM role.

For Ollama, you can also set:

```bash
OLLAMA_HOST=http://localhost:11434
```

For Vertex AI Claude, set these instead of an API key:

```bash
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
CLOUD_ML_REGION=global
# Optional custom Vertex AI endpoint:
# ANTHROPIC_VERTEX_BASE_URL=https://...
```

For `claude-sonnet-5-5`, use the [global endpoint or a supported multi-region endpoint](https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai#global-multi-region-and-regional-endpoints), with `region` / `CLOUD_ML_REGION` set to `global`, `us`, or `eu`.
Authenticate with `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key file.

For Gemini on Vertex AI (`provider: google` with `extra_kwargs.vertexai: true`), authenticate the same way and set `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, or pass `extra_kwargs.project_id` and `extra_kwargs.location`.
Gemini reads these variables and `GOOGLE_APPLICATION_CREDENTIALS` only from the process environment, not from `.env`.

`OPENAI_BASE_URL` is described in [Configuration — Model Providers](index.md#model-providers).

### File-based Secrets

For container environments (Kubernetes, Docker Swarm), supported API-key and secret variables can also use a `_FILE` suffix:

```bash
# Instead of setting the key directly:
ANTHROPIC_API_KEY=sk-ant-...

# Point to a file containing the key:
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

This works for all API key environment variables (e.g., `OPENAI_API_KEY_FILE`, `GOOGLE_API_KEY_FILE`, etc.).

## OpenAI API Models

Set `api: responses` or `api: chat_completions` on an `openai` model to choose the API independently of its model ID or endpoint.
When `api` is unset, GPT 5.4 and newer on the first-party OpenAI endpoint use Responses, and every other route, including all custom OpenAI-compatible endpoints, uses Chat Completions.
The endpoint must support the selected API.

```yaml
models:
  astra:
    provider: openai
    id: gpt-6-astra
    api: responses
    extra_kwargs:
      base_url: http://localhost:4000/v1
      reasoning_effort: high
```

[GPT-6 Astra requires Responses for function calling](https://developers.openai.com/api/docs/guides/latest-model), and GPT-6 Sol and Luna support function calling in Chat Completions only with `reasoning_effort: none`.
Through a proxy or custom alias, set `api: responses` for GPT-6 models, or tool calls on Astra, Sol, and Luna fail at their default reasoning effort.
Set `extra_kwargs.reasoning_effort` or `extra_kwargs.reasoning` only when you want to customize reasoning.
Selecting Responses keeps any `extra_kwargs.store: false` setting.
How `api` affects tool search is described in [Native Server-Side Tool Search](../tools/dynamic-tools.md#native-server-side-tool-search).

## OrcaRouter via the OpenAI-Compatible Endpoint

[OrcaRouter](https://docs.orcarouter.ai/introduction) works through the `openai` provider with its endpoint and an OrcaRouter API key:

```yaml
models:
  orcarouter:
    provider: openai
    id: orcarouter/auto
    api: chat_completions
    stream_idle_timeout_seconds: 300  # hosted service behind a custom endpoint; see Stalled Streams
    extra_kwargs:
      base_url: https://api.orcarouter.ai/v1
      api_key: your-orcarouter-api-key
```

`orcarouter/auto` lets OrcaRouter choose a model per request; replace it with an ID from its [catalog](https://docs.orcarouter.ai/getting-started/models) to pin one.
Set an agent's `model: orcarouter`, or select it for a thread with `!model orcarouter`.
To keep the key out of YAML, omit `extra_kwargs.api_key` and save the key in this model's **API Key** field in the dashboard's **Models** editor, so OrcaRouter and direct OpenAI models use separate keys.
`ORCAROUTER_API_KEY` is not read automatically.

## Codex Models with ChatGPT Login

Use `provider: codex` to call models through an authenticated local Codex CLI session instead of the billed OpenAI API.
Run `codex login` first so `~/.codex/auth.json` contains ChatGPT OAuth tokens; MindRoom refreshes the access token as needed.
Model access and usage limits depend on the logged-in ChatGPT account, so check the [current Codex model catalog](https://developers.openai.com/codex/models).

| Model | Model ID | Best fit |
|-------|----------|----------|
| GPT-6.1 Sol | `gpt-6.1-sol` | Near-Astra reasoning for long-running work; the starter default |
| GPT-6 Astra | `gpt-6-astra` | The hardest end-to-end reasoning and agentic work |
| GPT-6 Luna | `gpt-6-luna` | Efficient, high-volume work such as summaries and routing |

Older or preview slugs work when the account exposes them, the form `openai-codex/gpt-6.1-sol` is accepted, and the legacy `gpt-5.6` alias maps to `gpt-5.6-sol`.
If Codex state lives outside `~/.codex`, set `CODEX_HOME` in the process environment or `extra_kwargs.codex_home`; `~` prefixes are expanded.
`mindroom config init --provider codex` generates this config (see [CLI](../cli.md)):

```yaml
models:
  default:
    provider: codex
    id: gpt-6.1-sol
    display_name: Sol
    context_window: 258000
    extra_kwargs:
      reasoning_effort: medium
  astra:
    provider: codex
    id: gpt-6-astra
    display_name: Astra
    context_window: 258000
    extra_kwargs:
      reasoning_effort: medium
  luna:
    provider: codex
    id: gpt-6-luna
    display_name: Luna
    context_window: 258000
    extra_kwargs:
      reasoning_effort: low

router:
  model: luna

defaults:
  thread_summary_model: luna
```

Use `258000` as the context window: it is the effective budget of the Codex ChatGPT surface (just under 95% of its 272000-token window), not the larger window of the OpenAI API.
Set reasoning effort with `extra_kwargs.reasoning_effort`; GPT-6.1 Sol, GPT-6 Astra, and GPT-6 Luna accept `low`, `medium`, `high`, `xhigh`, and `max`.
Codex clients also offer Ultra for Sol and Astra, but Ultra's Codex-managed subagents are not available through MindRoom.
The provider supports text and image input with text output; transcription, text-to-speech, and realtime speech are not supported.
The adapter depends on the Codex CLI login format and backend, so a Codex CLI update can require a MindRoom update; use `provider: openai` for the public API contract and API billing.

## Kimi Models with Kimi Code Login

Use `provider: kimi` to call Kimi models through an authenticated local Kimi Code CLI session (Kimi Code subscription) instead of the billed Moonshot API.
Run `kimi` and `/login` first so `~/.kimi-code/credentials/kimi-code.json` contains OAuth tokens; MindRoom refreshes the access token as needed and calls `https://api.kimi.com/coding/v1`.

| Model | Model ID | Best fit |
|-------|----------|----------|
| Kimi K3 | `k3` | Flagship reasoning, long-horizon coding, and agent work with up to a 1M-token context |
| Kimi K3 256k | `k3-256k` | The same K3 generation with a 256k-token context |
| Kimi for Coding | `kimi-for-coding` | Coding-tuned tier exposed by the Kimi Code CLI |
| Kimi for Coding Highspeed | `kimi-for-coding-highspeed` | Faster coding tier exposed by the Kimi Code CLI |

[Kimi Code context limits depend on the subscription plan](https://www.kimi.com/code/docs/en/kimi-code/models.html): `k3` supports 1,048,576 tokens on Pro (legacy Allegretto) and higher, while Plus (legacy Moderato) is limited to 262,144.
The starter config and this example require Pro/Allegretto or higher; on Plus/Moderato, and for `k3-256k` on any plan, set `context_window: 262144`.

```yaml
models:
  default:
    provider: kimi
    id: k3
    context_window: 1048576
```

The form `kimi-code/k3` is accepted.
If Kimi Code state lives outside `~/.kimi-code`, set `KIMI_CODE_HOME` or `extra_kwargs.kimi_home`; `~` prefixes are expanded.
Kimi K3 always reasons before replying, so responses include reasoning tokens even for short answers.
The adapter depends on the Kimi Code CLI login format and backend, so a Kimi Code update can require a MindRoom update.

## OpenRouter Provider Routing

OpenRouter routes each request to one of several upstream providers serving the model, and upstream quality varies.
Control routing with OpenRouter [provider preferences](https://openrouter.ai/docs/features/provider-routing) in `extra_kwargs.extra_body`:

```yaml
models:
  deepseek:
    provider: openrouter
    id: deepseek/deepseek-v4.1-flash
    context_window: 1048576
    extra_kwargs:
      extra_body:
        provider:
          sort: price                     # cheapest available endpoint
          # order: [fireworks, together]  # or pin specific upstreams, in order
          # allow_fallbacks: false        # fail instead of using unlisted upstreams
```

Provider slugs are in the `tag` field of `https://openrouter.ai/api/v1/models/<model-id>/endpoints`.
Account-level OpenRouter settings (ignored providers, data-policy filters) still apply, so pinning an upstream your account excludes fails with `No endpoints found`.
Verify a new pin with a direct API request and check the `provider` field in the response.

## Azure OpenAI

Use `provider: azure` for models deployed through Azure OpenAI, with `id` set to your deployment name rather than the upstream model name.
Set the Azure variables listed in [Environment Variables](#environment-variables) in the config-adjacent `.env` file or the exported environment.

```yaml
models:
  default:
    provider: azure
    id: your-azure-openai-deployment
```

Deployment limits vary, so the starter config leaves `context_window` unset; set it to your deployment's limit when you know it.

## Amazon Bedrock Claude

Use `provider: bedrock_claude` to call Anthropic Claude through Amazon Bedrock, with `id` set to a Bedrock model ID or inference profile ID enabled in your AWS account and region.
The `aws_bedrock` optional extra installs automatically on first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set; otherwise install `mindroom[aws_bedrock]`.
AWS settings come from the config-adjacent `.env` file, the exported environment, a local AWS profile, or the runtime IAM role.

```yaml
models:
  default:
    provider: bedrock_claude
    id: anthropic.claude-opus-5-5
    context_window: 1000000
```

Bedrock lists Fable 5.1 and Sonnet 5 as open access, while Opus 5.5 and Sonnet 5.5 access can depend on the AWS account and region.
The Bedrock starter config defaults to Opus 5.5 with commented alternatives, so confirm access, or switch to its `fable` alternative or `id: anthropic.claude-sonnet-5`.

## Built-In Synthetic Model

Use `provider: synthetic` to run normal MindRoom conversations without an API key or model server, for example for load testing.
The model streams a repeatable random amount of Lorem Ipsum at a fixed rate, and when the agent has the `shell` tool it occasionally calls `run_shell_command` with `echo hi` before continuing.
The `extra_kwargs` values below are the defaults:

```yaml
models:
  synthetic:
    provider: synthetic
    id: lorem-ipsum
    extra_kwargs:
      seed: 1
      min_response_chars: 320
      max_response_chars: 960
      chunk_chars: 40
      chars_per_second: 80
      tool_call_probability: 0.2

agents:
  load_test:
    display_name: Load Test
    role: Generate synthetic traffic.
    model: synthetic
    tools: [shell]
    rooms: [lobby]
```

Tag `@load_test` in Lobby to receive a streamed synthetic reply.
Set `tool_call_probability` to `1` to call the shell tool on every turn or `0` to disable tool calls.
Set `chars_per_second` to `0` to stream without pacing delays for maximum throughput.
Changing `seed` changes the response length, split point, and tool-call choice, which stay the same for a given conversation history.

## Extra Kwargs

`extra_kwargs` sets additional parameters on the underlying [Agno](https://docs.agno.com/) model class.
Common options include:

- `base_url` - Custom API endpoint (useful for OpenAI-compatible servers)
- `temperature` - Sampling temperature
- `max_tokens` - Maximum tokens in response
- `extra_body` - Extra JSON body fields for OpenAI-compatible providers (e.g., [OpenRouter provider routing](#openrouter-provider-routing))

Some models reject sampling controls:

- Claude Fable 5.1, Opus 5.5, Sonnet 5.5, Opus 5, and Sonnet 5 reject non-default `temperature`, `top_p`, and `top_k`, so MindRoom omits them on Anthropic, Bedrock, and Vertex requests.
- MindRoom also omits those controls for direct Gemini 3.8 Flash and Gemini 3.5 Flash-Lite requests.
- [GPT-6 Astra does not support `temperature`, `top_p`, or `top_logprobs`](https://developers.openai.com/api/docs/guides/latest-model#update-api-and-model-parameters), and GPT-6 Sol and Luna reject them unless reasoning effort is `none`; leave them out of these models' `extra_kwargs`.
- For Mem0 memory extraction with `provider: openai`, use GPT-6 Luna; MindRoom drops the `temperature` and `top_p` values Mem0 sends, which GPT-6 models reject.

## Prompt Caching

Claude and OpenAI Responses models keep shared agent instructions (identity, role, workspace context files, and tool instructions) separate from the current date and session context, so the shared part can be reused across conversations when the provider cache allows it.
Cache reads and writes appear in usage metrics as cached and cache-write input tokens.

The `anthropic`, `bedrock_claude`, and `vertexai_claude` providers enable prompt caching with a one-hour lifetime by default.
The `openrouter` provider caches the same way for model IDs that route to Anthropic, such as `anthropic/claude-sonnet-5.5` and `~anthropic/claude-sonnet-latest`, using OpenRouter [`cache_control` breakpoints](https://openrouter.ai/docs/guides/best-practices/prompt-caching); other OpenRouter model families cache implicitly.
Set `extra_kwargs.cache_system_prompt: false` to turn off MindRoom's Claude cache breakpoints, or `extra_kwargs.extended_cache_time: false` to use the five-minute lifetime.

The `openai` Responses API and `codex` providers rely on provider-managed caching; on the public OpenAI API, GPT-5.6 and later also get an explicit cache breakpoint after the shared instructions.
See [OpenAI's prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) for eligibility and accounting.
Set `extra_kwargs.cache_system_prompt: false` on a Responses model to send the instructions as one undivided system message.
Chat Completions providers other than OpenRouter Claude routes keep their usual request format.

Kimi Code caches repeated request prefixes automatically and reports them as `cached_tokens`.
For `codex` and `kimi`, conversations share a `prompt_cache_key` per agent and requester within one MindRoom storage path, account, and channel, while different agents and requesters stay separate.
Set `extra_kwargs.prompt_cache_key` to choose your own cache group, for example to separate deployments that share a storage path and provider account, or set it to `null` to send no key.
A shared key still needs identical prefixes and tools, and provider routing can produce cache misses.

## Stalled Streams

A provider sometimes accepts a streamed request and then sends nothing more, which would leave the agent busy and queue every follow-up message behind it.
MindRoom ends a streamed request when the provider sends no event for `stream_idle_timeout_seconds`.
If nothing was streamed yet, MindRoom sends the request once more; a second silent attempt, or a stall after text already appeared, ends the turn with "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
The limit measures the gap between provider events, so long replies and slow tool calls are unaffected.
It applies only to streamed responses (`defaults.enable_streaming: true`, the default).

By default the limit is 300 seconds for hosted providers on their built-in endpoint.
Local servers can stay silent for minutes while a request waits in a queue or a model loads, so `ollama`, `llama_cpp`, and any model with a configured endpoint get no limit unless you set one.
A configured endpoint is a `base_url` anywhere in `extra_kwargs` (such as `extra_kwargs.base_url` or `extra_kwargs.client_params.base_url`) or one read from `OPENAI_BASE_URL` or `ANTHROPIC_VERTEX_BASE_URL`.
Set the limit explicitly for a hosted service behind a custom endpoint, such as OrcaRouter or the Z.ai coding endpoint.
Endpoint variables that only a provider SDK reads, such as `ANTHROPIC_BASE_URL`, are not detected; set `0` when one points at a local server.
For a local model, pick a value above its slowest queue wait plus model load; set `0` to turn the limit off for a hosted model:

```yaml
models:
  local:
    provider: openai
    id: qwen3-coder
    extra_kwargs:
      base_url: http://localhost:8080/v1
    stream_idle_timeout_seconds: 1800  # longest wait for a queued request plus model load
  deep_thinker:
    provider: openrouter
    id: openai/gpt-6-astra
    stream_idle_timeout_seconds: 0  # never cut off long silent reasoning
```

## Hot Reload

After a successful config reload, changes to a model definition, including its provider options, model ID, and `context_window`, apply from the next agent, team, or router response without restarting the bot.
Agents with voice calls enabled can still restart, because their call runtime keeps its configuration between responses.

## Model Overrides in Chat

Each agent, team, and the router normally answers with its own configured `model` (`router.model` for the router).
Users can change that per thread or per room, and the most specific choice wins:

1. An explicit model for one run, such as a delegated child run.
2. The thread override set with `!model`, the `thread_model` tool, or a client's model picker.
3. The runtime room default set with `!room_model`.
4. The authored room default in `room_models`.
5. The entity's configured model.

Thread and room overrides name models from the `models:` section and survive restarts.
A Matrix client's model picker lists the configured models and changes the thread override like `!model`.
The picker works in encrypted and unencrypted rooms when the router and at least one agent you may address are joined; teams alone do not qualify.
It shows each model's key, display name, provider, model ID, and icon, never API keys, hosts, local paths, or `extra_kwargs`.
Catalogs with more than 256 models or over 64 KiB are not offered to the picker; `!model` still works.
The client protocol is described in [Model Selection Protocol](../architecture/matrix.md#model-selection-protocol).

### `!model`

Show or switch the model that the agents, teams, and router you may address use in the current thread.

```
!model
!model list
!model opus
!model reset
```

`!model` and `!model list` show each thread override with the entities it applies to, plus the available model names.
The override applies from the next message in the thread, and only to the entities whose `access` admits the user who set it in this room; every other entity keeps its own thread override or room-level model.
During a configured team's turn, the team's thread override also applies to its member agents, because the team's `access` reaches them.
`!model reset` removes the override only for the entities you may address.
Other threads and rooms are unaffected.

### `!room_model`

Show or switch the default model for every agent, team, and the router in the current room.

```
!room_model
!room_model list
!room_model opus
!room_model reset
```

`!room_model` and `!room_model list` show the current runtime override and available model names to authorized room members.
Setting and resetting require a Matrix room admin; other users get `❌ Room admin only.`
`!room_model opus` stores a room override without modifying `config.yaml`, keyed by Matrix room ID, so it survives restarts and room-alias changes.
The new default applies to later turns; a turn in progress or waiting for approval keeps the model it started with.
`!room_model reset` removes the runtime override so `room_models` or each entity's configured model applies again.

To set an authored room default in `config.yaml`, map managed room names to model names with `room_models` (default `{}`):

```yaml
room_models:
  lobby: haiku
  dev: opus
```

### [`thread_model`]

The `thread_model` tool lets an agent list models and show, switch, or reset the current thread's model override, like `!model`.
It has no tool-specific configuration.

```yaml
agents:
  assistant:
    tools:
      - thread_model
```

- `list_models()` returns every configured model name with its provider and model ID and works outside a thread.
- `get_thread_model()` returns an `overrides` map from each entity with an active thread override to its model, plus the available model names; an override naming a model since removed from `models` is ignored and reported under `stale_overrides`.
- `switch_thread_model(model_name, when)` accepts a configured model name and rejects unknown names with the available list.
  `when` is `next-turn` (default), which keeps the current response on its model, or `after-toolcall`, which continues the current response with the new model after the tool call.
- `reset_thread_model()` removes the thread override so room-level selection applies again.

All functions except `list_models` require an active thread and return an error outside one.
Overrides set by the tool follow the same entity scope and team rules as [`!model`](#model) and are stored per thread root and entity in `mindroom_data/tracking/thread_models.json`.
