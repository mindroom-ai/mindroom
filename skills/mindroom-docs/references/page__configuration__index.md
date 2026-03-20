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

| Variable                   | Description                                                                                                                                                                             | Default                                     |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `MINDROOM_CONFIG_PATH`     | Path to `config.yaml`                                                                                                                                                                   | `./config.yaml` → `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH`    | Data storage directory                                                                                                                                                                  | `mindroom_data/` next to config             |
| `MINDROOM_CONFIG_TEMPLATE` | Path to a config template. When set and `config.yaml` does not exist, MindRoom copies this template to the config path. Used in Docker containers to seed config from bundled templates | Same as config path                         |
| `LOG_LEVEL`                | Logging level for `mindroom run` (`DEBUG`, `INFO`, `WARNING`, `ERROR`)                                                                                                                  | `INFO`                                      |

### Matrix

| Variable             | Description                | Default                     |
| -------------------- | -------------------------- | --------------------------- |
| `MATRIX_HOMESERVER`  | Matrix homeserver URL      | `http://localhost:8008`     |
| `MATRIX_SERVER_NAME` | Server name for federation | *(derived from homeserver)* |
| `MATRIX_SSL_VERIFY`  | Verify SSL certificates    | `true`                      |

### API Keys

Set the API key for each provider you use in `config.yaml`:

