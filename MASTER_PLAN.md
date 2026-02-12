# Master Plan: OpenAI-Compatible API for MindRoom Agents

> Synthesized from three independent plans: `feature-librechat-agent-bridge`, `openai-api`, and `main`.

## Goal

Expose MindRoom agents as an OpenAI-compatible chat completions API so any chat frontend (LibreChat, Open WebUI, LobeChat, etc.) can use MindRoom agents as selectable "models" — with full tool, knowledge base, memory, and session support. No Matrix dependency.

## Motivation

MindRoom is a general-purpose agent backend, but today it's locked behind Matrix as the only way to talk to agents. This limits adoption in two ways:

- **MindRoom becomes a universal agent backend.** An OpenAI-compatible API is the de facto interop standard. One implementation unlocks dozens of chat frontends — LibreChat, Open WebUI, LobeChat, ChatBox, BoltAI, and anything else that can talk to an OpenAI endpoint. MindRoom stops being "the Matrix agent thing" and becomes "the agent backend that works everywhere."
- **People already running chat UIs can plug in MindRoom without changing their workflow.** Someone using LibreChat today just adds a custom endpoint and sees MindRoom agents in their model dropdown, right next to Claude and GPT. No new UI to learn, no Matrix account to create, no bridges to configure. They pick an agent instead of picking a model, and everything — tools, memory, knowledge bases — works transparently behind the same chat interface they already use.

## Comparison of Source Plans

| Aspect | Plan A (`feature-librechat-agent-bridge`) | Plan B (`openai-api`) | Plan C (`main`) |
|---|---|---|---|
| **Architecture diagram** | ASCII flow diagram | None | None |
| **Pydantic models** | Full code definitions | None | None |
| **Streaming** | In MVP, with code sketch | Deferred to Phase 4 | In MVP, format-only |
| **Error format** | Not specified | OpenAI-style errors with HTTP status codes | Not specified |
| **Session strategy** | Hash of (agent, user, first_msg) + `X-Session-Id` | `X-LibreChat-Conversation-Id` + user + fallback | `X-Session-ID` or per-request UUID |
| **Auth** | Single `MINDROOM_API_KEY` env var | `OPENAI_COMPAT_API_KEYS` (comma-separated) | Single `MINDROOM_API_KEY` env var |
| **Auto-routing** | Not included | Not included | `auto` model + routing.py refactor |
| **Knowledge bases** | Explicit step, acknowledges skip-first approach | Not discussed | Not discussed |
| **Team naming** | `team/<name>` prefix | `team:<name>` prefix | `team/super_team` |
| **Testing** | Bullet list | Structured test plan + smoke tests | Bullet list |
| **Acceptance criteria** | None | Yes | None |
| **Risks** | None | Yes, with mitigations | None |
| **Phasing** | 6 steps (sequential) | 5 phases (incremental) | 6 steps (sequential) |
| **Compatibility hardening** | `dropParams` in LibreChat config | Tolerant parsing of temperature, max_tokens, stop, n | Accept but ignore |
| **LibreChat config** | Includes `dropParams`, `modelDisplayLabel` | Includes custom headers via placeholders | Basic config |
| **Routing.py refactor** | Not needed (no auto-routing) | Not needed (no auto-routing) | Extract MatrixID-free function |
| **Design principles** | Implicit | Explicit section | Architecture assessment |

### What each plan does best

- **Plan A** has the most implementation-ready detail: Pydantic models, streaming code sketch, knowledge base consideration, and the practical LibreChat `dropParams` config.
- **Plan B** has the strongest engineering rigor: error format spec, phased delivery, acceptance criteria, risks, testing structure, and multi-key auth.
- **Plan C** is the only one with auto-routing via a special `auto` model and the routing.py refactor to decouple from MatrixID.

### Key decisions for the master plan

1. **Streaming in MVP** — Plan B defers it, but streaming is straightforward (`stream_agent_response()` already exists) and most chat UIs expect it. Include it.
2. **Auto-routing as Phase 2** — Good idea from Plan C, but requires a routing.py refactor, so it's not Phase 1.
3. **Multi-key auth** — Plan B's `OPENAI_COMPAT_API_KEYS` (comma-separated) is more flexible than a single key. Adopt it.
4. **Session ID cascade** — Combine all three approaches into a priority chain: `X-Session-Id` header > `X-LibreChat-Conversation-Id` header > hash fallback.
5. **Error format** — Plan B's OpenAI-style error objects are necessary for client compatibility.
6. **Team prefix** — Use `team/` (slash, not colon) for consistency with URL-like model namespacing.

---

## Design Principles

