---
icon: lucide/layers
---

# Toolkit Configuration

Dynamic toolkits are groups of tools that agents can load and unload at runtime.
Define toolkit bundles in the top-level `toolkits` section and grant agents access via `allowed_toolkits` and `initial_toolkits`.

See [Dynamic Toolkits](../tools/dynamic-toolkits.md) for the user-facing guide covering how agents interact with toolkits at runtime.

## Top-Level `toolkits` Section

```yaml
toolkits:
  devops:
    description: Docker, AWS, and GitHub tools for infrastructure work
    tools:
      - docker
      - aws_lambda
      - github
  research:
    description: Web search and academic paper tools
    tools:
      - duckduckgo
      - arxiv
      - wikipedia
  data:
    description: Data analysis and visualization
    tools:
      - pandas
      - csv
      - visualization
      - sql:
          db_url: postgresql://localhost/analytics
```

Each toolkit has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | string | `""` | Short description shown to agents when they call `list_toolkits()` |
| `tools` | list | `[]` | Tool entries in the same format as agent `tools` (plain strings or single-key dicts with inline config overrides) |

## Per-Agent Settings

Agents opt into dynamic toolkits with two fields:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A general-purpose helper
    model: sonnet
    tools: [file, shell]
    allowed_toolkits: [devops, research, data]
    initial_toolkits: [research]
    rooms: [lobby]
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowed_toolkits` | list | `[]` | Toolkit names this agent may load at runtime. Each name must match a key in the top-level `toolkits` section |
| `initial_toolkits` | list | `[]` | Toolkits loaded automatically when a new session starts. Must be a subset of `allowed_toolkits`. These are sticky — the agent cannot unload them |

When `allowed_toolkits` is non-empty, MindRoom automatically adds the `dynamic_tools` meta-toolkit to the agent.
You do not need to add `dynamic_tools` to the agent's `tools` list.

## Validation Rules

MindRoom validates toolkit configuration at startup and rejects invalid configs with clear error messages.

### Reference Validation

- Every entry in `allowed_toolkits` and `initial_toolkits` must match a key in the top-level `toolkits` section.
- `initial_toolkits` must be a subset of `allowed_toolkits`.

### Reserved Tools

Toolkit definitions cannot include these reserved control-plane tools:

- `delegate`
- `dynamic_tools`
- `self_config`

These tools are managed by MindRoom internally and injected automatically when needed.

### Tool Registry

All tools listed in a toolkit must resolve through the normal tool registry.
Plugin-provided tools work as long as they are registered before config validation runs.

### Duplicate Tools

Each tool name must appear at most once within a single toolkit definition.
Duplicate entries within one toolkit are rejected.

### Scope Compatibility

Some integrations are restricted to shared or unscoped execution (see [Worker Routing](agents.md#worker-routing)).
If an agent with an isolating `worker_scope` (or `private.per`) has a toolkit containing a shared-only integration, MindRoom rejects the config at startup.

The following integrations are shared-only and cannot appear in toolkits assigned to agents with `user` or `user_agent` scope:

`spotify`, `homeassistant`

## Conflict Detection

When an agent loads a toolkit at runtime, MindRoom checks for tool name collisions.
If a toolkit tool is already present in the agent's active set (from static tools or another loaded toolkit) with different config overrides, the load is rejected with a conflict error.
Identical duplicate tools are silently deduplicated.

## Full Example

```yaml
models:
  default:
    provider: anthropic
    id: claude-sonnet-4-6

toolkits:
  devops:
    description: Infrastructure and deployment tools
    tools:
      - docker
      - aws_lambda
      - github
  research:
    description: Web search and academic sources
    tools:
      - duckduckgo
      - arxiv
      - wikipedia
  data:
    description: Data analysis and visualization
    tools:
      - pandas
      - csv
      - visualization

agents:
  assistant:
    display_name: Assistant
    role: A versatile helper that loads tools on demand
    model: default
    tools:
      - file
      - shell
    allowed_toolkits: [devops, research, data]
    initial_toolkits: [research]
    rooms: [lobby]

  researcher:
    display_name: Researcher
    role: Deep research specialist
    model: default
    tools:
      - duckduckgo
      - arxiv
    allowed_toolkits: [data]
    rooms: [lobby]

defaults:
  tools:
    - scheduler
```

In this example:

- `assistant` starts with `file`, `shell`, `scheduler` (from defaults), and the `research` toolkit tools.
  It can load `devops` or `data` on demand, and cannot unload `research` because it is in `initial_toolkits`.
- `researcher` starts with `duckduckgo`, `arxiv`, and `scheduler`.
  It can load `data` on demand for analysis work.
