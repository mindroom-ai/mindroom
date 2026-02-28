---
icon: lucide/settings
---

# Configuration

MindRoom is configured through a `config.yaml` file. This section covers all configuration options.

## Configuration File

MindRoom searches for the configuration file in this order (first match wins):

1. `MINDROOM_CONFIG_PATH` environment variable (if set)
2. `./config.yaml` (current working directory)
3. `~/.mindroom/config.yaml` (home directory)

Data storage (`mindroom_data/`) is placed next to the config file by default.

You can also validate a specific file directly:

```bash
mindroom config validate --path /path/to/config.yaml
```

## Environment Variables

### Core

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_CONFIG_PATH` | Path to `config.yaml` | `./config.yaml` → `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH` | Data storage directory | `mindroom_data/` next to config |
| `MINDROOM_CONFIG_TEMPLATE` | Template to seed config from (for containers) | Same as config path |

### Matrix

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://localhost:8008` |
| `MATRIX_SERVER_NAME` | Server name for federation | _(derived from homeserver)_ |
| `MATRIX_SSL_VERIFY` | Verify SSL certificates | `true` |

### API Keys

Set the API key for each provider you use in `config.yaml`:

| Variable | Provider |
|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `OPENAI_API_KEY` | OpenAI |
| `GOOGLE_API_KEY` | Google (Gemini) |
| `OPENROUTER_API_KEY` | OpenRouter |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `CEREBRAS_API_KEY` | Cerebras |
| `GROQ_API_KEY` | Groq |
| `OLLAMA_HOST` | Ollama (host URL, not a key) |

### Sandbox Proxy

See [Sandbox Proxy](../deployment/sandbox-proxy.md) for the full list of `MINDROOM_SANDBOX_*` variables.

## Basic Structure

```yaml
# Agent definitions (at least one recommended)
agents:
  assistant:
    display_name: Assistant        # Required: Human-readable name
    role: A helpful AI assistant   # Optional: Description of purpose
    model: sonnet                  # Optional: Model name (default: "default")
    tools: [file, shell]           # Optional: Agent-specific tools (merged with defaults.tools)
    include_default_tools: true    # Optional: Per-agent opt-out for defaults.tools
    skills: []                     # Optional: List of skill names
    instructions: []               # Optional: Custom instructions
    rooms: [lobby]                 # Optional: Rooms to auto-join
    markdown: true                 # Optional: Override default (inherits from defaults section)
    sandbox_tools: [shell, file]   # Optional: Override default (inherits from defaults section)
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)
    memory_backend: file           # Optional: Per-agent memory backend override (mem0 or file)
    knowledge_bases: [docs]         # Optional: Assign one or more configured knowledge bases
    context_files:                 # Optional: Load files into role context at init/reload
      - ./openclaw_data/SOUL.md
      - ./openclaw_data/AGENTS.md
      - ./openclaw_data/USER.md
      - ./openclaw_data/IDENTITY.md
      - ./openclaw_data/MEMORY.md
      - ./openclaw_data/TOOLS.md
      - ./openclaw_data/HEARTBEAT.md
  researcher:
    display_name: Researcher
    role: Research and gather information
    model: sonnet
  writer:
    display_name: Writer
    role: Write and edit content
    model: sonnet
  developer:
    display_name: Developer
    role: Write code and implement features
    model: sonnet
  reviewer:
    display_name: Reviewer
    role: Review code and provide feedback
    model: sonnet

# Model configurations (at least a "default" model is recommended)
models:
  default:
    provider: anthropic            # Required: openai, anthropic, ollama, google, gemini, vertexai_claude, groq, cerebras, openrouter, deepseek
    id: claude-sonnet-4-5-latest     # Required: Model ID for the provider
  sonnet:
    provider: anthropic            # Required: openai, anthropic, ollama, google, gemini, vertexai_claude, groq, cerebras, openrouter, deepseek
    id: claude-sonnet-4-5-latest     # Required: Model ID for the provider
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

# Culture configurations (optional)
cultures:
  engineering:
    description: Follow clean code principles and write tests  # Shared principles
    agents: [developer, reviewer]  # Agents assigned (each agent can belong to at most one culture)
    mode: automatic                # automatic, agentic, or manual

# Router configuration (optional)
router:
  model: default                   # Optional: Model for routing (default: "default")

# Default settings for all agents (optional)
defaults:
  tools: [scheduler]               # Default: ["scheduler"] (added to every agent; set [] to disable)
  markdown: true                   # Default: true
  enable_streaming: true           # Default: true (stream responses via message edits)
  learning: true                   # Default: true
  learning_mode: always            # Default: always (or agentic)
  max_preload_chars: 50000         # Hard cap for preloaded context from context_files
  show_stop_button: false          # Default: false (global only, cannot be overridden per-agent)
  num_history_runs: null           # Number of prior runs to include (null = all)
  num_history_messages: null       # Max messages from history (null = use num_history_runs)
  compress_tool_results: true      # Compress tool results in history to save context
  enable_session_summaries: false  # AI summaries of older conversation segments (costs extra LLM call)
  max_tool_calls_from_history: null  # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true            # Default: true (show tool call details inline in responses)
  sandbox_tools: null              # Default: null (tool names to sandbox; null = use env var config, [] = disable)

# defaults.tools are appended to each agent's tools list with duplicates removed.
# Set agents.<name>.include_default_tools: false to opt out a specific agent.

# Memory system configuration (optional)
memory:
  backend: mem0                    # Global default backend (mem0 or file); agents can override with memory_backend
  embedder:
    provider: openai               # Default: openai
    config:
      model: text-embedding-3-small  # Default embedding model
      api_key: null                # Optional: From env var
      host: null                   # Optional: For self-hosted
  llm:                             # Optional: LLM for memory operations
    provider: ollama
    config: {}

# Knowledge base configuration (optional)
knowledge_bases:
  docs:
    path: ./knowledge_docs/default # Folder containing documents for this base
    watch: true                    # Reindex automatically when files change
    git:                           # Optional: Sync this folder from a Git repository
      repo_url: https://github.com/pipefunc/pipefunc
      branch: main
      poll_interval_seconds: 300
      skip_hidden: true
      include_patterns: ["docs/**"]  # Optional: root-anchored glob filters
      exclude_patterns: []
      credentials_service: github_private # Optional: service in CredentialsManager

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

# Internal MindRoom user account (optional)
mindroom_user:
  username: mindroom_user          # Set before first startup (localpart only)
  display_name: MindRoomUser       # Can be changed later

# Matrix room onboarding/discoverability (optional)
matrix_room_access:
  mode: single_user_private        # Default keeps invite-only/private behavior
  multi_user_join_rule: public     # In multi_user mode: public or knock
  publish_to_room_directory: false # Publish managed rooms in server room directory
  invite_only_rooms: []            # Room keys/aliases/IDs that stay invite-only/private
  reconcile_existing_rooms: false  # Explicit migration of existing managed rooms

# Authorization (optional)
authorization:
  global_users: []                 # Users with access to all rooms
  room_permissions: {}             # Keys: room ID (!id), full alias (#alias:domain), or managed room key (alias)
  default_room_access: false       # Default: false
  agent_reply_permissions: {}      # Per-agent/team/router (or '*') reply allowlists; supports globs like '*:example.com'

# Room-specific model overrides (optional)
# Keys are room aliases, values are model names from the models section
# Example: room_models: {dev: sonnet, lobby: gpt4o}
room_models: {}

# Non-MindRoom bot accounts to exclude from multi-human detection (optional)
# These accounts won't trigger the mention requirement in threads
bot_accounts:
  - "@telegram:example.com"

# Plugin paths (optional)
plugins: []

# Timezone for scheduled tasks (optional)
timezone: America/Los_Angeles      # Default: UTC
```

