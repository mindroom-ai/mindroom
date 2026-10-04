---
icon: lucide/user
---

# Agent Configuration

Agents are the core building blocks of MindRoom.
Each agent is a specialized AI actor with specific capabilities.

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

    # Tools the agent can use (plain names or inline config overrides)
    tools:
      - file
      - shell
      - github
      # Per-agent tool config override (single-key dict syntax):
      # - shell:
      #     extra_env_passthrough: "DAWARICH_*"
      #     enable_run_shell_command: true

    # Skills the agent can use (defined in skills section or plugins)
    skills:
      - my_custom_skill

    # Custom instructions
    instructions:
      - Always read files before modifying them
      - Use clear variable names
      - Add comments for complex logic

    # Concise guidance included on every minimal-mode request (default: [])
    minimal_instructions: []

    # Rooms to join (will be created if they don't exist)
    rooms:
      - lobby
      - dev

    # Accept all, none, or matching inviter ID patterns
    accept_invites: true

    # Conversation access is separate from invitation acceptance
    access:
      current_room_members: false
      members_of_rooms:
        - lobby
      users: []

    # Enable markdown formatting
    markdown: true

    # Enable Agno Learning for this agent
    learning: true

    # Learning mode: always (automatic) or agentic (tool-driven)
    learning_mode: always

    # Memory backend override for this agent (optional: mem0, file, or none)
    memory_backend: file

    # Assign agent to one or more configured knowledge bases (optional)
    knowledge_bases: [docs]

    # Optional: additional files loaded into each freshly built agent instance
    context_files:
      - SOUL.md
      - AGENTS.md
      - USER.md
      - IDENTITY.md
      - TOOLS.md
      - HEARTBEAT.md

    # Whether to include defaults.tools for this agent (default: true)
    include_default_tools: true

    # Response mode: "thread" (replies in Matrix threads) or "room" (plain room messages)
    thread_mode: thread

    # Optional room-specific overrides for thread mode
    # Keys may be managed room aliases/names or Matrix room IDs
    room_thread_modes:
      lobby: thread
      bridge_telegram: room
      "!abc123:example.com": room


    # Tools to run in the sandbox proxy instead of the main process (optional, inherits from defaults)
    worker_tools: [shell, file]

    # How sandbox runtimes are shared (optional, inherits from defaults)
    worker_scope: user_agent

    # Allow this agent to read and modify its own config at runtime
    allow_self_config: false

    # Delegate tasks to other agents via tool calls
    delegate_to:
      - research
      - finance

    # History context controls (all optional, inherit from defaults)
    num_history_runs: null
    num_history_messages: null
    compress_tool_results: false
    max_tool_calls_from_history: null
    max_tool_calls_per_turn: null

    # Required compaction is enabled by default.
    # Eligible models and history policies use native compaction at the threshold.
    # Set enabled: false to disable automatic native and pre-reply text compaction.
    compaction:
      enabled: true
      threshold_percent: 0.8
      reserve_tokens: 16384
      timeout_seconds: 600

    # Keep <workspace>/thread_exports/ current with YAML exports of this agent's rooms
    # (true enables the defaults; see Thread Exports below)
    thread_exports: true

