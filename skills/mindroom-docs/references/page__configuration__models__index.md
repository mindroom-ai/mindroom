# Model Configuration

Models define the AI providers and model IDs used by agents.

## Supported Providers

- `anthropic` - Claude models (Anthropic)
- `bedrock_claude` - Anthropic Claude models on Amazon Bedrock
- `azure` - OpenAI models through Azure OpenAI deployments
- `openai` - GPT models and OpenAI-compatible endpoints
- `codex` or `openai_codex` - OpenAI models available through a local Codex CLI ChatGPT login
- `google` or `gemini` - Google Gemini models
- `vertexai_claude` - Anthropic Claude models on Google Vertex AI
- `ollama` - Local models via Ollama
- `groq` - Groq-hosted models (fast inference)
- `openrouter` - OpenRouter-hosted models (access to many providers)
- `cerebras` - Cerebras-hosted models
- `deepseek` - DeepSeek models
- `zai` - Z.ai GLM models

## Model Config Fields

Each model configuration supports the following fields:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | Yes | - | The AI provider (see supported providers above) |
| `id` | Yes | - | Model ID specific to the provider |
| `host` | No | `null` | Host URL for self-hosted models (e.g., Ollama) |
| `api_key` | No | `null` | API key (usually read from environment variables) |
| `extra_kwargs` | No | `null` | Additional provider-specific parameters |
| `context_window` | No | `null` | Actual provider context window size in tokens; MindRoom uses it as the default replay-planning window unless compaction sets a smaller `replay_window_tokens`; an explicit `compaction.model` needs its own `context_window` for summary generation; on `vertexai_claude` it also enables request-time fitting |

## Configuration Examples

```yaml
models:
  # Anthropic Claude
  sonnet:
    provider: anthropic
    id: claude-sonnet-5
    context_window: 1000000

  haiku:
    provider: anthropic
    id: claude-haiku-4-5
    context_window: 200000

  # Anthropic Claude on Amazon Bedrock
  bedrock_opus:
    provider: bedrock_claude
    id: anthropic.claude-opus-4-8
    context_window: 1000000

  # OpenAI
  gpt:
    provider: openai
    id: gpt-5.6
    context_window: 1050000

  # Azure OpenAI
  azure:
    provider: azure
    id: your-azure-openai-deployment

  # OpenAI via a Codex CLI ChatGPT login
  codex:
    provider: codex
    id: gpt-5.6
    context_window: 258000

  # Google Gemini (both 'google' and 'gemini' work as provider names)
  gemini:
    provider: google
    id: gemini-3.1-pro-preview

  # Anthropic Claude on Vertex AI
  vertex_claude:
    provider: vertexai_claude
    id: claude-sonnet-5
    extra_kwargs:
      project_id: your-gcp-project
      region: us-central1

  # Local via Ollama
  local:
    provider: ollama
    id: llama3.2
    host: http://localhost:11434  # Uses dedicated host field

  # OpenRouter (access to many model providers)
  openrouter:
    provider: openrouter
    id: anthropic/claude-sonnet-5

  # Groq (fast inference)
  groq:
    provider: groq
    id: llama-3.1-70b-versatile

  # Cerebras
  cerebras:
    provider: cerebras
    id: llama3.1-8b

  # DeepSeek
  deepseek:
    provider: deepseek
    id: deepseek-chat

  # Z.ai (GLM models)
  glm:
    provider: zai
    id: glm-5.2
    context_window: 1048576

  # Custom OpenAI-compatible endpoint (e.g., vLLM, llama.cpp server)
  custom:
    provider: openai
    id: my-model
    extra_kwargs:
      base_url: http://localhost:8080/v1
```

## OpenAI API Models

GPT 5.4 and newer models on the first-party `openai` provider use the Responses API.
This enables OpenAI's native deferred-tool search without disabling reasoning.
Older GPT models and models configured with a custom `extra_kwargs.base_url` keep using Chat Completions for OpenAI-compatible endpoint support.

## Codex Models with ChatGPT Login

