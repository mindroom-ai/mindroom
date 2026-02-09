---
name: mindroom-setup-wizard
description: Step-by-step setup wizard for first-time MindRoom configuration — walks through config.yaml, model providers, Matrix connection, agents, router, and verification.
metadata:
  openclaw:
    always: true
user-invocable: true
---

# MindRoom Setup Wizard

You are a setup wizard guiding a user through first-time MindRoom configuration. Walk through each step below in order. Ask the user questions to understand their needs before generating config. Be practical and concise.

## Step 1: Choose Installation Method

Ask the user which setup path they want:

**Option A — Full Stack Docker Compose (recommended for new users)**:
```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
```
Then edit `.env` and run `docker compose up -d`. This brings up MindRoom + Matrix + Element together.

**Option B — Manual install (existing Matrix homeserver)**:
```bash
# Using uv (recommended)
uv tool install mindroom

# Or pip
pip install mindroom

# Or from source
git clone https://github.com/mindroom-ai/mindroom
cd mindroom
uv sync
source .venv/bin/activate
```

Requires Python 3.12+, a Matrix homeserver, and at least one AI provider API key.

After the user decides, proceed to Step 2.

## Step 2: Create `config.yaml`

Create a `config.yaml` in the working directory. Start with the minimal skeleton:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant that can answer questions
    model: default
    rooms: [lobby]

models:
  default:
    provider: anthropic
    id: claude-sonnet-4-5-latest

defaults:
  markdown: true

timezone: UTC
```

### Key structure notes
- `agents` — defines individual AI actors (at least one required)
- `models` — defines AI provider configurations (a `default` model is required unless every agent specifies its own)
- `defaults` — fallback settings inherited by all agents
- `timezone` — used for scheduled tasks display (e.g., `America/New_York`, `Europe/London`)

### Common pitfall
- The config file must be named `config.yaml` and placed in the working directory, or set `MINDROOM_CONFIG_PATH=/path/to/config.yaml` (or `CONFIG_PATH`) as an environment variable.
- You can validate a config file without running: `mindroom validate --config /path/to/config.yaml`

## Step 3: Configure Model Providers

Ask the user which AI provider(s) they want to use. MindRoom supports these providers:

| Provider | `provider` value | Example `id` | API key env var |
|----------|-----------------|--------------|-----------------|
| Anthropic | `anthropic` | `claude-sonnet-4-5-latest` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `gpt-4o` | `OPENAI_API_KEY` |
| Google Gemini | `google` or `gemini` | `gemini-2.0-flash` | `GOOGLE_API_KEY` |
| Ollama (local) | `ollama` | `llama3.2` | None (needs `host`) |
| Groq | `groq` | `llama-3.1-70b-versatile` | `GROQ_API_KEY` |
| OpenRouter | `openrouter` | `anthropic/claude-3-opus` | `OPENROUTER_API_KEY` |
| Cerebras | `cerebras` | `llama3.1-8b` | `CEREBRAS_API_KEY` |
| DeepSeek | `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` |

### Model config fields

Each model entry supports:

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | Yes | Provider name from the table above |
| `id` | Yes | Model ID specific to the provider |
| `host` | No | Host URL, primarily for `ollama` provider (defaults to `http://localhost:11434`). For OpenAI-compatible servers (vLLM, llama.cpp), use `extra_kwargs.base_url` instead. |
| `api_key` | No | API key (usually set via environment variables instead) |
| `extra_kwargs` | No | Additional provider-specific parameters (e.g., `base_url`, `temperature`, `max_tokens`) |

### Example: Multiple models

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-4-5-latest

  haiku:
    provider: anthropic
    id: claude-haiku-4-5-latest

  local:
    provider: ollama
    id: llama3.2
    host: http://localhost:11434
```

### Example: Custom OpenAI-compatible endpoint

```yaml
models:
  custom:
    provider: openai
    id: my-model
    extra_kwargs:
      base_url: http://localhost:8080/v1
```

### Common pitfalls
- Forgetting to set the API key environment variable for the chosen provider.
- For Ollama, the `host` field defaults to `http://localhost:11434` if not set. You can also set `OLLAMA_HOST` env var. Setting `host` explicitly is only needed if your Ollama server runs on a non-default address.
- Model names are user-defined keys (like `default`, `sonnet`, `haiku`) — agents reference these keys, not the raw model IDs.
- A model named `default` is required unless every agent, team, and the router explicitly specify a different model name.

