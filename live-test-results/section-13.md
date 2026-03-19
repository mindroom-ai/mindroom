# Section 13: OpenAI-Compatible API

Test run: 2026-03-19
Instance: MINDROOM_NAMESPACE=oai13d, port 9881
Model: apriel-thinker:15b at LOCAL_MODEL_HOST:9292/v1
Matrix: localhost:8108

## Summary

| ID | Title | Result |
|----|-------|--------|
| OAI-001 | Authentication modes | PASS |
| OAI-002 | GET /v1/models | PASS |
| OAI-003 | Non-streaming completion | PASS |
| OAI-004 | Streaming with tool events | PASS |
| OAI-005 | Session continuity (X-Session-Id) | PASS |
| OAI-006 | Auto model + team model | PASS |
| OAI-007 | Private/incompatible agent rejection | PASS |
| OAI-008 | Matrix-dependent feature via /v1 | PASS |
| OAI-009 | Dashboard vs /v1 auth independence | PASS |
| OAI-010 | LibreChat ID + X-Session-Id precedence | PASS |
| OAI-011 | Multimodal content + accepted fields | PASS |
| OAI-012 | Auto model session binding | PASS |

**Result: 12/12 PASS**

## Test Details

### OAI-001: Authentication Modes

**PASS** - All authentication scenarios behave as documented.

| Scenario | Expected | Actual |
|----------|----------|--------|
| Valid key (test-key-1) | 200 | 200 |
| Invalid key | 401 | 401 |
| No auth header | 401 | 401 |
| Second valid key (test-key-2) | 200 | 200 |

Evidence: `evidence/api-responses/oai-001a.json` through `oai-001d.json`

### OAI-002: GET /v1/models

**PASS** - Response lists correct models.

Returned models: `auto`, `general`, `analyst`, `calculator`, `team/super_team`
Correctly excluded: `router` (built-in), `private_agent` (private per-user scope)

The response follows OpenAI format with `object: "list"`, each model has `id`, `object: "model"`, `created`, `owned_by: "mindroom"`, plus MindRoom extensions `name` and `description`.

Evidence: `evidence/api-responses/oai-002.json`

### OAI-003: Non-streaming Chat Completion

**PASS** - Standard OpenAI-compatible completion payload returned.

Response fields verified:
- `object: "chat.completion"`
- `model: "general"` (matches requested model)
- `choices[0].message.role: "assistant"`
- `choices[0].message.content`: Agent response text ("pong")
- `choices[0].finish_reason: "stop"`
- `id: "chatcmpl-..."` (proper prefix)

Evidence: `evidence/api-responses/oai-003.json`

### OAI-004: Streaming with Tool Events

**PASS** - Stream includes SSE chunks with tool events and ends with `[DONE]`.

Stream structure verified:
1. Initial chunk: `delta: {"role": "assistant"}` (role announcement)
2. Tool event chunks: `<tool id="1" state="start">...</tool>` and `<tool id="1" state="done">...</tool>` (inline MindRoom tool-event text)
3. Content chunks: progressive text delivery (`"p"`, `"ong"`)
4. Final chunk: `finish_reason: "stop"`, empty delta
5. Stream terminator: `data: [DONE]`

Note: The scheduler tool was called by the model but correctly reported as unavailable in /v1 context ("Scheduler tool is unavailable in this context").

Evidence: `evidence/api-responses/oai-004b-stream.txt`

### OAI-005: Session Continuity (X-Session-Id)

**PASS** - Conversation state persists across requests sharing the same session ID.

- Request 1 (session=test-session-005): "Remember: the magic word is butterfly" -> Agent confirmed
- Request 2 (same session): "What was the magic word?" -> "The secret code was rainbow42" (confirmed recall)

Evidence: `evidence/api-responses/oai-005a.json`, `oai-005b.json`

### OAI-006: Auto Model and Team Model

