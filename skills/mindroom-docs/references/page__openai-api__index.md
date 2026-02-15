# OpenAI-Compatible API

MindRoom exposes an OpenAI-compatible chat completions API so any chat frontend can use MindRoom agents as selectable "models". LibreChat, Open WebUI, LobeChat, ChatBox, BoltAI, and anything else that speaks the OpenAI protocol works out of the box.

## How It Works

The frontend calls `GET /v1/models` and sees your agents in the model picker. The user picks an agent and chats. The frontend sends standard OpenAI requests; MindRoom routes them to the selected agent with all its tools, instructions, and memory. The frontend doesn't know it's talking to an agent — it's transparent.

```
Chat Frontend (LibreChat, Open WebUI, etc.)
│
│  GET  /v1/models           → returns your agents as "models"
│  POST /v1/chat/completions → routes to the selected agent
│
└──→ MindRoom API ──→ ai_response() / stream_agent_response()
                         │
                         └──→ agents, tools, memory, knowledge bases
```

No Matrix auth dependency. You can run the OpenAI-compatible API standalone or alongside the Matrix bot.

## Setup

### 1. Set API keys

Add to your `.env`:

```
# Option A: Set API keys (recommended for production)
OPENAI_COMPAT_API_KEYS=sk-my-secret-key-1,sk-my-secret-key-2

# Option B: Allow unauthenticated access (local dev only)
OPENAI_COMPAT_ALLOW_UNAUTHENTICATED=true
```

Without either of these, the API returns 401 on all requests.

### 2. Start the backend

```
# Full backend (Matrix bot + API server)
uv run mindroom run

# Or via just
just start-backend-dev
```

The API is available at `http://localhost:8765/v1/`.

> [!IMPORTANT] If frontend and backend are served on the same domain behind a reverse proxy, route `/v1/*` to the backend (in addition to `/api/*`). Otherwise OpenAI-compatible requests can be handled by the frontend and fail.

### 3. Verify

```
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

```
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
        # Highest-priority session key used by MindRoom
        X-Session-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
        # Backward-compatible fallback used by MindRoom
        X-LibreChat-Conversation-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
```

`X-Session-Id` is recommended when you want deterministic backend session continuity. This is especially important for tools that keep long-lived backend sessions. `X-LibreChat-Conversation-Id` alone is still enough to keep continuity if you already use it.

### Open WebUI

1. Go to **Admin Settings > Connections > OpenAI > Manage**
1. Set API URL to `http://localhost:8765/v1`
1. Set API Key to one of your `OPENAI_COMPAT_API_KEYS`
1. Agents appear automatically in the model picker

### Any OpenAI-compatible client

Point the base URL at `http://localhost:8765/v1` and set the API key. MindRoom implements the OpenAI-compatible `GET /v1/models` and `POST /v1/chat/completions` endpoints.

## Features

### Model selection

Each agent in `config.yaml` appears as a selectable model. The model ID is the agent's internal name (e.g., `code`, `research`), and the display name comes from `display_name`.

### Auto-routing

Select the `auto` model to let MindRoom's router pick the best agent for each message, the same routing logic used in Matrix rooms.

### Teams

Teams are exposed as `team/<team_name>` models. Selecting `team/super_team` runs the full team collaboration or coordination workflow.

### Streaming

`stream: true` returns Server-Sent Events in the standard OpenAI format: role chunk, content chunks, finish chunk, `[DONE]`.

Tool rendering mode is controlled by `X-Tool-Event-Format`:

- No header: tool events are emitted as inline content text
- `openai`: strict OpenAI tool-calling mode (`delta.tool_calls` / `finish_reason: "tool_calls"`)

Strict `openai` mode requirements:

- `X-Session-Id` header is required
- only single-agent models are supported (`team/*` models are rejected)
- tool execution remains server-side; client returns `role="tool"` messages with `tool_call_id` + result content to continue the run
- one active strict-mode stream is allowed per `X-Session-Id` (parallel stream requests for the same session return `409`)

### Session continuity

Session IDs are derived from request headers:

1. `X-Session-Id` header (explicit control)
1. `X-LibreChat-Conversation-Id` header (automatic with LibreChat)
1. Random UUID fallback

