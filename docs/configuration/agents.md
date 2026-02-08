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

    # Number of previous messages to include for context
    num_history_runs: 10

    # Enable markdown formatting
    markdown: true

    # Whether to add history to messages (for context)
    add_history_to_messages: true

    # Enable Agno Learning for this agent
    learning: true

    # Learning mode: always (automatic) or agentic (tool-driven)
    learning_mode: always
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `display_name` | string | *required* | Human-readable name shown in Matrix |
| `role` | string | `""` | Description of the agent's purpose and behavior |
| `model` | string | `"default"` | Model name (must be defined in `models` section) |
| `tools` | list | `[]` | Tool names the agent can use |
| `skills` | list | `[]` | Skill names the agent can use |
| `instructions` | list | `[]` | Additional behavioral instructions |
| `rooms` | list | `[]` | Room names/aliases to auto-join |
| `num_history_runs` | int | from defaults | Previous conversation runs to include for context |
| `markdown` | bool | from defaults | Format responses as markdown |
| `add_history_to_messages` | bool | from defaults | Include conversation history in context |
| `learning` | bool | from defaults | Enable Agno Learning for this agent |
| `learning_mode` | string | from defaults | Learning mode: `always` or `agentic` |

Learning data is persisted to `STORAGE_PATH/learning/<agent>.db` (default: `mindroom_data/learning/<agent>.db`), so it survives container restarts when `mindroom_data` is mounted.

## Rich Prompt Agents

Certain agent names (the YAML key, not `display_name`) have built-in rich prompts:

`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`

When using these names, the built-in prompt replaces the `role` field and any custom `instructions` are ignored.

## Defaults

```yaml
defaults:
  num_history_runs: 5
  markdown: true
  add_history_to_messages: true
  learning: true
  learning_mode: always
  show_stop_button: false  # global-only, cannot be overridden per-agent
```

When an agent omits `learning` or `learning_mode`, MindRoom inherits those values from `defaults`.
