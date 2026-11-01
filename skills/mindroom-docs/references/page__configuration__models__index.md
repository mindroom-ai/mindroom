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

Letter case, surrounding spaces, and `-` versus `_` do not matter, so `Llama-CPP` means `llama_cpp`.
A `provider` value that does not match one of these fails config load with an error that names the model and lists these values, even when no agent uses that model.
Config load and `mindroom config validate` also fail, naming each reference, when an agent, a team, or the router (including the implicit `default` an entity without a `model` uses), a `room_models` or `room_thread_summary_models` entry, `defaults.thread_summary_model`, or `voice.intelligence.model` while voice is enabled names a model that `models` does not define.

To generate a starter config for one provider, use `mindroom config init --provider <preset>` (see [Getting Started](https://docs.mindroom.chat/getting-started/)).
The [macOS app's local model setup](https://docs.mindroom.chat/installation/macos-app/#local-ai-models) downloads and runs models with one click, with hardware recommendations including Qwen3.8 27B when memory permits.

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
| `context_window` | No | `null` | Provider context window in tokens, at least 1; budgets history replay and compaction (see [Context Window](https://docs.mindroom.chat/configuration/history/#context-window)); a `compaction.model` or `compaction.fallback_model` needs its own value |
| `pricing` | No | `null` | Prices in USD per million tokens for [Budgets](https://docs.mindroom.chat/budgets/) and [reply costs](https://docs.mindroom.chat/usage/#reply-cost): `input` and `output` (both required), plus `cache_read` and `cache_write`, which default to the `input` price; set `cache_write` for Claude models, whose cache writes cost more than input |
| `stream_idle_timeout_seconds` | No | `null` | Seconds a streamed request may go without a provider event before it counts as stalled, at least 0; `0` disables the limit (see [Stalled Streams](#stalled-streams)) |

The mapping key, such as `default` below, is the stable name agents, teams, the router, and commands use.
It may contain only letters, digits, `:`, `_`, and `-`, so a key such as `gpt-6.1` fails config load; name it `gpt-6-1` instead and set `display_name` to show the dotted name.
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
    id: claude-haiku-5-5
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

  # Local via Ollama (see Ollama Context Size)
  local:
    provider: ollama
    id: qwen3.8:27b
    context_window: 32768
    host: http://localhost:11434
    extra_kwargs:
      options:
        num_ctx: 32768

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

Models of every provider except `google`, `ollama`, and `synthetic` verify an HTTPS endpoint's certificate against the operating system's trust store, as do downloads of media that tools return as URLs, OpenAI embeddings, avatar generation, and the OpenAI speech requests of voice calls and outgoing voice messages, so a private CA installed in that store is trusted.
Setting `SSL_CERT_FILE` or `SSL_CERT_DIR` in the process environment replaces that store, so the file or directory must hold the public CAs too, for example the system CA bundle with the private CA appended.
An `SSL_CERT_DIR` must be a hashed directory, as `c_rehash` or `openssl rehash` creates.

Azure, Bedrock, Codex, and Kimi examples are in their own sections below.
Claude Fable 5.1 uses `claude-fable-5-1` on Anthropic, `anthropic.claude-fable-5-1` on Bedrock, and `anthropic/claude-fable-5.1` on OpenRouter.
Claude Opus 5.5 and Sonnet 5.5 use `claude-opus-5-5` and `claude-sonnet-5-5` on Anthropic and Vertex AI, `anthropic.claude-opus-5-5` and `anthropic.claude-sonnet-5-5` on Bedrock, and `anthropic/claude-opus-5.5` and `anthropic/claude-sonnet-5.5` on OpenRouter.
For the direct DeepSeek API, use `deepseek-flash` for V4.1 Flash or `deepseek-v4-pro` for Pro; the older `deepseek-v4-flash` name is a [temporary compatibility route to V4.1 Flash](https://api-docs.deepseek.com/updates/#date-2026-09-10).
OpenRouter serves the current V4.1 Flash release as `deepseek/deepseek-v4.1-flash-1017`.
[GLM 5.3](https://docs.z.ai/guides/llm/glm-5.3) is available through the GLM Coding Plan endpoint shown above.
The [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B) and each hosting provider document their own context limits.

### Claude 5.5 and Fable 5.1 Limits

Fable 5.1, Opus 5.5, and Sonnet 5.5 follow the same [tool-choice rules](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide): `auto` and `none` are allowed, while forcing `any` or a named tool returns an error.
They reject `thinking: {type: disabled}` and manual `budget_tokens`; lower effort with `extra_kwargs.output_config: {effort: low}` instead, or use `extra_kwargs.thinking: {type: between_tools}` on [Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5#turn-off-up-front-thinking).
With adaptive thinking, the notes these models write between tool calls are hidden, so responses show tool traces without that narration; Sonnet 5.5's `between_tools` setting returns those notes as text.
Fable 5.1, Opus 5.5, Sonnet 5.5, and Haiku 5.5 reuse their reasoning from an earlier turn only while everything sent before it is unchanged, so after a change they answer without it instead of failing, most often because the date or other content of the system prompt has changed, or a compaction or replay limit rewrote earlier turns.
With Sonnet 5.5's `between_tools` or on `bedrock_claude`, earlier turns' reasoning is never sent, while the current turn's reasoning between tool calls always is; Haiku 5.5 with `thinking: {type: disabled}` writes no reasoning to send.
A reply that resumes after a tool approval or a delegation keeps its reasoning, unless its history was trimmed to fit the context window or another reply or a compaction ran in the conversation while it waited; then it answers without that reasoning, and with `between_tools` or on `bedrock_claude`, an account created on or after August 31, 2026 can get a 400 error ``Invalid `signature` in `thinking` block`` on that resume.

## Environment Variables

Provider credentials come from environment variables, stored credentials, CLI logins, or a model-specific key.
A model uses the key saved for it in the dashboard's **Models** editor first, then `api_key` or `extra_kwargs.api_key` from its config, then the provider's shared key from the environment or credential store.
A key in `extra_kwargs.client_params.api_key` reaches the provider's API client over all of these, so that model never needs the shared key.
Setting both `api_key` and `extra_kwargs.api_key` is a validation error, and a blank key counts as unset.
A model that authenticates another way (`auth_token` for `anthropic`, `azure_ad_token` or `azure_ad_token_provider` for `azure`, or Vertex AI for `google` and `gemini`) never receives the shared key.
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
AWS_REGION=us-east-1          # or AWS_DEFAULT_REGION; either one exported in the process environment wins over the .env
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
# Optional key for a server that requires one, sent by models that configure no api_key:
# OLLAMA_API_KEY=...
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
Setting `GOOGLE_GENAI_USE_VERTEXAI=true` moves every `google` and `gemini` model to Vertex AI unless the model chooses its route itself: `extra_kwargs.client_params.enterprise`, then `extra_kwargs.client_params.vertexai`, then `extra_kwargs.vertexai`, set to `true` or `false`, wins over the variable.
`GOOGLE_GENAI_USE_ENTERPRISE` selects nothing.

`OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` are described in [Configuration — Model Providers](https://docs.mindroom.chat/configuration/#model-providers).

### File-based Secrets

For container environments (Kubernetes, Docker Swarm), supported API-key and secret variables can also use a `_FILE` suffix:

```bash
# Instead of setting the key directly:
ANTHROPIC_API_KEY=sk-ant-...

# Point to a file containing the key:
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

This works for every model provider key and every endpoint, project, region, Azure, and CLI login home variable MindRoom reads for model providers, every variable a tool's settings read, `TYPESAFE_API_KEY`, and the variables a skill's `requires.env` names (e.g., `OPENAI_API_KEY_FILE`, `GOOGLE_API_KEY_FILE`, `TAVILY_API_KEY_FILE`).
The plain variable wins when the process environment or the `.env` beside `config.yaml` [sets it](https://docs.mindroom.chat/configuration/#environment-variables).

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

[GPT-6 Astra, GPT-6.1 Sol, and GPT-6.1 Terra require Responses for function calling](https://developers.openai.com/api/docs/guides/latest-model), and GPT-6 Luna supports function calling in Chat Completions only with `reasoning_effort: none`.
Through a proxy or custom alias, set `api: responses` for GPT-6 models, or tool calls on Astra, Sol, Terra, and Luna fail at their default reasoning effort.
Set `extra_kwargs.reasoning_effort` or `extra_kwargs.reasoning` only when you want to customize reasoning.
A reply shows the model's reasoning while it thinks only when the model requests a reasoning summary, with `extra_kwargs.reasoning_summary: auto` or `extra_kwargs.reasoning: {summary: auto}`.
An unverified OpenAI organization then gets `Your organization must be verified to generate reasoning summaries`; verify the organization or remove the setting.
Selecting Responses keeps any `extra_kwargs.store: false` setting.
How `api` affects tool search is described in [Native Server-Side Tool Search](https://docs.mindroom.chat/tools/dynamic-tools/#native-server-side-tool-search).

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
| GPT-6.1 Terra | `gpt-6.1-terra` | Fast everyday coding, writing, screenshots, and computer use |
| GPT-6 Luna | `gpt-6-luna` | Efficient, high-volume work such as summaries and routing |

Older or preview slugs work when the account exposes them, the form `openai-codex/gpt-6.1-sol` is accepted, and the legacy `gpt-5.6` alias maps to `gpt-5.6-sol`.
If Codex state lives outside `~/.codex`, set `CODEX_HOME` in the `.env` beside `config.yaml` or the process environment, or `extra_kwargs.codex_home`; `~` prefixes are expanded.
`mindroom config init --provider codex` generates this config (see [CLI](https://docs.mindroom.chat/cli/)):

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

Use `258000` as the context window: it fits the Codex window of every model above, which is smaller than the OpenAI API window.
Set reasoning effort with `extra_kwargs.reasoning_effort`; GPT-6.1 Sol, GPT-6 Astra, GPT-6.1 Terra, and GPT-6 Luna accept `low`, `medium`, `high`, `xhigh`, and `max`.
Codex models ask for a reasoning summary (`auto`, as the Codex CLI does) unless `extra_kwargs.reasoning_summary` or `extra_kwargs.reasoning.summary` sets another, so a reply shows the model's reasoning while it thinks.
Codex clients also offer Ultra for Sol, Astra, and Terra, but Ultra's Codex-managed subagents are not available through MindRoom.
The provider supports text and image input with text output; transcription, text-to-speech, and realtime speech are not supported.
The adapter depends on the Codex CLI login format and backend, so a Codex CLI update can require a MindRoom update; use `provider: openai` for the public API contract and API billing.

## Kimi Models with Kimi Code Login

Use `provider: kimi` to call Kimi models through an authenticated local Kimi Code CLI session (Kimi Code subscription) instead of the billed Moonshot API.
Run `kimi` and `/login` first so `~/.kimi-code/credentials/kimi-code.json` contains OAuth tokens; MindRoom refreshes the access token as needed and calls `https://api.kimi.com/coding/v1`.

| Model | Model ID | Best fit |
|-------|----------|----------|
| Kimi K3.1 Pro | `k3.1-pro` | Flagship reasoning, long-horizon coding, and agent work with up to a 1M-token context; the starter default |
| Kimi K3.1 Pro Highspeed | `k3.1-pro-highspeed` | K3.1 Pro at about 2-3x the speed and twice the quota use, on Pro (legacy Allegretto) and higher |
| Kimi K3 | `k3` | The previous flagship generation with up to a 1M-token context |
| Kimi K3 256k | `k3-256k` | The same K3 generation with a 256k-token context |
| Kimi for Coding | `kimi-for-coding` | Coding-tuned tier exposed by the Kimi Code CLI |
| Kimi for Coding Highspeed | `kimi-for-coding-highspeed` | Faster coding tier exposed by the Kimi Code CLI |

[Kimi Code context limits depend on the subscription plan](https://www.kimi.com/code/docs/en/kimi-code/models.html): `k3.1-pro` and `k3` support 1,048,576 tokens on Pro (legacy Allegretto) and higher, while Plus (legacy Moderato) is limited to 262,144.
The starter config and this example require Pro/Allegretto or higher; on Plus/Moderato, and for `k3-256k` on any plan, set `context_window: 262144`.

```yaml
models:
  default:
    provider: kimi
    id: k3.1-pro
    context_window: 1048576
```

The form `kimi-code/k3.1-pro` is accepted.
If Kimi Code state lives outside `~/.kimi-code`, set `KIMI_CODE_HOME` in the `.env` beside `config.yaml` or the process environment, or `extra_kwargs.kimi_home`; `~` prefixes are expanded.
Kimi K3.1 Pro and K3 reason before replying by default, so responses include reasoning tokens even for short answers.
Setting `extra_kwargs.reasoning_effort` to `none` makes K3.1 Pro requests fail with HTTP 400, while K3 then answers with K2.8 Preview without reasoning.
The adapter depends on the Kimi Code CLI login format and backend, so a Kimi Code update can require a MindRoom update.

## OpenRouter Provider Routing

OpenRouter routes each request to one of several upstream providers serving the model, and upstream quality varies.
Control routing with OpenRouter [provider preferences](https://openrouter.ai/docs/features/provider-routing) in `extra_kwargs.extra_body`:

```yaml
models:
  deepseek:
    provider: openrouter
    id: deepseek/deepseek-v4.1-flash-1017
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
MindRoom's `LLM usage` log line names the upstream that served each OpenRouter response in `served_provider`.

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

## Ollama Context Size

Unless `extra_kwargs.options.num_ctx` is set, MindRoom sends Ollama no context size, so Ollama runs each model with its own default, which depends on the Ollama version and server and is often only a few thousand tokens.
When a conversation is longer, Ollama drops its oldest messages to fit, without an error, so the model answers without seeing them.
Set `extra_kwargs.options.num_ctx` to the context size Ollama should use, and `context_window` to the same value so MindRoom fits history into it (see [Context Window](https://docs.mindroom.chat/configuration/history/#context-window)), as the `local` model in [Configuration Examples](#configuration-examples) does.
A larger `num_ctx` needs more memory on the Ollama server.

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

`extra_kwargs` sets provider-specific options for the model's requests and API client.
Each provider accepts only the keys in the table below.
A model with any other key, including a misspelled key or a retired Agno model setting such as `retries`, fails config validation with an `Unsupported extra_kwargs` error that names the model and the unknown keys and lists the accepted ones; remove or correct those keys.
`max_retries` is not accepted, since MindRoom retries failed requests itself (see [Retries](#retries)); a model that sets it fails config validation with an error that says to remove it.
`role_map` is not accepted either: Chat Completions requests send the system prompt as `developer` to OpenAI's own API and as `system` to every other endpoint, including an OpenAI-compatible server such as vLLM or llama.cpp set as `provider: openai` with its `base_url`, so remove it.

- `base_url`, a string, sends requests to a custom endpoint, such as an OpenAI-compatible server.
- `api_key` is the same as the model's `api_key` field.
- `request_params` holds request body fields that MindRoom merges over its own.
- `extra_body` adds JSON body fields, for example for [OpenRouter provider routing](#openrouter-provider-routing).
- `client_params`, a mapping, passes options to the provider's API client; its `base_url`, when set, must be a string.

| Provider | Accepted keys |
|----------|---------------|
| `openai` with Chat Completions, `zai`, `llama_cpp` | `api_key`, `base_url`, `client_params`, `default_headers`, `default_query`, `extra_body`, `extra_headers`, `extra_query`, `frequency_penalty`, `logit_bias`, `logprobs`, `max_completion_tokens`, `max_tokens`, `metadata`, `organization`, `presence_penalty`, `reasoning_effort`, `request_params`, `seed`, `service_tier`, `stop`, `store`, `strict_output`, `temperature`, `timeout`, `top_logprobs`, `top_p`, `user`, `verbosity` |
| `azure` | The Chat Completions keys, plus `api_version`, `azure_endpoint`, `azure_deployment`, and `azure_ad_token` |
| `openrouter` | The Chat Completions keys, plus `models`, `cache_system_prompt`, and `extended_cache_time` |
| `deepseek` | The Chat Completions keys, plus `use_thinking` |
| `kimi` | The Chat Completions keys, plus `kimi_home` and `prompt_cache_key` |
| `groq` | `api_key`, `base_url`, `client_params`, `default_headers`, `default_query`, `extra_headers`, `extra_query`, `frequency_penalty`, `logit_bias`, `logprobs`, `max_tokens`, `presence_penalty`, `request_params`, `seed`, `stop`, `temperature`, `timeout`, `top_logprobs`, `top_p`, `user` |
| `cerebras` | `api_key`, `base_url`, `client_params`, `default_headers`, `default_query`, `extra_body`, `extra_headers`, `extra_query`, `max_completion_tokens`, `parallel_tool_calls`, `repetition_penalty`, `request_params`, `strict_output`, `temperature`, `timeout`, `top_k`, `top_p` |
| `openai` with Responses | `api_key`, `base_url`, `cache_system_prompt`, `client_params`, `default_headers`, `default_query`, `extra_body`, `extra_headers`, `extra_query`, `include`, `max_output_tokens`, `max_tool_calls`, `metadata`, `organization`, `parallel_tool_calls`, `reasoning`, `reasoning_effort`, `reasoning_summary`, `request_params`, `service_tier`, `store`, `strict_output`, `temperature`, `timeout`, `top_p`, `truncation`, `user`, `verbosity` |
| `codex` | The Responses keys without `max_output_tokens` and `temperature`, which the Codex endpoint rejects, plus `codex_home`, `default_instructions`, `prompt_cache_key`, and `session_id` |
| `anthropic` | `api_key`, `auth_token`, `betas`, `cache_system_prompt`, `client_params`, `context_management`, `default_headers`, `extended_cache_time`, `max_tokens`, `output_config`, `request_params`, `stop_sequences`, `temperature`, `thinking`, `timeout`, `top_k`, `top_p` |
| `vertexai_claude` | The `anthropic` keys without `auth_token`, plus `base_url`, `project_id`, and `region` |
| `bedrock_claude` | The `anthropic` keys without `auth_token`, plus `aws_access_key`, `aws_profile`, `aws_region`, `aws_secret_key`, and `aws_session_token` |
| `google`, `gemini` | `api_key`, `cached_content`, `client_params`, `frequency_penalty`, `generation_config`, `generative_model_kwargs`, `include_thoughts`, `location`, `logprobs`, `max_output_tokens`, `presence_penalty`, `project_id`, `safety_settings`, `search`, `seed`, `stop_sequences`, `temperature`, `thinking_budget`, `thinking_level`, `timeout`, `top_k`, `top_p`, `vertexai` |
| `ollama` | `api_key`, `client_params`, `format`, `keep_alive`, `options`, `request_params`, `think`, `timeout` |
| `synthetic` | `chars_per_second`, `chunk_chars`, `max_response_chars`, `min_response_chars`, `seed`, `tool_call_probability` (see [Built-In Synthetic Model](#built-in-synthetic-model)) |

On `deepseek`, `use_thinking`, a boolean that defaults to `true`, runs requests in DeepSeek's thinking mode, where the model reasons before it answers; set it to `false` to answer without thinking.
An `ollama` model's `think` turns a thinking model's reasoning on (`true`) or off (`false`), or sets its effort (`low`, `medium`, or `high`) on models that take a level; unset leaves the choice to Ollama.
A `google` or `gemini` model's `search: true` grounds its answers with Google Search, and search with tools needs a Gemini 3 or later model name on the Gemini API, such as `gemini-3.8-flash`: there the agent keeps its tools beside search, while other models, aliases such as `gemini-flash-latest`, and every Vertex AI model use search alone, in place of the agent's tools.

When a response reaches the model's output limit (`max_tokens` or the route's equivalent), its text is kept, but none of its tool calls run: the agent is asked to split the work into smaller tool calls, and a third such response ends the reply with an error.
A Claude model whose `extra_kwargs` sets no `max_tokens` can write up to its maximum output on `anthropic`, 128,000 tokens for Claude 5 models, except Claude Sonnet 4.5 and models older than Claude 4.5, which write up to 8,192 tokens, as every model does on `bedrock_claude` and `vertexai_claude`, whose quotas count the requested output when a request starts; set `max_tokens` to choose another limit.

Some models reject sampling controls:

- Claude Opus 4.7, Opus 4.8, Mythos Preview, and every Claude 5 and 5.x model reject non-default `temperature`, `top_p`, and `top_k`, so MindRoom omits them on Anthropic, Bedrock, and Vertex requests.
- MindRoom also omits those controls for direct Gemini 3.8 Flash, Gemini 3.6 Flash, and Gemini 3.5 Flash-Lite requests.
- [GPT-6 Astra and GPT-6.1 Sol do not support `temperature`, `top_p`, or `top_logprobs`](https://developers.openai.com/api/docs/guides/latest-model#update-api-and-model-parameters), and GPT-6.1 Terra and GPT-6 Luna reject them unless reasoning effort is `none`; leave them out of these models' `extra_kwargs`.

## Prompt Caching

An agent's system prompt has a stable part (identity, role, instructions, workspace context files, tool instructions, skills, and knowledge bases) and a turn part (the current date, file memory, and system enrichment); the stable part stays the same across turns and conversations, so providers that cache prompts can reuse it.
Cache reads and writes appear in usage metrics as cached and cache-write input tokens.

The `anthropic`, `bedrock_claude`, and `vertexai_claude` providers enable prompt caching with a one-hour lifetime by default.
The `openrouter` provider caches the same way for model IDs that route to Anthropic, such as `anthropic/claude-sonnet-5.5` and `~anthropic/claude-sonnet-latest`, using OpenRouter [`cache_control` breakpoints](https://openrouter.ai/docs/guides/best-practices/prompt-caching); other OpenRouter model families cache implicitly.
Set `extra_kwargs.cache_system_prompt: false` to turn off MindRoom's Claude cache breakpoints, or `extra_kwargs.extended_cache_time: false` to use the five-minute lifetime.

The `openai` Responses API and `codex` providers rely on provider-managed caching; on the public OpenAI API, GPT-5.6 and later also get an explicit cache breakpoint after the stable part of the system prompt.
See [OpenAI's prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) for eligibility and accounting.
Set `extra_kwargs.cache_system_prompt: false` on a Responses model to send no explicit breakpoint.
Chat Completions providers other than OpenRouter Claude routes keep their usual request format.

Kimi Code caches repeated request prefixes automatically and reports them as `cached_tokens`.
For `codex` and `kimi`, conversations share a `prompt_cache_key` per agent and requester within one MindRoom storage path, account, and channel, while different agents and requesters stay separate.
Set `extra_kwargs.prompt_cache_key` to choose your own cache group, for example to separate deployments that share a storage path and provider account, or set it to `null` to send no key.
A shared key still needs identical prefixes and tools, and provider routing can produce cache misses.

## Cold-Cache Model

Claude providers read a cached prompt for a small fraction of the normal input price and charge more than the normal price to write a new cache; the exact rates vary by model and provider.
Switching models mid-thread therefore usually costs money, because the new model starts without a cache.
Once the agent model's cache for a thread has expired, the next turn pays for a full write either way, and a cheaper model can continue a simple conversation for a fraction of the cost.
Set `agents.<name>.cheap_model` to the agent's cheaper alternative and `agents.<name>.cold_cache` to let a judge switch to it:

```yaml
agents:
  assistant:
    model: opus
    cheap_model: haiku
    cold_cache:
      judgment:
        provider: openai_decisions
        threshold: 0.8
```

Set `defaults.cheap_model` to give every agent the same cheap model; an agent's own `cheap_model` wins, and an agent whose `model` is the default cheap model has none.
`cold_cache: {}` enables the switch with the default judge, which asks `cheap_model` itself whether it can answer.
`cold_cache` requires a cheap model, and the cheap model needs `context_window` set on both models or neither; when set, it must be at least the agent model's.

| `cold_cache` field | Type | Default | Description |
|-------|------|---------|-------------|
| `idle_seconds` | integer | `3600` | Seconds since the agent's last reply in a thread after which its prompt cache counts as cold |
| `judgment` | object or null | `null` | LLM model alias, TypeSafe, or OpenAI Decisions backend; see [Judgment Backends](https://docs.mindroom.chat/configuration/threads/#judgment-backends). Unset asks `cheap_model` itself |

A thread counts as cold once the agent's last reply in it, timed from that reply's final edit, is older than `idle_seconds`.
The default of one hour matches the cache lifetime MindRoom sets for Claude; set it to your provider's, for example `300` with `extra_kwargs.extended_cache_time: false`.
A thread the agent has not replied in yet keeps the agent model, so no thread is decided by its first request.

On a cold turn, the judge sees the thread and the new request, and decides whether the cheap model can continue the whole conversation as well as the agent model; the question names both by their configured model `id`.
Tool calls in the thread reach it as `<tool>` tags with the short input and output previews the chat shows.
A simple request in a thread about coding, planning, or other demanding work counts as a no.
Long threads reach the judge as their newest messages that fit its 16 KB request, with long messages shortened to their start and end.
Only an accepting decision switches; a decline, abstention, timeout, or missing credential keeps the agent model.
Warm turns make no judgment call.

An accepted switch is saved as the agent's thread override, like `!model`, so the thread stays on the cheap model without further judgments, even after the cheap model's own cache expires.
`!model` shows it, and `!model reset` or another `!model` choice ends it.
`!model reset` returns the thread to automatic selection, so a later idle turn can switch it again; choose the agent's model with `!model` to keep it.
Removing `cold_cache` stops new switches, and after a `cheap_model` change new switches use the new model; threads that already switched stay on their saved model until their override is reset.

The switch never happens when a scheduled model, a `!model` or `!room_model` override (even one naming the agent's own model), or a `room_models` entry that selects a different model applies, or when the request carries images, audio, video, or files, or regenerates or resumes an earlier answer.
Earlier attachments reach the judge only as placeholders, so pick a cheap model that supports the same media as the agent model.
Agents in `thread_mode: room` keep their model, because a room has no thread to switch.
Teams and the router keep their models.
Each decision logs `Cold cache model selected` with a `reason`: `no_earlier_reply`, `warm`, `history_unavailable`, `request_incomplete`, `judgment_unavailable`, or `judged`.

## Retries

MindRoom retries a model request that fails with a rate limit, an overload, a server error (any 5xx status, a CDN's origin timeout included), a request timeout, a conflict (409), or a dropped connection up to four times, as long as nothing was streamed yet and no provider-hosted tool had started.
Before each retry it waits as long as the provider's `Retry-After` or Azure's `retry-after-ms` header asks, when that is at most a minute, and otherwise a delay that doubles with each retry.
SDK retry settings in `client_params`, such as `max_retries` or Gemini's `http_options.retry_options`, have no effect; the Gemini SDK still resends on its own after a dropped connection and during a file upload's transfer.
A request that still fails ends the turn with "⏱️ Rate limited. Please wait a moment and try again." for a rate limit, or "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly." otherwise.
A request larger than the account's tokens-per-minute limit, which OpenAI rejects as "Request too large", is not retried, since only a smaller request can pass; it ends the turn with "⚠️ This request is larger than the account's tokens-per-minute limit. Start a new thread or compact the conversation, then try again."
A request that OpenAI, Anthropic, Gemini, or Moonshot refuses because the account has no credit left or reached its monthly spend or usage limit, that Z.ai refuses because the account has no balance or its plan or package expired, or that any provider answers with HTTP `402 Payment Required` is not retried either, and the turn ends with an error that shows the provider's message, such as "⚠️ Error: You exceeded your current quota, please check your plan and billing details."

## Stalled Streams

A provider sometimes accepts a streamed request and then sends nothing more, which would leave the agent busy and queue every follow-up message behind it.
MindRoom ends a streamed request when the provider sends no event for `stream_idle_timeout_seconds`.
If nothing was streamed yet, MindRoom sends the request once more, unless a provider-hosted tool such as web search or code execution had started, since resending would run that tool again.
A second silent attempt, or a stall after text appeared or such a tool started, ends the turn with "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
The limit measures the gap between provider events, so long replies and slow tool calls are unaffected.
It applies to every model request, also when `defaults.enable_streaming` is off, except compaction summaries, which their own [`timeout_seconds`](https://docs.mindroom.chat/configuration/history/#agent-compaction-settings) bounds.

By default the limit is 300 seconds for hosted providers on their built-in endpoint.
Local servers can stay silent for minutes while a request waits in a queue or a model loads, so `ollama`, `llama_cpp`, and any model with a configured endpoint get no limit unless you set one.
A configured endpoint is a `base_url` anywhere in `extra_kwargs` (such as `extra_kwargs.base_url` or `extra_kwargs.client_params.base_url`) or one read from `OPENAI_BASE_URL`, `GROQ_BASE_URL`, `CEREBRAS_BASE_URL`, `ANTHROPIC_BASE_URL`, or `ANTHROPIC_VERTEX_BASE_URL`.
Set the limit explicitly for a hosted service behind a custom endpoint, such as OrcaRouter or the Z.ai coding endpoint.
Endpoint variables that only a provider SDK reads, such as `GOOGLE_GEMINI_BASE_URL`, are not detected; set `0` when one points at a local server.
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
The client protocol is described in [Model Selection Protocol](https://docs.mindroom.chat/architecture/matrix/#model-selection-protocol).

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

To set an authored room default in `config.yaml`, map room keys or Matrix room IDs to model names with `room_models` (default `{}`):

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