| Variable             | Provider                                                            |
| -------------------- | ------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`  | Anthropic (Claude)                                                  |
| `OPENAI_API_KEY`     | OpenAI                                                              |
| `GOOGLE_API_KEY`     | Google (Gemini)                                                     |
| `OPENROUTER_API_KEY` | OpenRouter                                                          |
| `DEEPSEEK_API_KEY`   | DeepSeek                                                            |
| `CEREBRAS_API_KEY`   | Cerebras                                                            |
| `GROQ_API_KEY`       | Groq                                                                |
| `OLLAMA_HOST`        | Ollama (host URL, not a key)                                        |
| `OPENAI_BASE_URL`    | Base URL for OpenAI-compatible APIs (e.g., local inference servers) |

All API key variables also support a `_FILE` suffix for file-based secrets (e.g., `ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key`). See [Model Configuration — File-based Secrets](https://docs.mindroom.chat/configuration/models/#file-based-secrets) for details.

### Operational

| Variable                                             | Description                                                                                                                                                                                                                                      | Default                          |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------- |
| `MINDROOM_NAMESPACE`                                 | Installation namespace for Matrix identity isolation (4–32 lowercase alphanumeric chars)                                                                                                                                                         | *(none)*                         |
| `MINDROOM_PORT`                                      | Port used by Google OAuth callback URL construction and deployment tooling. Does **not** change the API server bind port — use `mindroom run --api-port` for that                                                                                | `8765`                           |
| `MINDROOM_API_KEY`                                   | API key for authenticating dashboard/API requests (`mindroom config init` auto-generates one; unset = open access)                                                                                                                               | *(none)*                         |
| `MINDROOM_ENABLE_AI_CACHE`                           | Enable the AI response cache (caches model responses keyed by model, messages, and tools — useful during development to avoid repeated API calls)                                                                                                | `true`                           |
| `MINDROOM_NO_AUTO_INSTALL_TOOLS`                     | Set to `1`/`true`/`yes` to disable automatic tool dependency installation                                                                                                                                                                        | *(unset — auto-install enabled)* |
| `MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS` | Seconds to wait for homeserver to become reachable at startup (0 = skip). MindRoom polls the homeserver's `/_matrix/client/versions` endpoint with exponential backoff retry, detecting permanent errors (e.g., wrong URL) vs transient failures | *(wait indefinitely)*            |
| `MINDROOM_WORKER_BACKEND`                            | Worker backend for tool execution (`static_runner`, `docker`, or `kubernetes`)                                                                                                                                                                   | `static_runner`                  |

### OpenAI-Compatible API

| Variable                              | Description                                                            | Default                                          |
| ------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------ |
| `OPENAI_COMPAT_API_KEYS`              | Comma-separated API keys for authenticating `/v1/*` requests           | *(none — locked without this or the flag below)* |
| `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | Set to `true` to allow unauthenticated `/v1/*` access (local dev only) | *(unset — locked)*                               |

See [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/index.md) for the full auth matrix.

### Provisioning / Pairing

These are set automatically by `mindroom connect` and stored in `.env`:

| Variable                       | Description                                              |
| ------------------------------ | -------------------------------------------------------- |
| `MINDROOM_PROVISIONING_URL`    | Provisioning service URL (e.g., `https://mindroom.chat`) |
| `MINDROOM_LOCAL_CLIENT_ID`     | Client ID from hosted pairing                            |
| `MINDROOM_LOCAL_CLIENT_SECRET` | Client secret from hosted pairing                        |

### Frontend / Development

| Variable                       | Description                                                 | Default            |
| ------------------------------ | ----------------------------------------------------------- | ------------------ |
| `MINDROOM_FRONTEND_DIST`       | Override path to pre-built frontend assets                  | *(auto-detected)*  |
| `MINDROOM_AUTO_BUILD_FRONTEND` | Set to `0` to skip automatic frontend build                 | *(enabled)*        |
| `DOCKER_CONTAINER`             | Set to `true` when running inside the packaged Docker image | *(unset)*          |
| `BROWSER_EXECUTABLE_PATH`      | Path to browser executable for the browser tool             | *(system default)* |

### Vertex AI

| Variable                         | Description                                  |
| -------------------------------- | -------------------------------------------- |
| `ANTHROPIC_VERTEX_PROJECT_ID`    | Google Cloud project ID for Vertex AI Claude |
| `ANTHROPIC_VERTEX_BASE_URL`      | Custom Vertex AI base URL                    |
| `CLOUD_ML_REGION`                | Google Cloud region for Vertex AI            |
| `GOOGLE_CLOUD_PROJECT`           | Google Cloud project ID                      |
| `GOOGLE_CLOUD_LOCATION`          | Google Cloud region                          |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google service account JSON          |

Authenticate with `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS`.

### Worker / Sandbox

| Variable                       | Description                                | Default  |
| ------------------------------ | ------------------------------------------ | -------- |
| `MINDROOM_SANDBOX_PROXY_URL`   | Sandbox proxy endpoint URL (static runner) | *(none)* |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Auth token for the sandbox proxy           | *(none)* |

See [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) for the full list of `MINDROOM_SANDBOX_*` variables, including Kubernetes backend variables (`MINDROOM_SANDBOX_KUBERNETES_*`).

### SaaS-Only

| Variable      | Description                                                     | Default  |
| ------------- | --------------------------------------------------------------- | -------- |
| `CUSTOMER_ID` | Tenant identity for worker key derivation (SaaS platform only)  | *(none)* |
| `ACCOUNT_ID`  | Account identity for worker key derivation (SaaS platform only) | *(none)* |

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
    worker_tools: [shell, file]    # Optional: Override default (inherits from defaults section)
    worker_scope: user_agent       # Optional: Reuse one proxied runtime per requester+agent
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)
    memory_backend: file           # Optional: Per-agent memory backend override (mem0 or file)
    knowledge_bases: [docs]         # Optional: Assign one or more configured knowledge bases
    context_files:                 # Optional: Load files into each freshly built agent instance
      - SOUL.md
      - AGENTS.md
      - USER.md
      - IDENTITY.md
      - TOOLS.md
      - HEARTBEAT.md
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
    id: claude-sonnet-4-6            # Required: Model ID for the provider
  sonnet:
    provider: anthropic            # Required: openai, anthropic, ollama, google, gemini, vertexai_claude, groq, cerebras, openrouter, deepseek
    id: claude-sonnet-4-6            # Required: Model ID for the provider
    host: null                     # Optional: Host URL (e.g., for Ollama)
    api_key: null                  # Optional: API key (usually from env vars)
    extra_kwargs: null             # Optional: Provider-specific parameters
    context_window: null           # Optional: Context window in tokens (enables auto history trimming)

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
  show_stop_button: true           # Default: true (global only, cannot be overridden per-agent)
  num_history_runs: null           # Number of prior runs to include (null = all)
  num_history_messages: null       # Max messages from history (null = use num_history_runs)
  compress_tool_results: true      # Compress tool results in history to save context
  enable_session_summaries: false  # AI summaries of older conversation segments (costs extra LLM call)
  max_tool_calls_from_history: null  # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true            # Default: true (show tool call details inline in responses)
  worker_tools: null               # Default: null (tool names to route through workers; null = use MindRoom's default routing policy, [] = disable)
  worker_scope: null               # Default: null (no runtime reuse; set shared/user/user_agent to enable)
  allow_self_config: false         # Default: false (allow agents to modify their own config via a tool)

# defaults.tools are appended to each agent's tools list with duplicates removed.
# Set agents.<name>.include_default_tools: false to opt out a specific agent.

# Memory system configuration (optional)
memory:
  backend: mem0                    # Global default backend (mem0 or file); agents can override with memory_backend
  team_reads_member_memory: false  # Default: false (when true, team reads can access member agent memories)
  embedder:
    provider: openai               # Default: openai (openai, ollama, huggingface, sentence_transformers)
    config:
      model: text-embedding-3-small  # Default embedding model
      api_key: null                # Optional: From env var
      host: null                   # Optional: For self-hosted
      dimensions: null             # Optional: Embedding dimension override (e.g., 256)
  llm:                             # Optional: LLM for memory operations
    provider: ollama
    config: {}
  file:                            # File-backed memory settings (when backend: file)
    path: null                     # Optional: fallback root for file memory paths
    max_entrypoint_lines: 200      # Default: 200 (max lines preloaded from MEMORY.md)
  auto_flush:                      # Background memory auto-flush (file backend only)
    enabled: false                 # Default: false (enable background flush worker)
    flush_interval_seconds: 1800   # Default: 1800 (loop interval)
    idle_seconds: 120              # Default: 120 (idle time before flush eligibility)
    max_dirty_age_seconds: 600     # Default: 600 (force flush after this many seconds dirty)
    stale_ttl_seconds: 86400       # Default: 86400 (drop stale flush-state entries older than this)
    max_cross_session_reprioritize: 5  # Default: 5 (same-agent dirty sessions reprioritized per prompt)
    retry_cooldown_seconds: 30     # Default: 30 (cooldown before retrying a failed extraction)
    max_retry_cooldown_seconds: 300  # Default: 300 (upper bound for retry cooldown backoff)
    batch:
      max_sessions_per_cycle: 10   # Default: 10 (max sessions processed per auto-flush loop)
      max_sessions_per_agent_per_cycle: 3  # Default: 3 (max sessions per agent per loop)
    extractor:
      no_reply_token: NO_REPLY     # Default: NO_REPLY (token indicating no durable memory)
      max_messages_per_flush: 20   # Default: 20 (max messages considered per extraction)
      max_chars_per_flush: 12000   # Default: 12000 (max chars considered per extraction)
      max_extraction_seconds: 30   # Default: 30 (timeout for one extraction job)
      include_memory_context:
        memory_snippets: 5         # Default: 5 (max MEMORY.md snippets for dedupe context)
        snippet_max_chars: 400     # Default: 400 (max chars per snippet)
#
# See docs/memory.md for full auto-flush behavior and tuning guidance.
#
# Set memory.embedder.provider: sentence_transformers to run embeddings in-process.
# MindRoom auto-installs that optional extra on first use.

# Knowledge base configuration (optional)
knowledge_bases:
  docs:
    path: ./knowledge_docs          # Folder containing documents for this base (Pydantic default)
    watch: true                    # Reindex automatically when files change
    chunk_size: 5000               # Default: 5000 (max characters per indexed chunk)
    chunk_overlap: 0               # Default: 0 (overlapping characters between chunks)
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
  visible_router_echo: false       # Optional: show the normalized voice text from the router
  stt:
    provider: openai               # Default: openai
    model: whisper-1               # Default: whisper-1
    api_key: null
    host: null
  intelligence:
    model: default                 # Model for command recognition

# Internal MindRoom user account (optional, omit for hosted/public profiles)
# When present, defaults are: username: mindroom_user, display_name: MindRoomUser
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
  aliases: {}                      # Map canonical Matrix user IDs to bridge aliases (see authorization docs)
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

# Matrix Space grouping (optional)
matrix_space:
  enabled: true                    # Default: true (create a root Matrix Space for managed rooms)
  name: MindRoom                   # Default: "MindRoom" (display name for the root Space)

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
- [Matrix Space](https://docs.mindroom.chat/matrix-space/index.md) - Configure the root Matrix Space for managed rooms
- [Skills](https://docs.mindroom.chat/skills/index.md) - Skill format, gating, and allowlists
- [Plugins](https://docs.mindroom.chat/plugins/index.md) - Plugin manifest and tool/skill loading

## Notes

- All top-level sections are optional with sensible defaults, but at least one agent is recommended for Matrix interactions
- A model named `default` is required unless agents, teams, and the router all specify explicit non-`default` models
- Agents can set `knowledge_bases`, but each entry must exist in the top-level `knowledge_bases` section
- `agents.<name>.context_files` load files from the agent's workspace into each agent instance, so edits take effect on the next reply without restarting (see [Agents](https://docs.mindroom.chat/configuration/agents/index.md))
- `agents.<name>.room_thread_modes` overrides `thread_mode` for specific rooms, and resolution is room-aware for agents, teams, and router decisions (see [Agents](https://docs.mindroom.chat/configuration/agents/index.md))
- `memory.backend` sets the global memory default, and `agents.<name>.memory_backend` overrides it per agent
- `defaults.max_preload_chars` caps preloaded file context (`context_files`)
- When `authorization.default_room_access` is `false`, only users in `global_users` or room-specific `room_permissions` can interact with agents
- `authorization.agent_reply_permissions` can further restrict which users specific agents/teams/router will reply to
- `authorization.aliases` maps bridge bot user IDs to canonical users so bridged messages inherit the same permissions (see [Authorization](https://docs.mindroom.chat/authorization/index.md))
- `authorization.room_permissions` accepts room IDs, full room aliases, and managed room keys
- `matrix_room_access.mode` defaults to `single_user_private`; this preserves current private/invite-only behavior
- In `multi_user` mode, MindRoom sets managed room join rules and directory visibility from config
- Publishing to the room directory requires the managing service account (typically router) to have moderator/admin power in each room
- The `memory` system works out of the box with OpenAI; use `memory.llm` for memory summarization with a different provider
