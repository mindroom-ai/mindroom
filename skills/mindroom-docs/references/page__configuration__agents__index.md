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

    # Memory backend override for this agent (optional: mem0 or file)
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
    compress_tool_results: true
    enable_session_summaries: false
    max_tool_calls_from_history: null
```

## Configuration Options

| Option                        | Type   | Default     | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------- | ------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `display_name`                | string | *required*  | Human-readable name shown in Matrix as the bot's display name                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `role`                        | string | `""`        | System prompt describing the agent's purpose — guides its behavior and expertise                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `model`                       | string | `"default"` | Model name (must match a key in the `models` section)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `tools`                       | list   | `[]`        | Agent-specific tool names (see [Tools](https://docs.mindroom.chat/tools/index.md)); effective tools are `tools + defaults.tools` with duplicates removed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `include_default_tools`       | bool   | `true`      | When `true`, append `defaults.tools` to this agent's `tools`; set to `false` to opt this agent out                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `skills`                      | list   | `[]`        | Skill names the agent can use (see [Skills](https://docs.mindroom.chat/skills/index.md))                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `instructions`                | list   | `[]`        | Extra lines appended to the system prompt after the role                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `rooms`                       | list   | `[]`        | Room aliases to auto-join; rooms are created if they don't exist                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `markdown`                    | bool   | `null`      | When enabled, the agent is instructed to format responses as Markdown. Inherits from `defaults.markdown` (default: `true`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `learning`                    | bool   | `null`      | Enable [Agno Learning](https://docs.agno.com/agents/learning) — the agent builds a persistent profile of user preferences and adapts over time. Inherits from `defaults.learning` (default: `true`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `learning_mode`               | string | `null`      | `always`: agent automatically learns from every interaction. `agentic`: agent decides when to learn via a tool call. Inherits from `defaults.learning_mode` (default: `"always"`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `memory_backend`              | string | `null`      | Memory backend override for this agent (`"mem0"` or `"file"`). Inherits from global `memory.backend` when omitted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `private`                     | object | `null`      | Optional requester-private state for one shared agent definition. `private.per` defines which requester boundary gets a separate private instance of the agent's state. Private agents must not set `worker_scope`. Internally, MindRoom reuses that same requester boundary for worker execution, but `private.per` is still a different public config concept from `worker_scope`. `private.root` defaults to `<agent_name>_data`, `private.template_dir` copies a local template into each requester root without overwriting existing files, `private.context_files` loads private-root-relative files into role context, and `private.knowledge` adds requester-local knowledge indexed from that private root. `private` does not implicitly enable file memory, context files, or private knowledge, and private agents cannot participate in teams yet |
| `knowledge_bases`             | list   | `[]`        | Knowledge base IDs from top-level `knowledge_bases` — gives the agent RAG access to the indexed documents                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `context_files`               | list   | `[]`        | File paths (relative to the agent's workspace) loaded into each agent instance and prepended to role context (under `Personality Context`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `thread_mode`                 | string | `"thread"`  | `thread`: responses are sent in Matrix threads (default). `room`: responses are sent as plain room messages with a single persistent session per room — ideal for bridges (Telegram, Signal, WhatsApp) and mobile                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `room_thread_modes`           | map    | `{}`        | Per-room thread mode overrides keyed by room alias/name or Matrix room ID. Values are `thread` or `room`. Overrides apply before `thread_mode` fallback                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `num_history_runs`            | int    | `null`      | Number of prior Agno runs to include as history context (`null` = all). Mutually exclusive with `num_history_messages`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `num_history_messages`        | int    | `null`      | Max messages from history. Mutually exclusive with `num_history_runs`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `compress_tool_results`       | bool   | `null`      | Compress tool results in history to save context. Inherits from `defaults.compress_tool_results` (default: `true`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `enable_session_summaries`    | bool   | `null`      | Generate AI summaries of older conversation segments for compaction (each summary costs an extra LLM call). Inherits from `defaults.enable_session_summaries` (default: `false`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `max_tool_calls_from_history` | int    | `null`      | Limit tool call messages replayed from history (`null` = no limit)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `show_tool_calls`             | bool   | `null`      | Show tool-call markers and trace metadata in Matrix messages. Inherits from `defaults.show_tool_calls` (default: `true`). When `false`, inline markers and `io.mindroom.tool_trace` are omitted from sent Matrix message content. Note: this flag is not currently enforced by the OpenAI-compatible `/v1/chat/completions` path.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `worker_tools`                | list   | `null`      | Tool names to run in the [sandbox proxy](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) instead of the main process. Inherits from `defaults.worker_tools`. When omitted everywhere, MindRoom uses its built-in default. Set to `[]` to disable proxying for this agent                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `worker_scope`                | string | `null`      | How sandbox runtimes are shared for non-private agents. `shared`: one per agent. `user`: one per user (shared across agents). `user_agent`: one per user+agent pair. Inherits from `defaults.worker_scope`. Do not set this when the agent uses `private`, because `private.per` already defines the requester partition for that agent                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `allow_self_config`           | bool   | `null`      | Give this agent a scoped tool to read and modify its own configuration at runtime. Inherits from `defaults.allow_self_config` (default: `false`). Lighter-weight alternative to the `config_manager` tool                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `delegate_to`                 | list   | `[]`        | Agent names this agent can delegate tasks to via tool calls (see [Agent Delegation](#agent-delegation))                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

Each entry in `knowledge_bases` must match a key under `knowledge_bases` in `config.yaml`.

Per-agent fields with a `null` default inherit from the `defaults` section at runtime. Per-agent values override them. `memory.backend` is the global memory default, and `agents.<name>.memory_backend` overrides it per agent. `show_stop_button` and `enable_streaming` are global-only settings in `defaults` and cannot be overridden per-agent. The dashboard Agents tab exposes this as the **Memory Backend** selector for each agent.

Learning data is persisted under `agents/<name>/learning/<agent>.db`, so it survives container restarts when the storage directory is mounted. `context_files` are resolved relative to the agent's workspace directory (`agents/<name>/workspace/`). When the effective memory backend is `file`, the agent's canonical file memory root is that same workspace directory. Absolute paths and `..` traversal are rejected.

## Worker Routing

`worker_tools` decides which tools run in the sandbox proxy instead of the main MindRoom process. When omitted, MindRoom routes `coding`, `file`, `python`, and `shell` through the proxy by default. `worker_scope` controls how those sandbox runtimes are reused between calls. Six integrations are shared-only (they require `worker_scope` unset or `shared`): `google`, `spotify`, `gmail`, `google_calendar`, `google_sheets`, and `homeassistant`. Of those, `gmail`, `google_calendar`, `google_sheets`, and `homeassistant` also always stay local regardless of `worker_tools` (they are never proxied to the sandbox). `google` and `spotify` can still be proxied through the sandbox.

The supported `worker_scope` values are:

- `shared`: one runtime per agent, shared by all users.
- `user`: one runtime per user, shared across that user's agents.
- `user_agent`: one runtime per user+agent pair.

Leave `worker_scope` unset for unscoped execution — calls still run in the sandbox, but each call gets a fresh runtime instead of a persistent one. `worker_scope` also affects dashboard credential support and OpenAI-compatible agent eligibility.

### Filesystem Isolation

`worker_scope` controls runtime reuse, not filesystem security. When the effective memory backend is `file`, tools like `shell`, `file`, `python`, and `coding` get a default working directory (`base_dir`) at the agent's canonical workspace root. Without file-backed workspace state, those tools keep their normal defaults such as the current directory. Even when set, `base_dir` is a convenience, not a hard boundary.

Isolation depends on the worker backend:

- **Kubernetes dedicated workers** (`shared`, `user_agent`, unscoped): the runtime can only see its own agent's storage directory plus its worker-local scratch space. This is the strongest isolation available today.
- **Kubernetes dedicated workers** (`user`): the runtime can see all agents' storage, because `user` mode intentionally shares one runtime across multiple agents for a single user. Treat this as a shared workstation.
- **Shared-runner and local backends**: no hard filesystem boundary today, regardless of scope.

Use `user_agent` if you need per-agent filesystem isolation.

### Where Agent Data Lives

Agents without `private` store all their data in one canonical directory: `agents/<name>/` (context files, workspace, memory, sessions, learning). Changing `worker_scope` changes how tool runtimes are isolated. It does **not** change where that non-private agent's data lives. All runtimes for the same non-private agent read and write the same storage directory. If multiple runtimes run concurrently, files and databases in that directory must tolerate concurrent access. Agents that use `private` are different. They materialize one canonical state root per requester-scoped private instance under `private_instances/<scope-key>/<agent>/`. Workers mount those canonical private-instance roots. They do not own them.

The dashboard credential UI only works for unscoped agents and agents with `worker_scope=shared`. Agents using `user` or `user_agent` manage credentials through their worker runtime instead.

For more details on storage layout and isolation, see [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md).

## Private Instances

Use `private` when one shared agent definition should behave like a template that materializes a separate requester-local instance at runtime. The YAML definition stays shared. The private root, copied files, file-memory workspace, and private knowledge path do not. Private agents cannot participate in teams yet. That restriction also applies transitively: a shared team member that reaches a private agent through `delegate_to` is rejected.

`private.per` is not a second spelling of `worker_scope`. `private.per` chooses who gets a separate private instance of the agent's state. MindRoom then uses that same requester partition for worker execution, but that is an internal consequence of private execution, not the public meaning of `worker_scope`.

```
knowledge_bases:
  company_docs:
    path: ./company_docs
    watch: true

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
        watch: true
    knowledge_bases: [company_docs]