```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `display_name` | string | *required* | Human-readable name shown in Matrix as the bot's display name |
| `role` | string | `""` | System prompt describing the agent's purpose — guides its behavior and expertise |
| `model` | string | `"default"` | Model name (must match a key in the `models` section) |
| `tools` | list | `[]` | Agent-specific tool entries — plain strings or single-key dicts with config overrides (see [Tools](../tools/index.md) and [Per-Agent Tool Configuration](../tools/index.md#per-agent-tool-configuration)); effective tools are `tools + defaults.tools` with duplicates removed |
| `include_default_tools` | bool | `true` | When `true`, append `defaults.tools` to this agent's `tools`; set to `false` to opt this agent out |
| `skills` | list | `[]` | Skill names the agent can use (see [Skills](../skills.md)) |
| `skill_learning` | object | disabled | [Automatic skill learning](../skills.md#settings) settings; separate from Agno `learning_mode` |
| `instructions` | list | `[]` | Extra lines appended to the system prompt after the role |
| `rooms` | list | `[]` | Room aliases to auto-join; rooms are created if they don't exist |
| `participation` | object or null | `null` | Opt into adaptive replies in existing multi-human threads across authorized rooms, including ad hoc rooms; see [Adaptive Participation](threads.md#adaptive-participation) |
| `mid_turn` | object or null | `null` | Judge whether queued messages can wait until this agent finishes its active task; see [Mid-Turn Coalescing](threads.md#mid-turn-coalescing) |
| `accept_invites` | bool or list[string] | `true` | Accept all inbound Matrix room invites with `true`, none with `false` or `[]`, or only inviters matching an exact or wildcard Matrix user ID in the list. Accepted ad-hoc room IDs are persisted so memberships survive restarts and room cleanup. Approval-gated tools require the router in the room; agents can recover a missing router with their built-in zero-argument `invite_router` tool when the router's policy allows the current Matrix transport account |
| `markdown` | bool | `null` | When enabled, the agent is instructed to format responses as Markdown. Inherits from `defaults.markdown` (default: `true`) |
| `learning` | bool | `null` | Enable [Agno Learning](https://docs.agno.com/agents/learning) — the agent builds a persistent profile of user preferences and adapts over time. Inherits from `defaults.learning` (default: `true`) |
| `learning_mode` | string | `null` | `always`: agent automatically learns from every interaction. `agentic`: agent decides when to learn via a tool call. Inherits from `defaults.learning_mode` (default: `"always"`) |
| `memory_backend` | string | `null` | Memory backend override for this agent (`"mem0"`, `"file"`, or `"none"`). Inherits from global `memory.backend` when omitted |
| `memory_search` | object | `null` | File-memory search override for this agent. Supports `mode`, `include`, and `include_entrypoint`; omitted fields inherit from global `memory.search` |
| `private` | object | `null` | Optional requester-private state for one shared agent definition |
| `knowledge_bases` | list | `[]` | Knowledge base IDs from top-level `knowledge_bases`; semantic bases add indexed RAG search while file-mode bases expose workspace file paths for agents with file-aware tools |
| `access` | object | `null` | Conversation-access policy with `current_room_members`, `members_of_rooms`, and `users`. Omitting it grants members of this agent's own managed `rooms`. See [Authorization](../authorization.md) |
| `credential_managers` | list | `[]` | Concrete Matrix user IDs allowed to manage this agent's credentials and shared OAuth connections. Does not grant conversation access. Eligible requesters manage their own isolated OAuth connections separately; see [OAuth authorization](../oauth-framework.md) |
| `context_files` | list | `[]` | File paths (relative to the agent's workspace) loaded into each agent instance and prepended to role context (under `Personality Context`) |
| `thread_mode` | string | `"thread"` | `thread`: responses are sent in Matrix threads (default). `room`: responses are sent as plain room messages with a single persistent session per room — ideal for bridges (Telegram, Signal, WhatsApp) and mobile |
| `room_thread_modes` | map | `{}` | Per-room thread mode overrides keyed by room alias/name or Matrix room ID. Values are `thread` or `room`. Overrides apply before `thread_mode` fallback |
| `num_history_runs` | | | See [History & Compaction](history.md#history-settings) |
| `num_history_messages` | | | See [History & Compaction](history.md#history-settings) |
| `compress_tool_results` | | | See [History & Compaction](history.md#history-settings) |
| `compaction` | | | See [History & Compaction](history.md#history-settings) |
| `max_tool_calls_from_history` | | | See [History & Compaction](history.md#history-settings) |
| `max_tool_calls_per_turn` | int | `null` | Tool calls one turn may execute. Further calls return a tool error, and a turn that has made this many plus two model requests ends with the text produced so far, including loops of calls to unknown tools or with unparseable arguments (`null` = `defaults.max_tool_calls_per_turn`, 1000) |
| `show_tool_calls` | bool | `null` | Show tool-call markers and trace metadata in Matrix messages. Inherits from `defaults.show_tool_calls` (default: `true`). When `false`, inline markers and `io.mindroom.tool_trace` are omitted from sent Matrix message content. Routed tools may still show generic worker warmup text such as `Preparing isolated worker...`, but that copy never includes tool identifiers or tool-trace metadata. Note: this flag is not currently enforced by the OpenAI-compatible `/v1/chat/completions` path. |
| `worker_tools` | list | `null` | Tool names to run in the [sandbox proxy](../deployment/sandbox-proxy.md) instead of the main process. Inherits from `defaults.worker_tools`. When omitted everywhere, MindRoom uses its built-in default. Set to `[]` to disable proxying for this agent |
| `worker_scope` | string | `null` | How sandbox runtimes are shared for non-private agents. `shared`: one per agent. `user`: one per user (shared across agents). `user_agent`: one per user+agent pair. Inherits from `defaults.worker_scope`. Do not set this when the agent uses `private`, because `private.per` already defines the requester partition for that agent |
| `file_access` | string | `null` | Which files path-taking tools (`file`, `coding`, `attachments`, `matrix_message`, `gmail`, `google_drive`, `browser` uploads, `e2b` uploads) may use. `workspace`: the agent workspace and its attachments. `unrestricted`: any path the tool's process can reach. Code-execution tools such as `shell` and `python` are always unrestricted; isolate them with `worker_tools`. Inherits from `defaults.file_access` (default: `workspace`). See [Security Posture](../architecture/security-posture.md) |
| `allow_self_config` | bool | `null` | Give this agent a scoped tool to read and modify its own configuration at runtime. Inherits from `defaults.allow_self_config` (default: `false`). Lighter-weight alternative to the `config_manager` tool |
| `delegate_to` | list | `[]` | Allowed agent names for `run_subagent`, including itself if listed (see [Agent Delegation](../tools/agent-orchestration.md#agent-delegation)) |
| `thread_exports` | bool or object | `null` | Continuously export every thread from rooms this agent is joined to as YAML under `<workspace>/thread_exports/`. `true` enables the defaults; an object sets `invited_rooms` and `private_room_scope` (see [Thread Exports](../thread-exports.md#thread-exports)) |

Each entry in `knowledge_bases` must match a key under `knowledge_bases` in `config.yaml`.
See [Knowledge Bases](../knowledge.md) for `mode: semantic` and `mode: files`.

Per-agent fields with a corresponding setting in `defaults` inherit that setting when `null`.
`participation: null` disables adaptive participation; it does not inherit a global default.
Per-agent values override them.
`memory.backend` is the global memory default, and `agents.<name>.memory_backend` overrides it per agent.
Use `memory_backend: none` for stateless agents that should skip prompt memory lookup, automatic memory persistence, and the explicit `memory` tool.
`agents.<name>.memory_search` can override `mode`, `include`, and `include_entrypoint` when the effective memory backend is `file`.
Unset `memory_search` fields inherit from top-level `memory.search`.
`show_stop_button` and `enable_streaming` are global-only settings in `defaults` and cannot be overridden per-agent.
The dashboard Agents tab exposes this as the **Memory Backend** selector for each agent.
Agents use `agents.<name>.accept_invites`, while teams and the router use their own `accept_invites` options with the same durable invite semantics.
`true` accepts every valid invitation, `false` and `[]` reject every invitation, and a list accepts exact or wildcard Matrix user IDs after human-only alias resolution; non-human accounts retain their exact transport ID.
Invite acceptance and responder access are independent, so joining a room does not authorize its inviter to interact with the agent.
The agent continues to apply `access.users`, `access.current_room_members`, and `access.members_of_rooms` to every interaction after joining.

See [When the Router Is Missing](../tool-approval.md#when-the-router-is-missing), [Agent Compaction Settings](history.md#agent-compaction-settings), [Adaptive Participation](threads.md#adaptive-participation), [Mid-Turn Coalescing](threads.md#mid-turn-coalescing), [Per-Agent Tool Configuration](../tools/index.md#per-agent-tool-configuration), and [Worker Routing](../deployment/sandbox-proxy.md#worker-routing).

## Private Instances

Use `private` when one shared agent definition should behave like a template that materializes a separate requester-local instance at runtime.
The YAML definition stays shared.
The private root, copied files, file-memory workspace, and private knowledge path do not.
Private agents cannot be configured as team members.
Explicit Matrix messages that tag a private agent with other agents can form an ad hoc team for that requester.
That exception is direct only: a shared team member that reaches a private agent through `delegate_to` is still rejected.

`private.per` is not a second spelling of `worker_scope`.
`private.per` chooses who gets a separate private instance of the agent's state.
MindRoom then uses that same requester partition for worker execution, but that is an internal consequence of private execution, not the public meaning of `worker_scope`.

```yaml
knowledge_bases:
  company_docs:
    path: ./company_docs
    watch: false