- **Transport adapter only**: no new agent system; call existing `ai_response()` / `stream_agent_response()`.
- **Narrow and explicit**: avoid generic abstractions. Two endpoints, one file.
- **Deterministic by default**: explicit agent selection; auto-routing is opt-in.
- **Fail with OpenAI-compatible errors**: clients must parse our errors the same way they parse OpenAI's.
- **No config.yaml changes**: agents are already defined; the bridge just reads them.
- **No new dependencies**: FastAPI, Pydantic, and SSE streaming are already available.

## Architecture

```
Chat Frontend (LibreChat / Open WebUI / etc.)
┌────────────────┐                     MindRoom FastAPI
│                │  GET /v1/models      ┌──────────────────────────────┐
│  Model Picker  │ ───────────────────> │  openai_compat.py            │
│                │ <─────────────────── │  → list config.agents/teams  │
│                │                      │                              │
│  Chat UI       │  POST /v1/chat/      │  → parse OpenAI messages     │
│                │  completions         │  → derive session_id         │
│                │ ───────────────────> │  → call ai_response() or     │
│                │ <─────────────────── │    stream_agent_response()   │
│                │  (JSON or SSE)       │  → format to OpenAI response │
└────────────────┘                      └──────────────────────────────┘
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
| Model name | Agent name (`code`, `research`) or `team/<name>` or `auto` |
| `messages` array | `thread_history` + last user message as `prompt` |
| `stream: true` | `stream_agent_response()` → SSE |
| `stream: false` | `ai_response()` → JSON |
| `user` field | `user_id` parameter for Agno learning |
| `GET /v1/models` | Enumerate `config.agents` + `config.teams` |

## Implementation Phases

### Phase 1: Core endpoints (agents, streaming, auth)

**New file:** `src/mindroom/api/openai_compat.py`

#### Pydantic models

```python
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    user: str | None = None
    temperature: float | None = None     # Accepted, ignored
    max_tokens: int | None = None        # Accepted, ignored
    stop: str | list[str] | None = None  # Accepted, ignored
    n: int | None = None                 # Accepted, ignored

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

class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "mindroom"

class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelObject]

class OpenAIError(BaseModel):
    message: str
    type: str
    code: str | None = None

class OpenAIErrorResponse(BaseModel):
    error: OpenAIError
```

#### `GET /v1/models`

- Load config via `load_runtime_config()`
- Return each agent as `{"id": "code", "object": "model", "created": 0, "owned_by": "mindroom"}`
- Return each team as `{"id": "team/super_team", ...}`
- (Phase 2 adds `auto`)

#### `POST /v1/chat/completions`

Message conversion:
1. Extract the last `user` message as `prompt`
2. Convert all prior `user`/`assistant` messages to `thread_history`: `[{"sender": role, "body": content}]`
3. If a `system` message exists, prepend it to the prompt (the agent already has its own system instructions, so this is additive context)

Session ID derivation (priority cascade):
1. `X-Session-Id` header (explicit control)
2. `X-LibreChat-Conversation-Id` header + model (LibreChat-specific)
3. Hash of `(model, user_id or "anonymous", first_user_message_content)` (deterministic fallback)

Non-streaming: call `ai_response()`, return `ChatCompletionResponse`.

Streaming (SSE): iterate over `stream_agent_response()`, emit `data: {chunk}\n\n` for each `RunContentEvent`, skip `ToolCallStartedEvent`/`ToolCallCompletedEvent` (tool output appears inline in content). Emit `finish_reason: "stop"` then `data: [DONE]\n\n`.

```python
async def _stream_response(agent_name, prompt, session_id, config, ...):
    async def event_generator():
        async for chunk in stream_agent_response(agent_name, prompt, session_id, ...):
            if isinstance(chunk, RunContentEvent) and chunk.content:
                yield f"data: {json.dumps(delta_payload(chunk.content))}\n\n"
            elif isinstance(chunk, str):
                yield f"data: {json.dumps(delta_payload(chunk))}\n\n"
            # Skip tool events — tool results appear inline
        yield f"data: {json.dumps(finish_payload())}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

Team models (`team/...`): listed in `/v1/models` but return a `501` error with message "Team support via OpenAI API is not yet available" until Phase 3.

#### Authentication

- Env var: `OPENAI_COMPAT_API_KEYS` (comma-separated list of valid keys)
- Check `Authorization: Bearer <key>` on all `/v1/*` endpoints
- If env var is unset/empty: allow unauthenticated access (standalone/dev mode)
- Return `401` with OpenAI-style error on invalid key:
  ```json
  {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}
  ```

#### Error responses

All errors use OpenAI-style format:
- `400`: invalid payload (missing messages, empty messages)
- `401`: invalid or missing API key
- `404`: unknown model/agent: `{"error": {"message": "Model 'foo' not found", "type": "invalid_request_error", "code": "model_not_found"}}`
- `500`: runtime failure (agent error, model error)
- `501`: team models (until Phase 3)

