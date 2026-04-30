---
icon: lucide/user
---

# Agent Configuration

Agents are the core building blocks of MindRoom. Each agent is a specialized AI actor with specific capabilities.

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

    # Rooms to join (will be created if they don't exist)
    rooms:
      - lobby
      - dev

    # Accept authorized ad-hoc room invites for this agent
    accept_invites: true

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

    # Participate in room-level startup prewarm for rooms already joined at first sync (default: true)
    startup_thread_prewarm: true

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

    # Auto-compaction stays off until you author defaults.compaction
    # or a non-empty per-agent compaction override.
    # A bare per-agent compaction: {} only inherits authored defaults.
    compaction:
      enabled: true
      threshold_percent: 0.8
      reserve_tokens: 16384

```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `display_name` | string | *required* | Human-readable name shown in Matrix as the bot's display name |
| `role` | string | `""` | System prompt describing the agent's purpose — guides its behavior and expertise |
| `model` | string | `"default"` | Model name (must match a key in the `models` section) |
| `tools` | list | `[]` | Agent-specific tool entries — plain strings or single-key dicts with config overrides (see [Tools](../tools/index.md) and [Per-Agent Tool Configuration](#per-agent-tool-configuration)); effective tools are `tools + defaults.tools` with duplicates removed |
| `include_default_tools` | bool | `true` | When `true`, append `defaults.tools` to this agent's `tools`; set to `false` to opt this agent out |
| `skills` | list | `[]` | Skill names the agent can use (see [Skills](../skills.md)) |
| `instructions` | list | `[]` | Extra lines appended to the system prompt after the role |
| `rooms` | list | `[]` | Room aliases to auto-join; rooms are created if they don't exist |
| `accept_invites` | bool | `true` | Accept authorized inbound Matrix room invites for this agent. Invited room IDs are persisted so ad-hoc memberships survive restarts and room cleanup. Set to `false` to ignore new invites for this agent |
| `markdown` | bool | `null` | When enabled, the agent is instructed to format responses as Markdown. Inherits from `defaults.markdown` (default: `true`) |
| `learning` | bool | `null` | Enable [Agno Learning](https://docs.agno.com/agents/learning) — the agent builds a persistent profile of user preferences and adapts over time. Inherits from `defaults.learning` (default: `true`) |
| `learning_mode` | string | `null` | `always`: agent automatically learns from every interaction. `agentic`: agent decides when to learn via a tool call. Inherits from `defaults.learning_mode` (default: `"always"`) |
| `memory_backend` | string | `null` | Memory backend override for this agent (`"mem0"`, `"file"`, or `"none"`). Inherits from global `memory.backend` when omitted |
| `private` | object | `null` | Optional requester-private state for one shared agent definition. `private.per` defines which requester boundary gets a separate private instance of the agent's state. Private agents must not set `worker_scope`. Internally, MindRoom reuses that same requester boundary for worker execution, but `private.per` is still a different public config concept from `worker_scope`. `private.root` defaults to `<agent_name>_data`, `private.template_dir` copies a local template into each requester root without overwriting existing files, `private.context_files` loads private-root-relative files into role context, and `private.knowledge` adds PrivateAgentKnowledge indexed from that private root. `private` does not implicitly enable file memory, context files, or private knowledge, and private agents cannot participate in teams yet |
| `knowledge_bases` | list | `[]` | Knowledge base IDs from top-level `knowledge_bases` — gives the agent RAG access to the indexed documents |
| `context_files` | list | `[]` | File paths (relative to the agent's workspace) loaded into each agent instance and prepended to role context (under `Personality Context`) |
| `thread_mode` | string | `"thread"` | `thread`: responses are sent in Matrix threads (default). `room`: responses are sent as plain room messages with a single persistent session per room — ideal for bridges (Telegram, Signal, WhatsApp) and mobile |
| `room_thread_modes` | map | `{}` | Per-room thread mode overrides keyed by room alias/name or Matrix room ID. Values are `thread` or `room`. Overrides apply before `thread_mode` fallback |
| `startup_thread_prewarm` | bool | `true` | When enabled, this bot may prewarm recent thread snapshots for rooms already joined when first sync completes, which can reduce cold-cache latency for early thread replies after startup |
| `num_history_runs` | int | `null` | Number of prior Agno runs to include as history context (`null` = all). Mutually exclusive with `num_history_messages` |
| `num_history_messages` | int | `null` | Max messages from history. Mutually exclusive with `num_history_runs` |
| `compress_tool_results` | bool | `null` | Compress tool results in history to save context. Inherits from `defaults.compress_tool_results` (default: `false`). On Anthropic and Vertex Claude models, setting this to `true` can mutate replayed tool messages and invalidate prompt-cache prefixes |
| `compaction` | object | `null` | Per-agent auto-compaction overrides. When the active runtime model has a known `context_window`, MindRoom always computes a per-run replay plan that reduces or disables persisted replay before the model call if needed. Authoring `defaults.compaction` enables the optional destructive compaction phase for inheriting agents and teams. A non-empty per-agent block also enables that destructive phase for the agent, but a bare `compaction: {}` is only a no-op override that inherits authored defaults. Use `enabled`, `threshold_tokens`, `threshold_percent`, `reserve_tokens`, and `model` to change the destructive-compaction policy. Replay safety always uses the active runtime model window. If you set `compaction.model`, that summary model must also define its own `context_window`, but only for the durable summary-generation pass. Compaction is destructive inside the live session: it merges old history into `session.summary` and removes the compacted raw runs from `session.runs` |
| `max_tool_calls_from_history` | int | `null` | Limit tool call messages replayed from history (`null` = no limit) |
| `show_tool_calls` | bool | `null` | Show tool-call markers and trace metadata in Matrix messages. Inherits from `defaults.show_tool_calls` (default: `true`). When `false`, inline markers and `io.mindroom.tool_trace` are omitted from sent Matrix message content. Routed tools may still show generic worker warmup text such as `Preparing isolated worker...`, but that copy never includes tool identifiers or tool-trace metadata. Note: this flag is not currently enforced by the OpenAI-compatible `/v1/chat/completions` path. |
| `worker_tools` | list | `null` | Tool names to run in the [sandbox proxy](../deployment/sandbox-proxy.md) instead of the main process. Inherits from `defaults.worker_tools`. When omitted everywhere, MindRoom uses its built-in default. Set to `[]` to disable proxying for this agent |
| `worker_scope` | string | `null` | How sandbox runtimes are shared for non-private agents. `shared`: one per agent. `user`: one per user (shared across agents). `user_agent`: one per user+agent pair. Inherits from `defaults.worker_scope`. Do not set this when the agent uses `private`, because `private.per` already defines the requester partition for that agent |
| `allow_self_config` | bool | `null` | Give this agent a scoped tool to read and modify its own configuration at runtime. Inherits from `defaults.allow_self_config` (default: `false`). Lighter-weight alternative to the `config_manager` tool |
| `delegate_to` | list | `[]` | Agent names this agent can delegate tasks to via tool calls (see [Agent Delegation](#agent-delegation)) |

Each entry in `knowledge_bases` must match a key under `knowledge_bases` in `config.yaml`.

Per-agent fields with a `null` default inherit from the `defaults` section at runtime.
Per-agent values override them.
`memory.backend` is the global memory default, and `agents.<name>.memory_backend` overrides it per agent.
Use `memory_backend: none` for stateless agents that should skip prompt memory lookup, automatic memory persistence, and the explicit `memory` tool.
`show_stop_button` and `enable_streaming` are global-only settings in `defaults` and cannot be overridden per-agent.
The dashboard Agents tab exposes this as the **Memory Backend** selector for each agent.

Startup thread prewarm is a background, best-effort cache warmup for rooms already joined when first sync completes.
Agents use `agents.<name>.accept_invites`, while the router uses its own `router.accept_invites` option with the same durable invite semantics.
Teams do not currently expose a separate `accept_invites` option, but accepted team invites are still persisted as durable desired membership.
Invite acceptance still respects your normal authorization rules, so unauthorized senders cannot force an entity to join and persist a room.

MindRoom compacts in one visible lifecycle.
If the current reply needs compaction to preserve usable history, MindRoom sends `Compacting history...`, compacts before the model call, and edits that same notice with the result.
After a successful visible reply, MindRoom re-checks the updated persisted session and immediately compacts if the next reply would cross the compaction threshold.
It always plans the replay that is safe for the current model call when the active runtime model has a known `context_window`.
That replay planner can keep configured replay, reduce raw replay, fall back to summary-only replay, or disable persisted replay for the run.
Compaction rewrites the persisted Agno session in SQLite.
Older compacted runs are removed from `session.runs` and replaced by the merged `session.summary`, so raw pre-compaction runs are not retained for later audit or debugging.

Learning data is persisted under `agents/<name>/learning/<agent>.db`, so it survives container restarts when the storage directory is mounted.
`context_files` are resolved relative to the agent's workspace directory (`agents/<name>/workspace/`).
When the effective memory backend is `file`, the agent's canonical file memory root is that same workspace directory.
Absolute paths and `..` traversal are rejected.

## Per-Agent Tool Configuration

Tools can be plain strings or single-key dicts with inline config overrides.
This lets you customize tool behavior per agent without affecting other agents that use the same tool.

```yaml
agents:
  code:
    tools:
      - file                              # no override, uses defaults
      - shell:                            # per-agent override
          extra_env_passthrough: "DAWARICH_*"
          enable_run_shell_command: true
  research:
    tools:
      - shell                             # uses global defaults (no overrides)
      - duckduckgo
