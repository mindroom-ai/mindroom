---
icon: lucide/user
---

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
| `matrix_username` | string or null | `null` | Exact localpart of the Matrix account MindRoom creates for the agent, such as `jarvis` for `@jarvis:example.com`, using lowercase letters, digits, `.`, `_`, `=`, `-`, or `/`. When unset, the account is `mindroom_<agent key>`; unlike that default name, `matrix_username` gets no `_<MINDROOM_NAMESPACE>` suffix. MindRoom uses it when it creates the account, so set it before the agent's first start; changing it to another name later stops startup with `agents.<agent key>.matrix_username cannot be changed after the agent's Matrix account has been created.`, and removing it keeps the existing account. Installs paired with a provisioning service, such as hosted `mindroom.chat`, can register only `mindroom_` names the service allows, so a name such as `jarvis` is refused there. To use an account that already exists, see [Use an Existing Matrix Account](../matrix.md#use-an-existing-matrix-account) |
| `role` | string | `""` | System prompt describing the agent's purpose and expertise |
| `model` | string | `"default"` | Key of an entry under `models:` |
| `cheap_model` | string or null | `null` | Cheaper model key this agent may switch to; used by `cold_cache`. Inherits `defaults.cheap_model`. See [Cold-Cache Model](models.md#cold-cache-model) |
| `tools` | list | `[]` | Tool names or single-key dicts with per-agent overrides; see [Tools](../tools/index.md) and [Per-Agent Tool Configuration](../tools/index.md#per-agent-tool-configuration). The agent gets these plus `defaults.tools`, without duplicates |
| `include_default_tools` | bool | `true` | Set `false` to leave out `defaults.tools` and its overrides for this agent |
| `skills` | list | `[]` | Skill names the agent can use; see [Skills](../skills.md) |
| `skill_learning` | object | disabled | [Automatic skill learning](../skills.md#settings), separate from `learning` |
| `instructions` | list | `[]` | Extra lines appended to the system prompt after the role |
| `minimal_instructions` | list | `[]` | Guidance included on every minimal-mode request; see [Prompt and `minimal_instructions`](../tools/agent-cli.md#prompt-and-minimal_instructions) |
| `rooms` | list | `[]` | Room keys or Matrix room IDs to join; missing managed rooms are created (see [Rooms](../rooms.md#configuration)) |
| `accept_invites` | bool or list | `true` | `true` accepts every room invitation, `false` or `[]` accepts none, and a list accepts only inviters matching an exact or wildcard Matrix user ID. Rooms joined this way are kept across restarts. Joining a room never grants its members access; see [Authorization](../authorization.md) |
| `access` | object | `null` | Who may converse with the agent: `current_room_members`, `members_of_rooms`, and `users`. When omitted, members of the agent's own managed `rooms` have access. See [Responder access](../authorization.md#responder-access) |
| `credential_managers` | list | `[]` | Concrete Matrix user IDs (no wildcards) allowed to manage this agent's credentials and shared OAuth connections; grants no conversation access. See [OAuth](../oauth-framework.md) |
| `mcp_gateway_shared_credentials` | bool | `false` | Let users who may use this agent call its tools through the [MCP gateway](../deployment/mcp-gateway.md#agent-eligibility) even though its worker scope is `shared` or unset. Every caller then runs them with the agent's shared credentials and worker, without its instructions. Agents with `private` or `worker_scope: user` or `user_agent` are available without it |
| `participation` | object | `null` | Opt into adaptive replies in existing multi-human threads; see [Adaptive Participation](threads.md#adaptive-participation) |
| `mid_turn` | object | `null` | Judge whether messages queued during a response can wait until it finishes; see [Mid-Turn Coalescing](threads.md#mid-turn-coalescing) |
| `cold_cache` | object | `null` | Move an idle thread to `cheap_model` for good when a judge accepts it; see [Cold-Cache Model](models.md#cold-cache-model) |
| `listen` | object | `null` | Hear chosen rooms and act silently after a quiet period; see [Listening](#listening) |
| `contacts` | object | `null` | Let a personal agent talk to other people's personal agents and people after both owners approve; see [Agent Contacts](../agent-contacts.md) |
| `thread_mode` | string | `"thread"` | `thread` replies in Matrix threads; `room` sends plain room messages with one continuous conversation per room, which suits bridges (Telegram, Signal, WhatsApp) and mobile clients |
| `room_thread_modes` | map | `{}` | Per-room `thread` or `room` overrides keyed by room key or Matrix room ID; see [Thread Mode Resolution](threads.md#thread-mode-resolution) |
| `markdown` | bool | `null` | Tell the agent in its system prompt that its replies are shown as Markdown; chat replies are rendered as Markdown either way. Team leaders follow `defaults.markdown`, and voice calls leave the line out |
| `knowledge_graph` | bool | `null` | Build a [knowledge graph](../knowledge-graph.md) from this agent's conversations and give it the `knowledge_graph` tool |
| `knowledge_graph_edits` | bool | `null` | Let the agent correct its knowledge graph: delete wrong facts and entities, merge duplicate entities, and rewrite summaries; needs `knowledge_graph`. See [Letting the Agent Fix Its Graph](../knowledge-graph.md#letting-the-agent-fix-its-graph) |
| `timezone` | string | `null` | IANA timezone for this agent's dates, times, and schedules, such as `Europe/Amsterdam`; inherits the top-level `timezone`. See [Timezone](../scheduling.md#timezone) |
| `memory_backend` | string | `null` | `mem0`, `file`, or `none`, overriding `memory.backend`; `none` disables memory. See [Memory](../memory.md) |
| `memory_search` | object | `null` | File-memory search override (`mode`, `include`, `include_entrypoint`) when the backend is `file`; omitted fields inherit `memory.search`. See [Searching file memory](../memory.md#searching-file-memory) |
| `automations` | list | `null` | Automations such as `prompt_curation`, `dreaming`, or one a plugin provides; `null` inherits the `defaults.automations` entries the agent can run and `[]` turns them off. See [Automations](../scheduling.md#automations) |
| `knowledge_bases` | list | `[]` | Keys under top-level `knowledge_bases`, each at most once; see [Knowledge Bases](../knowledge.md) |
| `context_files` | list | `[]` | Workspace files preloaded into the prompt; see [File-Based Context Loading](#file-based-context-loading) |
| `private` | object | `null` | Give each requester a separate copy of the agent's state; see [Private Instances](#private-instances) |
| `num_history_runs`, `num_history_messages`, `max_tool_calls_from_history` | | | See [History Settings](history.md#history-settings) |
| `compaction` | object | `defaults.compaction` | Per-agent compaction overrides; `enabled: false` turns automatic compaction off. See [Agent Compaction Settings](history.md#agent-compaction-settings) |
| `max_tool_calls_per_turn` | int, >= 1 | `null` | Tool calls one turn may execute. Further calls return a tool error, and after this many plus two model requests the turn ends with the text produced so far, which also stops loops of unknown or malformed tool calls |
| `created_agents_per_owner` | int, >= 0 | `0` | How many agents each person may create through this agent with `create_agent`; `0` turns the tool off. It applies to this agent only and is not inherited from `defaults`. See [Created Agents and Self-Updates](../personal-agents.md#created-agents-and-self-updates) |
| `show_tool_calls` | bool | `null` | Show tool-call markers and trace metadata in Matrix messages; see [Tool Calls During Streaming](../streaming.md#tool-calls-during-streaming) |
| `show_reasoning` | bool | `null` | Show the model's reasoning on streamed replies to everyone in the room; see [Reasoning](../streaming.md#reasoning) |
| `worker_tools` | list | `null` | Tools to run in an isolated worker instead of the primary process; `[]` runs everything in the primary process. When unset here and in `defaults`, the deployment's [execution mode](../deployment/sandbox-proxy.md#execution-modes) decides. See [Worker Routing](../deployment/sandbox-proxy.md#worker-routing) |
| `worker_scope` | string | `null` | How worker runtimes are shared: `shared` (one per agent), `user` (one per user, across agents), or `user_agent` (one per user and agent). Not allowed together with `private`. See [Worker scopes](../deployment/sandbox-proxy.md#worker-scopes) |
| `file_access` | string | `null` | `workspace` limits path-taking tools such as `file`, `coding`, `attachments`, and `matrix_message` to the agent workspace and its attachments, and lets `file` and `coding` also use the agent's [shared directories](../tools/execution-and-coding.md#shared_directories); `unrestricted` allows any path the tool's process can reach. `shell`, `python`, and other code-execution tools are never confined; isolate them with `worker_tools`. See [File access](../architecture/security-posture.md#file-access) |
| `allow_self_config` | bool | `null` | Give the agent a tool to read and change its own entry under `agents:`; see [`self_config`](../tools/agent-orchestration.md#self_config) |
| `delegate_to` | list | `[]` | Agents this agent may run as subagents, including itself only when listed; see [Agent Delegation](../tools/agent-orchestration.md#agent-delegation) |
| `dedicated_subagent_workers` | bool | `false` | Let this agent run a subagent in a worker of its own on the Docker or Kubernetes worker backend; see [Dedicated workers](../tools/agent-orchestration.md#dedicated-workers) |
| `thread_exports` | bool or object | `null` | Keep YAML exports of the agent's threads under `<workspace>/thread_exports/`; `true` uses the defaults. See [Thread Exports](../thread-exports.md#thread-exports) |

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
A private agent is still one Matrix account, so use [personal agents](../personal-agents.md) when each person needs an agent of their own that others can mention or add to a team.

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
- It never runs for MindRoom's own router, agent, and team accounts or the internal `mindroom_user` account, for example when a shared agent's automation mentions it or assigns it a todo; such a run fails with `Private agent '<name>' requires a human or bot-account requester to resolve requester-local state`.
  A delegation to it from such a turn is refused with `Cannot delegate to '<name>': a private agent runs only for a person or bot account.`

### Private Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `private.per` | `user` or `user_agent` | *required* | Requester boundary that gets its own private instance; it also sets the agent's [worker scope](../deployment/sandbox-proxy.md#worker-scopes) |
| `private.root` | string | `<agent_name>_data` | Relative directory name of the private root; cannot be absolute, contain `..`, or start with `sessions`, `learning`, `chroma`, `mem0`, `knowledge_db`, `memory_files`, `calls`, `agent_modes.json`, `agent_modes.lock`, `browser`, `browser-profiles`, or `.sessions-recovery.lock` |
| `private.template_dir` | string | `null` | Directory copied into each private root; relative paths resolve from `config.yaml`, absolute paths are allowed, and a missing directory is a config error (`Agent '<name>' has invalid private.template_dir`) |
| `private.context_files` | list | `null` | Private-root-relative files preloaded into the prompt; cannot escape the private root |
| `private.knowledge` | object | `null` | Per-requester knowledge index built from the private root; see [Private Agent Knowledge](../knowledge.md#private-agent-knowledge). Omit it or set `enabled: false` for no index |
| `private.knowledge.enabled` | bool | `true` | Whether to index private knowledge |
| `private.knowledge.description` | string | `""` | What the private knowledge contains, shown to the agent in the `search_knowledge_base` tool description |
| `private.knowledge.path` | string | *required* when enabled | Knowledge directory relative to the private root (`.` allowed) |
| `private.knowledge.watch` | bool | `true` | Refresh the index in the background on access; when `false`, external edits need an explicit refresh |
| `private.knowledge.chunk_size` | int | `5000` | Maximum characters per indexed chunk (min: 128) |
| `private.knowledge.chunk_overlap` | int | `0` | Overlapping characters between adjacent chunks; must be smaller than `chunk_size` |
| `private.knowledge.git` | object | `null` | Git sync, with the same schema as `knowledge_bases.<id>.git`; needs a dedicated `path` subtree, see [Private Agent Knowledge](../knowledge.md#private-agent-knowledge) |

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

## Listening

A listening agent hears every message in chosen rooms from the people it may answer, whether or not the message is addressed to it.
After a room has been quiet for a while, the agent runs one silent turn over what it heard and calls only the tools you list, for example to remember something or react to a message.
It stays a normal agent in those rooms: it still answers when addressed, under the room's usual rules, and listening only adds the silent turns, so a message addressed to it is answered and also heard by the next silent turn.

The agent must already be in each listened room, for example because the room is in its `rooms`; listening does not join rooms, and MindRoom logs once when a listened room is not joined yet.
Every entry in `listen.tools` must also be one of the agent's own `tools`, or the config fails to load.
The default `listen.tools` is `[memory, react]`, so an agent with `listen:` and the default tools needs `memory` and `react` in its `tools:`.
A silent turn narrows the agent's own tools to this list and never grants one the agent lacks, so `worker_scope` checks, tool limits, and reload restarts apply to it as to every other tool.

```yaml
agents:
  scribe:
    display_name: Scribe
    rooms: [lobby]
    tools: [memory, react]
    listen:
      rooms: [lobby]
      quiet_seconds: 120
      tools: [memory, react]
      model: cheap
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `rooms` | list | *required* | Room keys, Matrix room IDs, or aliases to listen in, at least one. Each must resolve the way `room_thread_modes` keys do |
| `quiet_seconds` | int, >= 10 | `60` | Seconds without a message the agent hears before the silent turn runs |
| `max_messages` | int, 1 to 500 | `200` | Most messages one silent turn reads; the rest wait for the next turn |
| `max_runs_per_hour` | int, 1 to 60 | `6` | Most silent turns per hour for the agent, shared by its listened rooms in turn; further due runs are skipped, logged, and what was heard is kept |
| `tools` | list | `[memory, react]` | The only tools a silent turn may call, at least one, as toolkit names or `toolkit.function`. Each toolkit must also be in the agent's `tools`, and some are refused; see [What listed tools can do](#what-listed-tools-can-do) |
| `model` | string or null | `null` | Key of an entry under `models:` for silent turns; unset uses the agent's `model` |

