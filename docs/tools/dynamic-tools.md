---
icon: lucide/package-plus
---

# Dynamic Tools

Dynamic tools keep an agent's rarely used tools out of the model's tool list until the agent needs them, which saves context on every request.
Use them for agents with many optional tools.
Each authored entry in an agent's `tools:` list loads or unloads as one unit.

## Configuration

| Field | Type | Default | Effect |
| --- | --- | --- | --- |
| `defer` | bool | `false` | Hide the tool until the agent loads it for the current session. |
| `initial` | bool | `false` | Load a deferred tool at session start and keep it loaded; requires `defer: true`. |

A tool with neither flag is eager and appears in every request.

```yaml
agents:
  assistant:
    display_name: Assistant
    role: Help in chat
    tools:
      - shell
      - coding: {defer: true, initial: true}
      - searxng: {defer: true, host: https://search.example.test, fixed_max_results: 10}
      - name: serper
        defer: true
        overrides: {num_results: 10}
```

These configurations fail validation:

- `initial: true` without `defer: true` fails with `Tool entry initial=true requires defer=true`.
- Lazy loading is per agent, so `defaults.tools` entries reject both flags with `defaults.tools does not support defer or initial flags: <tools>`.
- Presets such as [`openclaw_compat`](../openclaw.md) cannot be deferred; set the flags on the individual member tools instead.
- The control-plane tools `delegate`, `dynamic_tools`, `external_trigger_manager`, `invite_router`, `self_config`, and `skill_manage` cannot be deferred and fail with `'<tool>' is a control-plane tool and cannot be deferred; defer/initial are only valid on runtime tools.`

## Native Server-Side Tool Search

Standard-mode agents on these models use the provider's own tool search instead of MindRoom's loading tools:

- Claude Opus 4.5, Sonnet 4.5, Haiku 4.5, and newer on the `anthropic` and `vertexai_claude` providers.
- GPT 5.4 and newer on `openai`, `codex`, and `openai_codex`.

For `openai`, an explicit `api: chat_completions` or a custom endpoint selects runtime loading instead.
The endpoint is a non-empty `extra_kwargs.base_url`, otherwise `OPENAI_BASE_URL`; native search applies only when it is unset or `https://api.openai.com/v1`.

On this path, deferred tools stay out of the model's context until the model searches for them, and discovering a tool does not invalidate the prompt cache.
The system prompt names the deferred toolkits and tells the model to search them before concluding that a tool is unavailable.
Discovered tools are called directly, so there is nothing to load or unload.
Tools with `initial: true` are ordinary always-visible tools.
Toolkit instructions are omitted from the system prompt when every function in the toolkit is deferred and not `initial`.

## Runtime Loading

On other models, an agent with at least one deferred tool gets the `dynamic_tools` toolkit, with `list_tools()`, `tool_search(query)`, `load_tool(tool_name)`, and `unload_tool(tool_name)`.
`tool_search` matches keywords and exact names only.
Tools with `initial: true` cannot be unloaded.

A loaded tool becomes callable in the agent's next tool-call step, not in the same parallel batch as `load_tool()`.
The agent continues the same response with the updated tools and the loaded toolkit's instructions, without waiting for another message.

Loaded tools are tracked per agent and per conversation session, so two agents in the same Matrix thread do not share them.
This state is held in memory, so a restart returns each session to its `initial` tools.

## Minimal Mode

In [minimal mode](agent-cli.md), every request still exposes only Bash.
The agent lists and calls its deferred tools through `mindroom-agent` like its other tools.