```

### Merge Order

MindRoom resolves tool configuration in layers:

1. Tool constructor defaults (hardcoded in tool code)
2. Credentials (dashboard or credential store)
3. `defaults.tools` overrides (global inline config)
4. `agents.<name>.tools` overrides (per-agent inline config)
5. Runtime overrides (sandbox proxy, init overrides)

Within the authored layers (`defaults.tools` and `agents.<name>.tools`), each field has three possible states:

- Key omitted: keep the value from the next lower layer unchanged.
- Concrete value: override the next lower layer with that value.
- `__MINDROOM_INHERIT__`: clear an inherited authored override and fall back to the next lower layer.

When the same tool appears in both `defaults.tools` and `agents.<name>.tools`, MindRoom merges them field-by-field.
Per-agent values win for overlapping keys, non-overlapping keys are kept from both, and `__MINDROOM_INHERIT__` removes the inherited authored value instead of passing the literal string to the tool.

### Defaults with Overrides

`defaults.tools` also accepts the single-key dict syntax for global overrides that apply to all agents:

```yaml
defaults:
  tools:
    - scheduler
    - shell:
        enable_run_shell_command: true     # global default for all agents
```

### Clearing An Inherited Override

Use `__MINDROOM_INHERIT__` when an agent should keep the tool but stop inheriting one authored field from `defaults.tools`.

Optional-field example:

```yaml
defaults:
  tools:
    - shell:
        extra_env_passthrough: "DAWARICH_*"
        enable_run_shell_command: true

