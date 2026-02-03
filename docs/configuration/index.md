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
    num_history_runs: 5            # Optional: Override default history
    markdown: true                 # Optional: Override default markdown
    add_history_to_messages: true  # Optional: Include history in context

# Model configurations (at least a "default" model is recommended)
models:
  sonnet:
    provider: anthropic            # Required: openai, anthropic, ollama, google/gemini, groq, cerebras, openrouter, deepseek
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

## Memory Configuration

The memory system stores agent memories using embeddings. Configuration is optional - defaults work out of the box with OpenAI.

```yaml
memory:
  embedder:
    provider: openai               # Provider: openai, ollama, huggingface, etc.
    config:
      model: text-embedding-3-small  # Embedding model
      api_key: null                # From OPENAI_API_KEY env var
      host: null                   # For self-hosted (Ollama, llama.cpp)
  llm:                             # Optional: LLM for memory summarization
    provider: ollama
    config:
      model: llama3.2
      host: http://localhost:11434
```

## Voice Configuration

Enable voice message transcription and command recognition.

```yaml
voice:
  enabled: true                    # Enable voice processing
  stt:
    provider: openai               # STT provider
    model: whisper-1               # STT model
    api_key: null                  # From env var
    host: null                     # For self-hosted
  intelligence:
    model: default                 # Model for command recognition
    confidence_threshold: 0.7      # Command detection threshold
```

## Authorization Configuration

Control which Matrix users can interact with agents.

```yaml
authorization:
  # Users with access to all rooms
  global_users:
    - "@admin:example.com"
    - "@developer:example.com"

  # Room-specific permissions
  room_permissions:
    "!roomid:example.com":
      - "@user1:example.com"
      - "@user2:example.com"

  # Default access for rooms not explicitly configured
  default_room_access: false       # false = deny by default
```

When `default_room_access` is `false`, only users in `global_users` or the specific room's `room_permissions` can interact with agents.

## Required vs Optional Fields

### Required Fields

| Section | Field | Description |
|---------|-------|-------------|
| `agents.<name>` | `display_name` | Human-readable name shown in Matrix |
| `models.<name>` | `provider` | Model provider (openai, anthropic, etc.) |
| `models.<name>` | `id` | Model ID for the provider |
| `teams.<name>` | `display_name` | Human-readable team name |
| `teams.<name>` | `role` | Description of team purpose |
| `teams.<name>` | `agents` | List of agent names in the team |

### Optional Sections

All top-level sections are technically optional (with sensible defaults), but you need at least one agent for MindRoom to function:

- `agents` - Defaults to empty; at least one agent is needed for a functional setup
- `models` - A model named "default" is needed unless all agents/teams specify explicit models
- `teams` - Only needed for multi-agent collaboration
- `router` - Only needed to customize routing behavior
- `defaults` - All fields have sensible defaults
- `memory` - Memory system works with defaults
- `voice` - Disabled by default
- `authorization` - No restrictions by default
- `room_models` - No room-specific overrides by default
- `plugins` - No plugins by default
- `timezone` - Defaults to UTC