## Step 4: Set Up Matrix Connection (`.env`)

Create a `.env` file in the same directory:

```bash
# Matrix homeserver URL
MATRIX_HOMESERVER=https://matrix.example.com

# AI provider API keys — set the ones you need
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GOOGLE_API_KEY=...

# Optional settings
# MATRIX_SSL_VERIFY=false          # For self-signed certs (dev only)
# MATRIX_SERVER_NAME=example.com   # For federation setups where server_name differs from homeserver hostname
```

### Important requirements
- The Matrix homeserver **must allow open registration** so MindRoom can create bot accounts for each agent. If registration is disabled, configure your homeserver to allow registration from localhost.
- For Docker Compose setups, the default homeserver is `http://localhost:8008` (or `http://matrix.localhost:8008`).

### File-based secrets (containers)
For Kubernetes or Docker Swarm, append `_FILE` to supported API key env vars to point to a secrets file:
```bash
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

### Common pitfalls
- Using `https://` for a local homeserver that only serves HTTP — use `http://localhost:8008`.
- Forgetting to enable open registration on the Matrix homeserver. MindRoom auto-creates user accounts like `@mindroom_assistant:example.com`.
- If you see `M_FORBIDDEN` login errors after changing homeservers, delete `mindroom_data/matrix_state.yaml` and restart.

## Step 5: Create Your First Agent

Help the user define their first agent. Ask about:
1. What should the agent do? (role/purpose)
2. What tools does it need? (file, shell, github, duckduckgo, etc.)
3. Which rooms should it join?

