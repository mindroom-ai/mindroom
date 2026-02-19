# Configuration

MindRoom is configured through a `config.yaml` file. This section covers all configuration options.

## Configuration File

MindRoom searches for the configuration file in this order (first match wins):

1. `MINDROOM_CONFIG_PATH` environment variable (if set)
1. `./config.yaml` (current working directory)
1. `~/.mindroom/config.yaml` (home directory)

Data storage (`mindroom_data/`) is placed next to the config file by default.

You can also validate a specific file directly:

```
mindroom config validate --path /path/to/config.yaml
```

## Environment Variables

### Core

| Variable                   | Description                                   | Default                                     |
| -------------------------- | --------------------------------------------- | ------------------------------------------- |
| `MINDROOM_CONFIG_PATH`     | Path to `config.yaml`                         | `./config.yaml` → `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH`    | Data storage directory                        | `mindroom_data/` next to config             |
| `MINDROOM_CONFIG_TEMPLATE` | Template to seed config from (for containers) | Same as config path                         |

### Matrix

| Variable             | Description                | Default                     |
| -------------------- | -------------------------- | --------------------------- |
| `MATRIX_HOMESERVER`  | Matrix homeserver URL      | `http://localhost:8008`     |
| `MATRIX_SERVER_NAME` | Server name for federation | *(derived from homeserver)* |
| `MATRIX_SSL_VERIFY`  | Verify SSL certificates    | `true`                      |

### API Keys

Set the API key for each provider you use in `config.yaml`:

| Variable             | Provider                     |
| -------------------- | ---------------------------- |
| `ANTHROPIC_API_KEY`  | Anthropic (Claude)           |
| `OPENAI_API_KEY`     | OpenAI                       |
| `GOOGLE_API_KEY`     | Google (Gemini)              |
| `OPENROUTER_API_KEY` | OpenRouter                   |
| `DEEPSEEK_API_KEY`   | DeepSeek                     |
| `CEREBRAS_API_KEY`   | Cerebras                     |
| `GROQ_API_KEY`       | Groq                         |
| `OLLAMA_HOST`        | Ollama (host URL, not a key) |

### Sandbox Proxy

See [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) for the full list of `MINDROOM_SANDBOX_*` variables.

## Basic Structure

```
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
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)
    knowledge_bases: [docs]         # Optional: Assign one or more configured knowledge bases
    context_files:                 # Optional: Load files into role context at init/reload
      - ./openclaw_data/SOUL.md
      - ./openclaw_data/AGENTS.md
      - ./openclaw_data/USER.md
      - ./openclaw_data/IDENTITY.md
      - ./openclaw_data/MEMORY.md
      - ./openclaw_data/TOOLS.md
      - ./openclaw_data/HEARTBEAT.md
    memory_dir: ./openclaw_data/memory  # Optional: Load MEMORY.md + dated files from this dir
    max_thread_messages: 80       # Optional: Per-agent thread-history cap (most recent N messages)

# Model configurations (at least a "default" model is recommended)
models:
  default:
    provider: anthropic            # Required: openai, anthropic, ollama, google, gemini, groq, cerebras, openrouter, deepseek
    id: claude-sonnet-4-5-latest     # Required: Model ID for the provider
  sonnet:
    provider: anthropic            # Required: openai, anthropic, ollama, google, gemini, groq, cerebras, openrouter, deepseek
    id: claude-sonnet-4-5-latest     # Required: Model ID for the provider
    host: null                     # Optional: Host URL (e.g., for Ollama)
    api_key: null                  # Optional: API key (usually from env vars)
    context_window: null           # Optional: Prompt budgeting window; defaults to 128000 if unset
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
  max_thread_messages: 50          # Default thread-history cap (most recent N messages)
  max_preload_chars: 50000         # Hard cap for preloaded context from context_files/memory_dir
  memory_flush:
    enabled: true
    threshold_percent: 80          # Flush trigger percentage of model context window
    timeout_seconds: 30
  show_stop_button: false          # Default: false (global only, cannot be overridden per-agent)

# defaults.tools are appended to each agent's tools list with duplicates removed.
# Set agents.<name>.include_default_tools: false to opt out a specific agent.

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

# Authorization (optional)
authorization:
  global_users: []                 # Users with access to all rooms
  room_permissions: {}             # Room-specific user permissions
  default_room_access: false       # Default: false

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

- [Agents](https://docs.mindroom.chat/configuration/agents/index.md) - Configure individual AI agents
- [Models](https://docs.mindroom.chat/configuration/models/index.md) - Configure AI model providers
- [Teams](https://docs.mindroom.chat/configuration/teams/index.md) - Configure multi-agent collaboration
- [Cultures](https://docs.mindroom.chat/configuration/cultures/index.md) - Configure shared agent cultures
- [Router](https://docs.mindroom.chat/configuration/router/index.md) - Configure message routing
- [Memory](https://docs.mindroom.chat/memory/index.md) - Configure memory providers and behavior
- [Knowledge Bases](https://docs.mindroom.chat/knowledge/index.md) - Configure file-backed knowledge bases
- [Voice](https://docs.mindroom.chat/voice/index.md) - Configure speech-to-text voice processing
- [Authorization](https://docs.mindroom.chat/authorization/index.md) - Configure user and room access control
- [Skills](https://docs.mindroom.chat/skills/index.md) - Skill format, gating, and allowlists
- [Plugins](https://docs.mindroom.chat/plugins/index.md) - Plugin manifest and tool/skill loading

## Notes

- All top-level sections are optional with sensible defaults, but you need at least one agent
- A model named `default` is required unless all agents/teams specify explicit models
- Agents can set `knowledge_bases`, but each entry must exist in the top-level `knowledge_bases` section
- `agents.<name>.context_files` and `agents.<name>.memory_dir` inject file-based context at agent creation/reload (see [Agents](https://docs.mindroom.chat/configuration/agents/index.md))
- `defaults.max_thread_messages` and `agents.<name>.max_thread_messages` control prompt thread-history length
- `defaults.max_preload_chars` caps preloaded file context (`context_files` + `memory_dir`)
- `defaults.memory_flush` controls pre-trim memory flush turns for agents with `memory_dir`
- `models.<name>.context_window` controls budgeting thresholds for trimming and flush triggers
- When `authorization.default_room_access` is `false`, only users in `global_users` or room-specific `room_permissions` can interact with agents
- The `memory` system works out of the box with OpenAI; use `memory.llm` for memory summarization with a different provider