Use `provider: codex` when you want MindRoom to call models exposed through an authenticated local Codex CLI session instead of the regular OpenAI API.
Run `codex login` first so `~/.codex/auth.json` contains ChatGPT OAuth tokens.
MindRoom refreshes the access token when needed and sends requests to the Codex Responses endpoint.
Codex is included across ChatGPT plans, including Free and Go, but model access and usage limits depend on the logged-in account and current rollout.
See the [current Codex model catalog](https://developers.openai.com/codex/models) instead of assuming every account exposes the same slugs.
MindRoom maps the `gpt-5.6` alias to GPT-5.6 Sol and passes other slugs through unchanged.

| Model | Model ID | Best fit |
|-------|----------|----------|
| GPT-5.6 Sol | `gpt-5.6` or `gpt-5.6-sol` | Hard, open-ended work requiring the strongest reasoning |
| GPT-5.6 Terra | `gpt-5.6-terra` | Balanced everyday work |
| GPT-5.6 Luna | `gpt-5.6-luna` | Fast, repeatable, cost-sensitive work |

Older or preview slugs can also work when the logged-in Codex account exposes them.
The LLM-plugin-style form `openai-codex/gpt-5.6` is accepted as an alternative to the bare alias.
If you keep Codex state outside `~/.codex`, pass `extra_kwargs.codex_home`; user-home prefixes such as `~/custom-codex` are expanded.
For starter config generation, use `mindroom config init --provider codex`.

```yaml
models:
  default:
    provider: codex
    id: gpt-5.6
    context_window: 258000
    # Prompt caching is enabled automatically per active agent session.
    extra_kwargs:
      reasoning_effort: medium
```

The `258000` context window is the conservative effective budget used by the Codex ChatGPT surface, not the larger context window exposed by the separately billed OpenAI API.
Set Codex reasoning effort through `extra_kwargs.reasoning_effort`.
Agno maps this to the Responses API `reasoning.effort` field.
Supported GPT-5.6 effort values are `low`, `medium`, `high`, `xhigh`, and `max`.
Codex clients also show Ultra, but Ultra adds Codex-managed subagent orchestration and is not reproduced by this model adapter.
The starter Codex profile uses `medium`.

The Codex provider supports text and image input with text output; transcription, text-to-speech, and realtime speech are not supported.

This adapter follows the local Codex CLI authentication-file and backend contracts, so upstream Codex changes can require a MindRoom update.
Use `provider: openai` when you want the public OpenAI API contract and API billing instead.

MindRoom sends a Codex prompt-cache key plus the Codex CLI session headers for each active agent session.
By default, that key is derived from the current execution identity, so separate Matrix threads can run concurrently without sharing one global cache key.
You can set `extra_kwargs.prompt_cache_key` to override that derived key for a model, but avoid a single low-cardinality value for many busy threads unless you intentionally want those requests routed together.
Live testing against the Codex ChatGPT endpoint reported `cached_tokens` only when the request included Codex CLI-style session headers tied to the prompt-cache key.
Repeated long requests then reported cache hits, while requests without those headers stayed at `cached_tokens: 0`, and `prompt_cache_retention` was rejected.
Treat Codex prompt caching as best-effort rather than guaranteed.

## OpenRouter Provider Routing

OpenRouter routes each request to one of several upstream providers serving the model, and upstream quality varies (we have seen a third-party host leak raw tool-call markup into a visible reply).
Control routing by passing OpenRouter [provider preferences](https://openrouter.ai/docs/features/provider-routing) through `extra_kwargs.extra_body`:

```yaml
models:
  deepseek:
    provider: openrouter
    id: deepseek/deepseek-v4-pro
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
MindRoom uses Agno's AWS Bedrock Claude model wrapper and auto-installs the `aws_bedrock` optional extra on first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
The `id` field should be the Bedrock model ID or inference profile ID enabled in your AWS account and region.
Use Opus when you want the highest Claude tier available through Bedrock.

```yaml
models:
  default:
    provider: bedrock_claude
    id: anthropic.claude-opus-4-8
    context_window: 1000000
```

MindRoom reads AWS settings from the config-adjacent `.env` file, exported environment, local AWS profile, or runtime IAM role.
For starter config generation, use `mindroom config init --provider bedrock_claude`.

## Context Window

Set `context_window` to the model provider's actual limit.
MindRoom uses it to budget persisted replay and required destructive compaction unless compaction config sets a smaller `replay_window_tokens` cap.
MindRoom always applies a final replay-fit step when the active runtime model has a known `context_window`.
That replay-fit step reduces or disables persisted replay for the current run when needed.
On `vertexai_claude` models, a known `context_window` also enables a request-time guard inside the provider call.
Before each request, including follow-up requests after tool results, MindRoom estimates the full provider payload and checks it against Vertex's exact token counter when it approaches the window.
When a request would exceed the window, MindRoom drops the oldest replayed history turns for that request only and logs a warning.
When the current turn alone cannot fit, the request fails with a clear provider error instead of being sent oversized.
Automatic destructive compaction is enabled by default through `defaults.compaction`.
Set `enabled: false` in `defaults.compaction` or a per-agent/per-team `compaction` override to disable automatic pre-reply compaction.
It runs only when history exceeds the hard replay budget for the next reply.

You can tune compaction behavior with these settings:

- Use `threshold_tokens` or `threshold_percent` to set the soft trigger budget. Crossing this soft trigger while still within the hard budget leaves the stored session unchanged and relies on replay fitting for that reply.
- Use `replay_window_tokens` to keep persisted replay, required-compaction planning, and summary input chunks within a smaller operational window without presenting that smaller value as the provider's request limit.
- Use `reserve_tokens` to leave hard-budget headroom for the current prompt and output.

When the active runtime model window is known, replay safety uses the smaller of it and `replay_window_tokens`.
When that model window is unknown, an explicit `replay_window_tokens` still supplies the replay-planning window.
The effective replay window also caps each compaction summary input chunk.
Destructive compaction requires the resolved summary input budget to exceed 2,000 tokens.

Manual `compact_context` records a durable request that runs before the next reply in the same conversation scope.
Manual `compact_context` remains available when a compaction model and context window are configured and the resolved summary input budget exceeds 2,000 tokens.
It still uses the active runtime window for the final replay-fit step, while an explicit `compaction.model` can supply the summary-generation window subject to the same minimum summary-input budget.
If you set `compaction.model`, that summary model must also define its own `context_window` for the durable summary-generation pass.
Required compaction runs before the reply with a Matrix lifecycle notice that is edited in place.
Otherwise MindRoom leaves the session unchanged and relies on replay fitting for that reply.
Replay planning uses a chars/4 approximation and reserves headroom for the current prompt and output.
Summary-input chunk sizing uses the model's tiktoken encoding when recognized.
Direct Anthropic, Vertex AI Claude, and Bedrock Claude summary models without a recognized encoding use one token per UTF-8 byte as a conservative upper bound.
MindRoom does not mutate configured `num_history_runs` to fit the window.
Instead, it computes the replay plan that actually fits the current call and uses compaction to keep future replay healthy.
If needed, that replay plan can reduce raw replay, fall back to summary-only replay, or disable persisted replay entirely for the run.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-5
    context_window: 1000000  # 1M tokens

defaults:
  compaction:
    replay_window_tokens: 200000  # Cap persisted replay and compaction summary chunks
```

This is useful for models with smaller context windows or long-running conversations that accumulate persisted history.

## Extra Kwargs

The `extra_kwargs` field passes additional parameters directly to the underlying [Agno](https://docs.agno.com/) model class. Common options include:

- `base_url` - Custom API endpoint (useful for OpenAI-compatible servers)
- `temperature` - Sampling temperature
- `max_tokens` - Maximum tokens in response
- `extra_body` - Extra JSON body fields for OpenAI-compatible providers (e.g., OpenRouter provider routing above)

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
CLOUD_ML_REGION=us-central1
```

Authenticate with `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key file.

### File-based Secrets

For container environments (Kubernetes, Docker Swarm), you can also use file-based secrets by appending `_FILE` to any environment variable name:

```bash
# Instead of setting the key directly:
ANTHROPIC_API_KEY=sk-ant-...

# Point to a file containing the key:
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

This works for all API key environment variables (e.g., `OPENAI_API_KEY_FILE`, `GOOGLE_API_KEY_FILE`, etc.).