agents:
  research:
    tools:
      - shell:
          extra_env_passthrough: __MINDROOM_INHERIT__
```

`research` still inherits `enable_run_shell_command: true`, but `extra_env_passthrough` falls back to the lower layer (persisted tool config if set, otherwise the tool's normal default).
For sandboxed `shell`, provider API keys and other committed runtime credentials are denied by default in both worker startup env and command env.
Use `extra_env_passthrough` when a specific exported process env value must be visible to shell commands.

Required non-secret field example:

```yaml
defaults:
  tools:
    - clickup:
        master_space_id: "space-default"

agents:
  ops:
    tools:
      - clickup:
          master_space_id: __MINDROOM_INHERIT__
```

`ops` still uses the `clickup` tool, but `master_space_id` no longer inherits `"space-default"`.
MindRoom falls back to the next lower layer, which is usually the stored tool config from the dashboard or credential store.

### `include_default_tools` vs `__MINDROOM_INHERIT__`

- `include_default_tools: false` is coarse-grained: it removes every tool and every override inherited from `defaults.tools` for that agent.
- `__MINDROOM_INHERIT__` is fine-grained: it keeps the tool and the rest of the inherited fields, but clears one specific authored override.

### Security Restrictions

Not all config fields can be overridden inline:

- `type="password"` fields are blocked (credentials must go through the dashboard or credential store)
- `base_dir` is blocked (runtime-only, set by the workspace system)
- Fields with `authored_override: false` in the tool metadata are blocked

MindRoom validates overrides at config load time and rejects unknown field names, wrong value types, and blocked fields with a clear error message.

### Backward Compatibility

Existing configs with plain string tool lists work unchanged:

```yaml
tools: [shell, file, duckduckgo]   # still valid
```

### Config Manager

The `!config` chat command and the `config_manager` tool preserve inline overrides when updating tool lists.
Adding or removing tools via chat does not discard existing per-agent overrides on other tools.

## Worker Routing

`worker_tools` decides which tools run in the sandbox proxy instead of the main MindRoom process.
When omitted, MindRoom routes `coding`, `file`, `python`, and `shell` through the proxy by default.
`worker_scope` controls how those sandbox runtimes are reused between calls.
The shared-only integrations require `worker_scope` unset or `shared`.
That list includes `spotify`, `homeassistant`, and all configured `mcp_<server_id>` tools.
Of those, `gmail`, `google_calendar`, `google_drive`, `google_sheets`, and `homeassistant` also always stay local regardless of `worker_tools` (they are never proxied to the sandbox).
`spotify` can still be proxied through the sandbox.

The supported `worker_scope` values are:

- `shared`: one runtime per agent, shared by all users.
- `user`: one runtime per user, shared across that user's agents.
- `user_agent`: one runtime per user+agent pair.

Leave `worker_scope` unset for unscoped execution — calls still run in the sandbox, but each call gets a fresh runtime instead of a persistent one.
`worker_scope` also affects dashboard credential support and OpenAI-compatible agent eligibility.

### Filesystem Isolation

`worker_scope` controls runtime reuse, not filesystem security.
When the effective memory backend is `file`, tools like `shell`, `file`, `python`, and `coding` get a default working directory (`base_dir`) at the agent's canonical workspace root.
Without file-backed workspace state, those tools keep their normal defaults such as the current directory.
Even when set, `base_dir` is a convenience, not a hard boundary.

Isolation depends on the worker backend:

- **Kubernetes dedicated workers** (`shared`, `user_agent`, unscoped): the runtime can only see its own agent's storage directory plus its worker-local scratch space.
  This is the strongest isolation available today.
- **Kubernetes dedicated workers** (`user`): the runtime can see all agents' storage, because `user` mode intentionally shares one runtime across multiple agents for a single user.
  Treat this as a shared workstation.
- **Shared-runner and local backends**: no hard filesystem boundary today, regardless of scope.

Use `user_agent` if you need per-agent filesystem isolation.

For per-workspace env that an agent can edit (PATH, package indexes, npm cache locations, etc.), drop a `.mindroom/worker-env.sh` script in the agent workspace; MindRoom sources it before each worker-routed `shell` or `python` request.
MindRoom-owned workspace identity, cache, and virtualenv env names are reasserted after the hook, so hooks cannot redirect `HOME`, `MINDROOM_AGENT_WORKSPACE`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `PYTHONPYCACHEPREFIX`, or `VIRTUAL_ENV`.
With `worker_scope: user`, the same runtime can move between several agent workspaces, and the hook is discovered from the current request's workspace — different agents get different overlays automatically.
See [Workspace env hook](../deployment/sandbox-proxy.md#workspace-env-hook-mindroomworker-envsh) for filename, filtering, and failure semantics.

### Where Agent Data Lives

Agents without `private` store all their data in one canonical directory: `agents/<name>/` (context files, workspace, memory, sessions, learning).
Changing `worker_scope` changes how tool runtimes are isolated.
It does **not** change where that non-private agent's data lives.
All runtimes for the same non-private agent read and write the same storage directory.
If multiple runtimes run concurrently, files and databases in that directory must tolerate concurrent access.
Agents that use `private` are different.
They materialize one canonical state root per requester-scoped private instance under `private_instances/<scope-key>/<agent>/`.
Workers mount those canonical private-instance roots.
They do not own them.

The dashboard's generic credential forms only work for unscoped agents and agents with `worker_scope=shared`.
OAuth providers that support scoped dashboard flows, such as the Google Drive, Gmail, Calendar, and Sheets providers, are the exception.
For those providers, the dashboard can connect scoped `user` and `user_agent` credentials, but the Google tools still execute in the primary MindRoom runtime.
Tools without a scoped OAuth provider still manage `user` and `user_agent` credentials through their worker runtime instead.

For more details on storage layout and isolation, see [Sandbox Proxy Isolation](../deployment/sandbox-proxy.md).

## Private Instances

Use `private` when one shared agent definition should behave like a template that materializes a separate requester-local instance at runtime.
The YAML definition stays shared.
The private root, copied files, file-memory workspace, and private knowledge path do not.
Private agents cannot participate in teams yet.
That restriction also applies transitively: a shared team member that reaches a private agent through `delegate_to` is rejected.

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
Workers mount the same canonical private-instance root when they execute that requester scope.
For a `mind` agent with `private.per: user`, different users get different private `mind_data/` trees even though the agent definition is shared.

### Private Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `private.per` | `user` or `user_agent` | *required* | Which requester boundary gets its own private instance of the agent's state. MindRoom also uses that same boundary for the agent's internal execution scope |
| `private.root` | string | `<agent_name>_data` | Private root name under the canonical private-instance state root. Must be a relative path and cannot escape with `..` |
| `private.template_dir` | string | `null` | Optional local directory copied recursively into each private root without overwriting existing files. Relative paths are resolved from `config.yaml`, and absolute paths are also allowed. MindRoom raises an error when the directory does not exist |
| `private.context_files` | list | `null` | Optional files loaded into role context from inside the private root. Each path is relative to the private root and cannot escape it |
| `private.knowledge` | object | `null` | Optional PrivateAgentKnowledge indexed from inside the private root. Sub-fields below. See [Knowledge Bases](../knowledge.md#private-agent-knowledge) |
| `private.knowledge.enabled` | bool | `true` | Whether to index PrivateAgentKnowledge for this private agent instance. Set to `false` to disable indexing |
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
- Private agents cannot participate in teams yet.
- Shared team members that reach a private agent through `delegate_to` are rejected for the same reason.
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

## Thread Mode Resolution

Thread mode is resolved per message using the current room ID.
For an agent, MindRoom checks `room_thread_modes` in this order.
First, it checks an exact room ID key.
Second, it checks the managed room key/alias associated with that room ID.
Third, it resolves each configured `room_thread_modes` key to a room ID and matches that against the current room.
If none match, it falls back to `thread_mode`.

For a team, MindRoom resolves mode per member agent for that room.
If all member agents resolve to the same mode, the team uses that mode.
If member modes differ, the team defaults to `thread`.

For the router, MindRoom resolves mode using agents relevant to the active room.
This includes agents directly configured for the room and agents included via `teams.<name>.rooms`.
If all relevant agents resolve to the same mode, the router uses that mode.
If modes are mixed, the router defaults to `thread`.

## File-Based Context Loading

You can inject file content directly into an agent's role context without using a knowledge base.

`context_files` behavior:

- Paths are relative to the agent's workspace (`agents/<name>/workspace/`)
- `private.context_files` paths are resolved relative to the effective private root
- Existing files are loaded in list order and added under `Personality Context`
- Missing files are skipped with a warning in logs

MindRoom loads the files when it builds an agent instance.
The normal Matrix and OpenAI-compatible reply paths build fresh agent instances per reply/request, so editing a context file affects the next reply without restarting the process.

## Agent Delegation

Agents can delegate tasks to other agents using the `delegate_to` field. When configured, a delegation tool is automatically added to the agent — no need to include `"delegate"` in the `tools` list.

The delegated agent runs as a fresh, one-shot instance with no shared session or history. It executes the task and returns its response as the tool result.

```yaml
agents:
  leader:
    display_name: Leader
    role: Orchestrate tasks by delegating to specialist agents
    model: sonnet
    delegate_to: [code, research]
    rooms: [lobby]

  code:
    display_name: CodeAgent
    role: Generate code, manage files
    model: sonnet
    tools: [file, shell]
    delegate_to: [research]  # can further delegate
    rooms: [lobby]

  research:
    display_name: ResearchAgent
    role: Research topics and provide summaries
    model: sonnet
    tools: [duckduckgo]
    rooms: [lobby]
