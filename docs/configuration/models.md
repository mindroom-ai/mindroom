---
icon: lucide/brain
---

# Model Configuration

Models define the AI providers and model IDs used by agents.
Examples were checked on September 28, 2026 against the [OpenAI catalog](https://developers.openai.com/api/docs/models), [Claude catalog](https://platform.claude.com/docs/en/models/overview), [Gemini catalog](https://ai.google.dev/gemini-api/docs/models), and [OpenRouter catalog](https://openrouter.ai/api/v1/models).
Provider-specific IDs can differ for the same model.

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

## Model Config Fields

Each model configuration supports the following fields:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | Yes | - | The AI provider (see supported providers above) |
| `id` | Yes | - | Model ID specific to the provider |
| `display_name` | No | `null` | Friendly model name shown in clients |
| `icon` | No | `null` | Image path relative to the config file's directory, or a Matrix `mxc://` URI |
| `api` | No | `null` | For `openai`, force `responses` or `chat_completions`; unset keeps automatic selection |
| `host` | No | `null` | Host URL for self-hosted models (e.g., Ollama) |
| `api_key` | No | `null` | Model-specific API key used instead of the provider's shared key; equivalent to `extra_kwargs.api_key` |
| `extra_kwargs` | No | `null` | Additional provider-specific parameters |
| `context_window` | No | `null` | Actual provider context window size in tokens; MindRoom uses it for compaction summary input and as the default replay-planning window unless compaction sets a smaller `replay_window_tokens`; an explicit `compaction.model` or `compaction.fallback_model` needs its own `context_window` for summary generation; on `vertexai_claude` it also enables request-time fitting |
| `stream_idle_timeout_seconds` | No | `null` | Seconds a streamed request may go without a provider event before MindRoom treats it as stalled; unset means 300 for hosted providers on their built-in endpoint and no limit for `ollama`, `llama_cpp`, or a configured endpoint; `0` disables the limit (see [Stalled Streams](#stalled-streams)) |

For Azure OpenAI, `id` is the Azure deployment name, not the underlying base-model name.
Provider credentials come from supported environment variables, stored credentials, CLI authentication, or a model-specific key.
A model uses the key saved for it in the dashboard's **Models** editor first, then `api_key` or `extra_kwargs.api_key` from its config, then the provider's shared key from the environment or credential store.
MindRoom never passes the shared key to a model whose `extra_kwargs` authenticate another way (`auth_token` for `anthropic`, `azure_ad_token` or `azure_ad_token_provider` for `azure`, or `vertexai: true` for `google` and `gemini`), although Agno may still read `ANTHROPIC_API_KEY` or `AZURE_OPENAI_API_KEY` from the process environment.
Setting both `api_key` and `extra_kwargs.api_key` is a validation error, and a blank key counts as unset.
`codex`, `kimi`, `bedrock_claude`, `vertexai_claude`, and `synthetic` models authenticate without an API key and ignore a configured one.

Presentation metadata is optional:

```yaml
models:
  default:
    provider: openai
    id: gpt-6-astra
    display_name: Quick helper
    icon: icons/helper.png
```

`display_name` changes the label clients show, while the mapping key (`default` above) remains the stable name used by agents, teams, routing, and commands.
For `icon`, use either a path relative to the directory containing the active config file or a Matrix media URI such as `mxc://server/media`.
Blank metadata uses the client's default presentation.
A missing or unreadable relative image also falls back at presentation time and does not prevent the runtime from starting.
These fields are presentation-only and are not passed to the model provider.

The Matrix model picker publishes only model keys, display names, provider names, model IDs, and optional Matrix media URLs.
Local icons must be valid PNG, JPEG, WebP, or GIF images no larger than 1 MiB.
At publication, the resolved image target must remain inside the resolved config directory, including when the path contains parent components or symlinks.
A contained path such as `icons/../helper.png` is accepted; an escaping target falls back to the client's provider icon.
The runtime validates image bytes and uploads them to Matrix media; identical bytes reuse the upload within that runtime client.
Changed bytes publish a new icon.
Unsupported, missing, or unreadable images fall back to the client's provider icon.
External image URLs are not fetched.
Local paths, API keys, provider hosts, and extra provider settings never enter discovery responses.
See [Matrix model selection protocol](../architecture/matrix.md#model-selection-protocol) for private discovery and confirmed thread changes.

## Hot Reload

After a successful config reload, edits to a referenced definition under `models` apply to the next agent, team, or router response without restarting its Matrix bot.
Response models, team members, and compaction budgets are resolved from the current config for each response.
This includes changes to provider options, model IDs, and `context_window`, including definitions selected by `compaction.model` or `compaction.fallback_model`.
Call-enabled agents can still restart because their call runtime retains configuration between responses.

Increasing `context_window` does not remove an explicit, smaller `compaction.replay_window_tokens` cap.
Update that cap too if you want a larger replay window.

## Claude Agent Prompt Caching

The `anthropic`, `bedrock_claude`, and `vertexai_claude` providers enable agent prompt caching with a one-hour lifetime by default.
MindRoom places a cache boundary after the shared agent identity, role, workspace context files, and tool instructions.
The current date and session context, including compaction summaries, learned context, and system-hook additions, follow that boundary while retaining system-message priority.
Changing those later sections does not invalidate the shared instruction prefix; identical tool definitions and prefix content can be reused across conversations when the provider cache is available.
The conversation cache boundaries still include the full system prompt, so repeated turns can reuse session context too.
Set `extra_kwargs.cache_system_prompt: false` to disable MindRoom's automatic Claude cache boundaries, or `extra_kwargs.extended_cache_time: false` to use the five-minute lifetime.

The `openrouter` provider applies the same cache boundaries and options to model IDs that route to Anthropic, such as `anthropic/claude-sonnet-5.5` and `~anthropic/claude-sonnet-latest`.
MindRoom sends them as OpenRouter [`cache_control` breakpoints](https://openrouter.ai/docs/guides/best-practices/prompt-caching) on message text parts and the last tool definition.
Other OpenRouter model families, such as OpenAI, DeepSeek, Z.ai, and Gemini, cache implicitly and keep their unmarked request format.
Cache reads and writes appear in usage metrics as cached and cache-write input tokens.
Compaction summaries through OpenRouter Claude skip cache writes because their prompts are not reused.

## OpenAI Responses Prompt Caching

The `openai` Responses API and `codex` providers send shared agent instructions as an initial developer message, followed by a separate developer message containing the current date and session context.
Both messages retain system-instruction priority, and compaction summaries remain in the session-specific suffix.
On the public OpenAI API, GPT-5.6 and later receive an explicit cache breakpoint at the end of the shared prefix, alongside the provider's default implicit caching.
See [OpenAI's prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) for cache eligibility and accounting.

Codex, older models, and custom OpenAI-compatible endpoints receive the same split without the explicit breakpoint field.
The Codex backend currently rejects that field, so cache reuse there still depends on provider-managed caching; splitting messages alone does not guarantee cross-conversation cache hits.
Set `extra_kwargs.cache_system_prompt: false` to preserve the original unsplit system message.
This option applies to Responses models; Chat Completions providers keep their existing request format, except the OpenRouter Claude routes described above.

## Configuration Examples

```yaml
models:
  # Anthropic Claude
  sonnet:
    provider: anthropic
    id: claude-sonnet-5-5
    context_window: 1000000

  fable:
    provider: anthropic
    id: claude-fable-5-1
    context_window: 1000000

  opus:
    provider: anthropic
    id: claude-opus-5-5
    context_window: 1000000

  haiku:
    provider: anthropic
    id: claude-haiku-4-5
    context_window: 200000

  # Anthropic Claude on Amazon Bedrock
  bedrock_opus:
    provider: bedrock_claude
    id: anthropic.claude-opus-5-5
    context_window: 1000000

  # OpenAI
  gpt:
    provider: openai
    id: gpt-6-astra
    context_window: 1050000

  # Azure OpenAI
  azure:
    provider: azure
    id: your-azure-openai-deployment

  # OpenAI via a Codex CLI ChatGPT login
  codex:
    provider: codex
    id: gpt-6.1-sol
    context_window: 258000

  # Kimi K3 via a Kimi Code CLI login (1M requires Pro/Allegretto or higher)
  kimi:
    provider: kimi
    id: k3
    context_window: 1048576

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
    host: http://localhost:11434  # Uses dedicated host field

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

Claude Fable 5.1 uses `claude-fable-5-1` on Anthropic, `anthropic.claude-fable-5-1` on Bedrock, and `anthropic/claude-fable-5.1` on OpenRouter.
Claude Opus 5.5 and Sonnet 5.5 use `claude-opus-5-5` and `claude-sonnet-5-5` on Anthropic and Vertex AI, `anthropic.claude-opus-5-5` and `anthropic.claude-sonnet-5-5` on Bedrock, and `anthropic/claude-opus-5.5` and `anthropic/claude-sonnet-5.5` on OpenRouter.
Their [tool-choice rules](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide) match Fable 5.1's: `auto` and `none` are allowed, while forcing `any` or a named tool returns an error.
Both reject `thinking: {type: disabled}` and manual `budget_tokens`; lower effort with `extra_kwargs.output_config: {effort: low}` instead, or use `extra_kwargs.thinking: {type: between_tools}` on [Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5#turn-off-up-front-thinking).
On Fable 5.1, Opus 5.5, and Sonnet 5.5 with adaptive thinking, longer notes the model writes between tool calls arrive as `thinking` blocks that are empty by default, so MindRoom responses show tool traces without that narration; Sonnet 5.5's `between_tools` setting returns those notes as text.
The [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B) and each hosting provider document their own context limits.
[GLM 5.3](https://docs.z.ai/guides/llm/glm-5.3) is available through the GLM Coding Plan endpoint shown above.
For the direct DeepSeek API, use `deepseek-flash` for V4.1 Flash or `deepseek-v4-pro` for Pro.
The older `deepseek-v4-flash` name remains accepted as a [temporary compatibility route to V4.1 Flash](https://api-docs.deepseek.com/updates/#date-2026-09-10).
OpenRouter uses the distinct model ID `deepseek/deepseek-v4.1-flash`.

## Built-In Synthetic Model

Use `provider: synthetic` to exercise normal MindRoom conversations without an API key or model server.
The model streams a seeded random amount of Lorem Ipsum at a fixed character rate.
When the agent has the `shell` tool, the model occasionally calls `run_shell_command` with `echo hi` and then continues its response.

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

Tag `@load_test` in Lobby to receive a streamed synthetic reply through the same Matrix path as any other agent.
Set `tool_call_probability: 1` to force the shell call on every turn, or `0` to disable tool calls.
Changing `seed` changes the repeatable response length, split point, and tool-call choice for each conversation history.

## OpenAI API Models

Set `api: responses` or `api: chat_completions` on an `openai` model to select the API independently of its model ID or endpoint.
Use explicit selection for proxies and custom model aliases; the endpoint must support the selected API.

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
Responses continuation works independently of the model ID, including custom aliases.
Set `extra_kwargs.reasoning_effort` or `extra_kwargs.reasoning` only when you want to customize reasoning.
`extra_kwargs.store: false` remains respected; selecting Responses does not override it.

When `api` is unset, GPT 5.4 and newer models on the first-party OpenAI endpoint use Responses.
Other routes, including all custom OpenAI-compatible endpoints, default to Chat Completions.
If you previously relied on automatic GPT-6 routing through a proxy, add `api: responses` before upgrading, or tool calls on Astra, Sol, and Luna fail at their default reasoning effort.
Explicit Chat Completions disables native deferred-tool search and keeps MindRoom's dynamic-tool discovery.
Selecting Responses on a custom endpoint does not enable OpenAI's hosted tool search; only supported first-party OpenAI and Codex routes use it.

## OrcaRouter via the OpenAI-Compatible Endpoint

[OrcaRouter](https://docs.orcarouter.ai/introduction) works through MindRoom's existing `openai` provider; no dedicated provider or plugin is required.
Configure its endpoint and an OrcaRouter API key on the model:

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

The `orcarouter/auto` ID lets OrcaRouter choose a model per request; you can replace it with a model ID from its [catalog](https://docs.orcarouter.ai/getting-started/models).
Set an agent's `model: orcarouter`, or select it for a thread with `!model orcarouter`.
`stream_idle_timeout_seconds` gives this custom endpoint the same [stall protection](#stalled-streams) that hosted providers get by default.

To keep the key out of YAML, omit `extra_kwargs.api_key` and save the OrcaRouter key in the dashboard's **Models** editor, in this model's **API Key** field.
That model-specific credential takes precedence over `extra_kwargs.api_key` and the shared OpenAI key, so OrcaRouter and direct OpenAI models can use separate keys.
MindRoom does not automatically read `ORCAROUTER_API_KEY` for `provider: openai`.

## Codex Models with ChatGPT Login

Use `provider: codex` when you want MindRoom to call models exposed through an authenticated local Codex CLI session instead of the regular OpenAI API.
Run `codex login` first so `~/.codex/auth.json` contains ChatGPT OAuth tokens.
MindRoom refreshes the access token when needed and sends requests to the Codex Responses endpoint.
Codex is included across ChatGPT plans, including Free and Go, but model access and usage limits depend on the logged-in account and current rollout.
See the [current Codex model catalog](https://developers.openai.com/codex/models) instead of assuming every account exposes the same slugs.
MindRoom maps the legacy `gpt-5.6` alias to `gpt-5.6-sol` and passes other slugs through unchanged.

| Model | Model ID | Best fit |
|-------|----------|----------|
| GPT-6.1 Sol | `gpt-6.1-sol` | Near-Astra reasoning for long-running work; the starter default |
| GPT-6 Astra | `gpt-6-astra` | The hardest end-to-end reasoning and agentic work |
| GPT-6 Luna | `gpt-6-luna` | Efficient, high-volume work such as summaries and routing |

Older or preview slugs can also work when the logged-in Codex account exposes them.
The LLM-plugin-style form `openai-codex/gpt-6.1-sol` is accepted as an alternative to the bare alias.
If you keep Codex state outside `~/.codex`, set `CODEX_HOME` in the process environment or pass `extra_kwargs.codex_home`; user-home prefixes such as `~/custom-codex` are expanded.
For starter config generation, use `mindroom config init --provider codex`.
The starter defines GPT-6.1 Sol as `default`, GPT-6 Astra as `astra`, and GPT-6 Luna at low reasoning effort as `luna`, with the display names Sol, Astra, and Luna, and runs the router and thread summaries (with their one-shot tags) on `luna`.

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

The `258000` context window is the conservative effective budget used by the Codex ChatGPT surface (just under 95% of its 272000-token window), not the larger context window exposed by the separately billed OpenAI API.
Set Codex reasoning effort through `extra_kwargs.reasoning_effort`.
Agno maps this to the Responses API `reasoning.effort` field.
GPT-6.1 Sol, GPT-6 Astra, and GPT-6 Luna accept `low`, `medium`, `high`, `xhigh`, and `max`.
Codex clients also show Ultra for GPT-6.1 Sol and GPT-6 Astra, but Ultra adds Codex-managed subagent orchestration and is not reproduced by this model adapter.
The starter Codex profile uses `medium` for `default` and `astra`, and `low` for `luna`.

The Codex provider supports text and image input with text output; transcription, text-to-speech, and realtime speech are not supported.

This adapter follows the local Codex CLI authentication-file and backend contracts, so upstream Codex changes can require a MindRoom update.
Use `provider: openai` when you want the public OpenAI API contract and API billing instead.

MindRoom derives the Codex `prompt_cache_key` from the storage-root path, tenant, account, channel, agent, and requester.
Rooms, threads, and session IDs are excluded, so related conversations can share a cache group while different agents, requesters, and storage roots remain separate.
The storage-root path is a local namespace, not a globally unique installation identifier; deployments with identical paths and execution scopes can share a cache group within the same provider account.
Use explicit cache-key overrides if those deployments need separate cache accounting.
Codex CLI session headers use a separate key for each conversation; changing the cache group does not combine session identities.
Set `extra_kwargs.prompt_cache_key` to override the cache group, or set it to `null` to omit the key while retaining the conversation headers.
Model calls without an execution identity do not receive a derived cache key or session headers.
Identical prefixes and tools are still required for reuse, and provider routing and cache availability can produce misses even with a shared key.
Live probes accepted separate cache keys and session headers and reported some repeated-request cache hits, but did not establish reliable cache reuse across conversations.

## Kimi Models with Kimi Code Login

Use `provider: kimi` when you want MindRoom to call Kimi models through an authenticated local Kimi Code CLI session (Kimi Code subscription) instead of the billed Moonshot API.
Run `kimi` and `/login` first so `~/.kimi-code/credentials/kimi-code.json` contains OAuth tokens.
MindRoom refreshes the access token when needed and sends requests to the Kimi Code OpenAI-compatible endpoint at `https://api.kimi.com/coding/v1`.

| Model | Model ID | Best fit |
|-------|----------|----------|
| Kimi K3 | `k3` | Flagship reasoning, long-horizon coding, and agent work with up to a 1M-token context |
| Kimi K3 256k | `k3-256k` | The same K3 generation with a 256k-token context |
| Kimi for Coding | `kimi-for-coding` | Coding-tuned tier exposed by the Kimi Code CLI |
| Kimi for Coding Highspeed | `kimi-for-coding-highspeed` | Faster coding tier exposed by the Kimi Code CLI |

[Kimi Code context limits depend on the subscription plan](https://www.kimi.com/code/docs/en/kimi-code/models.html): `k3` supports 1,048,576 tokens on Pro (legacy Allegretto) and higher plans, while Plus (legacy Moderato) is limited to 262,144 tokens.
Set `context_window: 262144` for Plus/Moderato.
The fixed-window `k3-256k` model also requires `context_window: 262144`; changing the model ID does not set MindRoom's context budget or grant a higher plan limit.

The CLI-config-style form `kimi-code/k3` is accepted as an alternative to the bare slug.
If you keep Kimi Code state outside `~/.kimi-code`, set `KIMI_CODE_HOME` or pass `extra_kwargs.kimi_home`; user-home prefixes such as `~/custom-kimi` are expanded.
For starter config generation, use `mindroom config init --provider kimi`.
The starter and the following 1M example require Pro/Allegretto or higher; Plus/Moderato users must change `context_window` to `262144`.

```yaml
models:
  default:
    provider: kimi
    id: k3
    context_window: 1048576
```

Kimi K3 always reasons before replying, so responses include reasoning tokens even for short answers.
This adapter follows the local Kimi Code CLI authentication-file and backend contracts, so upstream Kimi Code changes can require a MindRoom update.

Prompt caching is automatic on the Kimi Code endpoint: repeated request prefixes come back as `cached_tokens` with no opt-in.
MindRoom uses the same agent-and-requester cache grouping as Codex, so rooms and threads share a `prompt_cache_key` within the same storage-root path, tenant, account, and channel.
Set `extra_kwargs.prompt_cache_key` to override that group, or set it to `null` to omit the key.

## OpenRouter Provider Routing

OpenRouter routes each request to one of several upstream providers serving the model, and upstream quality varies (we have seen a third-party host leak raw tool-call markup into a visible reply).
Control routing by passing OpenRouter [provider preferences](https://openrouter.ai/docs/features/provider-routing) through `extra_kwargs.extra_body`:

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
Account-level OpenRouter settings (ignored providers, data-policy filters) still apply and cannot be overridden per request, so pinning an upstream your account excludes fails with `No endpoints found`.
Verify a new pin with a direct API test request and check the `provider` field in the response.

## Azure OpenAI

Use `provider: azure` when your model is deployed through Azure OpenAI.
The `id` field should be your Azure OpenAI deployment name, not necessarily the upstream model name.
MindRoom reads Azure OpenAI credentials and endpoint values from the config-adjacent `.env` file or exported environment.

```yaml
models:
  default:
    provider: azure
    id: your-azure-openai-deployment
```

Azure deployment limits vary, so starter configs do not set `context_window` for Azure.
Set `context_window` to the limit of your deployment when you know it.
Set `AZURE_OPENAI_API_VERSION` only when you need to override Agno's default API version.
For starter config generation, use `mindroom config init --provider azure`.

## Amazon Bedrock Claude

Use `provider: bedrock_claude` when you want MindRoom to call Anthropic Claude through Amazon Bedrock.
MindRoom uses Anthropic's Bedrock Mantle Messages client and auto-installs the `aws_bedrock` optional extra on first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
The `id` field should be the Bedrock model ID or inference profile ID enabled in your AWS account and region.
Bedrock lists Fable 5.1 and Sonnet 5 as open access, while Opus 5.5 and Sonnet 5.5 access can depend on the AWS account and region.
The generated Bedrock starter config defaults to Opus 5.5 and comments out a Sonnet 5.5 alternative, so confirm access, or use its `fable` alternative or `id: anthropic.claude-sonnet-5` instead.

```yaml
models:
  default:
    provider: bedrock_claude
    id: anthropic.claude-opus-5-5
    context_window: 1000000
```

MindRoom reads AWS settings from the config-adjacent `.env` file, exported environment, local AWS profile, or runtime IAM role.
For starter config generation, use `mindroom config init --provider bedrock_claude`.

## Stalled Streams

A provider sometimes accepts a streamed request and then sends nothing more, which would leave the agent busy and queue every follow-up message behind it.
HTTP read timeouts do not catch this, because proxies such as OpenRouter keep the connection alive with SSE comments while an upstream hangs.
MindRoom therefore ends a streamed request when the provider sends no event for `stream_idle_timeout_seconds`.
If nothing was streamed yet, MindRoom sends the request once more; a second silent attempt, or a stall after text already appeared, ends the turn with a "temporarily unavailable" error.
The limit covers the gap between provider events within one request, so long replies and slow tool calls are unaffected.
It applies only to streamed responses (`defaults.enable_streaming: true`, the default); with streaming disabled, requests rely on the provider client's own timeouts.

By default the limit is 300 seconds for hosted providers on their built-in endpoint.
Local servers often stay silent for minutes while a request waits behind another one or a model loads, so `ollama`, `llama_cpp`, and any model with a configured endpoint get no limit unless configured.
A configured endpoint is a `base_url` anywhere in `extra_kwargs` (such as `extra_kwargs.base_url` or `extra_kwargs.client_params.base_url`) or one MindRoom reads from the environment (`OPENAI_BASE_URL`, `ANTHROPIC_VERTEX_BASE_URL`).
MindRoom cannot tell a hosted service behind a custom endpoint, such as OrcaRouter or the Z.ai coding endpoint, from a local server, so set the limit explicitly to protect it.
Endpoint variables that only a provider SDK reads, such as `ANTHROPIC_BASE_URL`, are not detected; set `0` when one points at a local server.
To opt a local model in, pick a value above its slowest queue wait plus model load; set `0` to turn the limit off for a hosted model:

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

See [Context Window](history.md#context-window).

## Extra Kwargs

The `extra_kwargs` field configures additional parameters on the underlying [Agno](https://docs.agno.com/) model class.
Common options include:

- `base_url` - Custom API endpoint (useful for OpenAI-compatible servers)
- `temperature` - Sampling temperature
- `max_tokens` - Maximum tokens in response
- `extra_body` - Extra JSON body fields for OpenAI-compatible providers (e.g., OpenRouter provider routing above)

Claude Fable 5.1, Opus 5.5, Sonnet 5.5, Opus 5, and Sonnet 5 reject non-default `temperature`, `top_p`, and `top_k` values, so MindRoom omits those controls on Anthropic, Bedrock, and Vertex requests.
MindRoom also omits those deprecated controls for direct Gemini 3.8 Flash and Gemini 3.5 Flash-Lite requests.
[GPT-6 Astra does not support `temperature`, `top_p`, or `top_logprobs`](https://developers.openai.com/api/docs/guides/latest-model#update-api-and-model-parameters); omit these from authored model options.
GPT-6 Sol and Luna reject the same controls unless reasoning effort is `none`.
Automatic thread summaries omit their temperature override for GPT-6 Astra, Sol, and Luna, including their OpenRouter routes.
For Mem0 memory extraction with `provider: openai`, use GPT-6 Luna; MindRoom drops the `temperature` and `top_p` values Mem0 sends, which GPT-6 Astra, Sol, and Luna reject.

## Environment Variables

API keys are read from environment variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
# Optional, only when overriding Agno's default Azure OpenAI API version:
# AZURE_OPENAI_API_VERSION=2024-10-21
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
AWS_REGION=us-east-1
# Optional static credentials:
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
# Optional local profile instead:
AWS_PROFILE=...
```

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

### File-based Secrets

For container environments (Kubernetes, Docker Swarm), supported API-key and secret variables can also use a `_FILE` suffix:

```bash
# Instead of setting the key directly:
ANTHROPIC_API_KEY=sk-ant-...

# Point to a file containing the key:
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

This works for all API key environment variables (e.g., `OPENAI_API_KEY_FILE`, `GOOGLE_API_KEY_FILE`, etc.).

## Model Overrides in Chat

Model discovery uses Olm-encrypted to-device events, including for unencrypted rooms.
Only the configured router registers the receiver.
No room-state advertisement, extra state permissions, or external discovery endpoint is required.
The receiver authenticates the actual requesting device and checks current joined requester/router membership plus at least one configured, permitted, joined agent.
Teams alone do not qualify.
An included thread must be a readable, unredacted root in that room; encrypted roots are decrypted before validation.
Replies target only that authenticated device.
Sending a catalog never verifies a previously untrusted device.
Blocked devices, malformed requests, and unauthorized room/thread scopes receive no response.
Catalogs exceeding 256 models or 64 KiB UTF-8 JSON are unavailable rather than truncated.
An older or unavailable backend leaves the existing `!model` command available.

### `!model`

Show or switch the model that the agents, teams, and router you may address use in the current thread.

```
!model
!model list
!model opus
!model reset
```

The override applies only to the entities whose `access` admits the user who set it in this room; every other entity keeps its own thread override or room-level model.
During a configured team's turn, the team's thread override also applies to its member agents, because the team's `access` reaches them.
`!model reset` likewise removes the override only for the entities you may address.

`!model` and `!model list` show each thread override with the entities it applies to, and the available model names.
Model names come from the `models:` section of `config.yaml`.
The override applies from the next message in the thread and survives restarts.
Other threads keep their own thread override when present and otherwise use their room's effective default; other rooms remain independent.
Use `!room_model` for a durable runtime room default or `room_models` in `config.yaml` for an authored room default.
Agents with the `thread_model` tool can list configured models and switch either after the tool call in the current response or from the next turn.

### `!room_model`

Show or switch the model that every agent, team, and the router uses by default in the current room.

```
!room_model
!room_model list
!room_model opus
!room_model reset
```

`!room_model` and `!room_model list` show the current runtime override and available model names.
`!room_model opus` stores a durable room override without modifying `config.yaml`.
The new default applies to subsequent turns; an in-progress or approval-paused turn keeps the model choices it started with.
`!room_model reset` removes the runtime override so the configured `room_models` choice or each entity's configured model applies again.
Thread-level `!model` overrides and explicit per-run model choices take precedence over the room default.
Set and reset are Matrix room-admin-only actions, while status is available to authorized room members.
The override is keyed by Matrix room ID and stored under `mindroom_data/tracking`, so it survives restarts and room-alias changes.

### [`thread_model`]

`thread_model` lets agents list configured models or show, switch, and reset the model override for the current Matrix thread, mirroring the `!model` chat command.

#### What It Does

`thread_model` exposes `list_models()`, `get_thread_model()`, `switch_thread_model(model_name, when)`, and `reset_thread_model()`.
`list_models` returns every configured model alias with its provider and provider model ID and does not require an active thread.
The other three functions require an active thread context and return an error outside a thread.
`switch_thread_model` accepts a configured model name from the `models:` section of `config.yaml` and rejects unknown names with the available model list.
Its optional `when` argument accepts `after-toolcall` or `next-turn` and defaults to `next-turn`.
With `after-toolcall`, MindRoom rebuilds the current agent or team with the selected model and continues the same response after the tool call.
With `next-turn`, the current response continues with the model it started with and the selected model begins on the next user turn.
The override applies to the agents, teams, and router in the thread that the requester may address, and persists across restarts.
Every other entity keeps its own thread override or room-level model.
During a configured team's turn, the team's thread override also applies to its member agents, because the team's `access` reaches them.
`get_thread_model` returns an `overrides` map from each entity with an active thread override to its model, plus the available model names.
When a stored override names a model that has been removed from `config.models`, runtime resolution ignores it, and `get_thread_model` reports that entity under `stale_overrides` instead of `overrides`.
`reset_thread_model` removes the thread override of the entities the requester may address so room-level model selection applies to them: an active runtime `!room_model` override, then configured `room_models`, then each entity's configured model.

#### Configuration

This tool has no tool-specific inline configuration fields.

#### Example

```yaml
agents:
  assistant:
    tools:
      - thread_model
```

```python
list_models()
get_thread_model()
switch_thread_model("opus", when="after-toolcall")
reset_thread_model()
```

#### Notes

- The override is stored per thread root and entity in `mindroom_data/tracking/thread_models.json`.
- Users can manage the same override with the `!model` chat command; see [Chat Commands](../chat-commands.md).
- An explicit `active_model_name` (for example a delegated child run) still beats the thread override, and the thread override beats the runtime `!room_model` choice, configured `room_models`, and the authored entity model.
