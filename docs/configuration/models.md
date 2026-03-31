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
| `extra_kwargs` | No | `null` | Additional provider-specific parameters |
| `context_window` | No | `null` | Context window size in tokens. MindRoom needs it on the active model, or on an explicit `compaction.model`, to enforce replay budgets and run compaction |

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
When no `compaction` block is authored, that `context_window` still enables the default replay-overflow guard, which reduces or disables replay for the current run without rewriting stored history.
Authoring `compaction` lets you customize the thresholds, reserve, summary model, and notices, or disable destructive auto-compaction entirely.
Manual `compact_context`, `threshold_tokens`, and `threshold_percent` only take effect when MindRoom can resolve a usable context window from the active model or an explicit `compaction.model`.
The budget uses a chars/4 approximation and reserves headroom for the current prompt and output.
MindRoom does not mutate configured `num_history_runs` to fit the window.
Instead, it prepares scoped replay and optionally compacts older runs into `session.summary` when auto-compaction is enabled.
If the rewritten history is still too large, MindRoom performs additional compaction passes until the budget fits or it falls back to summary-only replay with no raw runs left.

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
