---
icon: lucide/settings
---

# Configuration

MindRoom is configured through a `config.yaml` file. This section covers all configuration options.

## Configuration File

The configuration file is loaded from the current directory by default. You can specify a different path:

```bash
mindroom run --config /path/to/config.yaml
```

## Basic Structure

```yaml
# Agent definitions (at least one recommended)
agents:
  assistant:
    display_name: Assistant        # Required: Human-readable name
    role: A helpful AI assistant   # Optional: Description of purpose
    model: sonnet                  # Optional: Model name (default: "default")
    tools: [file, shell]           # Optional: List of tool names
    skills: []                     # Optional: List of skill names
    instructions: []               # Optional: Custom instructions
    rooms: [lobby]                 # Optional: Rooms to auto-join
    num_history_runs: 5            # Optional: Override default (inherits from defaults section)
    markdown: true                 # Optional: Override default (inherits from defaults section)
    add_history_to_messages: true  # Optional: Override default (inherits from defaults section)
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)

# Model configurations (at least a "default" model is recommended)
models:
  sonnet:
    provider: anthropic            # Required: openai, anthropic, ollama, google, gemini, groq, cerebras, openrouter, deepseek
    id: claude-sonnet-4-latest     # Required: Model ID for the provider
    host: null                     # Optional: Host URL (e.g., for Ollama)
    api_key: null                  # Optional: API key (usually from env vars)
    extra_kwargs: null             # Optional: Provider-specific parameters

# Team configurations (optional)
teams:
  research_team:
    display_name: Research Team    # Required: Human-readable name
    role: Collaborative research   # Required: Description of team purpose
    agents: [researcher, writer]   # Required: List of agent names
    mode: collaborate              # Optional: "coordinate" or "collaborate" (default: coordinate)
    model: sonnet                  # Optional: Model for team coordination (default: "default")
    rooms: []                      # Optional: Rooms to auto-join

# Router configuration (optional)
router:
  model: haiku                     # Optional: Model for routing (default: "default")

# Default settings for all agents (optional)
defaults:
  num_history_runs: 5              # Default: 5
  markdown: true                   # Default: true
  add_history_to_messages: true    # Default: true
  show_stop_button: false          # Default: false
  learning: true                   # Default: true
  learning_mode: always            # Default: always (or agentic)

# Memory system configuration (optional)
memory:
  embedder:
    provider: openai               # Default: openai
    config:
      model: text-embedding-3-small  # Default embedding model
      api_key: null                # Optional: From env var
      host: null                   # Optional: For self-hosted
  llm:                             # Optional: LLM for memory operations
    provider: ollama
    config: {}

# Voice message handling (optional)
voice:
  enabled: false                   # Default: false
  stt:
    provider: openai               # Default: openai
    model: whisper-1               # Default: whisper-1
    api_key: null
    host: null
  intelligence:
    model: default                 # Model for command recognition
    confidence_threshold: 0.7      # Default: 0.7

# Authorization (optional)
authorization:
  global_users: []                 # Users with access to all rooms
  room_permissions: {}             # Room-specific user permissions
  default_room_access: false       # Default: false

# Room-specific model overrides (optional)
room_models: {}

# Plugin paths (optional)
plugins: []

# Timezone for scheduled tasks (optional)
timezone: America/Los_Angeles      # Default: UTC
```

## Sections

- [Agents](agents.md) - Configure individual AI agents
- [Models](models.md) - Configure AI model providers
- [Teams](teams.md) - Configure multi-agent collaboration
- [Router](router.md) - Configure message routing
- [Skills](../skills.md) - Skill format, gating, and allowlists
- [Plugins](../plugins.md) - Plugin manifest and tool/skill loading

## Notes

- All top-level sections are optional with sensible defaults, but you need at least one agent
- A model named `default` is required unless all agents/teams specify explicit models
- When `authorization.default_room_access` is `false`, only users in `global_users` or room-specific `room_permissions` can interact with agents
- The `memory` system works out of the box with OpenAI; use `memory.llm` for memory summarization with a different provider