#### Mount in `api/main.py`

```python
from mindroom.api.openai_compat import router as openai_compat_router
app.include_router(openai_compat_router)  # Uses its own bearer auth, not verify_user
```

#### Knowledge base integration

For Phase 1, pass `knowledge=None`. The agent still works — it just won't have RAG. This matches Plan A's pragmatic "skip-first" approach. Knowledge integration is a fast follow (look at how `bot.py` initializes knowledge managers via `knowledge.py` and pass the `Knowledge` object through).

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

Refactor the existing `suggest_agent_for_message()` to call this internally (so Matrix code doesn't break).

In `openai_compat.py`:
- Add `auto` to the `/v1/models` response
- When `model="auto"`, call `suggest_agent()` with all configured agent names
- If routing fails, fall back to the first agent in config (or return an error)
- Include the resolved agent name in the response's `model` field so the client knows who responded

### Phase 3: Team support (post-MVP)

- For `team/<name>` models, create agents on the fly via `create_agent()` (bypassing `MultiAgentOrchestrator`)
- Build a minimal `agno.Team` instance directly
- This avoids Matrix dependency entirely
- Decide team mode from config (`coordinate`/`collaborate`) or default to `coordinate`

### Phase 4: Knowledge base integration

- Look at how `bot.py` initializes `KnowledgeManager` for agents with `knowledge_bases`
- Pass the `Knowledge` object to `ai_response()` / `stream_agent_response()`
- May need to keep a small cache of initialized knowledge managers (they load vector stores)

## Files to create/modify

| File | Action | Phase |
|---|---|---|
| `src/mindroom/api/openai_compat.py` | **Create** | 1 |
| `src/mindroom/api/main.py` | **Edit** (2 lines: import + mount) | 1 |
| `tests/test_openai_compat.py` | **Create** | 1 |
| `src/mindroom/routing.py` | **Edit** (extract MatrixID-free function) | 2 |

## Testing Plan

### Unit / API tests (`tests/test_openai_compat.py`)

- **Models endpoint**: lists agents from config, lists teams with `team/` prefix
- **Chat completion (non-streaming)**: correct OpenAI response shape, correct agent invocation, message conversion (messages → thread_history + prompt)
- **Chat completion (streaming)**: SSE format, `data: [DONE]` terminator, content chunks
- **Auth**: valid key accepted, missing key returns 401, wrong key returns 401, no key required when env var unset
- **Errors**: unknown model returns 404, empty messages returns 400, team model returns 501
- **Session ID**: same conversation header → same session_id, different conversations → different session_ids

Mock strategy: monkeypatch `ai_response` and `stream_agent_response` for deterministic outputs. Use a temporary config fixture with 2-3 agents.

### Integration smoke test

```bash
# Start backend
just start-backend-dev

# List models
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

## LibreChat Configuration

```yaml
endpoints:
  custom:
    - name: "MindRoom"
      apiKey: "${MINDROOM_API_KEY}"
      baseURL: "http://localhost:8765/v1"
      models:
        fetch: true
      modelDisplayLabel: "MindRoom"
      dropParams: ["stop", "frequency_penalty", "presence_penalty", "top_p"]
      headers:
        X-LibreChat-Conversation-Id: "{{conversationId}}"
```

Agents appear in LibreChat's model dropdown. Selecting one routes messages to that MindRoom agent with full tool/knowledge/memory capabilities.

## Acceptance Criteria

- [ ] `GET /v1/models` lists all configured agents (and teams with `team/` prefix)
- [ ] `POST /v1/chat/completions` returns valid OpenAI-compatible JSON for non-streaming
- [ ] `POST /v1/chat/completions` with `stream: true` returns valid SSE stream
- [ ] Selecting agent X always routes to agent X (deterministic)
- [ ] Conversation continuity works across multiple turns (same session ID)
- [ ] Auth rejects invalid/missing keys when `OPENAI_COMPAT_API_KEYS` is set
- [ ] Auth allows unauthenticated access when env var is unset (standalone mode)
- [ ] Unknown model returns 404 with OpenAI error format
- [ ] All tests pass with `pytest`

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| OpenAI schema mismatch causes LibreChat parsing issues | Strict response-shape tests; manual LibreChat smoke test; use `dropParams` for unsupported fields |
| Session key instability breaks memory continuity | Deterministic session formula with explicit tests; priority cascade with `X-Session-Id` as most reliable |
| Team execution path depends on `MultiAgentOrchestrator` | Keep teams out of Phase 1; build direct `create_agent()` + `Team()` path in Phase 3 |
| Agno sees duplicate messages (session DB + thread_history) | Already a tested pattern from the Matrix bridge — not a new risk |
| Knowledge base initialization is expensive | Defer to Phase 4; cache initialized knowledge managers |