## Internal User Username

- Configure `mindroom_user.username` with the Matrix localpart you want before first startup.
- After the account is created, `mindroom_user.username` is locked and cannot be changed in-place.
- You can safely change `mindroom_user.display_name` at any time.

## Sections

- [Agents](agents.md) - Configure individual AI agents
- [Models](models.md) - Configure AI model providers
- [Teams](teams.md) - Configure multi-agent collaboration
- [Cultures](cultures.md) - Configure shared agent cultures
- [Router](router.md) - Configure message routing
- [Memory](../memory.md) - Configure memory providers and behavior
- [Knowledge Bases](../knowledge.md) - Configure file-backed knowledge bases
- [Voice](../voice.md) - Configure speech-to-text voice processing
- [Authorization](../authorization.md) - Configure user and room access control
- [Skills](../skills.md) - Skill format, gating, and allowlists
- [Plugins](../plugins.md) - Plugin manifest and tool/skill loading

## Notes

- All top-level sections are optional with sensible defaults, but at least one agent is recommended for Matrix interactions
- A model named `default` is required unless agents, teams, and the router all specify explicit non-`default` models
- Agents can set `knowledge_bases`, but each entry must exist in the top-level `knowledge_bases` section
- `agents.<name>.context_files` inject file-based context at agent creation/reload (see [Agents](agents.md))
- `memory.backend` sets the global memory default, and `agents.<name>.memory_backend` overrides it per agent
- `defaults.max_preload_chars` caps preloaded file context (`context_files`)
- When `authorization.default_room_access` is `false`, only users in `global_users` or room-specific `room_permissions` can interact with agents
- `authorization.agent_reply_permissions` can further restrict which users specific agents/teams/router will reply to
- `authorization.room_permissions` accepts room IDs, full room aliases, and managed room keys
- `matrix_room_access.mode` defaults to `single_user_private`; this preserves current private/invite-only behavior
- In `multi_user` mode, MindRoom sets managed room join rules and directory visibility from config
- Publishing to the room directory requires the managing service account (typically router) to have moderator/admin power in each room
- The `memory` system works out of the box with OpenAI; use `memory.llm` for memory summarization with a different provider
