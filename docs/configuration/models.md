---
icon: lucide/brain
---

# Model Configuration

Models define the AI providers and model IDs used by agents.

## Supported Providers

- `anthropic` - Claude models (Anthropic)
- `openai` - GPT models and OpenAI-compatible endpoints
- `google` or `gemini` - Google Gemini models
- `vertexai_claude` - Anthropic Claude models on Google Vertex AI
- `ollama` - Local models via Ollama
- `groq` - Groq-hosted models (fast inference)
- `openrouter` - OpenRouter-hosted models (access to many providers)
- `cerebras` - Cerebras-hosted models
- `deepseek` - DeepSeek models

## Model Config Fields

Each model configuration supports the following fields:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | Yes | - | The AI provider (see supported providers above) |
| `id` | Yes | - | Model ID specific to the provider |
| `host` | No | `null` | Host URL for self-hosted models (e.g., Ollama) |
| `api_key` | No | `null` | API key (usually read from environment variables) |
| `api_key_env_var` | No | `null` | Specific environment variable name to read the API key from at runtime |
| `extra_kwargs` | No | `null` | Additional provider-specific parameters |
| `context_window` | No | `null` | Context window size in tokens. MindRoom needs it on the active runtime model to enforce replay budgets, and an explicit `compaction.model` also needs its own `context_window` for destructive compaction |

## Configuration Examples

```yaml
models:
  # Anthropic Claude
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-6
    context_window: 200000

  haiku:
    provider: anthropic
    id: claude-haiku-4-5
    context_window: 200000

  # OpenAI
  gpt:
    provider: openai
    id: gpt-5.4

  # Route OpenAI-compatible traffic through a gateway with a non-default env var
  gpt_via_gateway:
    provider: openai
    id: gpt-5.4
    api_key_env_var: LITELLM_MASTER_KEY

  # Google Gemini (both 'google' and 'gemini' work as provider names)
  gemini:
    provider: google
    id: gemini-3.1-pro-preview

  # Anthropic Claude on Vertex AI
  vertex_claude:
    provider: vertexai_claude
    id: claude-sonnet-4-6
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
    id: anthropic/claude-sonnet-4.6

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

  # Custom OpenAI-compatible endpoint (e.g., vLLM, llama.cpp server)
  custom:
    provider: openai
    id: my-model
    extra_kwargs:
      base_url: http://localhost:8080/v1
```

## Context Window

When `context_window` is set, MindRoom uses it to budget persisted replay and auto-compaction before each run.
MindRoom always applies a final replay-fit step when the active runtime model has a known `context_window`.
That replay-fit step reduces or disables persisted replay for the current run when needed.
Authoring `defaults.compaction`, or a non-empty per-agent/per-team `compaction` override, adds an optional destructive compaction phase before that replay-fit step and lets you customize the thresholds, reserve, summary model, and notices, or disable destructive auto-compaction entirely.
A bare per-entity `compaction: {}` is only a no-op override that inherits authored defaults.
`threshold_tokens` and `threshold_percent` use the active runtime model window for replay budgeting.
Manual `compact_context` still uses that active runtime window for the final replay-fit step on the next run, but destructive compaction itself can be available whenever an explicit `compaction.model` has its own `context_window`.
If you set `compaction.model`, that summary model must also define its own `context_window` for the durable summary-generation pass.
The budget uses a chars/4 approximation and reserves headroom for the current prompt and output.
MindRoom does not mutate configured `num_history_runs` to fit the window.
Instead, it may first compact older runs into `session.summary`, and it then computes the replay plan that actually fits the current call.
If needed, that replay plan can reduce raw replay, fall back to summary-only replay, or disable persisted replay entirely for the run.

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-4-6
    context_window: 200000  # 200K tokens
```

This is useful for models with smaller context windows or long-running conversations that accumulate persisted history.

## Extra Kwargs

The `extra_kwargs` field passes additional parameters directly to the underlying [Agno](https://docs.agno.com/) model class. Common options include:

- `base_url` - Custom API endpoint (useful for OpenAI-compatible servers)
- `temperature` - Sampling temperature
- `max_tokens` - Maximum tokens in response

## Environment Variables

API keys are read from environment variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
CEREBRAS_API_KEY=...
DEEPSEEK_API_KEY=...
```

If you need one model to use a different secret than the provider default, set `api_key_env_var` on that model.
MindRoom resolves model credentials in this order:

1. `models.<name>.api_key`
2. `models.<name>.api_key_env_var`
3. shared credentials stored for `model:<name>`
4. shared provider credentials for the model provider

This is useful when one runtime needs, for example, OpenAI-compatible chat traffic to use a gateway key while other OpenAI-compatible features use the real OpenAI key.

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
