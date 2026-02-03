---
icon: lucide/brain
---

# Model Configuration

Models define the AI providers and model IDs used by agents.

## Supported Providers

- `anthropic` - Claude models
- `openai` - GPT models
- `google` - Gemini models
- `ollama` - Local models via Ollama
- `groq` - Groq-hosted models
- `cerebras` - Cerebras-hosted models

## Configuration

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

  # Google Gemini
  gemini:
    provider: google
    id: gemini-2.0-flash

  # Local via Ollama
  local:
    provider: ollama
    id: llama3.2
    extra_kwargs:
      host: http://localhost:11434

  # Custom OpenAI-compatible endpoint
  custom:
    provider: openai
    id: my-model
    extra_kwargs:
      base_url: http://localhost:8080/v1
```

## Environment Variables

API keys are read from environment variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...
GROQ_API_KEY=...
```
