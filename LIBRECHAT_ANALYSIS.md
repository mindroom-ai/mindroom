# LibreChat Tool-Call Display Analysis

## Executive conclusion

The current MindRoom proxy design will work functionally (tools run, answers continue), but it will **not** produce native stock LibreChat tool-call cards/collapsible tool UI.

What users will see in stock LibreChat is streamed assistant text/status updates (for example `🔧 Running ...`, `✅ ... done`) rather than native `TOOL_CALL` cards.

## Scope

This analysis answers:

- Will stock LibreChat display MindRoom tool calls "properly" (native tool-call UI) with the current proxy approach?
- Why or why not?

It uses code evidence from:

- This repo (`mindroom`)
- The referenced LibreChat codebase at `../mindroom-librechat`

## Findings and evidence

### 1. The current MindRoom proxy consumes tool-calls and emits status text

In `src/mindroom/proxy.py`, the proxy:

- parses upstream `tool_calls`
- executes them server-side
- appends `role="tool"` continuation messages
- emits text chunks like `🔧 Running ...` / `✅ ...`
- then emits final `finish_reason="stop"` to the UI

Evidence:

- `src/mindroom/proxy.py:268` checks for `finish_reason != "tool_calls"` and exits with stop
- `src/mindroom/proxy.py:275` streams `🔧 Running {tc.name}...`
- `src/mindroom/proxy.py:283` streams `{emoji} {tc.name} done/failed`
- `src/mindroom/proxy.py:284` appends `{"role": "tool", "tool_call_id": ..., "content": ...}`

So the UI-facing stream is text-first status output, not a tool-call event stream for frontend tool widgets.

### 2. MindRoom docs already describe this as status-chunk UX (not native tool cards)

The repo docs explicitly state the proxy injects status messages as content chunks.

Evidence:

- `docs/openai-api.md:197` explains proxy intercepts tool calls and continues loop
- `docs/openai-api.md:221` says: "The proxy injects status messages as content chunks"
- `docs/openai-api.md:224` to `docs/openai-api.md:227` shows the exact `🔧` / `✅` style output

This matches the current proxy code behavior.

### 3. Stock LibreChat native tool UI depends on `TOOL_CALL` content parts

In LibreChat frontend rendering, tool cards appear when message content contains `ContentTypes.TOOL_CALL`.

Evidence:

- `../mindroom-librechat/client/src/components/Chat/Messages/Content/Part.tsx:85` branches on `part.type === ContentTypes.TOOL_CALL`
- `../mindroom-librechat/client/src/components/Chat/Messages/Content/Part.tsx:143` renders `<ToolCall ... />`

If the message is plain text content, this path is not used.

### 4. Those `TOOL_CALL` parts are built from run-step events, not arbitrary status text

LibreChat step handler creates `TOOL_CALL` content from run-step events:

- `on_run_step` with `stepDetails.type === tool_calls`
- `on_run_step_delta` with tool call arg deltas
- `on_run_step_completed` with tool output

Evidence:

- `../mindroom-librechat/client/src/hooks/SSE/useStepHandler.ts:323`
- `../mindroom-librechat/client/src/hooks/SSE/useStepHandler.ts:482`
- `../mindroom-librechat/client/src/hooks/SSE/useStepHandler.ts:541`

If only plain text chunks arrive, frontend renders text, not native tool cards.

### 5. LibreChat tool execution is backend/graph-driven, not browser-side execution

LibreChat’s tool execution path is handled by backend event handlers:

- `ON_TOOL_EXECUTE` loads tools and invokes them server-side

Evidence:

- `../mindroom-librechat/packages/api/src/agents/handlers.ts:52` creates `ON_TOOL_EXECUTE` handler
- `../mindroom-librechat/packages/api/src/agents/handlers.ts:115` invokes tools (`tool.invoke(...)`)

Related graph behavior:

- model is bound with tools in the graph (`bindTools`)
- AI tool calls are extracted and executed/event-dispatched by `ToolNode`

Evidence:

- `../mindroom-librechat/node_modules/@librechat/agents/src/graphs/Graph.ts:572`
- `../mindroom-librechat/node_modules/@librechat/agents/src/tools/ToolNode.ts:527`
- `../mindroom-librechat/node_modules/@librechat/agents/src/tools/ToolNode.ts:541`

### 6. Message submission path in LibreChat routes through agents backend flow

Client payload construction routes to the agents server endpoint path.

Evidence:

- `../mindroom-librechat/packages/data-provider/src/createPayload.ts:25` builds server as agents endpoint

This reinforces that native tool cards depend on LibreChat’s internal run-step/tool pipeline, not on arbitrary assistant text.

### 7. Your own fork states exactly why a custom parser was added

Fork README notes upstream expectations did not match your server-side tool format, so a tag parser/rendering workaround was introduced.

Evidence:

- `../mindroom-librechat/README.md:7` says upstream expected OpenAI `delta.tool_calls` flow and that did not work for your setup
- `../mindroom-librechat/README.md:9` says fork renders `<tool>...</tool>` tags into ToolCall UI

This is consistent with the current conclusion: stock upstream behavior differs from your proxy status-text approach.

## Answer to the core question

With the current proxy design, stock LibreChat will:

- execute the end-to-end conversation/tool loop successfully
- display progress/status as normal assistant text
- **not** show native tool-call cards as if tool calls were truly local/native LibreChat tool events

## Practical implication

For native stock LibreChat tool-call UI, the integration must deliver the event/content shape LibreChat expects for `TOOL_CALL` rendering (run-step/tool-call semantics), not only text status chunks.

## Confidence

High. The behavior is directly evidenced by the current proxy implementation and the concrete rendering/event paths in the inspected LibreChat code.
