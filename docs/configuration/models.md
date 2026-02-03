---
icon: lucide/brain
---

# Model Configuration

Models define the AI providers and model IDs used by agents.

## Supported Providers

- `anthropic` - Claude models (Anthropic)
- `openai` - GPT models and OpenAI-compatible endpoints
- `google` or `gemini` - Google Gemini models
- `ollama` - Local models via Ollama
- `groq` - Groq-hosted models (fast inference)
- `openrouter` - OpenRouter-hosted models (access to many providers)
- `cerebras` - Cerebras-hosted models
- `deepseek` - DeepSeek models

## Model Config Fields

Each model configuration supports the following fields:

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | Yes | The AI provider (see supported providers above) |
| `id` | Yes | Model ID specific to the provider |
| `host` | No | Host URL for self-hosted models (e.g., Ollama) |
| `api_key` | No | API key (usually read from environment variables) |
| `extra_kwargs` | No | Additional provider-specific parameters |

## Configuration Examples

```yaml
models:
  # Anthropic Claude
  sonnet:
    provider: anthropic
    id: claude-sonnet-4-latest

  haiku:
    provider: anthropic
    id: claude-haiku-3-5-latest

  # OpenAI
  gpt4:
    provider: openai
    id: gpt-4o

  # Google Gemini (both 'google' and 'gemini' work as provider names)
  gemini:
    provider: google
    id: gemini-2.0-flash

  # Local via Ollama
  local:
    provider: ollama
    id: llama3.2
    host: http://localhost:11434  # Uses dedicated host field

  # OpenRouter (access to many model providers)
  openrouter:
    provider: openrouter
    id: anthropic/claude-3-opus

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

### File-based Secrets

For container environments (Kubernetes, Docker Swarm), you can also use file-based secrets by appending `_FILE` to any environment variable name:

```bash
# Instead of setting the key directly:
ANTHROPIC_API_KEY=sk-ant-...

# Point to a file containing the key:
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

This works for all API key environment variables (e.g., `OPENAI_API_KEY_FILE`, `GOOGLE_API_KEY_FILE`, etc.).
