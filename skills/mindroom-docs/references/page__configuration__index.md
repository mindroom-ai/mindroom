# Configuration

MindRoom is configured through one `config.yaml` file, optionally split across included files.
This page covers where the file lives, the top-level sections, environment variables, and root-level settings that have no dedicated page.
The [Sections](#sections) list links to the pages for agents, models, teams, the router, memory, knowledge bases, and other features.

## Configuration File

MindRoom searches for the configuration file in this order (first match wins):

1. `MINDROOM_CONFIG_PATH` environment variable (if set)
2. `./config.yaml` (current working directory)
3. `~/.mindroom/config.yaml` (home directory)

Data storage (`mindroom_data/`) is placed next to the config file by default.
`config.yaml` is watched at runtime, and edits are applied by hot reload without restarting the stack.

Validate a specific file with:

```bash
mindroom config validate --path /path/to/config.yaml
```

## Basic Structure

All top-level sections are optional; configure at least one agent for conversational replies.
Keep `models.default` configured, because room topic generation and automatic thread summaries fall back to it even when agents, teams, and the router select other models.

```yaml
# Agent definitions (see agents.md)
agents:
  assistant:
    display_name: Assistant        # Required: Human-readable name
    role: A helpful AI assistant   # Optional: Description of purpose
    model: sonnet                  # Optional: Model name (default: "default")
    tools:                         # Optional: Agent-specific tools (merged with defaults.tools)
      - file
      - shell
      # - shell:                    # Per-agent tool config overrides (single-key dict):
      #     extra_env_passthrough: "DAWARICH_*"
    include_default_tools: true    # Optional: Per-agent opt-out for defaults.tools
    skills: []                     # Optional: List of skill names
    instructions: []               # Optional: Custom instructions
    rooms: [lobby]                 # Optional: Rooms to auto-join
    access:                        # Optional: Conversation access (omit to grant members of this agent's rooms)
      current_room_members: false  # Allow joined members of whatever room the message arrives in
      members_of_rooms: [lobby]    # Allow joined members of these managed rooms
      users: []                    # Matrix user IDs or glob patterns
    credential_managers: []        # Optional: Concrete Matrix IDs allowed to manage this agent's credentials
    accept_invites: true           # Accept all, none, or matching inviter ID patterns
    markdown: true                 # Optional: Override default (inherits from defaults section)
    worker_tools: [shell, file]    # Optional: Override default (inherits from defaults section)
    worker_scope: user_agent       # Optional: Reuse one proxied runtime per requester+agent
    file_access: workspace         # Optional: workspace or unrestricted (inherits from defaults section)
    learning: true                 # Optional: Override default (inherits from defaults section)
    learning_mode: always          # Optional: Override default (inherits from defaults section)
    memory_backend: file           # Optional: Per-agent memory backend override (mem0, file, or none)
    memory_search:                 # Optional: Per-agent file-memory search override
      mode: semantic               # keyword or semantic; omitted fields inherit memory.search
      include:
        - memory/**/*.md
      include_entrypoint: false
    knowledge_bases: [docs]        # Optional: Must be keys of the top-level knowledge_bases section
    context_files:                 # Optional: Workspace files loaded into each reply's context
      - SOUL.md
      - AGENTS.md
      - USER.md
  researcher:
    display_name: Researcher
    role: Research and gather information
    model: sonnet
  writer:
    display_name: Writer
    role: Write and edit content
    model: sonnet

# Model configurations (see models.md)
models:
  default:
    provider: anthropic            # Required: anthropic, azure, bedrock_claude, openai, codex, kimi, llama_cpp, ollama, google, gemini, vertexai_claude, groq, cerebras, openrouter, deepseek, zai, or synthetic
    id: claude-sonnet-5-5          # Required: Model ID for the provider
  sonnet:
    provider: anthropic
    id: claude-sonnet-5-5
    host: null                     # Optional: Host URL (e.g., for Ollama)
    api_key: null                  # Optional: Model-specific API key used instead of the provider's shared key
    extra_kwargs: null             # Optional: Provider-specific parameters
    context_window: null           # Optional: Needed on the active runtime model for replay safety; explicit compaction.model also needs its own window
    stream_idle_timeout_seconds: null  # Optional: Seconds without a provider event before a stream counts as stalled; unset = 300 on a provider's built-in endpoint, none for ollama, llama_cpp, or a configured endpoint; 0 disables

# Team configurations (see teams.md)
teams:
  research_team:
    display_name: Research Team    # Required: Human-readable name
    role: Collaborative research   # Required: Description of team purpose
    agents: [researcher, writer]   # Required: List of agent names
    mode: collaborate              # Optional: "coordinate" or "collaborate" (default: coordinate)
    model: sonnet                  # Optional: Model for team coordination (default: "default")
    num_history_runs: 8            # Optional: Team-scoped replay policy
    num_history_messages: null     # Optional: Mutually exclusive with num_history_runs
    max_tool_calls_from_history: 6 # Optional: Limit replayed tool call messages
    max_tool_calls_per_turn: 200   # Optional: Coordinator tool calls per team turn, delegations included; members keep their own budgets (default: defaults.max_tool_calls_per_turn)
    compaction:                    # Optional: Team-scoped compaction overrides
      enabled: true
      threshold_percent: 0.8
      reserve_tokens: 16384
      timeout_seconds: 600
    rooms: []                      # Optional: Rooms to auto-join

# Router configuration (see router.md)
router:
  model: default                   # Optional: Model for routing (default: "default")

# Default settings for all agents
defaults:
  tools:                           # Default: ["scheduler"] (appended to every agent's tools without duplicates; set [] to disable)
    - scheduler                    # Plain string or single-key dict with inline config overrides
  markdown: true                   # Default: true
  enable_streaming: true           # Default: true (stream responses via message edits; global only)
  large_message_strategy: sidecar  # Default: sidecar (or split: deliver oversized responses as lossless segmented messages)
  streaming:                       # Global only: streamed edit cadence
    update_interval: 5.0           # Default: 5.0 (steady-state seconds between streamed edits)
    min_update_interval: 0.5       # Default: 0.5 (fast-start seconds between early edits)
    interval_ramp_seconds: 15.0    # Default: 15.0 (set 0 to disable interval ramping)
    max_idle: 2.0                  # Default: 2.0 (event-driven idle ceiling before the next edit)
  learning: true                   # Default: true
  learning_mode: always            # Default: always (or agentic)
  max_preload_chars: 50000         # Hard cap for preloaded context from context_files
  max_tool_calls_per_turn: 1000    # Default: 1000 (tool calls one agent or team turn may execute; later calls return a tool error, and the turn ends with its text so far after this many plus two model requests)
  tool_output_auto_save_threshold_bytes: 51200  # Auto-save supported tool outputs larger than 50 KiB
  show_stop_button: true           # Default: true (global only, cannot be overridden per-agent)
  auto_resume_after_restart: true  # Default: true (see Automatic Restart Resumption below)
  num_history_runs: null           # Number of prior runs to include (null = all)
  num_history_messages: null       # Max messages from history (null = use num_history_runs)
  compress_tool_results: false     # Enabling can invalidate Anthropic/Vertex Claude prompt caches
  compaction:                      # Enabled by default; set enabled: false to disable automatic compaction (see agents.md)
    enabled: true
    threshold_percent: 0.8
    replay_window_tokens: null     # Optional operational cap; does not change the model's real context window
    reserve_tokens: 16384
    timeout_seconds: 600           # Maximum seconds allowed for each compaction summary request
  max_tool_calls_from_history: null  # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true            # Default: true (show tool details inline)
  worker_tools: null               # Default: null (tool names to route through workers; null = MindRoom's default routing policy, [] = disable)
  worker_scope: null               # Default: null (no runtime reuse; set shared/user/user_agent to enable)
  file_access: workspace           # Default: workspace (path tools stay in the agent workspace; unrestricted allows any path)
  worker_grantable_credentials: null  # Default: null (deny; list credential service names isolated workers may load, e.g. [openai, github_private])
  allow_self_config: false         # Default: false (allow agents to modify their own config via a tool)
  thread_summary_model: null       # Default: null (uses the default model)
  thread_summary_temperature: 0.2  # Default: 0.2 (set null to use provider defaults)
  thread_summary_first_threshold: 1  # Default: 1 (first automatic thread summary after first message)
  thread_summary_subsequent_interval: 10  # Default: 10 (messages between later automatic thread summaries)

# Memory system configuration (see memory.md)
memory:
  backend: mem0                    # Global default backend (mem0, file, or none); agents can override with memory_backend
  team_reads_member_memory: false  # Default: false (when true, team reads can access member agent memories)
  embedder:
    provider: openai               # Default: openai (openai, ollama, huggingface, sentence_transformers)
    config:
      model: text-embedding-3-small  # Default embedding model
      credentials_service: null     # Optional: strict named credential binding (e.g. embedder)
      api_key: null                # Optional: explicit embedder key
      host: null                   # Optional: For self-hosted
      dimensions: null             # Optional: Embedding dimension override (e.g., 256)
  llm:                             # Optional: LLM for memory operations
    provider: ollama
    config: {}
  file:                            # File-backed memory settings (when backend: file)
    path: null                     # Optional: fallback root for file memory paths
    max_entrypoint_lines: 200      # Default: 200 (max lines preloaded from MEMORY.md)
  search:                          # File-backed memory search settings
    mode: keyword                  # Default: keyword (keyword or semantic)
    include:                       # Root-relative globs below the effective file-memory root
      - memory/**/*.md             # Default: dated daily memory files
    include_entrypoint: false      # Default: false (MEMORY.md is already preloaded)
  auto_flush:                      # Background memory auto-flush (file backend only; tuning fields in memory.md)
    enabled: false                 # Default: false

# Knowledge base configuration (see knowledge.md)
knowledge_bases:
  docs:
    description: Product documentation, support notes, and operating procedures
    mode: semantic                 # Default: semantic (files skips embeddings and exposes workspace files)
    path: ./knowledge_docs         # Folder containing documents for this base
    watch: false                   # Direct external edits require reindex; API mutations still schedule refresh
    require_content_before_publish: false  # Keep a cold semantic index initializing until a managed file exists
    chunk_size: 5000               # Default: 5000 (max characters per indexed chunk)
    chunk_overlap: 0               # Default: 0 (overlapping characters between chunks)
    git:                           # Optional: Sync this folder from a Git repository
      repo_url: https://github.com/pipefunc/pipefunc
      branch: main
      poll_interval_seconds: 300   # Interval for background Git refresh scheduling
      credentials_service: github_private # Optional: service in CredentialsManager

# Voice message handling (see voice.md)
voice:
  enabled: false                   # Default: false
  visible_router_echo: true        # Optional: show router voice progress or direct fallback
  stt:
    provider: openai               # Default: openai
    model: gpt-transcribe          # Default: gpt-transcribe
    credentials_service: openai    # Named credential service for speech
    api_key: null
    host: null
  intelligence:
    model: default                 # Model for mention normalization and light ASR cleanup

# Voice calls via Element Call / MatrixRTC (see voice-calls.md)
calls:
  enabled: false                   # Default: false
  profiles:                        # Reusable backend-specific profiles (realtime or cascaded)
    openai-realtime:
      backend: realtime
      model: gpt-realtime-2.1
      credentials_service: openai
      voice: marin
  agents:                          # Profile name by enabled agent (at most one agent per room)
    assistant: openai-realtime
  livekit_service_url: null        # Optional override for .well-known discovery

# Internal MindRoom user account (optional, omit for hosted/public profiles)
mindroom_user:
  username: mindroom_user          # Default: mindroom_user (set before first startup; see Internal User Username)
  display_name: MindRoomUser       # Default: MindRoomUser (can be changed later)

# Access (see authorization.md)
administrators: []                 # Concrete Matrix IDs with platform and credential authority
room_defaults:
  join_policy: invite              # invite, knock, or public
  listed: false                    # Publish managed rooms in the room directory
  encrypted: false                 # Enable Matrix E2EE; enabling is irreversible
  invite_users: []                 # Automatic invitation roster only
  admins: []                       # Matrix room power level 100 only
authorization:
  config_command_enabled: false    # Enable !config for platform administrators
  aliases: {}                      # Map canonical Matrix user IDs to bridge aliases

# Managed rooms, created even before agents or teams are assigned
rooms:
  lobby:
    display_name: Lobby
    description: Main assistant room
    join_policy: invite             # Optional override
    listed: false                   # Optional override
    encrypted: false                # Optional override
    invite_users: []                # Replaces room_defaults.invite_users
    admins: []                      # Replaces room_defaults.admins

# Room-specific model overrides: room alias -> model name from models
# Room admins can override this at runtime with !room_model without modifying this file.
room_models: {}                    # Example: {dev: sonnet, lobby: gpt4o}

# Room-specific automatic thread summary models: room alias or raw Matrix room ID -> model name
room_thread_summary_models: {}     # Example: {lobby: haiku}

# Non-MindRoom bot accounts, such as bridge bots, that do not count as humans in multi-human threads
bot_accounts:
  - "@telegram:example.com"

# Plugin paths (see plugins.md)
plugins: []

# Matrix Space grouping (see matrix-space.md)
matrix_space:
  enabled: true                    # Default: true (create a root Matrix Space for managed rooms)
  name: MindRoom                   # Default: "MindRoom" (display name for the root Space)

# Matrix sync transport
matrix_sync:
  mode: classic                    # Default: classic (/v3/sync); sliding uses MSC4186 Simplified Sliding Sync on compatible homeservers
  sliding_timeline_limit: 100      # Per-room Sliding window, minimum 1
  max_response_bytes: 16777216     # Per-session HTTP response limit: 16 MiB
  max_pending_bytes: 67108864      # Per-session encoded pending output limit: 64 MiB

# Durable Matrix event journal (changes require restart)
event_journal:
  backend: sqlite                  # Default: sqlite; uses <storage>/tracking/event_journal.db

# Timezone for scheduled tasks (see scheduling.md)
timezone: America/Los_Angeles      # Default: UTC
scheduler_catch_up_grace_seconds: 3600  # Recurring catch-up window; 0 disables it
```

A durable store stays bound to the `matrix_sync.mode` it first used.
Publishing managed rooms to the room directory requires the managing service account (typically the router) to have moderator or admin power in each room.
Room reconciliation also lets ordinary members (power level 0) write thread tags, which requires the managing service account to be joined and able to update `m.room.power_levels`.
Monolithic configurations that still use retired access fields are migrated automatically when they load, while configurations using `!include` must be migrated manually; see [Authorization](https://docs.mindroom.chat/authorization/#automatic-migration).

## Splitting the Configuration Into Multiple Files

Large configs can be split across multiple files with Home-Assistant-style include tags.

| Tag | Result |
|-----|--------|
| `!include rel/path.yaml` | The parsed content of that YAML file, with nested include tags resolved recursively |
| `!expand rel/path.yaml` | Splices the file's YAML list into the surrounding list at this item |
| `!include_text rel/path.md` | The file's raw text as a string (UTF-8, one trailing newline stripped), for long prompt or instruction blocks |
| `!include_dir_list rel/dir` | A list with one item per YAML file in the directory |
| `!include_dir_named rel/dir` | A mapping of filename-without-extension to each file's parsed content |
| `!include_dir_merge_list rel/dir` | The concatenation of the lists contained in each file |
| `!include_dir_merge_named rel/dir` | The merge of the mappings contained in each file |

A realistic split keeps each agent in its own file and long prompts in Markdown:

```yaml
# config.yaml
agents: !include_dir_merge_named agents/
models: !include models.yaml

defaults:
  tools: [scheduler]
  markdown: true
```

```yaml
# agents/researcher.yaml
researcher:
  display_name: Researcher
  role: Deep research and analysis
  model: default
  tools: !include _shared/web_tools.yaml
  instructions:
    - !include_text ../prompts/researcher.md
```

```yaml
# agents/_shared/web_tools.yaml
- duckduckgo
- website
- browser
```

Relative paths resolve against the directory of the file containing the tag.
Absolute paths are rejected, and every include must stay inside the top-level config file's directory, so config edits cannot read arbitrary host files.
Explicit include paths may not contain hidden components starting with `.` (other than `.` and `..`), so `!include_text .env` fails.
Directory includes recurse into subdirectories, take only `.yaml`/`.yml` files in lexicographic order of their relative path, and skip files and directories whose name starts with `.` or `_`.
Use `_`-prefixed snippet files, such as `agents/_shared/web_tools.yaml` above, to share values between files, because YAML anchors cannot cross an include boundary.

### Expanding Shared Lists

Use `!expand` as a list item to reuse a shared list and add items for individual agents:

```yaml
# agents/researcher.yaml
researcher:
  role: Deep research and analysis
  tools:
    - !expand _shared/web_tools.yaml
    - calculator
```

With the `agents/_shared/web_tools.yaml` file above, this resolves to `tools: [duckduckgo, website, browser, calculator]`.
The compact form `tools: [!expand _shared/web_tools.yaml, calculator]` works too, and a list may contain several expansions anywhere among ordinary items.
Expansion preserves order and duplicates and splices only one level, so nested lists inside the shared list stay nested.
An ordinary `- !include tools.yaml` still inserts the included value as one item, even when that value is a list.
The expanded file must resolve to a YAML list: `[]` adds nothing, while an empty file, `null`, a scalar, or a mapping raises an error.
`!expand` is only valid as a list item; use `tools: !include tools.yaml` to replace an entire list.

### Include Errors and Hot Reload

- Include cycles are rejected with the full chain (`config.yaml -> agents/a.yaml -> config.yaml`).
- Missing files and directories are reported with the including file and line.
- `!include_dir_merge_named` raises on duplicate keys across files, naming both files, instead of letting the later file win as Home Assistant does.
- `!include_dir_named` raises on duplicate filename stems across subdirectories.
- An empty included file resolves to `null` under `!include` and contributes nothing to directory includes.

Editing any file in the include tree triggers the same hot reload as editing `config.yaml`, about one second after the files stop changing.
Deleting an included file does not trigger a reload by itself; remove its `!include` reference too, and that edit triggers the reload.
After a failed reload, fixing any file the failed attempt read triggers a retry.

When migrating an existing monolith, diff the fully merged config before and after the split:

```bash
mindroom config resolve > before.yaml
# split config.yaml into include files
mindroom config resolve > after.yaml
diff before.yaml after.yaml
```

When the configuration uses include tags, structured saves from the dashboard and self-config tools are rejected with a request to edit the source files instead; the raw config editor keeps editing the top-level file's text.
See [Dashboard](https://docs.mindroom.chat/dashboard/) for the API responses.
`MINDROOM_CONFIG_TEMPLATE` seeding copies only the single template file, so bundled templates that use includes must ship the whole directory.

## MCP Servers

MindRoom can connect to external Model Context Protocol servers through the top-level `mcp_servers` block.
See [MCP](https://docs.mindroom.chat/mcp/) for transport-specific config, tool naming, examples, and agent setup.

## Adaptive Participation and Mid-Turn Coalescing

Agents can opt into answering untagged messages in multi-human threads with `agents.<name>.participation`, and into finishing their active task before handling queued messages with `agents.<name>.mid_turn`.
See [Adaptive Participation](https://docs.mindroom.chat/configuration/agents/#adaptive-participation) and [Mid-Turn Coalescing](https://docs.mindroom.chat/configuration/agents/#mid-turn-coalescing).

## Tool Approval

<video controls playsinline preload="metadata" aria-label="An agent pauses for approval before it books a meeting" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/6a2033ea-3354-4afd-9d58-fc617b18cd24#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/d62d98e8-c066-4e1f-8a8f-d840da7b0bd1#t=0.1" type="video/mp4">
</video>

Use the top-level `tool_approval` block to gate tool calls behind human approval in Matrix conversations.
This partial example gates Slack message sending and file uploads, plus shell calls selected by a review script.
It does not gate every Slack operation, and the same function names in other toolkits also match.

```yaml
tool_approval:
  default: auto_approve
  timeout_days: 7
  rules:
    - match: send_message*
      action: require_approval
    - match: upload_file
      action: require_approval
    - match: run_shell_command
      script: ./approval_scripts/shell_review.py
      timeout_days: 3
```

`default` (`auto_approve` or `require_approval`) applies to calls that no rule matches.
Rules are evaluated in order and the first matching rule wins.
`match` is a case-sensitive glob over exposed function names, not toolkit identifiers.
Each rule must set exactly one of `action` or `script`.
`action: require_approval` always pauses the call and sends a Matrix approval card.
`script` names a Python file defining `check(tool_name, arguments, agent_name) -> bool`, and approval is required only when it returns `True`.
`timeout_days` sets the default approval expiry window and can be overridden per rule; an expired card can take up to about a minute to show its expired state.

### Approving and Denying

- React to the approval card with `✅` to approve the tool call.
- Reply to the card to deny the call; the start of the reply, up to 2,000 plain ASCII characters or fewer with characters such as emoji, is recorded as the denial reason.
- Only the original human requester can approve or deny their pending call.
- Eligible cards also offer auto-approval for 5, 10, or 30 minutes.
  It covers the original call, matching pending calls, and later calls for the same room, thread, requester, agent, and exact tool operation, with any arguments, without further cards.
  For generic MCP dispatch, the server and remote tool name are part of the operation.
  Timed approval needs a thread and complete reviewable arguments; native tool-authored confirmations and background-script approvals stay per-call.
  The grant deadline is fixed when the call is accepted and survives restarts.
  The original requester can stop auto-approval from the originating card, and changes to configured bindings or room membership end matching grants.
  Expiry or stopping auto-approval does not cancel calls already approved.

While approval is pending, the agent stops typing and the conversation can continue.
Pending approvals survive restarts and config reloads, and an approved call resumes the paused response.
The approved call runs only with the exact arguments shown on the card; if the saved call no longer matches, it fails without running.
Tool calls authored by agents, the system, or configured bridge bots are denied instead of entering the approval flow.
An agent acting on a request relayed by another agent's reply asks the original human for approval.
See [Agents](https://docs.mindroom.chat/configuration/agents/#configuration-options) when approval fails because the router is not in the room.

### Approval Card Contents

Cards show a redacted preview of the tool arguments, and clients can expand the complete redacted arguments when the preview is truncated.
Fields whose names mark them as secret, such as `password`, `credentials`, or an `Authorization` header, are hidden while their field name stays visible.
Inside text, only credentials in known token formats, such as `sk-…`, `ghp_…`, `github_pat_…`, `xoxb-…`, `AIza…`, and JSON Web Tokens, are replaced by numbered placeholders like `⟦secret-1⟧`; equal tokens get the same number.
Other inline secrets, such as `export DB_PASSWORD=hunter2` or an inline `Authorization: Basic …` header, stay visible, so keep secrets in credentials or secret-named fields.
Deny the call when a hidden field or a placeholder where a command, host, path, or delimiter belongs could change what runs.
A card cannot be approved when its complete redacted arguments exceed 2 MB, nest deeper than 32 levels, or fail to upload; approving it then denies the call.

### Tools That Cannot Pause for Approval

The OpenAI-compatible `/v1/chat/completions` API has no approval transport, so it hides every tool function that matches a required-approval rule, including script rules.
Skill, knowledge-search, and learning functions such as `get_skill_script`, `search_knowledge_base`, and `update_user_memory` cannot pause for approval on any channel, so they are hidden when a required-approval rule or `default: require_approval` applies to them.
Add an `auto_approve` rule for such a function to keep it available.
When knowledge search or all skill functions are hidden, the agent's prompt stops describing them; minimal mode still includes skill contents as context documents.

## Environment Variables

### Core

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_CONFIG_PATH` | Path to `config.yaml` | `./config.yaml` → `~/.mindroom/config.yaml` |
| `MINDROOM_STORAGE_PATH` | Data storage directory | `mindroom_data/` next to config |
| `MINDROOM_SESSION_STORAGE_PATH` | Dedicated root for agent and team session SQLite databases; relative paths resolve from the config directory, and other state stays under `MINDROOM_STORAGE_PATH` | `MINDROOM_STORAGE_PATH` |
| `MINDROOM_CONFIG_TEMPLATE` | Template copied to the config path when `config.yaml` does not exist, used by Docker images to seed config | Same as config path |
| `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` | Base64-encoded 32-byte key for encrypted-at-rest credential files (see [Credential Storage Encryption](#credential-storage-encryption)) | unset |
| `LOG_LEVEL` | Logging level for `mindroom run` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `MINDROOM_LOGGER_LEVELS` | Comma- or semicolon-separated logger level overrides, for example `mindroom:DEBUG,httpx:WARNING,nio:WARNING` | unset |
| `MINDROOM_LOG_FORMAT` | `text` for readable logs or `json` for structured logs | `text` |
| `NO_COLOR` | Any nonempty value disables console colors and styled tracebacks; installed background services set this to `1` | unset |

Set `LOG_LEVEL`, `MINDROOM_LOGGER_LEVELS`, `MINDROOM_LOG_FORMAT`, and `NO_COLOR` in the process environment, because the config-adjacent `.env` does not supply these logging controls.
Container `env_file`/`--env-file` injection sets process variables and therefore works.
`mindroom run --log-level` takes precedence over `LOG_LEVEL`.

### Matrix

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://localhost:8008` |
| `MATRIX_SERVER_NAME` | Server name for federation | _(derived from homeserver)_ |
| `MATRIX_SSL_VERIFY` | Verify the TLS certificate of `MATRIX_HOMESERVER`; provisioning service requests and other homeservers are always verified | `true` |
| `MINDROOM_DESKTOP_MATRIX_HOMESERVER` | Public Matrix URL printed by `!desktop setup` when it differs from the runtime's internal homeserver URL | `MATRIX_HOMESERVER` |
| `MINDROOM_DESKTOP_CLOUDFLARE_ACCESS` | Include `--cloudflare-access` in the Desktop login and pairing commands printed by `!desktop setup` | `false` |

### API Keys

Set the API key for each provider you use in `config.yaml`:

| Variable | Provider |
|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint |
| `AZURE_OPENAI_API_VERSION` | Optional Azure OpenAI API version override |
| `OPENAI_API_KEY` | OpenAI |
| `GOOGLE_API_KEY` | Google (Gemini) |
| `OPENROUTER_API_KEY` | OpenRouter |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `ZAI_API_KEY` | Z.ai (GLM models) |
| `CEREBRAS_API_KEY` | Cerebras |
| `GROQ_API_KEY` | Groq |
| `OLLAMA_HOST` | Ollama (host URL, not a key) |
| `EMBEDDER_API_KEY` | Dedicated semantic-search embedder key (optional; falls back to the shared `OPENAI_API_KEY`) |
| `OPENAI_BASE_URL` | Base URL for OpenAI-compatible APIs (e.g., local inference servers) |
| `TYPESAFE_API_KEY` | System One judgments (`judgment.provider: typesafe`) |

For `provider: openai` models, `OPENAI_BASE_URL` can come from the config-adjacent `.env` or the process environment, and the process environment wins.
A model's `extra_kwargs.base_url` overrides this setting, and `extra_kwargs.client_params.base_url` overrides the model endpoint when constructing SDK clients.

**Automatic credential import**: When MindRoom starts or `mindroom doctor` runs, it copies these variables from the process environment or the config-adjacent `.env` into the shared credentials store: `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `OLLAMA_HOST`, `GITHUB_TOKEN` (stored as `github_private`), `EMBEDDER_API_KEY` (stored as `embedder`), and `GOOGLE_APPLICATION_CREDENTIALS` (stored as `google_vertex_adc`).
Every one of these except `GOOGLE_APPLICATION_CREDENTIALS` also accepts a `_FILE` variant for file-based secrets (e.g., `ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key`), read only when the plain variable is unset or empty; see [Model Configuration — File-based Secrets](https://docs.mindroom.chat/configuration/models/#file-based-secrets).
Services declared through [Credential Seeds](#credential-seeds) are imported the same way.
MindRoom logs a notice naming the service and the variable that supplied it when a stored value is first imported or changes, and never logs the value.
Environment import never overwrites credentials saved through the dashboard.
To stop an import, remove the variable and any `_FILE` variant from both the process environment and `.env`, then delete the stored credential in the dashboard or with `DELETE /api/credentials/{service}`.

### Codex CLI Subscription Auth

The `codex` provider does not use an API key environment variable.
Run `codex login` so `~/.codex/auth.json` contains ChatGPT OAuth tokens.
Set `CODEX_HOME` only if your Codex CLI state lives outside `~/.codex`.

### Operational

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_NAMESPACE` | Installation namespace for Matrix identity isolation (4–32 lowercase alphanumeric chars) | _(none)_ |
| `MINDROOM_PORT` | Port used by Google OAuth callback URL construction and deployment tooling; the API server bind port is set with `mindroom run --api-port` instead | `8765` |
| `MINDROOM_API_KEY` | API key for dashboard/API requests; `mindroom config init` and first-run `mindroom run` add a generated key to `.env` when it has none; unset or empty means open access | _(none)_ |
| `MINDROOM_DASHBOARD_ALLOWED_HOSTS` | Comma-separated extra host names that unauthenticated dashboard or `/v1` requests and their browser `Origin` may name; loopback names, IP addresses, and the hosts of `MINDROOM_PUBLIC_URL`, `MINDROOM_BASE_URL`, `MINDROOM_URL`, and `MINDROOM_SCRIPT_GATEWAY_URL` are always allowed | _(none)_ |
| `MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed credentialed dashboard CORS responses; cookie and trusted-upstream mutations still require the app's own origin | `http://localhost:3003`, `http://localhost:5173`, `http://127.0.0.1:3003`, `http://127.0.0.1:5173` |
| `MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS` | `true` allows every dashboard API origin while disabling credentialed CORS responses; without dashboard authentication, origins outside `MINDROOM_DASHBOARD_ALLOWED_HOSTS` are still refused | _(unset)_ |
| `MINDROOM_NO_AUTO_INSTALL_TOOLS` | `1`/`true`/`yes` disables automatic tool dependency installation | _(unset — auto-install enabled)_ |
| `MINDROOM_MATRIX_HOMESERVER_STARTUP_TIMEOUT_SECONDS` | Seconds to wait at startup for the homeserver to answer `/_matrix/client/versions` (`0` = wait indefinitely); a certificate verification failure stops startup immediately (see [Matrix TLS trust](https://docs.mindroom.chat/architecture/matrix/#tls-trust)) | _(wait indefinitely)_ |
| `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` | Positive seconds allowed for the first Matrix sync response | `600` |
| `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS` | Positive seconds the sync watchdog and `/api/health` keep waiting while event catch-up is still making progress; set it above the deployment's observed healthy catch-up time | `600` |
| `MINDROOM_SCRIPT_GATEWAY_URL` | Complete worker-reachable background-script gateway base URL, including `/api/script-gateway`; required for Kubernetes, and for Docker unless a reachable `MINDROOM_PUBLIC_URL` is configured | _(none)_ |
| `MINDROOM_SCRIPT_GATEWAY_PORT` | Port for a second listener, bound to the `--api-host` address, that serves only `/api/script-gateway` so Kubernetes script workers can reach the gateway without the general API | _(none)_ |
| `MINDROOM_SCRIPT_GATEWAY_ISOLATED` | Operator attestation that the Kubernetes worker's script-gateway listener exposes only `/api/script-gateway`; required for Kubernetes background scripts and does not create network isolation itself | `false` |
| `MINDROOM_KUBERNETES_DEFAULT_SCRIPT_RESOURCE_PROFILE` | Default Kubernetes background-script profile (`small`, `standard`, or `large`) when `start_script` omits `resource_profile` | `small` |
| `MINDROOM_KUBERNETES_SCRIPT_RESOURCE_PROFILES_JSON` | JSON object defining exact CPU and memory requests and limits for the `small`, `standard`, and `large` background-script profiles | Built-in bounded profiles |
| `MINDROOM_SCRIPT_RETENTION_SECONDS` | Positive seconds to retain finished background-script runs, tool-call receipts, and approval records before pruning | `2592000` (30 days) |
| `MINDROOM_WORKER_BACKEND` | Worker backend for tool execution (`static_runner`, `docker`, or `kubernetes`) | `static_runner` |

See [Background Scripts](https://docs.mindroom.chat/tools/background-scripts/) for the script gateway setup.

### OpenAI-Compatible API

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_COMPAT_API_KEYS` | Comma-separated API keys for authenticating `/v1/*` requests | _(none — locked without this or the flag below)_ |
| `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED` | `true` allows unauthenticated `/v1/*` access (local dev only); such requests must address an allowed host (see `MINDROOM_DASHBOARD_ALLOWED_HOSTS`) and must not come from another site's page | _(unset — locked)_ |

See [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/) for the full auth matrix.

### Provisioning / Pairing

These are set automatically by `mindroom connect` and stored in `.env`:

| Variable | Description |
|----------|-------------|
| `MINDROOM_PROVISIONING_URL` | Provisioning service URL (e.g., `https://mindroom.chat`) |
| `MINDROOM_LOCAL_CLIENT_ID` | Client ID from hosted pairing |
| `MINDROOM_LOCAL_CLIENT_SECRET` | Client secret from hosted pairing |

### Frontend / Development

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_FRONTEND_DIST` | Override path to pre-built frontend assets | _(auto-detected)_ |
| `MINDROOM_AUTO_BUILD_FRONTEND` | Set to `0` to skip automatic frontend build | _(enabled)_ |
| `DOCKER_CONTAINER` | Set to `true` when running inside the packaged Docker image | _(unset)_ |
| `BROWSER_EXECUTABLE_PATH` | Path to browser executable for the browser tool | _(system default)_ |

### Vertex AI

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_VERTEX_PROJECT_ID` | Google Cloud project ID for Vertex AI Claude |
| `ANTHROPIC_VERTEX_BASE_URL` | Custom Vertex AI base URL |
| `CLOUD_ML_REGION` | Google Cloud region for Vertex AI |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID |
| `GOOGLE_CLOUD_LOCATION` | Google Cloud region |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to Google service account JSON |

Authenticate with `gcloud auth application-default login` or set `GOOGLE_APPLICATION_CREDENTIALS`.

### Worker / Sandbox

| Variable | Description | Default |
|----------|-------------|---------|
| `MINDROOM_SANDBOX_PROXY_URL` | Sandbox proxy endpoint URL (static runner) | _(none)_ |
| `MINDROOM_SANDBOX_PROXY_TOKEN` | Auth token for the sandbox proxy | _(none)_ |

See [Sandbox Proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/) for the full list of `MINDROOM_SANDBOX_*` variables and Kubernetes backend variables (`MINDROOM_KUBERNETES_WORKER_*`), and for `defaults.worker_grantable_credentials`.

### SaaS-Only

| Variable | Description | Default |
|----------|-------------|---------|
| `CUSTOMER_ID` | Tenant identity for worker key derivation (SaaS platform only) | _(none)_ |
| `ACCOUNT_ID` | Account identity for worker key derivation (SaaS platform only) | _(none)_ |

## Credential Seeds

MindRoom can create shared credential services at startup from explicit seed declarations, for deployment-managed credentials.
Set `MINDROOM_CREDENTIAL_SEEDS_FILE` to a JSON file path (relative paths resolve from the config directory), or `MINDROOM_CREDENTIAL_SEEDS_JSON` to equivalent inline JSON.
Credential fields can read from env vars, from files, or from literal values:

```json
[
  {
    "service": "example_oauth_client",
    "credentials": {
      "client_id": {"env": "EXAMPLE_CLIENT_ID"},
      "client_secret": {"env": "EXAMPLE_CLIENT_SECRET"}
    }
  }
]
```

If an env ref such as `EXAMPLE_CLIENT_SECRET` is unset, MindRoom also checks `EXAMPLE_CLIENT_SECRET_FILE` and reads that file.
If any declared field is missing or empty, MindRoom skips that seed instead of creating a partial credential.
Env and file refs always produce strings, so a tool's boolean field accepts the exact strings `true` and `false` as well as JSON booleans, and any other value makes that tool fail to load.
Seeded credentials are updated on later startups, but seeds only replace credentials previously imported from the environment; credentials saved through the dashboard or with no recorded source are preserved.
OAuth token services whose names end with `_oauth` cannot be seeded; connect them through OAuth instead.
OAuth client configuration services whose names end with `_oauth_client` can be seeded.
To stop seeding one service, remove its entry from each declaration that lists it and delete the stored credential with `DELETE /api/credentials/{service}`.

## Credential Storage Encryption

Set `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` to encrypt stored credential files at rest.
The key must be a base64-encoded 32-byte value and must stay stable across restarts.
Generate one with:

```bash
python - <<'PY'
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
PY
```

With encryption enabled, credential files are written with mode `0600` in directories with mode `0700`.
Existing plaintext credential files become unreadable and cannot be overwritten, so back them up and recreate them under encryption, or remove the key to read them again.
Encrypted credentials become readable again when their correct key is restored.
Dedicated Docker and Kubernetes workers never receive the key; see [credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases) for how tool settings reach them.

## Event Journal

`event_journal.backend` defaults to `sqlite`, which stores the durable Matrix event journal at `<storage>/tracking/event_journal.db`.
SQLite has no separate path setting.
To use PostgreSQL, install the `postgres` extra (for example `uvx --from 'mindroom[postgres]' mindroom run`, or `--extra postgres` when syncing a source checkout), then select the backend and provide a connection URL:

```yaml
event_journal:
  backend: postgres
  database_url_env: MINDROOM_EVENT_CACHE_DATABASE_URL
```

`MINDROOM_EVENT_CACHE_DATABASE_URL` is the default variable name, read from the process environment or the config-adjacent `.env`, with the process environment winning.
Alternatively, `event_journal.database_url` supplies an inline URL that takes precedence over the variable.
Custom `database_url_env` names must be `DATABASE_URL` or end in `_DATABASE_URL`.
A URL alone does not switch a SQLite journal to PostgreSQL; `backend: postgres` is required.
Changes to the effective store apply after restarting MindRoom.
See the [journal binding and migration commands](https://docs.mindroom.chat/cli/#journal) before moving, restoring, or adopting a journal.

## Automatic Restart Resumption

`defaults.auto_resume_after_restart` defaults to `true`, so after a restart the router posts resume prompts in eligible threads whose conversations were interrupted.
Set it to `false` to suppress those prompts and resume the work manually.
Work that was superseded by later messages, or whose requester or room membership no longer applies, is not resumed.

## Matrix Sync Limits

`matrix_sync.max_response_bytes` and `matrix_sync.max_pending_bytes` accept positive integers in bytes and apply to both Classic and Sliding Sync.
The defaults are 16 MiB per HTTP response and 64 MiB of encoded pending output per sync session.
If a large initial sync exceeds the response bound, increase `max_response_bytes` enough to fit the response.
If preparing events exceeds the pending bound, increase `max_pending_bytes`; encoded events can take more space than the HTTP response, so raising the response limit may also require raising the pending limit.
Each bot has its own sync session, so allow sufficient memory and disk space when increasing these limits.
Changing either setting through config reload restarts running agents.
Large initial syncs may also need more startup time: `MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS` sets the first-sync allowance, and the runtime Helm chart's `probes.startup` values set the Kubernetes startup probe allowance.

## Debug Logging

Every completed model request emits a structured `LLM usage` log event with provider and model identifiers and, when the provider reports usage, input tokens, uncached input tokens, and a cache-read ratio, without prompt content.
When usage is unavailable, the event sets `usage_available` to false.

Set `debug.log_llm_requests: true` to log provider requests as JSONL under `debug.llm_request_log_dir` (default `mindroom_data/logs/llm_requests`) for troubleshooting.
Those records include prompts, messages, the final tool array sent to the provider, model parameters, correlation IDs, requester metadata, and source Matrix event metadata.
The same flag records successful tool calls in `mindroom_data/tracking/tool_calls.jsonl`, with timing for each call; tool failures are always recorded there.
Credential-bearing fields such as tokens, cookies, passwords, API keys, and authorization headers are redacted, but these files can still contain sensitive prompt, argument, and result data, so leave the flag disabled unless you are actively debugging.

Export `MINDROOM_TIMING=1` in the process environment before startup to emit structured debug timing events for tool calls and one INFO-level `Dispatch pipeline timing` summary per turn, including `time_to_model_request_ms` and context, queue, payload, agent-build, and model spans.
The config-adjacent `.env` does not enable this flag.

```bash
LOG_LEVEL=DEBUG MINDROOM_LOG_FORMAT=json MINDROOM_TIMING=1 mindroom run
```

To find what grows the primary process's memory, set `MINDROOM_HEAP_PROBE_INTERVAL_SECONDS` in the process environment or the config-adjacent `.env`.
The primary then logs one `heap_type_probe` event per interval with the 25 most common garbage-collected object types and their counts, resident memory (`rss_bytes`), glibc allocator totals under `malloc` (`arena_bytes`, `arena_in_use_bytes`, `arena_free_bytes`, `mmap_bytes`), and `python_allocated_blocks`.
Compare successive events: resident memory that grows while `arena_in_use_bytes` plus `mmap_bytes` and `python_allocated_blocks` stay flat suggests freed memory the allocator keeps, and growth in those suggests live objects.
Strings, bytes, and numbers are not counted, and no object contents are logged.
Each probe briefly pauses the event loop like a full garbage-collection pass, so prefer intervals of several minutes on large processes.
Unset or `0` disables the probe (the default), and any other value below `60` fails startup.

See [Operational Log Events](https://docs.mindroom.chat/deployment/operational-log-events/) for stable log events to alert on.

## Built-In Prompt Overrides

Use the optional root `prompts` block to override MindRoom's built-in prompts without editing Python code.
Keys must match the uppercase global names in `src/mindroom/prompts.py` exactly, and unknown keys fail config validation.

```yaml
prompts:
  HIDDEN_TOOL_CALLS_PROMPT: |
    Tool calls are hidden from users.
    Answer directly without narrating internal tool use.
  ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE: |
    Decide which agent or team should respond to this message.

    Available agents and teams:

    {agents_info}

    User message: {message}

    Return only the agent or team name.
```

The allowed names are listed in `PROMPT_DEFAULT_NAMES` and the allowed placeholder fields per prompt in `PROMPT_TEMPLATE_FIELDS`, both in `mindroom.prompts`.
Unsupported placeholders fail validation at config load.
Placeholders are exact `{field_name}` replacements, not Jinja or Python `str.format`, so `{message.text}`, `{message!r}`, and `{message:.2f}` are rejected.
Escape literal braces as `{{` and `}}` in overrides that use placeholders.
Changed prompt overrides take effect through hot reload.

## Managed Avatars

Every agent, team, and managed room gets a Matrix avatar by default, chosen from the painted stock avatars in the [MindRoom assets repository](https://github.com/mindroom-ai/assets/blob/main/avatars/painted/README.md).
An agent named after a stock avatar (for example `mind`, `code`, `research`, `writer`, or `email`) uses that picture, and any other agent or team receives a stable stock avatar chosen from its name.
A room served by exactly one agent or team shows that entity's avatar, and any other room receives a stable stock avatar chosen from its key.
The root Matrix Space uses `avatars/spaces/root_space.png` when present and otherwise the stock `mind-logo` image.
Stock avatars not bundled in the install are downloaded once and cached under `<storage>/avatars/stock/`.
A failed download is retried at the first start after 24 hours.
`mindroom avatars sync` retries room and root Space avatars immediately and lets the next start retry agent and team avatars.
Avatars are filled in only where the Matrix profile or room has none, so pictures you set yourself are kept.
Managed room avatars are chosen only when a room is created, so run `mindroom avatars sync` to fill in existing rooms.
To choose a picture, place a PNG at `avatars/<agents|teams|rooms|spaces>/<name>.png` next to `config.yaml`; containerized deployments read these overrides from `<storage>/avatars/` instead.

MindRoom can also generate custom avatars for agents, teams, rooms, and the root Matrix Space.
Override the built-in avatar prompt styles with the root `prompts` block; omitted prompts use the built-in defaults.

```yaml
prompts:
  AVATAR_CHARACTER_STYLE: "professional AI avatar portrait, abstract geometric silhouette"
  AVATAR_ROOM_STYLE: "minimalist wayfinding icon, precise geometry, strong silhouette"
  AVATAR_AGENT_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI agent avatar."
  AVATAR_TEAM_SYSTEM_PROMPT: "You are creating distinctive visual elements for a professional AI team avatar."
  AVATAR_ROOM_SYSTEM_PROMPT: "You are creating a refined, minimalist icon design for a room avatar."
```

`mindroom avatars generate` creates a custom file for every entity without a workspace or bundled avatar file; downloaded stock avatars do not count.
Run `mindroom avatars generate --force` to overwrite existing workspace avatar files after changing prompts or styles.
Generation uses `gpt-6-astra` for prompt creation and `gpt-image-2.5-sunburst` for 1024x1024 PNG rendering, and requires only `OPENAI_API_KEY` or `OPENAI_API_KEY_FILE`.
`mindroom avatars sync` only fills missing Matrix avatars by default.
Run `mindroom avatars sync --force` to reset every managed room and the root Space to its default avatar: its own file, its single agent's or team's avatar, or its stock pick.

## Personal Agent Rooms

The optional `personal_rooms` section creates one private, unlisted room for each eligible human who joins an onboarding room.
The router watches the onboarding rooms; the selected agent does not need to join them.

```yaml
rooms:
  lobby: {}
agents:
  helper:
    display_name: Helper
    rooms: []
    access:
      members_of_rooms: [lobby]
personal_rooms:
  agent: helper
  onboarding_rooms: [lobby]
  commands: ["!personal"]
  alias_prefix: personal
  name: "Personal room for {user}"
  topic: "Private conversation with {agent}."
  welcome: "Welcome {user}! This is your personal room with {agent}."
  welcome_dispatch: false
  confirmation: ""
  backfill: false
  auto_join_requester: false
  requester_admin: false
  # avatar: avatars/personal.png
  avatar_from_requester: false
```

`personal_rooms` is an object or `null` (the default, which disables onboarding).
When enabled, its fields are:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `agent` | string | Required | Configured agent that owns and answers in personal rooms. |
| `onboarding_rooms` | list of strings | Required | Nonempty list of configured onboarding rooms observed by the router. |
| `commands` | list of strings | `[]` | Exact self-onboarding commands beginning with `!`, without whitespace or arguments. |
| `alias_prefix` | string | `"personal"` | Lowercase alias prefix, matching `[a-z0-9_-]{1,40}`. |
| `name` | string | `"Personal room for {user}"` | New room name template, up to 1,000 characters. |
| `topic` | string | `"Private conversation with {agent}."` | New room topic template, up to 1,000 characters. |
| `welcome` | string | `"Welcome {user}! This is your personal room with {agent}."` | Welcome template, up to 10,000 characters; empty disables new welcomes. |
| `welcome_dispatch` | boolean | `false` | Dispatch the welcome to the selected agent after the human joins; otherwise send a notice. |
| `confirmation` | string | `""` | Optional once-only notice template in the original onboarding room, up to 10,000 characters. |
| `backfill` | boolean | `false` | Also reconcile current eligible onboarding-room members on startup and config reload. |
| `auto_join_requester` | boolean | `false` | Use the homeserver admin API to join the requester during initial creation of a new personal room. |
| `requester_admin` | boolean | `false` | Grant the human Matrix room administration; otherwise the agent owns room state. |
| `avatar` | string or `null` | `null` | Room avatar file, relative to the configuration; fills only an empty avatar. |
| `avatar_from_requester` | boolean | `false` | Copy the human's profile avatar into an empty room avatar when no avatar file is configured. |

Templates support `{user}` (full Matrix user ID), `{room}` (room alias), and `{agent}` (display name).
Aliases are built from `alias_prefix`, a hash of the user ID, and the installation namespace.

### Onboarding Behavior

- A human joining an onboarding room, or sending one of the `commands` there, onboards only that human.
- Existing agent access rules still apply, and agent and service accounts are excluded.
- `auto_join_requester: true` requires a source Matrix account allowed to use the homeserver's Synapse-compatible admin join API.
  It joins only the requester of a newly created room, never force-joins someone who already joined, left, or was banned, and does not apply to existing or imported rooms.
- A requester who left or was banned from their personal room is not re-added by restarts or backfill; a fresh join of the onboarding room may re-invite them to a newly created personal room, but never overrides a ban.
- The owner may invite other MindRoom agents into their personal room, and an invited agent sees only messages sent after its invite.
  Anyone else invited into a room MindRoom created is removed with a notice explaining why.
- The welcome is a notice by default.
  With `welcome_dispatch: true`, the welcome is sent to the selected agent as a trusted message on the human's behalf after the human joins, so the agent starts the conversation.
  The welcome is delivered once, and its text and dispatch mode are fixed when the room is created, so later template changes do not affect pending welcomes.
  Disabling `personal_rooms` or revoking the human's access prevents a pending welcome.
- `confirmation` sends one notice in the original onboarding room after creation and invitation; everyone in that room can see it, including any room alias it contains.
- An explicit `avatar` file takes priority over `avatar_from_requester`, and both fill only an empty room avatar.
- Personal rooms are kept across restarts and room cleanup, including after the feature is disabled, and disabling onboarding does not revoke existing access.

### Troubleshooting Personal Rooms

Onboarding failures affect only that requester, and the onboarding room keeps serving everyone else.
Reconciliation runs at startup and after a configuration reload, and retries a failed room after doubling delays up to hourly; a reload retries it at once.

| Log message | Meaning |
| --- | --- |
| `Personal-room validation failed for an onboarding trigger` | An existing personal room failed ownership or membership checks, for example because another account already holds the alias, or a guest the agent cannot remove was raised to the agent's power level. |
| `Personal-room imported roster has unattested members` | An imported room contains members that its seed record does not list; the warning names them, and the room is not changed. |
| `Personal-room onboarding trigger failed` | Any other failure, such as an invite refused by the requester's server. |

A join or command that arrives before the personal-room agent connects waits until it does.

### Operator-seeded Existing Rooms

Operators can adopt an existing room, keeping its room ID and history, by writing a trusted `PersonalRoomRecord` before enabling onboarding.

1. Stop the runtime.
2. In the room, have the agent author an `org.mindroom.personal_room` state event with an empty state key and exactly `{"user_id": "<requester>", "agent_user_id": "<agent>"}` as content.
3. Make sure the target agent is joined with room admin power, the room's directory visibility is private, and its join rule is invite-only.
4. Write the record with `personal_room_record_path(runtime_paths, agent_name, user_id)` and `write_personal_room(path, record)` from `mindroom.matrix.personal_room_store`.
   Records live at `tracking/agents/<agent>/personal_rooms/<full-sha256-user-id>.json` under the storage root, outside every directory a worker mounts.

For a private room with shared history and one already permitted guest, a record looks like this:

```json
{
  "user_id": "@alice:example.test",
  "alias": "#personal-alice:example.test",
  "source_room_id": "!lobby:example.test",
  "room_id": "!existing:example.test",
  "welcome_completed": true,
  "adoption": {
    "creator_user_id": "@router:example.test",
    "agent_user_id": "@helper:example.test",
    "router_user_id": "@router:example.test",
    "expected_history_visibility": "shared",
    "additional_user_ids": ["@guest:example.test"]
  }
}
```

- Adoption requires the exact room ID; an alias alone never authorizes adoption.
- `creator_user_id` must match the room's creator, and `agent_user_id` must match the selected agent's Matrix identity.
- `router_user_id` (optional) lets this installation's router account stay in the room, including during ordinary cleanup after onboarding is disabled.
- `expected_history_visibility` must exactly match the room's current `invited`, `joined`, or `shared` history setting and defaults to `invited`; `world_readable` is never accepted.
- `additional_user_ids` (default `[]`) lists existing participants who may be joined, invited, or knocking.
  It only permits them; it does not invite anyone or grant power.
  No other members besides the requester, agent, seeded router, and these users are allowed.
- Set `welcome_completed: true` only when onboarding is already complete, and omit pending welcome content from completed imports.

Normal authorization and current onboarding-room membership still apply.
MindRoom verifies these conditions before inviting or sending, fails reconciliation if history visibility or membership changed, and never rewrites the room's name, topic, or history.
No import CLI or historical-format converter is provided.

## Internal User Username

- Configure `mindroom_user.username` with the Matrix localpart to request before first startup.
- After the account is created, `mindroom_user.username` cannot be changed in place.
- Hosted provisioning can return a different actual Matrix ID, which MindRoom then uses.
- You can change `mindroom_user.display_name` at any time.

## Sections

- [Agents](https://docs.mindroom.chat/configuration/agents/) - Configure individual AI agents
- [Models](https://docs.mindroom.chat/configuration/models/) - Configure AI model providers
- [Teams](https://docs.mindroom.chat/configuration/teams/) - Configure multi-agent collaboration
- [Router](https://docs.mindroom.chat/configuration/router/) - Configure message routing
- [Dynamic Tools](https://docs.mindroom.chat/tools/dynamic-tools/) - Configure optional tools that agents load on demand
- [Memory](https://docs.mindroom.chat/memory/) - Configure memory providers and behavior
- [Knowledge Bases](https://docs.mindroom.chat/knowledge/) - Configure file-backed knowledge bases
- [Voice](https://docs.mindroom.chat/voice/) - Configure speech-to-text voice processing
- [Voice Calls](https://docs.mindroom.chat/voice-calls/) - Configure agents joining Element Call voice calls
- [Authorization](https://docs.mindroom.chat/authorization/) - Configure user and room access control, invitations, and bridge aliases
- [Matrix Space](https://docs.mindroom.chat/matrix-space/) - Configure the root Matrix Space for managed rooms
- [Skills](https://docs.mindroom.chat/skills/) - Skill format, gating, and allowlists
- [Plugins](https://docs.mindroom.chat/plugins/) - Plugin manifest and tool/skill loading
- [Scheduling](https://docs.mindroom.chat/scheduling/) - Scheduled tasks, timezone, and catch-up
- [Thread summaries](https://docs.mindroom.chat/tools/matrix-and-attachments/) - Automatic and manual thread summaries and tags
