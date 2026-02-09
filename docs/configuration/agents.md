---
icon: lucide/user
---

# Agent Configuration

Agents are the core building blocks of MindRoom. Each agent is a specialized AI actor with specific capabilities.

## Basic Agent

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: sonnet
    rooms: [lobby]
```

## Full Configuration

```yaml
agents:
  developer:
    # Display name shown in Matrix
    display_name: Developer

    # Role description - guides the agent's behavior
    role: Generate code, manage files, execute shell commands

    # Model to use (defined in models section)
    model: sonnet

    # Tools the agent can use
    tools:
      - file
      - shell
      - github

    # Skills the agent can use (defined in skills section or plugins)
    skills:
      - my_custom_skill

    # Custom instructions
    instructions:
      - Always read files before modifying them
      - Use clear variable names
      - Add comments for complex logic

    # Rooms to join (will be created if they don't exist)
    rooms:
      - lobby
      - dev

    # Enable markdown formatting
    markdown: true

    # Enable Agno Learning for this agent
    learning: true

    # Learning mode: always (automatic) or agentic (tool-driven)
    learning_mode: always

    # Assign agent to a configured knowledge base (optional)
    knowledge_base: docs
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `display_name` | string | *required* | Human-readable name shown in Matrix as the bot's display name |
| `role` | string | `""` | System prompt describing the agent's purpose — guides its behavior and expertise |
| `model` | string | `"default"` | Model name (must match a key in the `models` section) |
| `tools` | list | `[]` | Tool names the agent can use (see [Tools](../tools/index.md) for available options) |
| `skills` | list | `[]` | Skill names the agent can use (see [Skills](../skills.md)) |
| `instructions` | list | `[]` | Extra lines appended to the system prompt after the role |
| `rooms` | list | `[]` | Room aliases to auto-join; rooms are created if they don't exist |
| `markdown` | bool | `true` | When enabled, the agent is instructed to format responses as Markdown |
| `learning` | bool | `true` | Enable [Agno Learning](https://docs.agno.com/agents/learning) — the agent builds a persistent profile of user preferences and adapts over time |
| `learning_mode` | string | `"always"` | `always`: agent automatically learns from every interaction. `agentic`: agent decides when to learn via a tool call |
| `knowledge_base` | string or null | `null` | Knowledge base ID from top-level `knowledge_bases` — gives the agent RAG access to the indexed documents |

Skills are opt-in: a skill is only loaded when its name appears in an agent's `skills` list. `metadata.openclaw.always: true` bypasses eligibility requirements, but it does not auto-attach the skill to agents.

If `knowledge_base` is set, it must match a key under `knowledge_bases` in `config.yaml`.

All per-agent settings above that show a default value inherit from the `defaults` section. Per-agent values override them.

Learning data is persisted to `STORAGE_PATH/learning/<agent>.db` (default: `mindroom_data/learning/<agent>.db`), so it survives container restarts when `mindroom_data` is mounted.

## Rich Prompt Agents

Certain agent names (the YAML key, not `display_name`) have built-in rich prompts:

`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`

When using these names, the built-in prompt replaces the `role` field and any custom `instructions` are ignored.

## Defaults

The `defaults` section sets fallback values for all agents. Any agent that omits a setting inherits the value from here.

```yaml
defaults:
  markdown: true             # Format responses as Markdown
  learning: true             # Enable Agno Learning
  learning_mode: always      # "always" or "agentic"
  show_stop_button: false    # Show a stop button while agent is responding (global-only, cannot be overridden per-agent)
```
