---
icon: lucide/plug
---

# OpenAI-Compatible API

MindRoom exposes an OpenAI-compatible chat completions API so any chat frontend can use MindRoom agents as selectable "models". LibreChat, Open WebUI, LobeChat, ChatBox, BoltAI, and anything else that speaks the OpenAI protocol works out of the box.

## How It Works

The frontend calls `GET /v1/models` and sees your agents in the model picker. The user picks an agent and chats. The frontend sends standard OpenAI requests; MindRoom routes them to the selected agent with all its tools, instructions, and memory. The frontend doesn't know it's talking to an agent — it's transparent.

```
Chat Frontend (LibreChat, Open WebUI, etc.)
│
│  GET /v1/models          → returns your agents as "models"
│  POST /v1/chat/completions → routes to the selected agent
│
└──→ MindRoom API ──→ ai_response() / stream_agent_response()
                         │
                         └──→ agents, tools, memory, knowledge bases
```

No Matrix dependency. The API works standalone.

## Setup

### 1. Set API keys

Add to your `.env`:

```bash
# Option A: Set API keys (recommended for production)
OPENAI_COMPAT_API_KEYS=sk-my-secret-key-1,sk-my-secret-key-2

# Option B: Allow unauthenticated access (local dev only)
OPENAI_COMPAT_ALLOW_UNAUTHENTICATED=true
```

Without either of these, the API returns 401 on all requests.

### 2. Start the backend

```bash
uv run mindroom run
# or
just start-backend-dev
```

The API is available at `http://localhost:8765/v1/`.

### 3. Verify

```bash
# List available agents
curl -H "Authorization: Bearer sk-my-secret-key-1" \
  http://localhost:8765/v1/models

# Chat (non-streaming)
curl -H "Authorization: Bearer sk-my-secret-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"general","messages":[{"role":"user","content":"Hello"}]}' \
  http://localhost:8765/v1/chat/completions

# Chat (streaming)
curl -N -H "Authorization: Bearer sk-my-secret-key-1" \
  -H "Content-Type: application/json" \
  -d '{"model":"general","messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  http://localhost:8765/v1/chat/completions
```

## Client Configuration

### LibreChat

Add to your `librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "MindRoom"
      apiKey: "${MINDROOM_API_KEY}"
      baseURL: "http://localhost:8765/v1"
      models:
        default: ["general"]
        fetch: true
      modelDisplayLabel: "MindRoom"
      titleConvo: true
      titleModel: "general"
      dropParams: ["stop", "frequency_penalty", "presence_penalty", "top_p"]
      headers:
        X-LibreChat-Conversation-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
```

### Open WebUI

1. Go to **Admin Settings > Connections > OpenAI > Manage**
2. Set API URL to `http://localhost:8765/v1`
3. Set API Key to one of your `OPENAI_COMPAT_API_KEYS`
4. Agents appear automatically in the model picker

### Any OpenAI-compatible client

Point the base URL at `http://localhost:8765/v1` and set the API key. MindRoom responds to the same endpoints as the OpenAI API.

## Features

### Model selection

Each agent in `config.yaml` appears as a selectable model. The model ID is the agent's internal name (e.g., `code`, `research`), and the display name comes from `display_name`.

### Auto-routing

Select the `auto` model to let MindRoom's router pick the best agent for each message, the same routing logic used in Matrix rooms.

### Teams

Teams are exposed as `team/<team_name>` models. Selecting `team/super_team` runs the full team collaboration or coordination workflow.

### Streaming

`stream: true` returns Server-Sent Events in the standard OpenAI format: role chunk, content chunks, finish chunk, `[DONE]`.

Tool calls appear inline as text in the stream (not as native OpenAI `tool_calls` deltas).

### Session continuity

Session IDs are derived from request headers:

1. `X-Session-Id` header (explicit control)
2. `X-LibreChat-Conversation-Id` header (automatic with LibreChat)
3. Random UUID fallback

Agent memory and conversation history persist across requests with the same session ID.

### Knowledge bases

Agents with configured `knowledge_bases` in `config.yaml` get RAG support automatically. No additional API configuration needed.

## What's ignored

The API accepts but ignores these OpenAI parameters (the agent's own config controls them):

- `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`
- `tools`, `tool_choice` (agents use their configured tools)
- `stop`, `frequency_penalty`, `presence_penalty`, `seed`
- `response_format`, `logprobs`, `logit_bias`
- `stream_options` (usage stats are always zeros)

Client `system` / `developer` messages are prepended to the prompt. They augment the agent's built-in instructions, not replace them.

## Authentication

| `OPENAI_COMPAT_API_KEYS` | `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | Behavior |
|---|---|---|
| Set | (any) | Bearer token required, must match one of the comma-separated keys |
| Unset | `true` | No authentication required |
| Unset | Unset/`false` | All requests return 401 (locked) |

The OpenAI-compatible API uses its own auth, separate from the dashboard's Supabase JWT auth.

## Limitations

- **Token usage is always zeros** — Agno doesn't expose token counts
- **No native `tool_calls` format** — tool results appear inline in content text
- **No room memory** — only agent-scoped memory (no `room_id` in API requests)
- **Scheduler tool unavailable** — scheduling requires Matrix context and is stripped from API agents
