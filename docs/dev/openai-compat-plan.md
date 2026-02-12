# Master Plan: OpenAI-Compatible API for MindRoom Agents

> Synthesized from three independent plans, refined by two rounds of multi-agent review.

## Goal

Expose MindRoom agents as an OpenAI-compatible chat completions API so any chat frontend (LibreChat, Open WebUI, LobeChat, etc.) can use MindRoom agents as selectable "models" — with full tool, memory, and session support in Phase 1, and knowledge base support in a follow-up phase. No Matrix dependency.

## Motivation

MindRoom is a general-purpose agent backend, but today it's locked behind Matrix as the only way to talk to agents. This limits adoption in two ways:

- **MindRoom becomes a universal agent backend.** An OpenAI-compatible API is the de facto interop standard. One implementation unlocks dozens of chat frontends — LibreChat, Open WebUI, LobeChat, ChatBox, BoltAI, and anything else that can talk to an OpenAI endpoint. MindRoom stops being "the Matrix agent thing" and becomes "the agent backend that works everywhere."
- **People already running chat UIs can plug in MindRoom without changing their workflow.** Someone using LibreChat today just adds a custom endpoint and sees MindRoom agents in their model dropdown, right next to Claude and GPT. No new UI to learn, no Matrix account to create, no bridges to configure. They pick an agent instead of picking a model, and tools/memory work transparently in Phase 1, with knowledge bases added in a later phase.

## What the User Sees

A user opens LibreChat (or Open WebUI, LobeChat, etc.) and sees a model picker. Instead of just "GPT-4" and "Claude", they also see:

- **CodeAgent** — "Generate code, manage files, execute shell commands"
- **ResearchAgent** — "Research topics, summarize findings"
- **GeneralAgent** — "General-purpose assistant"

Each one is a fully-featured MindRoom agent with its own tools, personality, memory, and instructions. The user picks "CodeAgent", types a question, and gets a response from an agent that has file tools, shell access, and coding-specific instructions — not just a raw LLM.

When the admin adds a new agent to `config.yaml`, it automatically appears in the frontend's model list the next time the frontend refreshes (typically on page load). No frontend reconfiguration needed.

### Auto-discovery flow

1. **One-time setup**: User adds MindRoom as a custom OpenAI-compatible endpoint in their chat frontend (URL + API key). Done once.
2. **Model fetch**: The frontend calls `GET /v1/models`. MindRoom reads `config.yaml` and returns every configured agent as a model object with its display name and description.
3. **Agent selection**: The frontend populates its model picker. The user sees agent names and descriptions alongside any other configured models.
4. **Chat**: The user picks an agent and chats. The frontend sends `POST /v1/chat/completions` with `model: "code"`. MindRoom routes to that specific agent with all its tools, instructions, and memory.
5. **Dynamic updates**: When an admin adds, removes, or renames an agent in `config.yaml`, the change appears automatically — `load_runtime_config()` re-reads the YAML file on each call, so hot-reloaded agents are immediately discoverable.

The frontend does not know or care that "CodeAgent" is an agent with tools and memory rather than a raw model. It is transparent.

---

## Design Principles

- **Transport adapter only**: no new agent system; call existing `ai_response()` / `stream_agent_response()`.
- **Narrow and explicit**: avoid generic abstractions. Two endpoints, one file.
- **Deterministic by default**: explicit agent selection; auto-routing is opt-in.
- **Fail with OpenAI-compatible errors**: clients must parse our errors the same way they parse OpenAI's.
- **Tolerant input, strict output**: accept and ignore unknown request fields (`extra="ignore"`) so any OpenAI client works without 422 errors, but produce spec-compliant responses.
- **No config.yaml changes**: agents are already defined; the bridge just reads them.
- **No new dependencies**: FastAPI, Pydantic, and SSE streaming are already available.

## Architecture

```
Chat Frontend (LibreChat / Open WebUI / etc.)
┌────────────────┐                     MindRoom FastAPI
│                │  GET /v1/models      ┌──────────────────────────────┐
│  Model Picker  │ ───────────────────> │  openai_compat.py            │
│                │ <─────────────────── │  → list config.agents        │
│                │                      │    (with name + description) │
│  Chat UI       │  POST /v1/chat/      │                              │
│                │  completions         │  → parse OpenAI messages     │
│                │ ───────────────────> │  → derive session_id         │
│                │ <─────────────────── │  → call ai_response() or    │
│                │  (JSON or SSE)       │    stream_agent_response()   │
└────────────────┘                      │  → format to OpenAI response │
                                        └──────────────────────────────┘
                                              │
                                              │ calls (Matrix-free)
                                              ▼
                                        ┌──────────────┐
                                        │  ai.py       │
                                        │  agents.py   │
                                        │  memory/     │
                                        │  knowledge.py│
                                        └──────────────┘
```

