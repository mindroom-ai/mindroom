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
      - ./openclaw_data/AGENTS.md
      - ./openclaw_data/USER.md
      - ./openclaw_data/IDENTITY.md
      - ./openclaw_data/MEMORY.md
      - ./openclaw_data/TOOLS.md
      - ./openclaw_data/HEARTBEAT.md

    # Optional: directory-based memory context (MEMORY.md + dated files)
    memory_dir: ./openclaw_data/memory

    # Whether to include defaults.tools for this agent (default: true)
    include_default_tools: true

    # Response mode: "thread" (replies in Matrix threads) or "room" (plain room messages)
    thread_mode: thread

    # Tools to execute through the sandbox proxy (optional, inherits from defaults)
    sandbox_tools: [shell, file]

    # Allow this agent to read and modify its own config at runtime
    allow_self_config: false

    # History context controls (all optional, inherit from defaults)
    num_history_runs: null
    num_history_messages: null
    compress_tool_results: true
    enable_session_summaries: false
    max_tool_calls_from_history: null
```

## Configuration Options

| Option                        | Type   | Default     | Description                                                                                                                                                                                                                                                 |
| ----------------------------- | ------ | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `display_name`                | string | *required*  | Human-readable name shown in Matrix as the bot's display name                                                                                                                                                                                               |
| `role`                        | string | `""`        | System prompt describing the agent's purpose — guides its behavior and expertise                                                                                                                                                                            |
| `model`                       | string | `"default"` | Model name (must match a key in the `models` section)                                                                                                                                                                                                       |
| `tools`                       | list   | `[]`        | Agent-specific tool names (see [Tools](https://docs.mindroom.chat/tools/index.md)); effective tools are `tools + defaults.tools` with duplicates removed                                                                                                    |
| `include_default_tools`       | bool   | `true`      | When `true`, append `defaults.tools` to this agent's `tools`; set to `false` to opt this agent out                                                                                                                                                          |
| `skills`                      | list   | `[]`        | Skill names the agent can use (see [Skills](https://docs.mindroom.chat/skills/index.md))                                                                                                                                                                    |
| `instructions`                | list   | `[]`        | Extra lines appended to the system prompt after the role                                                                                                                                                                                                    |
| `rooms`                       | list   | `[]`        | Room aliases to auto-join; rooms are created if they don't exist                                                                                                                                                                                            |
| `markdown`                    | bool   | `null`      | When enabled, the agent is instructed to format responses as Markdown. Inherits from `defaults.markdown` (default: `true`)                                                                                                                                  |
| `learning`                    | bool   | `null`      | Enable [Agno Learning](https://docs.agno.com/agents/learning) — the agent builds a persistent profile of user preferences and adapts over time. Inherits from `defaults.learning` (default: `true`)                                                         |
| `learning_mode`               | string | `null`      | `always`: agent automatically learns from every interaction. `agentic`: agent decides when to learn via a tool call. Inherits from `defaults.learning_mode` (default: `"always"`)                                                                           |
| `knowledge_bases`             | list   | `[]`        | Knowledge base IDs from top-level `knowledge_bases` — gives the agent RAG access to the indexed documents                                                                                                                                                   |
| `context_files`               | list   | `[]`        | File paths loaded at agent init/reload and prepended to role context (under `Personality Context`)                                                                                                                                                          |
| `memory_dir`                  | string | `null`      | Directory loaded at agent init/reload for `MEMORY.md` and dated files (under `Memory Context`)                                                                                                                                                              |
| `thread_mode`                 | string | `"thread"`  | `thread`: responses are sent in Matrix threads (default). `room`: responses are sent as plain room messages with a single persistent session per room — ideal for bridges (Telegram, Signal, WhatsApp) and mobile                                           |
| `num_history_runs`            | int    | `null`      | Number of prior Agno runs to include as history context (`null` = all). Mutually exclusive with `num_history_messages`                                                                                                                                      |
| `num_history_messages`        | int    | `null`      | Max messages from history. Mutually exclusive with `num_history_runs`                                                                                                                                                                                       |
| `compress_tool_results`       | bool   | `null`      | Compress tool results in history to save context. Inherits from `defaults.compress_tool_results` (default: `true`)                                                                                                                                          |
| `enable_session_summaries`    | bool   | `null`      | Generate AI summaries of older conversation segments for compaction (each summary costs an extra LLM call). Inherits from `defaults.enable_session_summaries` (default: `false`)                                                                            |
| `max_tool_calls_from_history` | int    | `null`      | Limit tool call messages replayed from history (`null` = no limit)                                                                                                                                                                                          |
| `show_tool_calls`             | bool   | `null`      | Show tool call details inline in responses. Inherits from `defaults.show_tool_calls` (default: `true`). Set to `false` to hide `<tool>…</tool>` blocks (metadata is still tracked)                                                                          |
| `sandbox_tools`               | list   | `null`      | Tool names to execute through the [sandbox proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md). Inherits from `defaults.sandbox_tools` (default: `null` — defers to env vars). Set to `[]` to explicitly disable sandboxing for this agent |
| `allow_self_config`           | bool   | `null`      | Give this agent a scoped tool to read and modify its own configuration at runtime. Inherits from `defaults.allow_self_config` (default: `false`). Lighter-weight alternative to the `config_manager` tool                                                   |

Each entry in `knowledge_bases` must match a key under `knowledge_bases` in `config.yaml`.

Per-agent fields with a `null` default inherit from the `defaults` section at runtime. Per-agent values override them. `show_stop_button` and `enable_streaming` are global-only settings in `defaults` and cannot be overridden per-agent.

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
- Preloaded context is capped by `defaults.max_preload_chars`; if exceeded, truncation drops daily files first, then `MEMORY.md`, then personality files

This loading happens when the agent is created (and on config reload), not continuously on every message.

## Rich Prompt Agents

Certain agent names (the YAML key, not `display_name`) have built-in rich prompts:

`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`

When using these names, the built-in prompt replaces the `role` field and any custom `instructions` are ignored.

## Defaults

The `defaults` section sets fallback values for all agents. Any agent that omits a setting inherits the value from here.

```
defaults:
  tools: [scheduler]                   # Tools added to every agent by default (set [] to disable)
  markdown: true                        # Format responses as Markdown
  learning: true                        # Enable Agno Learning
  learning_mode: always                 # "always" or "agentic"
  max_preload_chars: 50000              # Hard cap for preloaded context from context_files/memory_dir
  show_stop_button: false               # Show a stop button while agent is responding (global-only, cannot be overridden per-agent)
  num_history_runs: null                # Number of prior runs to include (null = all)
  num_history_messages: null            # Max messages from history (null = use num_history_runs)
  enable_streaming: true                # Stream agent responses via progressive message edits
  compress_tool_results: true           # Compress tool results in history to save context
  enable_session_summaries: false       # AI summaries of older conversation segments (costs extra LLM call)
  max_tool_calls_from_history: null     # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true                 # Show tool call details inline in responses
  sandbox_tools: null                    # Tool names to sandbox (null = use env var config, [] = disable)
  allow_self_config: false               # Allow agents to read/modify their own config at runtime
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