```

Example template directory:

```
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

In the example above, each requester gets their own effective `mind_data/` root under a canonical private-instance state root in shared storage. That private root is not created next to `config.yaml`. It is not stored under `workers/<worker>/`. Workers mount the same canonical private-instance root when they execute that requester scope. For a `mind` agent with `private.per: user`, different users get different private `mind_data/` trees even though the agent definition is shared.

### Private Fields

| Field                             | Type                   | Default             | Description                                                                                                                                                                                                                                            |
| --------------------------------- | ---------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `private.per`                     | `user` or `user_agent` | *required*          | Which requester boundary gets its own private instance of the agent's state. MindRoom also uses that same boundary for the agent's internal execution scope                                                                                            |
| `private.root`                    | string                 | `<agent_name>_data` | Private root name under the canonical private-instance state root. Must be a relative path and cannot escape with `..`                                                                                                                                 |
| `private.template_dir`            | string                 | `null`              | Optional local directory copied recursively into each private root without overwriting existing files. Relative paths are resolved from `config.yaml`, and absolute paths are also allowed. MindRoom raises an error when the directory does not exist |
| `private.context_files`           | list                   | `null`              | Optional files loaded into role context from inside the private root. Each path is relative to the private root and cannot escape it                                                                                                                   |
| `private.knowledge`               | object                 | `null`              | Optional requester-local knowledge indexed from inside the private root. Sub-fields below. See [Knowledge Bases](https://docs.mindroom.chat/knowledge/#private-agent-knowledge)                                                                        |
| `private.knowledge.enabled`       | bool                   | `true`              | Whether to index requester-local knowledge for this private agent instance. Set to `false` to disable indexing                                                                                                                                         |
| `private.knowledge.path`          | string                 | `null`              | Path to a private knowledge directory relative to the private root                                                                                                                                                                                     |
| `private.knowledge.watch`         | bool                   | `true`              | Watch the private knowledge directory for changes and auto-reindex                                                                                                                                                                                     |
| `private.knowledge.chunk_size`    | int                    | `5000`              | Maximum characters per indexed chunk (min: 128)                                                                                                                                                                                                        |
| `private.knowledge.chunk_overlap` | int                    | `0`                 | Overlapping characters between adjacent chunks (min: 0)                                                                                                                                                                                                |
| `private.knowledge.git`           | object                 | `null`              | Optional Git sync configuration for requester-local private knowledge (same schema as top-level `knowledge_bases.<id>.git`)                                                                                                                            |

### Runtime Behavior

1. MindRoom resolves the canonical private-instance state root from `private.per`.
1. MindRoom creates the effective private root inside that canonical private-instance state root.
1. If `private.template_dir` is set, MindRoom copies the template directory into the private root without overwriting files that already exist there.
1. MindRoom loads any `private.context_files` from that private root when the agent is created or reloaded.
1. If `memory_backend: file` is enabled, MindRoom uses that same private root as the file-memory root for that requester.
1. If `private.knowledge.path` is configured, MindRoom indexes that private-root-relative path as requester-local knowledge for that requester only.

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
- Set `private.context_files` explicitly for any copied files you want loaded into role context.
- Set `private.knowledge.path` explicitly for any copied files or folders you want indexed as requester-local knowledge.
- Omit `private.knowledge` entirely, or set `private.knowledge.enabled: false`, when you do not want requester-local knowledge indexing.
- `private` cannot be combined with `worker_scope`.
- Top-level `knowledge_bases` remain shared or company-wide corpora, so one agent can use both requester-local knowledge and shared knowledge in the same run.
- Top-level `context_files` remain the shared workspace-relative mechanism used by single-user setups, including the default `mindroom config init` output.
- Custom templates are fully supported.
- The Mind-style filenames shown above are a convention, not a requirement, unless you choose to reference them in `private.context_files` or `private.knowledge.path`.

## Thread Mode Resolution

Thread mode is resolved per message using the current room ID. For an agent, MindRoom checks `room_thread_modes` in this order. First, it checks an exact room ID key. Second, it checks the managed room key/alias associated with that room ID. Third, it resolves each configured `room_thread_modes` key to a room ID and matches that against the current room. If none match, it falls back to `thread_mode`.

For a team, MindRoom resolves mode per member agent for that room. If all member agents resolve to the same mode, the team uses that mode. If member modes differ, the team defaults to `thread`.

For the router, MindRoom resolves mode using agents relevant to the active room. This includes agents directly configured for the room and agents included via `teams.<name>.rooms`. If all relevant agents resolve to the same mode, the router uses that mode. If modes are mixed, the router defaults to `thread`.

## File-Based Context Loading

You can inject file content directly into an agent's role context without using a knowledge base.

`context_files` behavior:

- Paths are relative to the agent's workspace (`agents/<name>/workspace/`)
- `private.context_files` paths are resolved relative to the effective private root
- Existing files are loaded in list order and added under `Personality Context`
- Missing files are skipped with a warning in logs

MindRoom loads the files when it builds an agent instance. The normal Matrix and OpenAI-compatible reply paths build fresh agent instances per reply/request, so editing a context file affects the next reply without restarting the process.

## Agent Delegation

Agents can delegate tasks to other agents using the `delegate_to` field. When configured, a delegation tool is automatically added to the agent — no need to include `"delegate"` in the `tools` list.

The delegated agent runs as a fresh, one-shot instance with no shared session or history. It executes the task and returns its response as the tool result.

```
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

Agent and team YAML keys must contain only alphanumeric characters and underscores (matching `^[a-zA-Z0-9_]+$`). Agent and team names must be distinct — the same key cannot appear in both `agents:` and `teams:`.

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
  max_preload_chars: 50000              # Hard cap for preloaded context from context_files
  show_stop_button: true                # Show a stop button while agent is responding (global-only, cannot be overridden per-agent)
  num_history_runs: null                # Number of prior runs to include (null = all)
  num_history_messages: null            # Max messages from history (null = use num_history_runs)
  enable_streaming: true                # Stream agent responses via progressive message edits
  compress_tool_results: true           # Compress tool results in history to save context
  enable_session_summaries: false       # AI summaries of older conversation segments (costs extra LLM call)
  max_tool_calls_from_history: null     # Limit tool call messages replayed from history (null = no limit)
  show_tool_calls: true                 # Show tool-call markers and trace metadata in message content
  worker_tools: null                     # Tool names to route through workers (null = use MindRoom's default routing policy, [] = disable)
  worker_scope: null                     # Worker runtime reuse for proxied tools (shared, user, user_agent)
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
