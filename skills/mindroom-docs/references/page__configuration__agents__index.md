# Agent Configuration

This page covers the `agents:` section of `config.yaml`, where each agent is one AI actor with its own model, tools, rooms, and instructions.
It also covers requester-private agents, files preloaded into an agent's prompt, naming rules, and the `defaults:` section that every agent inherits from.

## Basic Agent

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful AI assistant
    model: sonnet
    rooms: [lobby]
```

The key (`assistant`) is the agent's name, `model` names an entry under `models:`, and `rooms` lists the rooms the agent joins.

A coding agent that runs its code tools in a worker, preloads workspace notes, and replies as plain room messages in a bridged room:

```yaml
agents:
  developer:
    display_name: Developer
    role: Generate code, manage files, execute shell commands
    model: sonnet
    tools:
      - file
      - github
      - shell:
          extra_env_passthrough: "DAWARICH_*"
    worker_tools: [shell, file]
    instructions:
      - Always read files before modifying them
    context_files: [AGENTS.md, USER.md]
    rooms: [lobby, dev, bridge_telegram]
    room_thread_modes:
      bridge_telegram: room
    access:
      members_of_rooms: [dev]
    delegate_to: [research]
```

To keep `defaults.tools` away from one agent:

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Focus on deep research
    include_default_tools: false
    tools: [duckduckgo]
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `display_name` | string | *required* | Name shown in Matrix |
| `role` | string | `""` | System prompt describing the agent's purpose and expertise |
| `model` | string | `"default"` | Key of an entry under `models:` |
| `tools` | list | `[]` | Tool names or single-key dicts with per-agent overrides; see [Tools](https://docs.mindroom.chat/tools/) and [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration). The agent gets these plus `defaults.tools`, without duplicates |
| `include_default_tools` | bool | `true` | Set `false` to leave out `defaults.tools` and its overrides for this agent |
| `skills` | list | `[]` | Skill names the agent can use; see [Skills](https://docs.mindroom.chat/skills/) |
| `skill_learning` | object | disabled | [Automatic skill learning](https://docs.mindroom.chat/skills/#settings), separate from `learning` |
| `instructions` | list | `[]` | Extra lines appended to the system prompt after the role |
| `minimal_instructions` | list | `[]` | Guidance included on every minimal-mode request; see [Prompt and `minimal_instructions`](https://docs.mindroom.chat/tools/agent-cli/#prompt-and-minimal_instructions) |
| `rooms` | list | `[]` | Room keys, aliases, or Matrix room IDs to join; missing managed rooms are created (see [Rooms](https://docs.mindroom.chat/rooms/)) |
| `accept_invites` | bool or list | `true` | `true` accepts every room invitation, `false` or `[]` accepts none, and a list accepts only inviters matching an exact or wildcard Matrix user ID. Rooms joined this way are kept across restarts. Joining a room never grants its members access; see [Authorization](https://docs.mindroom.chat/authorization/) |
| `access` | object | `null` | Who may converse with the agent: `current_room_members`, `members_of_rooms`, and `users`. When omitted, members of the agent's own managed `rooms` have access. See [Responder access](https://docs.mindroom.chat/authorization/#responder-access) |
| `credential_managers` | list | `[]` | Concrete Matrix user IDs (no wildcards) allowed to manage this agent's credentials and shared OAuth connections; grants no conversation access. See [OAuth](https://docs.mindroom.chat/oauth-framework/) |
| `participation` | object | `null` | Opt into adaptive replies in existing multi-human threads; see [Adaptive Participation](https://docs.mindroom.chat/configuration/threads/#adaptive-participation) |
| `mid_turn` | object | `null` | Judge whether messages queued during a response can wait until it finishes; see [Mid-Turn Coalescing](https://docs.mindroom.chat/configuration/threads/#mid-turn-coalescing) |
| `thread_mode` | string | `"thread"` | `thread` replies in Matrix threads; `room` sends plain room messages with one continuous conversation per room, which suits bridges (Telegram, Signal, WhatsApp) and mobile clients |
| `room_thread_modes` | map | `{}` | Per-room `thread` or `room` overrides keyed by room key, alias, or Matrix room ID; see [Thread Mode Resolution](https://docs.mindroom.chat/configuration/threads/#thread-mode-resolution) |
| `markdown` | bool | `null` | Instruct the agent to format replies as Markdown |
| `learning` | bool | `null` | Enable [Agno Learning](https://docs.mindroom.chat/memory/#agno-learning), a persistent profile of user preferences |
| `learning_mode` | string | `null` | `always` learns after every turn; `agentic` lets the agent decide through a tool call |
| `memory_backend` | string | `null` | `mem0`, `file`, or `none`, overriding `memory.backend`; `none` disables memory but not `learning`. See [Memory](https://docs.mindroom.chat/memory/) |
| `memory_search` | object | `null` | File-memory search override (`mode`, `include`, `include_entrypoint`) when the backend is `file`; omitted fields inherit `memory.search`. See [Searching file memory](https://docs.mindroom.chat/memory/#searching-file-memory) |
| `knowledge_bases` | list | `[]` | Keys under top-level `knowledge_bases`, each at most once; see [Knowledge Bases](https://docs.mindroom.chat/knowledge/) |
| `context_files` | list | `[]` | Workspace files preloaded into the prompt; see [File-Based Context Loading](#file-based-context-loading) |
| `private` | object | `null` | Give each requester a separate copy of the agent's state; see [Private Instances](#private-instances) |
| `num_history_runs`, `num_history_messages`, `compress_tool_results`, `max_tool_calls_from_history` | | | See [History Settings](https://docs.mindroom.chat/configuration/history/#history-settings) |
| `compaction` | object | `defaults.compaction` | Per-agent compaction overrides; `enabled: false` turns automatic compaction off. See [Agent Compaction Settings](https://docs.mindroom.chat/configuration/history/#agent-compaction-settings) |
| `max_tool_calls_per_turn` | int, >= 1 | `null` | Tool calls one turn may execute. Further calls return a tool error, and after this many plus two model requests the turn ends with the text produced so far, which also stops loops of unknown or malformed tool calls |
| `show_tool_calls` | bool | `null` | Show tool-call markers and trace metadata in Matrix messages; see [Tool Calls During Streaming](https://docs.mindroom.chat/streaming/#tool-calls-during-streaming) |
| `worker_tools` | list | `null` | Tools to run in an isolated worker instead of the primary process; `[]` runs everything in the primary process. When unset here and in `defaults`, the deployment's [execution mode](https://docs.mindroom.chat/deployment/sandbox-proxy/#execution-modes) decides. See [Worker Routing](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-routing) |
| `worker_scope` | string | `null` | How worker runtimes are shared: `shared` (one per agent), `user` (one per user, across agents), or `user_agent` (one per user and agent). Not allowed together with `private`. See [Worker scopes](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-scopes) |
| `file_access` | string | `null` | `workspace` limits path-taking tools such as `file`, `coding`, `attachments`, and `matrix_message` to the agent workspace and its attachments; `unrestricted` allows any path the tool's process can reach. `shell`, `python`, and other code-execution tools are never confined; isolate them with `worker_tools`. See [File access](https://docs.mindroom.chat/architecture/security-posture/#file-access) |
| `allow_self_config` | bool | `null` | Give the agent a tool to read and change its own entry under `agents:`; see [`self_config`](https://docs.mindroom.chat/tools/agent-orchestration/#self_config) |
| `delegate_to` | list | `[]` | Agents this agent may run as subagents, including itself only when listed; see [Agent Delegation](https://docs.mindroom.chat/tools/agent-orchestration/#agent-delegation) |
| `thread_exports` | bool or object | `null` | Keep YAML exports of the agent's threads under `<workspace>/thread_exports/`; `true` uses the defaults. See [Thread Exports](https://docs.mindroom.chat/thread-exports/#thread-exports) |

Unset fields that appear in the inheritance table under [Defaults](#defaults) take the `defaults` value; a per-agent value overrides it.
`memory_backend` and `memory_search` inherit from the top-level `memory:` section instead.

## Private Instances

Use `private` when one shared agent definition should give each requester their own workspace, file memory, and knowledge index.
The YAML stays shared, while each requester's files live in their own private root.

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
      context_files: [SOUL.md, USER.md, MEMORY.md]
      knowledge:
        path: memory
        watch: false
    knowledge_bases: [company_docs]
```