### What a silent turn does

The agent hears every message in a listed room that it may hear.
That uses the same sender authorization its replies use, judged on the person a relay speaks for rather than the relaying account.
Once nothing the agent hears has been said in the room for `quiet_seconds`, one silent turn runs over at most `max_messages` of what it heard.
Messages it does not hear, such as its own or those of a sender it may not hear from, never delay that turn, unless there are more than `max_messages` of them waiting.
More than `max_messages` messages waiting, a message awaiting its refetch, or another agent's reply still being written counts as busy, and what came before waits too; a reply still being written is heard once it is finished.
In a room that never goes quiet, or stays busy, a turn runs anyway once the oldest message waiting is ten times `quiet_seconds` old, and a reply still unfinished by then is heard as it stands.
A turn also hears no more than fits its model's `context_window`, and the rest waits for the next turn.

The turn's own text is never sent to the room, and approval-gated tools and interactive questions are not offered.
It calls only its listen tools, each with its usual effect; see [What listed tools can do](#what-listed-tools-can-do).
No more than `max_runs_per_hour` turns run per hour; the count starts again after a restart.
Each turn runs under the config current when it starts, so a reload that removes a tool or a sender applies to every turn after it.

The first run for a room starts at "now".
The agent never reads history from before listening was turned on, and a room it stops listening in forgets its place.
After a restart, MindRoom repairs the gaps it can, so the agent hears most of what it had not yet heard.
Some messages from a long outage may not be heard.

Only shared agents can listen: `listen` on a private agent, which has no single identity to listen as, fails config load.
A listening agent skips contact rooms, where only a reply passes the contact's message policy.

### What listed tools can do

`listen.tools` is a configuration choice.
A silent turn calls only the tools it names, but each keeps its usual effects, heard text can drive any of them, and some act with the agent's full authority later or elsewhere:

- `scheduler`, one of the default agent tools, schedules a later turn of the agent, which runs as a normal turn with all of the agent's tools, as the agent's own requester.
- `script` starts a background script, and `shell` and other tools that run code, such as `python` and `coding`, run with the agent's worker, workspace, and credentials; they can write any workspace file, `MEMORY.md` included.
- A tool that starts turns, or checks who asked, sees the agent's own account as the requester, because no person asked for a silent turn.
- A tool that sends still sends, for example `matrix_api`, `matrix_voice_message`, `thread_move`, and messengers such as `slack`, `discord`, `telegram`, `whatsapp`, `twilio`, `email`, `gmail`, `outlook_mail`, and `microsoft_teams`.

List these only if you trust everyone whose messages the agent hears.

MindRoom holds a silent turn to a few lines of its own:

- Config load refuses `matrix_message`, presets, and the tools a contact turn never offers: `contacts`, `delegate`, `agent_manager`, `self_config`, `config_manager`, `dynamic_workflow`, `claude_agent`, `skill_manage`, and `dynamic_tools`.
- The turn's tool context carries `listen.tools`, so a tool that builds its own catalog from it, such as the functions a `script` may call, offers nothing outside the list.
- No call may save its result to a file with `mindroom_output_path`, so a tool that writes no files of its own, such as `react`, cannot overwrite one.
- A shell in a silent turn does not offer `mindroom-agent`, because no requester owns a grant for it.

### Privacy and cost

Everyone in a listened room whom the agent may answer is heard, including people who never addressed it.
Heard text is not extracted into memory automatically, and a silent turn stores no run in session history, so a heard message that is later redacted does not linger in a stored run.
What a silent turn writes with its `memory` tool, though, is the agent's shared memory: heard text can resurface in the agent's replies in other rooms and to other users.
So is what it writes with `knowledge_graph`, which an agent with `knowledge_graph` has on its own and which writes the shared graph when `knowledge_graph_edits` is on.
A listed tool that writes files can write heard text into `MEMORY.md` as well (see [What listed tools can do](#what-listed-tools-can-do)).
Leave `memory`, `knowledge_graph`, and such tools out of `listen.tools`, or don't listen, in rooms whose talk must stay there.

A silent turn records its usage under the kind `listening` (see [Usage](../usage.md)).
[Budgets](../budgets.md) do not apply, because no requester asked for the turn.
Bound the cost with `model`, a cheaper alias, and with `max_runs_per_hour`.

### Reactions

The [`react`](../tools/matrix-and-attachments.md#react) tool adds an emoji to a message in the current room, and cannot send messages.
It suits a silent turn that should acknowledge what it heard without writing in the room.

### Known limits

- A failed or timed-out silent turn is retried, and so is one cut off by a restart after its tools have acted, so a memory write or reaction may happen twice.
- A heard message is skipped with a warning when the turns covering it keep failing, after the batch is halved down to that one message, or when it waits more than 10 minutes for a refetch.
  A turn that fails on its provider, unreachable, busy, out of credit, or refusing the credentials, counts toward no skip; the agent's rooms wait a minute and try again.
- A backlog longer than `max_messages`, or a message awaiting its refetch, holds the room's turn for up to ten times `quiet_seconds`.
- A message whose timestamp is already older than the room's place when it arrives, such as one delayed by federation or backdated by a bridge, is not heard; MindRoom reads a room in timestamp order and logs nothing for it.
  The place moves onto a message the agent did not hear only once it is 30 seconds old, or one quiet window if that is shorter, so a message that arrives a moment late is still heard.
- A message stamped later than now, such as one from a server whose clock runs ahead, waits to be heard until that time comes.
- Heard text can carry prompt injection that drives the listed tools; read [Listening agents](../architecture/security-posture.md#listening-agents) before adding a tool to `listen.tools`.

## Naming Rules

Agent and team keys may contain only letters, digits, and underscores (`^[a-zA-Z0-9_]+$`).
The same key cannot appear under both `agents:` and `teams:`.
`router`, `user`, and `_shared` are reserved.

## Defaults

The `defaults` section sets values every agent inherits unless it sets its own, plus global-only settings that cannot be overridden per agent.

```yaml
defaults:
  tools: [scheduler]
  max_tool_calls_per_turn: 200
  worker_tools: [shell, file, python]
  enable_streaming: true
  max_preload_chars: 50000
```

These defaults apply to each agent that leaves the same field unset:

| Field | Default |
|-------|---------|
| `markdown` | `true` |
| `cheap_model` | `null`; ignored for an agent whose `model` is the same. See [Cold-Cache Model](models.md#cold-cache-model) |
| `knowledge_graph` | `false` |
| `knowledge_graph_edits` | `true` |
| `num_history_runs`, `num_history_messages`, `max_tool_calls_from_history` | `null` (no limit) |
| `compaction` | enabled; see [Agent Compaction Settings](history.md#agent-compaction-settings) |
| `max_tool_calls_per_turn` | `1000` |
| `show_tool_calls` | `true` |
| `show_reasoning` | `true` |
| `worker_tools` | `null` (the deployment's execution mode decides) |
| `worker_scope` | `null` |
| `file_access` | `workspace` |
| `allow_self_config` | `false` |
| `automations` | `[]`; private agents inherit them only as `personal_rooms.agent`, and agents without file memory skip the built-ins. See [Automations](../scheduling.md#automations) |

`defaults.tools` (default `[scheduler]`) is added to every agent with `include_default_tools: true`; set it to `[]` to add nothing.
It accepts the same per-tool overrides as `agents.<name>.tools`.

These settings are global-only:

| Field | Default | Description |
|-------|---------|-------------|
| `enable_streaming` | `true` | Show replies as progressive message edits; see [Streaming](../streaming.md#configuration) |
| `streaming` | see [Streaming](../streaming.md#configuration) | Timing settings for progressive edits |
| `large_message_strategy` | `sidecar` | Oversized replies as a preview with the full text attached (`sidecar`) or as several complete messages (`split`); see [Large Messages](../streaming.md#large-messages) |
| `coalescing.debounce_ms` | `1000` | Milliseconds (>= 0) to wait after media for more attachments or a trailing caption before replying; text replies immediately |
| `show_stop_button` | `true` | Add a 🛑 reaction while an agent responds; see [Stop Button](../chat-commands.md#stop-button) |
| `max_consecutive_agent_replies` | `50` | Consecutive agent or team messages (>= 1) before agents stop waking each other; see [Agents mentioning other agents](../authorization.md#agents-mentioning-other-agents) |
| `max_preload_chars` | `50000` | Cap (>= 1) on preloaded context files; see [File-Based Context Loading](#file-based-context-loading) |
| `tool_output_auto_save_threshold_bytes` | `51200` | Larger tool outputs are saved to the workspace; see [Workspace and tool output files](../tools/execution-and-coding.md#workspace-and-tool-output-files) |
| `worker_grantable_credentials` | `null` | Shared credential services available inside isolated workers (`null` grants none); Google OAuth client and token services, `google_vertex_adc`, and `slack_app` cannot be granted. See [Credential leases](../deployment/sandbox-proxy.md#credential-leases) |
| `thread_summary_model`, `thread_summary_temperature`, `thread_summary_first_threshold`, `thread_summary_subsequent_interval` | `null`, `0.2`, `1`, `10` | Automatic thread summaries; see [Automatic Thread Summaries](../tools/matrix-and-attachments.md#automatic-thread-summaries) |
