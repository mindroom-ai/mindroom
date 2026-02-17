# Agent Configuration

Agents are the core building blocks of MindRoom. Each agent is a specialized AI actor with specific capabilities.

## Basic Agent

```
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: sonnet
    rooms: [lobby]
```

## Full Configuration

```
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

    # Assign agent to one or more configured knowledge bases (optional)
    knowledge_bases: [docs]

    # Optional: additional files loaded into role context at agent init/reload
    context_files:
      - ./openclaw_data/SOUL.md
      - ./openclaw_data/USER.md
      - ./openclaw_data/AGENTS.md

    # Optional: directory-based memory context (MEMORY.md + dated files)
    memory_dir: ./openclaw_data/memory
```

## Configuration Options

| Option                  | Type   | Default     | Description                                                                                                                                              |
| ----------------------- | ------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `display_name`          | string | *required*  | Human-readable name shown in Matrix as the bot's display name                                                                                            |
| `role`                  | string | `""`        | System prompt describing the agent's purpose — guides its behavior and expertise                                                                         |
| `model`                 | string | `"default"` | Model name (must match a key in the `models` section)                                                                                                    |
| `tools`                 | list   | `[]`        | Agent-specific tool names (see [Tools](https://docs.mindroom.chat/tools/index.md)); effective tools are `defaults.tools + tools` with duplicates removed |
| `include_default_tools` | bool   | `true`      | When `true`, append `defaults.tools` to this agent's `tools`; set to `false` to opt this agent out                                                       |
| `skills`                | list   | `[]`        | Skill names the agent can use (see [Skills](https://docs.mindroom.chat/skills/index.md))                                                                 |
| `instructions`          | list   | `[]`        | Extra lines appended to the system prompt after the role                                                                                                 |
| `rooms`                 | list   | `[]`        | Room aliases to auto-join; rooms are created if they don't exist                                                                                         |
| `markdown`              | bool   | `true`      | When enabled, the agent is instructed to format responses as Markdown                                                                                    |
| `learning`              | bool   | `true`      | Enable [Agno Learning](https://docs.agno.com/agents/learning) — the agent builds a persistent profile of user preferences and adapts over time           |
| `learning_mode`         | string | `"always"`  | `always`: agent automatically learns from every interaction. `agentic`: agent decides when to learn via a tool call                                      |
| `knowledge_bases`       | list   | `[]`        | Knowledge base IDs from top-level `knowledge_bases` — gives the agent RAG access to the indexed documents                                                |
| `context_files`         | list   | `[]`        | File paths loaded at agent init/reload and prepended to role context (under `Personality Context`)                                                       |
| `memory_dir`            | string | `null`      | Directory loaded at agent init/reload for `MEMORY.md` and dated files (under `Memory Context`)                                                           |

Each entry in `knowledge_bases` must match a key under `knowledge_bases` in `config.yaml`.

All per-agent settings above that show a default value inherit from the `defaults` section. Per-agent values override them.

Learning data is persisted to `mindroom_data/learning/<agent>.db`, so it survives container restarts when the storage directory is mounted.

## File-Based Context Loading

You can inject file content directly into an agent's role context without using a knowledge base.

`context_files` behavior:

- Paths are resolved relative to the config file directory
- Existing files are loaded in list order and added under `Personality Context`
- Missing files are skipped with a warning in logs

`memory_dir` behavior:

- Paths are resolved relative to the config file directory
- Loads `MEMORY.md` (uppercase) if present
- Loads dated files named `YYYY-MM-DD.md` for yesterday and today
- Content is added under `Memory Context`

This loading happens when the agent is created (and on config reload), not continuously on every message.

## Rich Prompt Agents

Certain agent names (the YAML key, not `display_name`) have built-in rich prompts:

`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`

When using these names, the built-in prompt replaces the `role` field and any custom `instructions` are ignored.

## Defaults

The `defaults` section sets fallback values for all agents. Any agent that omits a setting inherits the value from here.

```
defaults:
  tools: [scheduler]        # Tools added to every agent by default (set [] to disable)
  markdown: true             # Format responses as Markdown
  learning: true             # Enable Agno Learning
  learning_mode: always      # "always" or "agentic"
  show_stop_button: false    # Show a stop button while agent is responding (global-only, cannot be overridden per-agent)
```

To opt out a specific agent:

```
agents:
  researcher:
    display_name: Researcher
    role: Focus on deep research
    include_default_tools: false
    tools: [web_search]
```