Here every user gets their own `mind_data/` root, seeded from `./mind_template/` (for example `SOUL.md`, `USER.md`, `MEMORY.md`, and a `memory/` folder), while `company_docs` stays shared by everyone.
Private roots live at `<storage>/private_instances/<requester scope>/<agent>/<private.root>/`, not next to `config.yaml`, and dedicated workers mount only that root.

How a private agent behaves:

- The template directory is copied into each new private root, and template files added later are copied in without overwriting requester edits.
- The private root is created even without a template.
- `private.context_files` load from the private root; agent-level `context_files` still load from the shared agent workspace.
- With `memory_backend: file`, the private root is the requester's file-memory root; with any other backend, private files exist but are not file memory, and `none` turns memory off.
- Nothing is enabled implicitly: set `memory_backend`, `private.context_files`, and `private.knowledge` explicitly for whatever the template provides; the file names are up to you.
- Because `private.per` sets the agent's worker scope, configuring both `private` and `worker_scope` fails with `Private agents derive their execution scope from private.per; configure private or worker_scope, not both`.
- Private agents cannot be configured as team members, and a shared team member cannot reach one through `delegate_to`.
  Tagging a private agent together with other agents in a message still forms an ad hoc team for that requester.
- A private agent runs only for a known requester; otherwise it fails with `Private agent '<name>' requires an active execution identity to resolve requester-local state`.

