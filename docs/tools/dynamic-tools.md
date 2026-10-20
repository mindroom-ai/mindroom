---
icon: lucide/package-plus
---

# Dynamic Tools

Dynamic tools keep the definitions of an agent's rarely used tools out of the model's context until the agent searches for them, which saves context on every request.
Use them for agents with many optional tools.

## Configuration

| Field | Type | Default | Effect |
| --- | --- | --- | --- |
| `defer` | bool | `false` | Keep the tool's function definitions out of the model's context until the agent searches for them. |
| `initial` | bool | `false` | Send a deferred tool as an ordinary tool on every request; requires `defer: true`. |

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

Standard-mode agents on these models use the provider's own tool search instead of [MindRoom's](#mindroom-tool-search):

- Claude Opus 4.5, Sonnet 4.5, Haiku 4.5, and newer on the `anthropic` and `vertexai_claude` providers.
- GPT 5.4 and newer on `openai`, `codex`, and `openai_codex`.

For `openai`, an explicit `api: chat_completions` or a custom endpoint selects MindRoom's tool search instead.
The endpoint is a non-empty `extra_kwargs.base_url`, otherwise `OPENAI_BASE_URL`; native search applies only when it is unset or `https://api.openai.com/v1`.

On this path, deferred tools stay out of the model's context until the model searches for them, and discovering a tool does not invalidate the prompt cache.
The system prompt names the deferred toolkits and tells the model to search them before concluding that a tool is unavailable.
Discovered tools are called directly.
Toolkit instructions are omitted from the system prompt when every function in the toolkit is deferred and not `initial`.

## MindRoom Tool Search

On every other model, an agent with at least one deferred tool gets the `dynamic_tools` toolkit, with `tool_search(query, max_results)` and `call_tool(name, arguments)`.
The system prompt lists the deferred toolkits with their descriptions.
`tool_search` matches function names, toolkit names, and keywords in descriptions and parameter names, and returns the full definitions of up to `max_results` functions (default 5, at most 20) with their toolkit's instructions.
The agent then calls a function with `call_tool`, passing its arguments as a JSON object; approval rules, hooks, and the chat display treat the call as a call of that function.
Discovering and calling deferred tools never changes the tool list or the system prompt, so it does not invalidate the provider's prompt cache.
Found definitions stay in the conversation until compaction summarizes them, after which the agent searches again.

## Minimal Mode

In [minimal mode](agent-cli.md), every request still exposes only Bash.
The agent lists and calls its deferred tools through `mindroom-agent` like its other tools.
