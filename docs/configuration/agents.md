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

    # Whether to include conversation history in context
    add_history_to_messages: true
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `display_name` | string | *required* | Human-readable name shown in Matrix |
| `role` | string | `""` | Description of the agent's purpose and behavior |
| `model` | string | `"default"` | Model name (must be defined in `models` section) |
| `tools` | list | `[]` | List of tool names the agent can use |
| `skills` | list | `[]` | List of skill names the agent can use |
| `instructions` | list | `[]` | Additional instructions for the agent |
| `rooms` | list | `[]` | Room names/aliases the agent should join |
| `num_history_runs` | int | `5` (from defaults) | Number of previous conversation runs to include |
| `markdown` | bool | `true` (from defaults) | Whether to format responses as markdown |
| `add_history_to_messages` | bool | `true` (from defaults) | Whether to include conversation history in context |

## Rich Prompt Agents

Some agent names (the YAML key, not `display_name`) have built-in rich prompts that provide detailed behavior:

- `code` - Code generation, file management, shell commands
- `research` - Web research and information gathering
- `calculator` - Mathematical calculations
- `general` - General-purpose assistant
- `shell` - Shell command execution
- `summary` - Text summarization
- `finance` - Financial analysis
- `news` - News aggregation
- `data_analyst` - Data analysis

When using these names, the built-in prompt is used instead of the `role` field. Note that both the `role` and `instructions` fields from YAML are ignored for rich prompt agents - the built-in prompt is used as-is.

## Defaults

Default values can be configured globally:

```yaml
defaults:
  num_history_runs: 5
  markdown: true
  add_history_to_messages: true
  show_stop_button: false
```

The `num_history_runs`, `markdown`, and `add_history_to_messages` defaults apply to all agents unless overridden in the agent's configuration. The `show_stop_button` setting is global-only and cannot be overridden per-agent.

## Tools

See [Tools](../tools/index.md) for the full list of available tools.

## Skills

Skills provide reusable capabilities that can be shared across agents. See [Skills](../skills.md) for more information.
