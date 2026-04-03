---
icon: lucide/layers
---

# Dynamic Toolkits

Dynamic toolkits let agents load and unload groups of tools at runtime without restarting MindRoom.
This reduces context overhead by keeping rarely used tools out of the agent's active tool set until they are actually needed.

## Why Dynamic Toolkits

Every tool in an agent's active set adds to the system prompt and consumes context window tokens.
An agent with 20+ tools pays that cost on every request, even when most tools go unused.
Dynamic toolkits solve this by letting the agent start with a minimal tool set and pull in additional capabilities on demand.

For example, a general-purpose assistant might start with `file` and `shell`, then load a `devops` toolkit (containing `docker`, `aws_lambda`, `github`) only when the user asks about infrastructure.

## The Three Meta-Tools

When an agent has `allowed_toolkits` configured, MindRoom automatically adds a `dynamic_tools` toolkit with three tools:

### `list_toolkits()`

Returns all allowed toolkits for the agent with their descriptions, tool names, and current loaded state.
The agent calls this when it is unsure which toolkit contains a needed capability.

### `load_tools(toolkit)`

Loads one allowed toolkit by name.
The tools in that toolkit become available on the **next request** in the same session, not later in the current model run.

### `unload_tools(toolkit)`

Removes one loaded toolkit by name.
Like `load_tools`, the change takes effect on the **next request** in the same session.
Toolkits listed in `initial_toolkits` are sticky and cannot be unloaded.

## Next-Request Activation

Tool changes do not take effect immediately.
When an agent calls `load_tools("devops")`, the devops tools are not available in the same response — they appear on the next message in the same conversation thread.
This is a fundamental design constraint: the model's tool list is fixed for the duration of one request.

The agent receives a confirmation message like:

```
Toolkit 'devops' will be available on the next request in this session.
```

## Session State

Dynamic toolkit state is persisted per agent per session in the agent's SQLite session database.
When a session starts, `initial_toolkits` are loaded automatically.
On subsequent requests, MindRoom reads the persisted loaded-toolkit set from the session and merges the corresponding tools into the agent's runtime tool list.

If the config changes between requests (a toolkit is removed or an agent's `allowed_toolkits` is modified), MindRoom sanitizes the persisted state and drops any toolkits that are no longer valid.

## Conflict Detection

When loading a toolkit, MindRoom checks for tool name collisions with the agent's existing tools.
If a toolkit contains a tool that is already active (from the agent's static tools or another loaded toolkit) and the two definitions have different config overrides, the load is rejected with a conflict error.
Identical duplicate tools are allowed — the existing definition is kept.

## V1 Limitation: Per-Agent State

In V1, dynamic toolkit state is per-agent, not shared across team members.
When an agent participates in a team conversation, each team member manages its own loaded toolkits independently.
Loading a toolkit for one member does not load it for the others.

## Example: Agent Using Dynamic Toolkits

Given this configuration:

```yaml
toolkits:
  devops:
    description: Docker, AWS, and GitHub tools for infrastructure work
    tools: [docker, aws_lambda, github]
  research:
    description: Web search and academic paper tools
    tools: [duckduckgo, arxiv, wikipedia]

agents:
  assistant:
    display_name: Assistant
    role: A general-purpose helper
    model: sonnet
    tools: [file, shell]
    allowed_toolkits: [devops, research]
    initial_toolkits: [research]
    rooms: [lobby]
```

The assistant starts each new session with `file`, `shell`, and the `research` tools (`duckduckgo`, `arxiv`, `wikipedia`) already loaded.
The `research` toolkit is sticky because it is in `initial_toolkits` and cannot be unloaded.

A typical conversation might look like:

1. **User**: "What papers exist on transformer architectures?"
   - Agent uses `arxiv` (already loaded via `research` toolkit) to search.

2. **User**: "Now deploy the demo app to AWS."
   - Agent calls `list_toolkits()` to check available toolkits.
   - Agent calls `load_tools("devops")` to load infrastructure tools.
   - Agent responds: "I've loaded the devops toolkit. I'll have Docker, AWS Lambda, and GitHub tools available on your next message."

3. **User**: "Go ahead."
   - Agent now has `docker`, `aws_lambda`, and `github` available and proceeds with the deployment.

## See Also

- [Toolkit Configuration](../configuration/toolkits.md) — config reference for the `toolkits` section and per-agent settings
- [Tools Overview](index.md) — enabling and configuring tools
- [Built-in Tools](builtin.md) — complete list of available tools