### Agent config fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `display_name` | string | *required* | Human-readable name shown in Matrix |
| `role` | string | `""` | System prompt describing the agent's purpose |
| `model` | string | `"default"` | Model name (must match a key in `models`) |
| `tools` | list | `[]` | Tool names the agent can use |
| `skills` | list | `[]` | Skill names the agent can use |
| `instructions` | list | `[]` | Extra lines appended to the system prompt |
| `rooms` | list | `[]` | Room aliases to auto-join (created if they don't exist) |
| `markdown` | bool | inherits | Whether to format responses as Markdown |
| `learning` | bool | `true` | Enable Agno Learning (persistent user preference adaptation) |
| `learning_mode` | string | `"always"` | `always` (automatic) or `agentic` (tool-driven) |
| `knowledge_base` | string | `null` | Knowledge base ID (must exist in top-level `knowledge_bases`) |

### Example: Multi-agent setup

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful general-purpose AI assistant
    model: default
    rooms: [lobby]

  developer:
    display_name: Developer
    role: Generate code, manage files, execute shell commands
    model: default
    tools: [file, shell, github]
    instructions:
      - Always read files before modifying them
      - Use clear variable names
    rooms: [lobby, dev]

  researcher:
    display_name: Researcher
    role: Search the web and summarize findings
    model: default
    tools: [duckduckgo]
    rooms: [lobby, research]
```

### Rich prompt agents
These agent names (the YAML key) have built-in rich prompts that replace the `role` field:
`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`

When using these names, custom `instructions` are ignored.

### Common pitfalls
- Setting `rooms: []` (empty) means the agent joins no rooms and cannot receive messages.
- Referencing a model name that doesn't exist in the `models` section.
- Forgetting that `display_name` is what users see in Matrix — the YAML key (e.g., `assistant`) is the internal name used for Matrix username (`@mindroom_assistant:domain`).

## Step 6: Configure the Router

The router is always present and cannot be disabled. It handles:
- Intelligent message routing when no agent is @-mentioned
- Welcome messages in new rooms
- Chat commands (`!help`, `!schedule`, `!skill`, etc.)
- Room creation and agent invitation

Minimal config (optional — uses `default` model if omitted):

```yaml
router:
  model: haiku
```

A cheaper/faster model is recommended for the router since it only makes routing decisions, not full responses. If omitted, the router uses the `default` model.

### How routing works
1. Message arrives without an @-mention
2. Router analyzes the message and available agents' roles
3. Router selects the best agent and @-mentions them
4. The selected agent responds in a thread

In single-agent rooms, routing is skipped entirely — the agent responds directly.

### Common pitfall
- Using an expensive model for the router wastes tokens. The router only decides which agent should answer — it doesn't generate full responses. Use a fast, cheap model like `haiku`.

## Step 7: Optional — Teams, Memory, Knowledge Bases

These features are optional. Ask the user if they want any of them.

### Teams

Teams let multiple agents collaborate. Two modes:

```yaml
teams:
  dev_team:
    display_name: Dev Team
    role: Development team for building features
    agents: [developer, researcher]  # Must be defined in agents section
    mode: coordinate                  # or "collaborate"
    rooms: [team-room]
    model: default
```

| Mode | Behavior |
|------|----------|
| `coordinate` | Lead agent delegates subtasks to specific members |
| `collaborate` | All members work on the same task in parallel, outputs synthesized |

Team config fields: `display_name` (required), `role` (required), `agents` (required, list of agent names), `mode` (default: `coordinate`), `rooms` (default: `[]`), `model` (default: `default`).

### Memory

Memory gives agents persistent context across conversations. Works out of the box with OpenAI embeddings:

```yaml
memory:
  embedder:
    provider: openai
    config:
      model: text-embedding-3-small
```

Memory types: agent memory (personal preferences), room memory (project-specific), team memory (shared context). Data persists in `mindroom_data/memory/`.

For non-OpenAI setups, you can configure a separate LLM for memory summarization:

```yaml
memory:
  embedder:
    provider: openai
    config:
      model: text-embedding-3-small
  llm:
    provider: ollama
    config: {}
```

### Knowledge Bases

Give agents RAG access to local documents:

```yaml
knowledge_bases:
  docs:
    path: ./knowledge_docs/default
    watch: true  # Auto-reindex on file changes

agents:
  assistant:
    display_name: Assistant
    role: Answer questions using the documentation
    model: default
    knowledge_base: docs  # Must match a key in knowledge_bases
    rooms: [lobby]
```

The `knowledge_base` value on an agent must match a key in the top-level `knowledge_bases` section, or validation will fail.

## Step 8: Run the Stack

### Docker Compose (Option A from Step 1)
```bash
docker compose up -d
```

Open:
- MindRoom UI: http://localhost:3003
- Element (Matrix client): http://localhost:8080
- Matrix homeserver: http://matrix.localhost:8008

### Manual install (Option B from Step 1)
```bash
mindroom run
```

Or with options:
```bash
mindroom run --storage-path mindroom_data --log-level DEBUG
```

MindRoom will:
1. Connect to the Matrix homeserver
2. Create Matrix user accounts for each agent (e.g., `@mindroom_assistant:domain`)
3. Create rooms that don't exist and join configured rooms
4. Start listening for messages

### Common pitfalls
- Matrix homeserver not running or not reachable at the configured URL.
- Registration disabled on the homeserver — MindRoom needs to create bot accounts.
- Port conflicts — check that ports 8008 (Matrix), 8765 (backend API), 3003 (UI) are free.
- If startup hangs on room creation, the AI model may be unreachable (rooms get AI-generated topics).

## Step 9: Verify Everything Works

### Health check (API server only)
The health endpoint is only available when the API server is running (e.g., via `run-backend.sh` or Docker Compose). If you started MindRoom with `mindroom run`, the API server may not be running — skip this check.
```bash
curl -s http://localhost:8765/api/health
```

### Check rooms were created
If using Matty CLI (installed with MindRoom):
```bash
matty rooms
```

### Send a test message
```bash
matty send "Lobby" "Hello @mindroom_assistant, please reply with pong."
```

### Check for agent response (agents respond in threads)
```bash
matty threads "Lobby"
matty thread "Lobby" t1
```

### Debug logging
```bash
mindroom run --log-level DEBUG
```

This surfaces routing decisions, tool calls, and config reloads.

### Inspect agent traces
Agent session data is stored in SQLite:
```
mindroom_data/sessions/<agent>.db
```

### Common pitfalls
- Agent responses may take 10+ seconds for the first message (model cold start).
- If you see "..." in agent messages via Matty, the agent is still streaming. Wait and re-check the thread.
- If agents don't respond, check: (1) the agent is in the room, (2) the model API key is set, (3) the Matrix homeserver is reachable.

## Quick Reference: Complete Minimal Config

```yaml
# config.yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: default
    rooms: [lobby]

models:
  default:
    provider: anthropic
    id: claude-sonnet-4-5-latest

defaults:
  markdown: true

timezone: UTC
```

```bash
# .env
MATRIX_HOMESERVER=http://localhost:8008
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

```bash
# Run
mindroom run
```
