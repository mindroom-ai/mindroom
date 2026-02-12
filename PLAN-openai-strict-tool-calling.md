# PLAN: OpenAI-Compatible Tool Calling (`X-Tool-Event-Format: openai`)

## Goal
Implement a strict OpenAI tool-calling mode in `src/mindroom/api/openai_compat.py` that:
- Emits standard OpenAI `tool_calls` envelopes (streaming and non-streaming).
- Uses Agno pause/continue (`RunPausedEvent` + `acontinue_run`) instead of synthetic replay.
- Keeps existing modes unchanged:
  - default: inline text tool events
  - `open-webui`: `<details>` HTML rendering

## Scope
- Add `X-Tool-Event-Format: openai` as a new additive mode.
- Support single-agent execution in this PR (`model=<agent>` and `model=auto` after resolving to one agent).
- Reject team models for `openai` mode in this PR.

## Non-Goals (This PR)
- No client-side proxy/catch-all executor.
- No LibreChat/Open WebUI frontend changes.
- No multi-worker distributed pending-run storage.

## Why This Design
- Agno already supports external tool execution pauses and continuation.
- We can produce protocol-correct OpenAI tool-call payloads without embedding UI markup.
- This preserves real-time streaming and avoids post-hoc replay synthesis.

## Key Design Decisions
1. Continuation API uses `requirements` (`RunRequirement`) and **not** deprecated `updated_tools`.
2. `openai` mode requires stable session identity, so `X-Session-Id` is required (400 otherwise).
3. Pending paused runs are stored in-memory with TTL for this PR (single-process limitation documented).
4. Tool IDs are guaranteed: if Agno tool call ID is missing, synthesize `call_<uuid>`.
5. Server tools remain the source of truth; request `tools` payload is ignored (current behavior).

## Architecture

### 1) Data model and request parsing
1. Extend `ChatMessage` with optional OpenAI fields:
   - `tool_call_id: str | None` (for `role="tool"` messages)
   - `tool_calls: list[...] | None` (for `role="assistant"` messages)
2. Add typed models for OpenAI tool call payloads (`id`, `type=function`, `function.name`, `function.arguments`).
3. Add helper to extract tool results from incoming request messages:
   - `tool_call_id -> result_content` mapping for continuation.
4. Keep `_convert_messages()` behavior unchanged for non-`openai` modes.

### 2) Mode routing in `chat_completions`
1. Detect `tool_event_format = request.headers.get("x-tool-event-format")`.
2. If `tool_event_format == "openai"`:
   - enforce `X-Session-Id` presence.
   - reject team models with OpenAI-style 400 error.
   - route to dedicated strict handlers:
     - streaming strict handler when `req.stream=True`
     - non-stream strict handler when `req.stream=False`
3. Keep existing handlers for default and `open-webui` modes.

### 3) External execution enablement
1. For strict mode, create agent and mark all tool functions as externally executed.
2. Implementation detail:
   - iterate toolkits/functions and set `Function.external_execution = True`.
3. Agent is created per request (current architecture), so mutation is request-scoped.

### 4) Pending run state
1. Add dataclass (module-private):
   - `run_id`
   - `agent_name`
   - `session_id`
   - `requirements: list[RunRequirement]`
   - `created_at`
2. In-memory store: `dict[str, PendingRun]` keyed by derived/namespaced session ID.
3. Add per-session locking (`asyncio.Lock`) to avoid same-session continuation races.
4. Add TTL cleanup helper invoked on strict-mode requests.
5. Document limitation: in-memory store requires single worker or sticky routing.

### 5) Strict mode: first run
1. Build prompt/history with existing pipeline.
2. Run agent with strict tool execution enabled.
3. Streaming path:
   - emit role chunk.
   - emit `delta.content` for `RunContentEvent`.
   - on `RunPausedEvent`, filter `requirements` needing external execution and emit `delta.tool_calls` chunks.
   - terminate with `finish_reason: "tool_calls"`.
   - persist pending run state.
4. Non-streaming path:
   - if run pauses for external execution, return assistant message with `tool_calls` and `finish_reason: "tool_calls"`, then persist state.
   - otherwise return normal assistant completion (`finish_reason: "stop"`).

### 6) Strict mode: continuation
1. Detect continuation by presence of pending run for session.
2. Parse incoming `role="tool"` messages and build `tool_call_id -> content` map.
3. Validate all pending external-execution requirements are satisfied:
   - missing/unknown IDs -> 400.
4. For each requirement:
   - set tool result via `requirement.set_external_execution_result(content)`.
5. Recreate agent (same agent name/session storage), enable external execution, continue run via:
   - `agent.acontinue_run(run_id=..., requirements=..., session_id=..., stream_events=True/False)`.
6. Output handling mirrors first-run flow:
   - may pause again (emit tool calls + replace pending state), or
   - complete (emit assistant output + clear pending state).

### 7) OpenAI output formatting
1. Streaming tool call output:
   - role chunk first.
   - `delta.tool_calls` chunks with fields: `index`, `id`, `type="function"`, `function.name`, `function.arguments`.
   - arguments emitted as full JSON string in one chunk per tool call.
   - terminal chunk: `finish_reason: "tool_calls"`.
2. Non-streaming tool call output:
   - assistant message contains `tool_calls` array.
   - `finish_reason: "tool_calls"`.
3. Completion path always returns standard `chat.completion` / `chat.completion.chunk` shape.

### 8) Validation and error handling
1. `openai` mode validation:
   - missing `X-Session-Id` -> 400
   - team model -> 400
   - continuation with no pending run -> 400
   - unknown/missing tool_call_id results -> 400
2. Keep OpenAI-style error envelope for all failures.
3. If non-tool pause occurs (confirmation/user-input), return explicit 400 in strict mode for now.

## Test Plan (`tests/test_openai_compat.py`)
1. Streaming first pass pauses and emits valid `delta.tool_calls` + `finish_reason="tool_calls"`.
2. Non-stream first pass returns assistant `tool_calls` + `finish_reason="tool_calls"`.
3. Continuation (tool messages) resumes and returns final assistant output.
4. Re-pause across multiple rounds replaces pending state correctly.
5. Validation errors:
   - no `X-Session-Id`
   - team model + `openai`
   - continuation without pending state
   - missing/unknown `tool_call_id`
6. Missing Agno tool_call_id generates synthetic `call_...` ID.
7. Concurrent same-session continuation is serialized (no double-consume).
8. Regression tests: default and `open-webui` behavior unchanged.

## Documentation Updates
Update `docs/openai-api.md`:
1. Add `openai` mode header behavior and lifecycle.
2. Document required `X-Session-Id` for strict tool-calling mode.
3. Document current limitation: single-process pending-run state in this PR.

## Rollout
1. Additive, header-gated mode only.
2. Keep defaults unchanged.
3. Verify with:
   - `uv run pytest tests/test_openai_compat.py -q`
   - `just test-backend`
   - `pre-commit run --all-files`

## Acceptance Criteria
- Existing behaviors unchanged.
- `openai` mode emits OpenAI-compatible tool-call payloads.
- Pause/continue works for at least one full tool round trip (streaming and non-streaming).
- All tests and hooks pass.