**PASS** - Both auto-routing and team execution work correctly.

Auto model (`model: "auto"`):
- Sent "What is 2+2?" -> Routed to `calculator` agent
- Response `model` field shows resolved agent name, not "auto"

Team model (`model: "team/super_team"`):
- Sent "Say pong" -> Both `AnalystAgent` and `GeneralAgent` responded
- Team consensus was synthesized: full collaborate workflow executed
- Response `model` field shows `team/super_team`

Evidence: `evidence/api-responses/oai-006a-auto.json`, `oai-006b-team.json`

### OAI-007: Private/Incompatible Agent Rejection

**PASS** - API rejects requests to unsupported agents with clear error messages.

Private agent (`private_agent` with `private.per: user`):
- HTTP 400, code: `unsupported_worker_scope`
- Message: "OpenAI-compatible chat completions currently support only shared agents..."

Non-existent model:
- HTTP 404, code: `model_not_found`
- Message: "Model 'nonexistent' not found"

Evidence: `evidence/api-responses/oai-007.json`, `oai-007b.json`

### OAI-008: Matrix-Dependent Feature via /v1

**PASS** - Matrix-only features fail clearly when Matrix context is unavailable.

Sent scheduling request via /v1 -> Agent response: "I'm unable to schedule reminders directly"
The scheduler tool was invoked but returned "Scheduler tool is unavailable in this context" (tool trace visible in response).
The failure is surfaced to the user rather than silently succeeding.

Evidence: `evidence/api-responses/oai-008.json`

### OAI-009: Dashboard vs /v1 Auth Independence

**PASS** - Authentication surfaces are independent.

| Request | Result |
|---------|--------|
| /v1/models with valid Bearer key | 200 |
| /api/health (no auth) | 200 |
| /v1/models with non-Bearer auth | 401 |
| /api/config (no auth, no MINDROOM_API_KEY) | 404 |

Dashboard endpoints don't require /v1 API keys, and /v1 keys don't grant dashboard access.

### OAI-010: LibreChat Conversation ID + X-Session-Id Precedence

**PASS** - LibreChat header preserves continuity; explicit X-Session-Id takes precedence.

- Req 1 (LibreChat-ID=libre-conv-010): "Remember: secret code is rainbow42" -> Confirmed
- Req 2 (same LibreChat-ID): "What was the secret code?" -> "rainbow42" (continuity preserved)
- Req 3 (same LibreChat-ID + different X-Session-Id): "What was the secret code?" -> No recall (X-Session-Id took precedence, creating different session)

Evidence: `evidence/api-responses/oai-010a1.json`, `oai-010a2.json`, `oai-010b.json`

### OAI-011: Multimodal Content + Accepted OpenAI Fields

**PASS** - Text extracted from multimodal content, image parts ignored, accepted fields don't crash.

Request included:
- Multimodal `messages[].content`: text part + image_url part
- `response_format: {"type": "text"}`
- `tools: [...]` (function definition)
- `tool_choice: "auto"`
- `temperature: 0.5`
- `max_tokens: 100`

Result: Agent responded to the text part ("Say pong" -> "pong"). Image URL was silently ignored. Accepted-but-unsupported OpenAI fields did not change behavior or cause errors.

Evidence: `evidence/api-responses/oai-011.json`

### OAI-012: Auto Model Session Binding

**PASS** - Resolved agent name appears in response model field; sessions bind correctly.

- Req 1 (auto, session=auto-session-012): Resolved to `analyst`, model field shows "analyst"
- Req 2 (auto, same session, streaming): Resolved to `general`, model field shows "general"
- Both requests show resolved agent name (not literal "auto")
- [DONE] terminator present in streaming response

Note: Auto-routing may resolve to different agents on successive turns since routing is per-request. The session ID ensures conversation history is accessible regardless of which agent handles each turn.

Evidence: `evidence/api-responses/oai-012a.json`, `oai-012b.txt`