### Private Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `private.per` | `user` or `user_agent` | *required* | Requester boundary that gets its own private instance; it also sets the agent's [worker scope](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-scopes) |
| `private.root` | string | `<agent_name>_data` | Relative directory name of the private root; cannot be absolute, contain `..`, or start with `sessions`, `learning`, `chroma`, `knowledge_db`, `memory_files`, `calls`, `agent_modes.json`, `agent_modes.lock`, `browser`, `browser-profiles`, or `.sessions-recovery.lock` |
| `private.template_dir` | string | `null` | Directory copied into each private root; relative paths resolve from `config.yaml`, absolute paths are allowed, and a missing directory is a config error (`Agent '<name>' has invalid private.template_dir`) |
| `private.context_files` | list | `null` | Private-root-relative files preloaded into the prompt; cannot escape the private root |
| `private.knowledge` | object | `null` | Per-requester knowledge index built from the private root; see [Private Agent Knowledge](https://docs.mindroom.chat/knowledge/#private-agent-knowledge). Omit it or set `enabled: false` for no index |
| `private.knowledge.enabled` | bool | `true` | Whether to index private knowledge |
| `private.knowledge.description` | string | `""` | What the private knowledge contains, shown to the agent in the `search_knowledge_base` tool description |
| `private.knowledge.path` | string | *required* when enabled | Knowledge directory relative to the private root (`.` allowed) |
| `private.knowledge.watch` | bool | `true` | Refresh the index in the background on access; when `false`, external edits need an explicit refresh |
| `private.knowledge.chunk_size` | int | `5000` | Maximum characters per indexed chunk (min: 128) |
| `private.knowledge.chunk_overlap` | int | `0` | Overlapping characters between adjacent chunks; must be smaller than `chunk_size` |
| `private.knowledge.git` | object | `null` | Git sync, with the same schema as `knowledge_bases.<id>.git`; needs a dedicated `path` subtree, see [Private Agent Knowledge](https://docs.mindroom.chat/knowledge/#private-agent-knowledge) |

## File-Based Context Loading

`context_files` inlines files into the agent's prompt under a `Personality Context` section, without a knowledge base.

- Paths are relative to the agent's workspace, `agents/<name>/workspace/` in the storage directory; absolute paths and `..` are rejected.
  With `memory_backend: file`, the same workspace holds the agent's file memory.
- Files load in list order, each headed by its resolved path so the agent knows where to edit it.
- Missing files are skipped with a warning in the logs.
- Edits take effect on the next reply without a restart.

`defaults.max_preload_chars` (default `50000`) caps the section.
When it is exceeded, MindRoom drops whole files from the start of the list first and then trims the last remaining file from its end.
Each affected file keeps its path and a marker with the omitted character count, so the agent can open the file for the rest.
A cap too small to hold the headings and markers fails with `max_preload_chars=<n> cannot fit required context headings and omission markers`.

## Naming Rules

Agent and team keys may contain only letters, digits, and underscores (`^[a-zA-Z0-9_]+$`).
The same key cannot appear under both `agents:` and `teams:`.
`router`, `user`, and `_shared` are reserved.

## Defaults

The `defaults` section sets values every agent inherits unless it sets its own, plus global-only settings that cannot be overridden per agent.

```yaml
defaults:
  tools: [scheduler]
  learning_mode: agentic
  max_tool_calls_per_turn: 200
  worker_tools: [shell, file, python]
  enable_streaming: true
  max_preload_chars: 50000
```

These defaults apply to each agent that leaves the same field unset:

| Field | Default |
|-------|---------|
| `markdown` | `true` |
| `learning` | `true` |
| `learning_mode` | `always` |
| `num_history_runs`, `num_history_messages`, `max_tool_calls_from_history` | `null` (no limit) |
| `compress_tool_results` | `false` |
| `compaction` | enabled; see [Agent Compaction Settings](https://docs.mindroom.chat/configuration/history/#agent-compaction-settings) |
| `max_tool_calls_per_turn` | `1000` |
| `show_tool_calls` | `true` |
| `worker_tools` | `null` (the deployment's execution mode decides) |
| `worker_scope` | `null` |
| `file_access` | `workspace` |
| `allow_self_config` | `false` |

`defaults.tools` (default `[scheduler]`) is added to every agent with `include_default_tools: true`; set it to `[]` to add nothing.
It accepts the same per-tool overrides as `agents.<name>.tools`.

These settings are global-only:

| Field | Default | Description |
|-------|---------|-------------|
| `enable_streaming` | `true` | Show replies as progressive message edits; see [Streaming](https://docs.mindroom.chat/streaming/#configuration) |
| `streaming` | see [Streaming](https://docs.mindroom.chat/streaming/#configuration) | Timing settings for progressive edits |
| `large_message_strategy` | `sidecar` | Oversized replies as a preview with the full text attached (`sidecar`) or as several complete messages (`split`); see [Large Messages](https://docs.mindroom.chat/streaming/#large-messages) |
| `coalescing.debounce_ms` | `1000` | Milliseconds (>= 0) to wait after media for more attachments or a trailing caption before replying; text replies immediately |
| `show_stop_button` | `true` | Add a 🛑 reaction while an agent responds; see [Stop Button](https://docs.mindroom.chat/chat-commands/#stop-button) |
| `max_consecutive_agent_replies` | `50` | Consecutive agent or team messages (>= 1) before agents stop waking each other; see [Agents mentioning other agents](https://docs.mindroom.chat/authorization/#agents-mentioning-other-agents) |
| `max_preload_chars` | `50000` | Cap (>= 1) on preloaded context files; see [File-Based Context Loading](#file-based-context-loading) |
| `tool_output_auto_save_threshold_bytes` | `51200` | Larger tool outputs are saved to the workspace; see [Workspace and tool output files](https://docs.mindroom.chat/tools/execution-and-coding/#workspace-and-tool-output-files) |
| `worker_grantable_credentials` | `null` | Shared credential services available inside isolated workers (`null` grants none); Google OAuth client and token services and `google_vertex_adc` cannot be granted. See [Credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases) |
| `thread_summary_model`, `thread_summary_temperature`, `thread_summary_first_threshold`, `thread_summary_subsequent_interval` | `null`, `0.2`, `1`, `10` | Automatic thread summaries; see [Automatic Thread Summaries](https://docs.mindroom.chat/tools/matrix-and-attachments/#automatic-thread-summaries) |
