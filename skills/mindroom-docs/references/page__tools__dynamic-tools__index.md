# Dynamic Tools

Dynamic tools let an agent keep rarely used tools out of the provider-visible schema list until it needs them.
The loading unit is one authored entry in the agent's `tools:` list.
On providers with server-side tool search the gating happens inside the provider API; everywhere else MindRoom performs the schema gating in its runtime.

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

## Native Server-Side Tool Search

Claude models since Opus 4.5 / Sonnet 4.5 / Haiku 4.5 on the direct `anthropic` provider, and GPT models 5.4 or newer on the first-party `openai`, `codex`, and `openai_codex` providers, use the provider's server-side tool search automatically.
Vertex Claude uses MindRoom's runtime fallback because Vertex does not accept Anthropic's beta tool-search schema.
On this path every deferred tool ships in every request tagged `defer_loading: true` together with the provider's tool-search entry, so deferred schemas stay out of the model's rendered context until the model searches for them.
Tool discovery never invalidates the prompt cache: Anthropic expands discovered tool references inline in the message stream, and OpenAI loads discovered tools at the end of the context window.
The `dynamic_tools` manager, its prompt blocks, and session loaded-tool state are not used on this path; all deferred toolkits are attached at agent build, so discovered calls execute directly.
`defer: true, initial: true` tools stay in the rendered schema list as plain non-deferred tools.

## Runtime Tools

On providers without native tool search, when an agent has at least one deferred tool and a stable session id, MindRoom injects the `dynamic_tools` manager.
The manager exposes `list_tools()`, `tool_search(query)`, `load_tool(tool_name)`, and `unload_tool(tool_name)`.
Search is plain keyword and exact-name lookup only.
A newly loaded tool becomes callable once it appears in the agent's available tools, never in the same parallel tool-call batch as `load_tool()`.
A standalone agent continues the same task in a later tool-call step within the same response, because its run loop rebuilds the agent with the updated schema and resumes the turn without waiting for another user message.
Team members and other embedded agents run without that continuation loop, so their loads and unloads take effect on the next request in the same session.

## State Scope

Loaded state is keyed by the exact `(agent, session_id)` pair.
Two agents in the same Matrix thread do not share loaded tools.
Native tool-search sessions neither read nor write this state.
