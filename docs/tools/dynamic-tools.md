# Dynamic Tools

Dynamic tools let an agent keep rarely used tools out of the provider-visible schema list until it needs them.
The loading unit is one authored entry in the agent's `tools:` list.
MindRoom performs the schema gating in its runtime, so the feature works across model providers.

## Configuration

Add `defer: true` to a tool entry to make it lazy.
Add `initial: true` with `defer: true` when the tool should start loaded for every new session and remain sticky.
The `initial` flag is rejected unless `defer` is also true.
Lazy loading is per-agent, so `defaults.tools` does not accept `defer` or `initial`.
Tool presets such as `openclaw_compat` also do not accept `defer` or `initial`; configure the individual member tools directly when they need lazy loading.

```yaml
agents:
  assistant:
    display_name: Assistant
    role: Help in chat
    tools:
      - shell
      - coding: {defer: true, initial: true, restrict_to_base_dir: false}
      - searxng: {defer: true, host: https://search.example.test, fixed_max_results: 10}
      - name: serper
        defer: true
        overrides: {num_results: 10}
```

No flags means the tool is eager and appears in every request.
`defer: true` hides the tool schema until the agent loads that authored tool for the current session.
`defer: true, initial: true` loads the tool at session start and prevents unloading.

## Runtime Tools

When an agent has at least one deferred tool and a stable session id, MindRoom injects the `dynamic_tools` manager.
The manager exposes `list_tools()`, `tool_search(query)`, `load_tool(tool_name)`, and `unload_tool(tool_name)`.
Search is plain keyword and exact-name lookup only.
After `load_tool()` or `unload_tool()` succeeds, the agent should continue the same task in a later tool-call step in the same response.
It should not wait for another user message, and it should not call a newly loaded tool in the same parallel tool-call batch as `load_tool()`.

## State Scope

Loaded state is keyed by the exact `(agent, session_id)` pair.
Two agents in the same Matrix thread do not share loaded tools.