Agent memory and conversation history persist across requests with the same session ID. For persistent backend tool sessions (for example a long-running coding session), prefer `X-Session-Id`.

### Claude Agent tool sessions

If an agent enables the `claude_agent` tool, the same `X-Session-Id` keeps the Claude backend session alive across turns. This lets a user continue one long coding flow instead of starting a fresh Claude process on every request. See [Claude Agent Sessions](https://docs.mindroom.chat/tools/builtin/#claude-agent-sessions) for configuration details.

Parallel Claude sub-sessions are supported by using different `session_label` values in tool calls:

- Same `session_label`: one shared Claude session (serialized by a per-session lock)
- Different `session_label`: independent Claude sessions that can run concurrently

In strict `openai` mode, MindRoom keeps paused tool-call state in-memory with a short TTL. Use a stable `X-Session-Id` across the initial request and continuation requests.

### Knowledge bases

Agents with configured `knowledge_bases` in `config.yaml` get RAG support automatically. No additional API configuration needed.

### Tool execution endpoint

`POST /v1/tools/execute` runs a single tool function server-side and returns the result. This is used by the proxy (see below) but can also be called directly.

```
curl -X POST -H "Authorization: Bearer sk-my-key" \
  -H "Content-Type: application/json" \
  -d '{"agent":"general","tool_name":"web_search","arguments":{"query":"test"}}' \
  http://localhost:8765/v1/tools/execute
```

Response:

```
{"tool_call_id": "call_abc123...", "result": "3 results found..."}
```

The endpoint uses the same API key auth as `/v1/chat/completions`.

### Tool-calling proxy

Chat UIs like LibreChat and Open WebUI don't auto-execute tools from API responses. The proxy handles the tool execution loop transparently: it intercepts `tool_calls`, calls `/v1/tools/execute`, and sends continuation requests until the agent produces a final response.

```
UI (LibreChat / Open WebUI)
  ↕ standard OpenAI protocol
Proxy (localhost:8766)
  ↕ strict OpenAI mode
MindRoom (localhost:8765)
```

#### Running the proxy

```
# Via CLI
mindroom proxy --upstream http://localhost:8765 --port 8766

# Via module
python -m mindroom.proxy --upstream http://localhost:8765 --port 8766
```

Then point your UI at `http://localhost:8766/v1` instead of the MindRoom backend directly.

#### What the user sees

The proxy injects status messages as content chunks so the user sees tool activity in real-time:

```
🔧 Running web_search...
✅ web_search done

Here are the search results...
```

#### Options

| Flag         | Default                 | Description          |
| ------------ | ----------------------- | -------------------- |
| `--upstream` | `http://localhost:8765` | MindRoom backend URL |
| `--port`     | `8766`                  | Proxy listen port    |
| `--host`     | `0.0.0.0`               | Proxy bind address   |

## What's ignored

The API accepts but ignores these OpenAI parameters (the agent's own config controls them):

- `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`
- `tools`, `tool_choice` (agents use their configured tools)
- `stop`, `frequency_penalty`, `presence_penalty`, `seed`
- `response_format`, `logprobs`, `logit_bias`
- `stream_options` (usage stats are always zeros)

Client `system` / `developer` messages are prepended to the prompt. They augment the agent's built-in instructions, not replace them.

## Authentication

| `OPENAI_COMPAT_API_KEYS` | `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | Behavior                                                          |
| ------------------------ | ------------------------------------- | ----------------------------------------------------------------- |
| Set                      | (any)                                 | Bearer token required, must match one of the comma-separated keys |
| Unset                    | `true`                                | No authentication required                                        |
| Unset                    | Unset/`false`                         | All requests return 401 (locked)                                  |

The OpenAI-compatible API uses its own auth, separate from the dashboard's Supabase JWT auth.

## Limitations

- **Token usage is always zeros** — Agno doesn't expose token counts
- **Strict `openai` mode state is process-local** — paused tool-call state is in-memory (single-process or sticky routing recommended)
- **Strict `openai` mode cleanup is request-driven** — stale paused runs/locks are pruned on incoming requests (not by a background task)
- **No room memory** — only agent-scoped memory (no `room_id` in API requests)
- **Scheduler tool unavailable** — scheduling requires Matrix context and is stripped from API agents
