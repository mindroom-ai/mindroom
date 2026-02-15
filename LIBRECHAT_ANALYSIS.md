# LibreChat Tool-Call Display Analysis

## Executive summary

The current MindRoom proxy design is **functionally correct** but **UI-semantic incompatible** with stock LibreChat native tool-call rendering.

- Functional: yes (tool loop completes, assistant continues).
- Native LibreChat tool cards/collapsible tool UI: no (with current proxy output format).

Users on stock LibreChat will see plain assistant text/status chunks (`🔧 Running ...`, `✅ ...`) rather than `TOOL_CALL` cards.

## What "works properly" means (explicit criteria)

To avoid ambiguity, there are three different success levels:

1. **Execution correctness**: tool calls actually run and final answer returns.
2. **Protocol correctness**: OpenAI-compatible request/response loop is valid.
3. **UI-native correctness (your goal)**: LibreChat shows native tool-call UI cards/progress/output, not plain text emulation.

Current proxy satisfies 1 and mostly 2, but not 3.

## Scope and evidence base

This analysis uses:

- MindRoom code/docs in this repo
- LibreChat code in `../mindroom-librechat` (the codebase you asked me to inspect)

## Architecture observed

### A) Current MindRoom proxy path

```
LibreChat UI -> MindRoom proxy (/v1/chat/completions)
             -> MindRoom strict mode upstream
             <- proxy consumes tool_calls
             -> proxy executes tools via /v1/tools/execute
             -> proxy appends role=tool continuation
             <- proxy emits status/content text chunks to UI
```

### B) Stock LibreChat native tool-call rendering path

```
Frontend SSE consumer -> receives semantic events ({event, data})
                      -> useStepHandler builds ContentTypes.TOOL_CALL parts
                      -> Part.tsx renders <ToolCall .../> and specialized tool UIs
```

## Core findings

### 1. Current proxy intentionally converts tool activity into text chunks

Proxy behavior in `src/mindroom/proxy.py`:

- Parses `delta.tool_calls`
- Executes each tool server-side
- Appends `role="tool"` continuation messages
- Emits text status chunks to downstream client
- Emits final stop chunk

Evidence:

- `src/mindroom/proxy.py:268` finish check for `tool_calls`
- `src/mindroom/proxy.py:275` emits `🔧 Running ...`
- `src/mindroom/proxy.py:283` emits `✅ ... done` / failure text
- `src/mindroom/proxy.py:284` appends tool continuation message

### 2. MindRoom docs describe this exact UX as status-chunk output

Evidence:

- `docs/openai-api.md:197` proxy intercepts tool calls and loops continuation
- `docs/openai-api.md:221` says status is injected as content chunks
- `docs/openai-api.md:224` to `docs/openai-api.md:227` sample output is text-based

So native tool cards are not the current documented behavior.

### 3. Stock LibreChat native tool cards render only from `ContentTypes.TOOL_CALL`

Evidence:

- `../mindroom-librechat/client/src/components/Chat/Messages/Content/Part.tsx:85`
- `../mindroom-librechat/client/src/components/Chat/Messages/Content/Part.tsx:143`

If content part type is text, the text renderer path is used instead.

### 4. `ContentTypes.TOOL_CALL` parts are constructed from step events

Frontend step handler materializes tool-call parts from:

- `on_run_step` tool call start
- `on_run_step_delta` argument deltas
- `on_run_step_completed` final output

Evidence:

- `../mindroom-librechat/client/src/hooks/SSE/useStepHandler.ts:323`
- `../mindroom-librechat/client/src/hooks/SSE/useStepHandler.ts:482`
- `../mindroom-librechat/client/src/hooks/SSE/useStepHandler.ts:541`

### 5. SSE consumer expects semantic `{event,data}` envelopes in this path

`useSSE` routes payloads with `data.event != null` into `stepHandler`.

Evidence:

- `../mindroom-librechat/client/src/hooks/SSE/useSSE.ts:144`
- `../mindroom-librechat/client/src/hooks/SSE/useSSE.ts:145`

Backend stream manager also documents SSE envelope as `{ event, data }`.

Evidence:

- `../mindroom-librechat/packages/api/src/stream/GenerationJobManager.ts:809`

### 6. Tool execution in LibreChat is backend/graph event-driven, not browser local execution

Evidence:

- `../mindroom-librechat/packages/api/src/agents/handlers.ts:52` creates `ON_TOOL_EXECUTE`
- `../mindroom-librechat/packages/api/src/agents/handlers.ts:115` executes `tool.invoke(...)`
- `../mindroom-librechat/node_modules/@librechat/agents/src/graphs/Graph.ts:572` binds tools to model
- `../mindroom-librechat/node_modules/@librechat/agents/src/tools/ToolNode.ts:541` executes via event path

### 7. Request routing in inspected LibreChat snapshot goes through agents flow

Payload server target is agents endpoint path.

Evidence:

- `../mindroom-librechat/packages/data-provider/src/createPayload.ts:25`

This further ties UI-native tool rendering to the internal agent step-event pipeline.

### 8. Your fork already documents a workaround because upstream flow was insufficient

Evidence:

- `../mindroom-librechat/README.md:7`
- `../mindroom-librechat/README.md:9`

That aligns with this conclusion: stock behavior and MindRoom server-side tool execution model are not automatically aligned for native cards.

## Protocol mismatch table (current state)

| Aspect | Current MindRoom proxy output | Stock LibreChat native tool-card expectation |
|---|---|---|
| Tool call surfaced to UI | Text chunks (`delta.content`) | Step events -> `ContentTypes.TOOL_CALL` parts |
| Progress updates | Text (`🔧`, `✅`) | `on_run_step*` event sequence |
| Final chunk | `finish_reason: "stop"` after proxy loop | Tool part completion updates + final assistant content |
| Visual result | Plain text stream | Native ToolCall/ExecuteCode/WebSearch cards |

## Direct answer

With the current design, stock LibreChat will:

- run tools and finish the answer correctly
- not display native tool-call cards in the way you intended

So it "works," but not "properly" for your UI-native objective.

## Why this matters for your design intent

Your intent is to make server-executed tools appear client-native in standard UIs.
Current proxy design achieves a good compatibility fallback, but not true native semantics for stock LibreChat.

## Design options from here

1. **Keep current proxy design (no LibreChat changes)**
- Pros: lowest complexity, works today.
- Cons: no native tool cards.

2. **Maintain a LibreChat fork/plugin renderer (your current fork direction)**
- Pros: can render tool activity richly from custom tags/text.
- Cons: not stock-upstream compatible for third-party LibreChat users.

3. **Build a LibreChat-semantic adapter path**
- Emit the step-event/content-part shapes stock LibreChat uses for `TOOL_CALL`.
- Pros: native UI in stock behavior path.
- Cons: higher complexity; must match LibreChat event contracts tightly.

## Acceptance criteria for "native LibreChat success"

A run is considered native-correct only if all are true:

- Tool start appears as a tool card (not plain text).
- Streaming args/progress update that card.
- Tool output finalizes the same card.
- Final assistant text appears after tool card(s), preserving order.

## Confidence and caveats

Confidence: high for this inspected code snapshot.

Caveat:

- LibreChat evolves quickly; event contracts may shift by version.
  Re-verify against exact upstream commit/version before implementing final adapter behavior.