No Matrix dependency. MindRoom can run purely as an OpenAI-compatible agent server.

## Concept Mapping

| OpenAI API | MindRoom |
|---|---|
| Model name | Agent name (`code`, `research`) in Phase 1; `auto` and `team/<name>` in later phases |
| `messages` array | `thread_history` + last user message as `prompt` |
| `stream: true` | `stream_agent_response()` → SSE |
| `stream: false` | `ai_response()` → JSON |
| `user` field | `user_id` parameter for Agno learning |
| `GET /v1/models` | Enumerate `config.agents` (Phase 1), add `auto` (Phase 2), teams (Phase 3). Router agent is excluded (internal construct). |

## Implementation Phases

### Phase 1: Core endpoints (agents, streaming, auth)

**New file:** `src/mindroom/api/openai_compat.py`

#### Pydantic models

```python
from pydantic import BaseModel, ConfigDict, Field

class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict] | None = None   # list form for multimodal, None for tool-call-only assistant msgs

class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # silently drop unknown fields from any client

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    user: str | None = None
    # Accepted but ignored — agent's model config controls these:
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    response_format: dict | None = None
    tools: list | None = None          # ignored — agent uses its own configured tools
    tool_choice: str | dict | None = None  # ignored
    stream_options: dict | None = None     # ignored (include_usage is a no-op since usage is zeros)
    logprobs: bool | None = None
    logit_bias: dict | None = None

# --- Non-streaming response models ---

class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)
    system_fingerprint: str | None = None

# --- Streaming response models ---

class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: dict                       # {"role": "assistant"} or {"content": "..."} or {}
    finish_reason: str | None = None  # null on all chunks except the final one

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    system_fingerprint: str | None = None

# --- Model listing ---

class ModelObject(BaseModel):
    id: str                               # internal agent name: "code" (stable, URL-safe)
    object: str = "model"
    created: int = 0
    owned_by: str = "mindroom"
    # Extra fields — ignored by strict clients, used by Open WebUI (reads `name`) and others:
    name: str | None = None               # display name from config: "CodeAgent"
    description: str | None = None        # agent role from config: "Generate code, manage files..."

class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelObject]

# --- Error response ---

class OpenAIError(BaseModel):
    message: str
    type: str
    param: str | None = None
    code: str | None = None

class OpenAIErrorResponse(BaseModel):
    error: OpenAIError
```

**Model ID strategy:** Model IDs use the internal agent name (`code`, not `CodeAgent`) for stability. Renaming an agent's `display_name` in config does not break existing client configurations or session history. Display names are conveyed via the `name` field in the model response for frontends that support it (Open WebUI reads `name`; LibreChat uses the `id`).

#### `GET /v1/models`

- Call `config, _ = load_runtime_config()` (returns a tuple)
- Return each agent with rich metadata:
  ```python
  ModelObject(
      id=agent_name,                       # "code"
      name=agent_config.display_name,      # "CodeAgent"
      description=agent_config.role,       # "Generate code, manage files..."
      created=int(config_file_mtime),
  )
  ```
- Do not return the router agent (internal construct, not user-selectable)
- Do not return teams in Phase 1; Phase 2 adds `auto`; Phase 3 adds `team/<name>`
- If no agents are configured, return an empty list

#### `POST /v1/chat/completions`

**Message conversion:**
1. Extract the last `user` message as `prompt`
2. Convert all prior `user`/`assistant` messages to `thread_history`: `[{"sender": role, "body": content}]`
3. If a `system` or `developer` message exists, prepend it to the prompt. Note: this is additive — the agent already has its own system instructions and personality. Client system messages augment, not replace, the agent's built-in identity.
4. Skip `tool` role messages (these are from previous tool-call conversations the client replays; the agent handles its own tool calls)
5. When `content` is a list (multimodal), extract and concatenate the `text` parts: `" ".join(p["text"] for p in content if p.get("type") == "text")`
6. Pass `request.user` (if present) as `user_id` to `ai_response()` / `stream_agent_response()` — this drives Agno's LearningMachine