```

**Constraints:**

- Targets must reference existing agent names in the config
- An agent cannot delegate to itself
- Recursive delegation is supported (agent A delegates to B, B delegates to C) up to a maximum depth of 3

## Naming Rules

Agent and team YAML keys must contain only alphanumeric characters and underscores (matching `^[a-zA-Z0-9_]+$`).
Agent and team names must be distinct — the same key cannot appear in both `agents:` and `teams:`.

## Rich Prompt Agents

Certain agent names (the YAML key, not `display_name`) have built-in rich prompts:

`code`, `research`, `calculator`, `general`, `shell`, `summary`, `finance`, `news`, `data_analyst`

When using these names, the built-in prompt replaces the `role` field and any custom `instructions` are ignored.

## Defaults

The `defaults` section sets fallback values for all agents. Any agent that omits a setting inherits the value from here.

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
  show_stop_button: true                # Show a stop button while agent is responding (global-only, cannot be overridden per-agent)
  num_history_runs: null                # Number of prior runs to include (null = all)
  num_history_messages: null            # Max messages from history (null = use num_history_runs)
  enable_streaming: true                # Stream agent responses via progressive message edits
  streaming:
    update_interval: 5.0                # Steady-state seconds between streamed edits
    min_update_interval: 0.5            # Fast-start seconds between early edits
    interval_ramp_seconds: 15.0         # Set 0 to disable interval ramping
    max_idle: 2.0                       # Event-driven idle ceiling before the next edit
  compress_tool_results: false          # Safer default; enabling can invalidate Anthropic/Vertex Claude prompt caches
  # Auto-compaction is disabled until you author this block.
  # compaction:
  #   enabled: true
  #   threshold_percent: 0.8
  #   reserve_tokens: 16384
  max_tool_calls_from_history: null     # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true                 # Show tool-call markers and trace metadata; hidden mode still allows generic worker warmup copy
  worker_tools: null                     # Tool names to route through workers (null = use MindRoom's default routing policy, [] = disable)
  worker_scope: null                     # Worker runtime reuse for proxied tools (shared, user, user_agent)
  allow_self_config: false               # Allow agents to read/modify their own config at runtime
```

`defaults.streaming` is global-only and controls the timing of progressive message edits for streaming responses.

To opt out a specific agent:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Focus on deep research
    include_default_tools: false
    tools: [web_search]
```