agents:
  mind:
    display_name: Mind
    role: A persistent personal AI companion
    model: sonnet
    tools: [file, shell]
    worker_tools: [file, shell]
    memory_backend: file
    memory_search:
      mode: semantic
      include:
        - memory/**/*.md
      include_entrypoint: false
    private:
      per: user
      root: mind_data
      template_dir: ./mind_template
      context_files:
        - SOUL.md
        - AGENTS.md
        - USER.md
        - IDENTITY.md
        - TOOLS.md
        - HEARTBEAT.md
        - MEMORY.md
      knowledge:
        path: memory
        watch: false
    knowledge_bases: [company_docs]
```

Example template directory:

```text
mind_template/
├── SOUL.md
├── AGENTS.md
├── USER.md
├── IDENTITY.md
├── TOOLS.md
├── HEARTBEAT.md
├── MEMORY.md
└── memory/
```

In the example above, each requester gets their own effective `mind_data/` root under a canonical private-instance state root in shared storage.
That private root is not created next to `config.yaml`.
It is not stored under `workers/<worker>/`.
Dedicated workers mount that `mind_data/` private root, not the state root around it, when they execute that requester scope.
For a `mind` agent with `private.per: user`, different users get different private `mind_data/` trees even though the agent definition is shared.

### Private Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `private.per` | `user` or `user_agent` | *required* | Which requester boundary gets its own private instance of the agent's state. MindRoom also uses that same boundary for the agent's internal execution scope |
| `private.root` | string | `<agent_name>_data` | Private root name under the canonical private-instance state root, and the workspace dedicated workers mount. Must be a relative path, cannot escape with `..`, and cannot start with a name the primary writes beside it: `sessions`, `learning`, `chroma`, `knowledge_db`, `memory_files`, `calls`, `agent_modes.json`, `agent_modes.lock`, `browser`, `browser-profiles`, or `.sessions-recovery.lock` |
| `private.template_dir` | string | `null` | Optional local directory copied recursively into each private root without overwriting existing files. Relative paths are resolved from `config.yaml`, and absolute paths are also allowed. MindRoom raises an error when the directory does not exist |
| `private.context_files` | list | `null` | Optional files loaded into role context from inside the private root. Each path is relative to the private root and cannot escape it |
| `private.knowledge` | object | `null` | Optional PrivateAgentKnowledge indexed from inside the private root. Sub-fields below. See [Knowledge Bases](../knowledge.md#private-agent-knowledge) |
| `private.knowledge.enabled` | bool | `true` | Whether to index PrivateAgentKnowledge for this private agent instance. Set to `false` to disable indexing |
| `private.knowledge.description` | string | `""` | Short description of what the private knowledge contains. Agents see this in the `search_knowledge_base` tool description so they know when the source is relevant |
| `private.knowledge.path` | string | `null` | Path to a private knowledge directory relative to the private root |
| `private.knowledge.watch` | bool | `true` | When true, PrivateAgentKnowledge schedules background refresh on access. When false, direct external edits require explicit refresh |
| `private.knowledge.chunk_size` | int | `5000` | Maximum characters per indexed chunk (min: 128) |
| `private.knowledge.chunk_overlap` | int | `0` | Overlapping characters between adjacent chunks (min: 0) |
| `private.knowledge.git` | object | `null` | Optional Git sync configuration for PrivateAgentKnowledge (same schema as top-level `knowledge_bases.<id>.git`) |

### Runtime Behavior

1. MindRoom resolves the canonical private-instance state root from `private.per`.
2. MindRoom creates the effective private root inside that canonical private-instance state root.
3. If `private.template_dir` is set, MindRoom copies the template directory into the private root without overwriting files that already exist there.
4. MindRoom loads any `private.context_files` from that private root when the agent is created or reloaded.
5. If `memory_backend: file` is enabled, MindRoom uses that same private root as the file-memory root for that requester.
6. If `private.knowledge.path` is configured, MindRoom indexes that private-root-relative path as PrivateAgentKnowledge for that requester only.

### Important Rules

- `private` is explicit opt-in.
- `private` does not automatically enable file memory.
- `private` does not automatically load any context files.
- `private` does not automatically create a private knowledge base.
- Private agents cannot be configured as team members.
- Explicit Matrix ad hoc teams can include directly tagged private agents for requester-scoped execution.
- Shared team members that reach a private agent through `delegate_to` are rejected.
- If `private.template_dir` is omitted, MindRoom still creates the private root.
- Private agents require an active requester-scoped runtime context.
- MindRoom raises an error instead of silently falling back to a shared config-relative path when that requester scope is missing.
- Set `memory_backend: file` if you want `MEMORY.md` and `memory/` inside the private root to be the agent's actual file memory.
- Set `memory_backend: none` if the private agent should stay stateless while still using its private files and knowledge configuration.
- Set `private.context_files` explicitly for any copied files you want loaded into role context.
- Set `private.knowledge.path` explicitly for any copied files or folders you want indexed as PrivateAgentKnowledge.
- Omit `private.knowledge` entirely, or set `private.knowledge.enabled: false`, when you do not want PrivateAgentKnowledge indexing.
- `private` cannot be combined with `worker_scope`.
- Top-level `knowledge_bases` remain shared or company-wide corpora, so one agent can use both PrivateAgentKnowledge and shared knowledge in the same run.
- Top-level `context_files` remain the shared workspace-relative mechanism used by single-user setups, including the default `mindroom config init` output.
- Custom templates are fully supported.
- The Mind-style filenames shown above are a convention, not a requirement, unless you choose to reference them in `private.context_files` or `private.knowledge.path`.

See [Thread Exports](../thread-exports.md#thread-exports) and [Thread Mode Resolution](threads.md#thread-mode-resolution).

## File-Based Context Loading

You can inject file content directly into an agent's role context without using a knowledge base.

`context_files` behavior:

- Paths are relative to the agent's workspace (`agents/<name>/workspace/`)
- `private.context_files` paths are resolved relative to the effective private root
- Existing files are loaded in list order and added under `Personality Context`
- Missing files are skipped with a warning in logs

MindRoom loads the files when it builds an agent instance.
The normal Matrix and OpenAI-compatible reply paths build fresh agent instances per reply/request, so editing a context file affects the next reply without restarting the process.

`context_files` are resolved relative to the agent's workspace directory (`agents/<name>/workspace/`).
When the effective memory backend is `file`, the agent's canonical file memory root is that same workspace directory.
Absolute paths and `..` traversal are rejected.

Each part of the `Personality Context` section is headed by the resolved path of the file it was read from, and the section states that those files are already inlined so the agent does not spend a turn re-reading them.
When the rendered section exceeds `defaults.max_preload_chars`, MindRoom drops earlier file bodies first, trims the final surviving body from its end, and leaves a per-file marker giving each affected path and omitted-character count, followed by a summary marker for the section.
A dropped file therefore still appears with its path, so the agent can open it when it needs the omitted part.
If the configured cap cannot contain the section heading plus all required per-file and summary markers, agent materialization fails explicitly instead of silently removing source paths.

See [Agent Delegation](../tools/agent-orchestration.md#agent-delegation).

## Naming Rules

Agent and team YAML keys must contain only alphanumeric characters and underscores (matching `^[a-zA-Z0-9_]+$`).
Agent and team names must be distinct — the same key cannot appear in both `agents:` and `teams:`.
The names `router`, `user`, and `_shared` are reserved for MindRoom's own accounts and storage.

## Defaults

The `defaults` section sets fallback values for supported per-agent override fields and global-only agent behavior.
Supported override fields inherit when omitted, `defaults.tools` is merged only when `include_default_tools` is true, and global-only defaults apply to every agent.

```yaml
defaults:
  tools:                                # Tools added to every agent by default (set [] to disable)
    - scheduler
    # Per-agent tool config overrides also work in defaults:
    # - shell:
    #     enable_run_shell_command: true
  markdown: true                        # Format responses as Markdown
  learning: true                        # Enable Agno Learning
  learning_mode: always                 # "always" or "agentic"
  max_preload_chars: 50000              # Hard cap for preloaded context from context_files
  tool_output_auto_save_threshold_bytes: 51200  # Auto-save supported tool outputs larger than 50 KiB
  show_stop_button: true                # Show a stop button while agent is responding (global-only, cannot be overridden per-agent)
  max_consecutive_agent_replies: 50    # Agent or team messages in a row before agents stop waking each other (global-only; see authorization.md)
  num_history_runs: null                # Number of prior runs to include (null = all)
  num_history_messages: null            # Max messages from history (null = use num_history_runs)
  enable_streaming: true                # Stream agent responses via progressive message edits
  streaming:
    update_interval: 5.0                # Steady-state seconds between streamed edits
    min_update_interval: 0.5            # Fast-start seconds between early edits
    interval_ramp_seconds: 15.0         # Set 0 to disable interval ramping
    max_idle: 2.0                       # Event-driven idle ceiling before the next edit
  compress_tool_results: false          # Safer default; enabling can invalidate Anthropic/Vertex Claude prompt caches
  compaction:
    enabled: true
    threshold_percent: 0.8
    reserve_tokens: 16384
    timeout_seconds: 600
  max_tool_calls_from_history: null     # Limit tool call messages replayed from history (null = no limit)
  max_tool_calls_per_turn: 1000        # Tool calls one agent or team turn may execute
  show_tool_calls: true                 # Show tool-call markers and trace metadata; hidden mode still allows generic worker warmup copy
  worker_tools: null                     # Tool names to route through workers (null = use MindRoom's default routing policy, [] = disable)
  worker_scope: null                     # Worker runtime reuse for proxied tools (shared, user, user_agent)
  allow_self_config: false               # Allow agents to read/modify their own config at runtime
  file_access: workspace                 # workspace or unrestricted; see the file_access field above
  worker_grantable_credentials: null     # Shared credential services leased to isolated workers (null denies all); Google OAuth client and token services and google_vertex_adc cannot be granted
  large_message_strategy: sidecar        # Oversized replies: sidecar (preview plus full text as an attachment) or split (several complete messages); global-only
  coalescing:
    debounce_ms: 1000                    # Milliseconds to wait for more attachments or a trailing caption after media; text dispatches immediately
  thread_summary_model: null             # Model alias for automatic thread summaries (null = default)
  thread_summary_temperature: 0.2        # null uses provider defaults
  thread_summary_first_threshold: 1      # Messages before the first automatic summary (integer >= 1)
  thread_summary_subsequent_interval: 10 # Messages between later automatic summaries (integer >= 1)
```

`defaults.streaming` is global-only and controls the timing of progressive message edits for streaming responses.

To opt out a specific agent:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Focus on deep research
    include_default_tools: false
    tools: [duckduckgo]
```

See [Conversation mode](../tools/agent-cli.md#conversation-mode) and [Automatic skill learning](../skills.md#settings).
