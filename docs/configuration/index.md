---
icon: lucide/settings
---

# Configuration

MindRoom is configured through a `config.yaml` file. This section covers all configuration options.

## Configuration File

The configuration file defaults to `./config.yaml`. To use a different path:

```bash
MINDROOM_CONFIG_PATH=/path/to/config.yaml mindroom run
```

(`CONFIG_PATH` is also supported for compatibility.)

You can also validate a specific file directly:

```bash
mindroom validate --config /path/to/config.yaml
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
    markdown: true                 # Optional: Override default (inherits from defaults section)
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)
    knowledge_bases: [docs]         # Optional: Assign one or more configured knowledge bases

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
  model: default                   # Optional: Model for routing (default: "default")

# Default settings for all agents (optional)
defaults:
  markdown: true                   # Default: true
  show_stop_button: false          # Default: false (global only, cannot be overridden per-agent)
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

# Authorization (optional)
authorization:
  global_users: []                 # Users with access to all rooms
  room_permissions: {}             # Room-specific user permissions
  default_room_access: false       # Default: false

# Room-specific model overrides (optional)
# Keys are room aliases, values are model names from the models section
# Example: room_models: {dev: sonnet, lobby: gpt4o}
room_models: {}

# Plugin paths (optional)
plugins: []

# Timezone for scheduled tasks (optional)
timezone: America/Los_Angeles      # Default: UTC
```

## Git-backed Knowledge Bases

Each knowledge base can optionally sync from Git by setting `knowledge_bases.<base_id>.git`.

- One knowledge base maps to one local folder and optional one Git repo.
- You can configure multiple knowledge bases, each with its own `git` settings.
- Sync behavior is: `git fetch` then `git reset --hard origin/<branch>`.
- Local uncommitted changes inside that checkout are discarded on sync.
- Git polling runs even when `watch: false`; `watch` controls only local filesystem watching.
- GitHub/GitLab webhooks are not part of this V1; updates are pull-based via polling.

### Git Fields

- `repo_url` (required): repository URL to clone/fetch.
- `branch` (default `main`): branch to track.
- `poll_interval_seconds` (default `300`, minimum `5`): polling interval.
- `credentials_service` (optional): service name in CredentialsManager for private HTTPS repos.
- `skip_hidden` (default `true`): skip files/folders with any path segment starting with `.`.
- `include_patterns` (optional): root-anchored glob patterns to include (for example `docs/**` or `content/post/*/index.md`).
- `exclude_patterns` (optional): root-anchored glob patterns excluded after include filtering.

### Pattern Semantics

- Patterns are matched from the repository root.
- `*` matches one path segment, `**` matches zero or more segments.
- If `include_patterns` is empty, all non-hidden files are eligible.
- If `include_patterns` is set, a file must match at least one include pattern.
- `exclude_patterns` are applied last and remove matching files.

### Private Repository Authentication

For private HTTPS repositories, set credentials under a service name and reference it via `credentials_service`.

```bash
curl -X POST http://localhost:8765/api/credentials/github_private \
  -H "Content-Type: application/json" \
  -d '{"credentials":{"username":"x-access-token","token":"ghp_your_token_here"}}'
```

You can also set credentials from the Dashboard **Credentials** tab. The service name must match `credentials_service`.

Expected credential fields for Git HTTPS auth:

- `username` + `token`
- `username` + `password`
- `api_key` (uses username `x-access-token` by default if no username is provided)

### Example: Clone Pipefunc, Index Only `docs/`

```yaml
knowledge_bases:
  pipefunc_docs:
    path: ./knowledge_docs/pipefunc
    watch: false
    git:
      repo_url: https://github.com/pipefunc/pipefunc
      branch: main
      poll_interval_seconds: 300
      include_patterns:
        - "docs/**"
```

## Sections

- [Agents](agents.md) - Configure individual AI agents
- [Models](models.md) - Configure AI model providers
- [Teams](teams.md) - Configure multi-agent collaboration
- [Router](router.md) - Configure message routing
- [Memory](../memory.md) - Configure memory providers and behavior
- [Knowledge](../dashboard.md#knowledge) - Configure file-backed knowledge bases
- [Voice](../voice.md) - Configure speech-to-text voice processing
- [Authorization](../authorization.md) - Configure user and room access control
- [Skills](../skills.md) - Skill format, gating, and allowlists
- [Plugins](../plugins.md) - Plugin manifest and tool/skill loading

## Notes

- All top-level sections are optional with sensible defaults, but you need at least one agent
- A model named `default` is required unless all agents/teams specify explicit models
- Agents can set `knowledge_bases`, but each entry must exist in the top-level `knowledge_bases` section
- When `authorization.default_room_access` is `false`, only users in `global_users` or room-specific `room_permissions` can interact with agents
- The `memory` system works out of the box with OpenAI; use `memory.llm` for memory summarization with a different provider