**Session ID derivation (priority cascade):**
1. `X-Session-Id` header (explicit control)
2. `X-LibreChat-Conversation-Id` header + model (LibreChat-specific)
3. Hash of `(model, user_id or "anonymous", first_user_message_content)` (deterministic fallback)

Both LibreChat and Open WebUI send the full message history with every request (standard OpenAI protocol). The session ID is only needed for Agno's internal tracking (memory, learning), not for message replay.

**Non-streaming:** call `ai_response()`, return `ChatCompletionResponse`.

**Streaming (SSE):**

```python
from agno.run.agent import RunContentEvent, ToolCallStartedEvent, ToolCallCompletedEvent

async def _stream_response(agent_name, prompt, session_id, config, ...):
    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    created = int(time.time())

    async def event_generator():
        # 1. Initial role announcement chunk
        yield f"data: {json.dumps(chunk_payload(completion_id, created, agent_name, delta={'role': 'assistant'}))}\n\n"

        # 2. Content chunks
        async for event in stream_agent_response(agent_name, prompt, session_id, ...):
            if isinstance(event, RunContentEvent) and event.content:
                yield f"data: {json.dumps(chunk_payload(completion_id, created, agent_name, delta={'content': str(event.content)}))}\n\n"
            elif isinstance(event, (ToolCallStartedEvent, ToolCallCompletedEvent)):
                # Format tool events as content text using the same pattern as
                # stream_agent_response() in ai.py (format_tool_started_event / complete_pending_tool_block)
                text = _format_tool_event(event)
                if text:
                    yield f"data: {json.dumps(chunk_payload(completion_id, created, agent_name, delta={'content': text}))}\n\n"
            elif isinstance(event, str):
                # Error message string or cached full response
                yield f"data: {json.dumps(chunk_payload(completion_id, created, agent_name, delta={'content': event}))}\n\n"

        # 3. Final chunk with finish_reason
        yield f"data: {json.dumps(chunk_payload(completion_id, created, agent_name, delta={}, finish_reason='stop'))}\n\n"

        # 4. Stream terminator
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Team models** (`team/...`): not listed in `/v1/models` in Phase 1. If requested explicitly, return `501` with message "Team support via OpenAI API is not yet available" until Phase 3.

#### Error handling: `ai_response()` never raises

`ai_response()` catches all exceptions internally and returns a user-friendly error *string* instead of raising. This means the OpenAI compat layer will get a 200-level response containing error text as if it were a normal agent reply. The layer must detect these error strings and convert them to proper HTTP 500 responses with OpenAI error format. Same applies to `stream_agent_response()`, which yields error strings.

Detection heuristic: error strings from `get_user_friendly_error_message()` in `error_handling.py` contain emoji prefixes and recognizable patterns. The simplest approach is to check if the response starts with known error emoji characters or the `[agent_name]` bracket pattern.

#### Scheduler tool: Matrix dependency in all agents

`create_agent()` appends the `scheduler` tool to ALL agents by default (`DEFAULT_AGENT_TOOL_NAMES = ["scheduler"]`). The scheduler uses a `ContextVar` (`SchedulingToolContext`) — when no context is set, `get_scheduling_tool_context()` returns `None`, and the tool fails at call time when it tries to use the `None` context. The compat layer should strip `scheduler` from the tool list when creating agents for API use, or accept that scheduler tool calls will fail with a runtime error (acceptable for Phase 1, since most chat interactions don't trigger scheduling).

#### Room memory: not available without room_id

`ai_response()` calls `build_memory_enhanced_prompt()` with `room_id`. When `room_id=None` (as in API calls), room-scoped memories are not retrieved. Agent-scoped memory still works. This is an acceptable limitation — document it, or in the future allow an `X-Room-Id` header.

#### Storage path

Import and use `STORAGE_PATH_OBJ` from `mindroom.constants` as the `storage_path` parameter. This is the same default `create_agent()` uses when `storage_path=None`, meaning API and Matrix agents share the same session databases and memory stores. This is intentional — an agent remembers things regardless of which transport was used.

#### Authentication

- Env var: `OPENAI_COMPAT_API_KEYS` (comma-separated list of valid keys)
- Check `Authorization: Bearer <key>` on all `/v1/*` endpoints
- If env var is unset/empty: allow unauthenticated access (standalone/dev mode)
- Return `401` with OpenAI-style error on invalid key:
  ```json
  {"error": {"message": "Invalid API key", "type": "invalid_request_error", "param": null, "code": "invalid_api_key"}}
  ```

#### Error responses

All errors use OpenAI-style format:
- `400`: invalid payload (missing messages, empty messages)
- `401`: invalid or missing API key
- `404`: unknown model/agent: `{"error": {"message": "Model 'foo' not found", "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}`
- `500`: runtime failure (agent error, model error)
- `501`: team models (until Phase 3)

#### Mount in `api/main.py`

```python
from mindroom.api.openai_compat import router as openai_compat_router
app.include_router(openai_compat_router)  # Uses its own bearer auth, not verify_user
```

CORS is already handled by the existing `CORSMiddleware` in `main.py` — no additional configuration needed for the `/v1/` prefix.

Health check is already available at `GET /api/health`. No separate `/v1/health` endpoint needed.

#### Logging

Use the existing `get_logger(__name__)` from `mindroom.logging_config`. Log at minimum:
- Each incoming request with model name and streaming mode
- Session ID derivation result
- Agent response completion or error

#### Knowledge base integration

For Phase 1, pass `knowledge=None`. The agent still works — it just won't have RAG. Knowledge integration is a fast follow (look at how `bot.py` initializes `KnowledgeManager` and its `MultiKnowledgeVectorDb` for agents with multiple knowledge bases, and pass the `Knowledge` object through).

### Phase 2: Auto-routing via `auto` model

**Modify:** `src/mindroom/routing.py`

Extract the core routing logic into a MatrixID-free function:

```python
async def suggest_agent(
    message: str,
    available_agent_names: list[str],
    config: Config,
    thread_context: list[dict[str, Any]] | None = None,
) -> str | None:
```

The existing `suggest_agent_for_message()` does two MatrixID-specific things: (1) converts `MatrixID` list to agent names, and (2) uses `MatrixID.parse()` to format sender names in thread context. The new function takes plain strings for both, and the old function calls into it after doing the MatrixID conversions.

In `openai_compat.py`:
- Add `auto` to the `/v1/models` response
- When `model="auto"`, call `suggest_agent()` with all configured agent names
- If routing fails, fall back to the first agent in config (or return an error)
- Include the resolved agent name in the response's `model` field so the client knows who responded

### Phase 3: Team support (post-MVP)

- For `team/<name>` models, create agents on the fly via `create_agent()` (bypassing `MultiAgentOrchestrator`)
- Build a minimal `agno.Team` instance directly, replicating the team construction logic from `teams.py` (`_create_team_instance`)
- This avoids Matrix dependency entirely
- Decide team mode from config (`coordinate`/`collaborate`) or default to `coordinate`
- Note: `bot.py` defines `MultiKnowledgeVectorDb` for merging multiple knowledge bases — this logic must also be extracted if teams need knowledge support

### Phase 4: Knowledge base integration

- Look at how `bot.py` initializes `KnowledgeManager` for agents with `knowledge_bases`
- Pass the `Knowledge` object to `ai_response()` / `stream_agent_response()`
- Cache initialized knowledge managers (they load vector stores, which is expensive)

### Future considerations

- Agent avatars/icons for chat frontends that support per-model icons
- Richer capability metadata (list of tools per agent) for frontends that show it
- Native OpenAI `tool_calls` format (streaming tool call deltas) instead of inline text

## Files to create/modify

| File | Action | Phase |
|---|---|---|
| `src/mindroom/api/openai_compat.py` | **Create** | 1 |
| `src/mindroom/api/main.py` | **Edit** (2 lines: import + mount) | 1 |
| `tests/test_openai_compat.py` | **Create** | 1 |
| `src/mindroom/routing.py` | **Edit** (extract MatrixID-free function) | 2 |

## Testing Plan

### Unit / API tests (`tests/test_openai_compat.py`)

- **Models endpoint**: lists agents from config with `name` and `description`; no teams; router excluded; empty agents returns empty list
- **Chat completion (non-streaming)**: correct OpenAI response shape, correct agent invocation, message conversion (messages → thread_history + prompt)
- **Chat completion (streaming)**: SSE format with initial role chunk, content chunks, `finish_reason: "stop"` final chunk, `data: [DONE]` terminator
- **Auth**: valid key accepted, missing key returns 401, wrong key returns 401, no key required when env var unset
- **Errors**: unknown model returns 404 with `param: "model"`, empty messages returns 400, explicit `team/...` model returns 501
- **Session ID**: same conversation header → same session_id, different conversations → different session_ids
- **Message parsing**: `content` as string, as list of content parts, as `None` — all handled correctly; `developer` role treated as `system`; `tool` role messages skipped
- **Extra fields**: request with unknown fields (e.g., `logit_bias`, `seed`) does not cause 422
- **Error string detection**: `ai_response()` returning error string → HTTP 500 response
- **User ID passthrough**: `request.user` forwarded as `user_id` to agent functions

Mock strategy: monkeypatch `ai_response` and `stream_agent_response` for deterministic outputs. Use a temporary config fixture with 2-3 agents.

### Integration smoke test

```bash
# Start backend
just start-backend-dev

# List models (should show agent names and descriptions)
curl -s -H "Authorization: Bearer $KEY" http://localhost:8765/v1/models | python -m json.tool

# Non-streaming completion
curl -s -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"general","messages":[{"role":"user","content":"Hello"}]}' \
  http://localhost:8765/v1/chat/completions | python -m json.tool

# Streaming completion
curl -N -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"general","messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  http://localhost:8765/v1/chat/completions
```

## Client Configuration

### LibreChat

```yaml
endpoints:
  custom:
    - name: "MindRoom"
      apiKey: "${OPENAI_COMPAT_API_KEY}"
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
        X-LibreChat-User-Id: "{{LIBRECHAT_USER_ID}}"
```

### Open WebUI

1. Go to **Admin Settings → Connections → OpenAI → Manage**
2. Set API URL to: `http://localhost:8765/v1`
3. Set API Key to one of your `OPENAI_COMPAT_API_KEYS`
4. Models auto-discover via `/v1/models` — Open WebUI will display the `name` field (agent display name) instead of the raw ID

## Acceptance Criteria

- [ ] `GET /v1/models` lists all configured agents with `name` and `description` (teams added in Phase 3)
- [ ] `POST /v1/chat/completions` returns valid OpenAI-compatible JSON for non-streaming
- [ ] `POST /v1/chat/completions` with `stream: true` returns valid SSE stream with role chunk, content chunks, finish chunk, and `[DONE]`
- [ ] Selecting agent X always routes to agent X (deterministic)
- [ ] Conversation continuity works across multiple turns (same session ID)
- [ ] Auth rejects invalid/missing keys when `OPENAI_COMPAT_API_KEYS` is set
- [ ] Auth allows unauthenticated access when env var is unset (standalone mode)
- [ ] Unknown model returns 404 with OpenAI error format
- [ ] Request with extra/unknown fields does not return 422
- [ ] Adding a new agent to config.yaml makes it appear in `/v1/models` without restart
- [ ] All tests pass with `pytest`

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| OpenAI schema mismatch causes client parsing issues | Strict response-shape tests; separate chunk model for streaming; `ConfigDict(extra="ignore")` on requests; manual LibreChat + Open WebUI smoke test |
| Session key instability breaks memory continuity | Deterministic session formula with explicit tests; priority cascade with `X-Session-Id` as most reliable |
| Team execution path depends on `MultiAgentOrchestrator` | Keep teams out of Phase 1; build direct `create_agent()` + `Team()` path in Phase 3 |
| `ai_response()` swallows exceptions as strings | Detect error strings via emoji/bracket prefix heuristic; convert to HTTP 500 |
| Scheduler tool fails without Matrix context | `ContextVar` returns `None`; strip scheduler from API agents or accept runtime error |

## Known Limitations (Phase 1)

- **No RAG/knowledge bases** — agents work but without knowledge base retrieval (Phase 4)
- **No team support** — teams return 501 if requested (Phase 3)
- **No auto-routing** — must specify agent name explicitly (Phase 2)
- **No room memory** — only agent-scoped memory, not room-scoped (no `room_id`)
- **No native tool_calls format** — tool results appear inline in content text
- **Client-provided `tools`/`tool_choice` are ignored** — the agent uses its own configured tools
- **Client-provided `system` messages augment, not replace** the agent's built-in instructions
- **Token usage is zeros** — Agno doesn't expose token counts through `ai_response()`
- **Scheduler tool may fail** — `SchedulingToolContext` is `None` without Matrix
- **Shared state** — API and Matrix agents share memory/session stores (intentional)
