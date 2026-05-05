# Agent Orchestration

Use these tools and presets to coordinate other agents, change runtime configuration, import OpenClaw-style workspaces, and keep long-lived Claude coding sessions alive across turns.

## What This Page Covers

This page documents the built-in tools in the `agent-orchestration` group.
Use these tools when you need multi-agent coordination, runtime config changes, config-only presets, or persistent Claude Agent SDK sessions.

## Tools On This Page

- \[`subagents`\] - Spawn Matrix-backed sub-agent sessions and message them later by session key or label.
- \[`delegate`\] - Run another configured agent as a one-shot specialist and return its answer inline.
- \[`config_manager`\] - Inspect MindRoom config and create, update, validate, or template agents and teams.
- \[`self_config`\] - Let an agent read and update only its own configuration.
- \[`openclaw_compat`\] - Config-only preset that expands to native MindRoom tools.
- \[`claude_agent`\] - Persistent Claude Agent SDK sessions with optional gateway support and per-session labels.

## Common Setup Notes

All six entries on this page are MindRoom-native orchestration features rather than third-party OAuth integrations. Only \[`claude_agent`\] has tool-specific credential fields. \[`delegate`\] and \[`self_config`\] can be added automatically based on agent config, so they are not limited to explicit `tools:` entries. `agents.<name>.delegate_to` auto-enables \[`delegate`\] when the list is non-empty and the current delegation depth is below the hard limit of 3. `agents.<name>.allow_self_config` or `defaults.allow_self_config` auto-enables \[`self_config`\]. \[`config_manager`\] and \[`self_config`\] both save changes by revalidating the full runtime config before rewriting `config.yaml`. \[`subagents`\] requires a live Matrix tool runtime context with `room_id`, `requester_id`, Matrix client access, and a writable storage path. \[`openclaw_compat`\] is a config preset, not a runtime toolkit. `Config.expand_tool_names()` expands presets and implied tools while deduping and preserving order. For \[`openclaw_compat`\], that means `matrix_message` is added directly and `attachments` is added indirectly through `Config.IMPLIED_TOOLS`.

## \[`subagents`\]

`subagents` creates and tracks Matrix-backed sub-agent sessions that can continue across multiple tool calls.

### What It Does

`subagents` exposes `agents_list()`, `sessions_spawn()`, `sessions_send()`, and `list_sessions()`.
All four calls return JSON strings with a `status` field, a `tool` field, and operation-specific payload data.
`agents_list()` returns the configured agent IDs and the current agent name.
`sessions_spawn(task, summary, tag, label=None, agent_id=None)` requires a non-empty task plus a normalized summary and tag.
`sessions_spawn()` posts a fresh room-level Matrix message that mentions the target agent, then treats the resulting event ID as the root of a new isolated session thread.
After the spawn succeeds, it writes the requested thread summary and tag through the lower-level thread summary and thread tag APIs.
If you pass a `label` and the current `(agent_name, room_id, requester_id)` scope already has a matching tracked session, `sessions_spawn()` reuses that session instead of creating a new one and still applies the requested summary and tag to the existing thread.
If the post-spawn summary or tag write fails, the spawn still succeeds and the response includes a `warnings` list describing the follow-up failure.
`sessions_send()` sends a follow-up message into an existing tracked session.
If you omit `session_key`, `sessions_send()` defaults to the current room or thread session key from `create_session_id(room_id, thread_id)`.
If you pass `label` without `session_key`, `sessions_send()` resolves the most recent in-scope session with that label.
If you pass `agent_id`, `sessions_send()` prefixes the outgoing message with `@mindroom_<agent_id>` before sending it.
Tracked sessions are persisted in `subagents/session_registry.json` under the current runtime storage root.
`list_sessions()` paginates those tracked sessions with a default `limit` of 50 and a maximum of 200.
Isolated spawned sessions require thread-capable agents.
If the target agent uses `thread_mode=room`, `sessions_spawn()` fails and threaded `sessions_send()` calls to that session also fail.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  coordinator:
    display_name: Coordinator
    role: Break work into long-running threaded sub-sessions
    model: sonnet
    tools:
      - subagents
```

```python
agents_list()
sessions_spawn(
    task="Review the failing deployment and propose a rollback plan.",
    summary="Investigate the failing deployment and propose a safe rollback plan.",
    tag="incident-rollback",
    label="incident-42",
    agent_id="ops",
)
sessions_send(
    message="Add a short list of commands we should run first.",
    label="incident-42",
)
list_sessions(limit=20)
```

### Notes

- Session tracking is scoped to the current `agent_name`, `room_id`, and `requester_id`, so labels are not global across unrelated conversations.
- `sessions_spawn()` returns normalized `summary` and `tag` values in the success payload and may include `warnings` if the follow-up summary or tag write fails after the session is created.
- Use \[`subagents`\] when you want a continuing Matrix thread that other agents or humans can revisit later.
- Use \[`delegate`\] instead when you want a one-shot specialist answer returned directly as the tool result.

## \[`delegate`\]

`delegate` runs another configured agent as a fresh one-shot specialist and returns that agent's response inline.

### What It Does

`delegate` exposes one tool call, `delegate_task(agent_name, task)`.
The delegated agent is created with `create_agent()` and runs independently with no shared session or chat history from the caller.
The caller waits for the delegated agent to finish, and the delegated agent's `response.content` becomes the tool result.
MindRoom gives the delegated agent any already-published last-good knowledge indexes and schedules missing or stale refresh work in the background.
Interactive questions are disabled for delegated runs. Unlike \[`subagents`\], \[`delegate`\] does not create a Matrix thread, does not write to the room timeline, and does not keep a reusable session handle. If `agent_name` is not in the caller's allowed `delegate_to` list, the tool returns an error string.
Empty tasks are rejected.

### Configuration

This tool has no tool-specific inline configuration fields.
Enable it by setting `delegate_to` on the agent config.
MindRoom adds the tool automatically when `delegate_to` is non-empty, so listing `delegate` in `tools:` is usually unnecessary.

### Example

```yaml
agents:
  lead:
    display_name: Lead
    role: Coordinate specialist agents
    model: sonnet
    delegate_to:
      - code
      - research

  code:
    display_name: Code
    role: Implement and debug code changes
    model: sonnet
    tools:
      - coding
      - shell

  research:
    display_name: Research
    role: Gather sources and summarize findings
    model: sonnet
    tools:
      - duckduckgo
```

```python
delegate_task(
    agent_name="research",
    task="Summarize the three main risks in this proposal and cite supporting facts.",
)
```

### Notes

- `Config.validate_delegate_to()` rejects self-delegation and unknown target agents at config-load time.
- Recursive delegation is supported, but only up to a maximum depth of 3.
- Use \[`subagents`\] when you need an ongoing threaded workflow.
- Use \[`delegate`\] when you need a synchronous specialist answer inside the current run.

## \[`config_manager`\]

`config_manager` is the broad config-authoring toolkit for inspecting MindRoom and creating or updating agents and teams.

### What It Does

`config_manager` exposes `get_info()`, `manage_agent()`, and `manage_team()`.
`get_info(info_type, name=None)` supports `mindroom_docs`, `config_schema`, `available_models`, `agents`, `teams`, `available_tools`, `tool_details`, `agent_config`, and `agent_template`.
`tool_details` requires `name` and reads from live `TOOL_METADATA`, so it includes real config fields and statuses from the current worktree.
`agent_config` returns the authored YAML for a specific agent.
`agent_template` generates starter YAML for one of the built-in template types: `researcher`, `developer`, `social`, `communicator`, `analyst`, or `productivity`.
`manage_agent()` supports `create`, `update`, and `validate`.
Agent creates and updates validate tool names against the live registry and validate knowledge base IDs against the current config.
When a plain string tool list replaces an existing tool list, `config_manager` preserves inline overrides for retained tools instead of flattening them away.
On create, `include_default_tools` falls back to `true` when you omit it.
`manage_team()` creates a new team with `coordinate` or `collaborate` mode and rejects unknown member agents or duplicate team names.
All writes go through full runtime config validation before `config.yaml` is saved.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  builder:
    display_name: Builder
    role: Create and maintain MindRoom agents and teams
    model: sonnet
    tools:
      - config_manager
```

```python
get_info("available_tools")
get_info("tool_details", name="claude_agent")
manage_agent(
    operation="create",
    agent_name="triage",
    display_name="Triage",
    role="Sort incoming requests and hand them to the right specialist.",
    tools=["duckduckgo", "subagents"],
    model="default",
    rooms=["lobby"],
)
manage_team(
    team_name="incident_team",
    display_name="Incident Team",
    role="Coordinate incident response across ops and code agents.",
    agents=["ops", "code"],
    mode="coordinate",
)
```

### Notes

- \[`config_manager`\] is broader and more privileged than \[`self_config`\] because it can inspect and modify other agents and teams.
- `manage_team()` creates teams, but it does not expose a separate update operation on this branch.
- Use [Agent Configuration](https://docs.mindroom.chat/configuration/agents/index.md) for the full authored schema outside the tool's curated helper surface.

## \[`self_config`\]

`self_config` lets an agent inspect and update only its own config entry.

### What It Does

`self_config` exposes `get_own_config()` and `update_own_config()`.
`get_own_config()` returns the current agent's authored YAML block.
`update_own_config()` only changes fields that you pass explicitly.
On this branch, `update_own_config()` can modify `display_name`, `role`, `instructions`, `tools`, `model`, `rooms`, `markdown`, `learning`, `learning_mode`, `knowledge_bases`, `skills`, `include_default_tools`, `show_tool_calls`, `thread_mode`, `num_history_runs`, `num_history_messages`, `compress_tool_results`, `max_tool_calls_from_history`, and `context_files`.
The update path validates tool names against the live registry and validates knowledge base IDs against the current config.
It also preserves inline tool overrides for retained tools when a string-only tool list is provided.
Updates are validated through `AgentConfig.model_validate()` before the file is saved.
Only the current agent can be changed.
There is no path to modify other agents or teams through this tool.

### Configuration

This tool has no tool-specific inline configuration fields.
The normal way to enable it is `agents.<name>.allow_self_config: true` or `defaults.allow_self_config: true`.

### Example

```yaml
defaults:
  allow_self_config: false

agents:
  research:
    display_name: Research
    role: Research and summarize external sources
    model: sonnet
    allow_self_config: true
    tools:
      - duckduckgo
      - wikipedia
```

```python
get_own_config()
update_own_config(
    instructions=[
        "Cite sources for factual claims.",
        "Prefer concise summaries with clear takeaways.",
    ],
    tools=["duckduckgo", "wikipedia", "subagents"],
    thread_mode="room",
    context_files=["SOUL.md", "USER.md"],
)
```

### Notes

- `self_config` blocks privileged self-escalation by rejecting `config_manager` in its `tools` update list.
- `include_default_tools=True` is also rejected when `defaults.tools` contains blocked privileged tools such as `config_manager`.
- Use \[`self_config`\] for narrow self-tuning at runtime and \[`config_manager`\] for full config-authoring workflows.

## \[`openclaw_compat`\]

`openclaw_compat` is a config-only preset for OpenClaw-style workspace portability.

### What It Does

`openclaw_compat` is not a runtime toolkit.
The registered factory returns an empty `Toolkit`, and the real behavior comes from `Config.TOOL_PRESETS`.
`Config.expand_tool_names()` expands `openclaw_compat` into `shell`, `coding`, `duckduckgo`, `website`, `browser`, `scheduler`, `subagents`, and `matrix_message`.
`matrix_message` then implies `attachments`, so the effective enabled set also includes `attachments` even though the preset does not list it directly.
Preset expansion dedupes while preserving order, so adding `openclaw_compat` alongside one of its member tools does not create duplicates.
This preset is meant for OpenClaw-compatible workspace behavior inside MindRoom rather than for cloning the full OpenClaw gateway control plane.

### Configuration

This preset has no inline configuration fields.

### Example

```yaml
agents:
  openclaw:
    display_name: OpenClawAgent
    role: OpenClaw-style personal assistant with a file-first workspace
    model: opus
    include_default_tools: false
    learning: false
    memory_backend: file
    context_files:
      - SOUL.md
      - AGENTS.md
      - USER.md
      - IDENTITY.md
      - TOOLS.md
      - HEARTBEAT.md
    tools:
      - openclaw_compat
      - python
```

### Notes

- \[`openclaw_compat`\] is a preset name that belongs in `tools:` but does not expose callable runtime methods of its own.
- Use the dedicated [OpenClaw Workspace Import](https://docs.mindroom.chat/openclaw/index.md) guide for workspace layout, file memory behavior, and migration details.
- If you only need one or two of the member tools, configure those tools directly instead of using the preset.

## \[`claude_agent`\]

`claude_agent` keeps persistent Claude Agent SDK coding sessions alive across turns and exposes explicit session lifecycle controls.

### What It Does

`claude_agent` exposes `claude_start_session()`, `claude_send()`, `claude_session_status()`, `claude_interrupt()`, and `claude_end_session()`.
`claude_send()` automatically creates the session if it does not already exist, so `claude_start_session()` is optional.
Session keys are namespaced by agent identity and Agno run session ID, with optional `session_label` suffixes for parallel sub-sessions.
The same session key is serialized by an `asyncio.Lock`, so concurrent calls to one label run one after the other.
Different `session_label` values create distinct Claude sessions that can proceed independently.
Idle sessions expire after `session_ttl_minutes`, which defaults to 60 minutes.
The process-wide session manager keeps at most `max_sessions` active sessions per agent namespace, defaulting to 200.
`resume` and `fork_session` only apply when creating a new session.
`fork_session=True` requires a non-empty `resume` session ID.
If a session already exists for the computed key, passing `resume` or `fork_session` returns an error instead of silently changing the live session.
`claude_session_status()` reports age, idle time, and the underlying Claude session ID once Claude has returned a result.
On SDK failures, the tool includes recent Claude CLI stderr lines in its error output to help debug gateway or CLI issues.

### Configuration

| Option                       | Type       | Required | Default   | Notes                                                                                                                               |
| ---------------------------- | ---------- | -------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `api_key`                    | `password` | `no`     | `null`    | Anthropic API key or gateway-compatible key material. Usually stored in credentials JSON or dashboard setup instead of inline YAML. |
| `anthropic_base_url`         | `url`      | `no`     | `null`    | Optional Anthropic-compatible gateway root URL. Use the host root, not a `/v1` suffix.                                              |
| `anthropic_auth_token`       | `password` | `no`     | `null`    | Optional bearer token for Anthropic-compatible gateways.                                                                            |
| `disable_experimental_betas` | `boolean`  | `no`     | `false`   | Sets `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` for gateway compatibility.                                                          |
| `cwd`                        | `text`     | `no`     | `null`    | Working directory passed to the Claude Agent SDK client.                                                                            |
| `model`                      | `text`     | `no`     | `null`    | Claude model override. When omitted, the tool falls back to the current agent model ID when one is available.                       |
| `permission_mode`            | `text`     | `no`     | `default` | One of `default`, `acceptEdits`, `plan`, or `bypassPermissions`. Invalid values fall back to `default`.                             |
| `continue_conversation`      | `boolean`  | `no`     | `false`   | Continue the same Claude conversation context across queries in one session.                                                        |
| `allowed_tools`              | `text`     | `no`     | `null`    | Comma-separated Claude Code tool names to allow.                                                                                    |
| `disallowed_tools`           | `text`     | `no`     | `null`    | Comma-separated Claude Code tool names to deny.                                                                                     |
| `max_turns`                  | `number`   | `no`     | `null`    | Maximum Claude turns per query. Values below 1 are normalized up to 1.                                                              |
| `system_prompt`              | `text`     | `no`     | `null`    | Extra system prompt passed directly to the Claude Agent SDK.                                                                        |
| `cli_path`                   | `text`     | `no`     | `null`    | Optional path to the Claude CLI executable.                                                                                         |
| `session_ttl_minutes`        | `number`   | `no`     | `60`      | Idle-session expiration window in minutes. Values below 1 are normalized up to 1.                                                   |
| `max_sessions`               | `number`   | `no`     | `200`     | Maximum live sessions per agent namespace. Values below 1 are normalized up to 1.                                                   |

### Example

```yaml
agents:
  code:
    display_name: Code Agent
    role: Coding assistant with persistent Claude sessions
    model: default
    tools:
      - claude_agent:
          model: claude-sonnet-4-6
          cwd: /workspace/project
          permission_mode: acceptEdits
          continue_conversation: true
          session_ttl_minutes: 180
          max_sessions: 20
```

```json
{
  "api_key": "sk-ant-or-proxy-key",
  "model": "claude-sonnet-4-6",
  "permission_mode": "default",
  "continue_conversation": true,
  "session_ttl_minutes": 60,
  "max_sessions": 200
}
```

```json
{
  "api_key": "sk-dummy",
  "anthropic_base_url": "http://litellm.local",
  "anthropic_auth_token": "sk-dummy",
  "disable_experimental_betas": true
}
```

```python
claude_send(
    prompt="Refactor the failing test and explain the diff.",
    session_label="bugfix",
)
claude_session_status(session_label="bugfix")
claude_interrupt(session_label="bugfix")
claude_end_session(session_label="bugfix")
```

### Notes

- Dashboard setup and `mindroom_data/credentials/claude_agent_credentials.json` both feed the same tool credential fields, because runtime credentials are stored as `<service>_credentials.json`.
- For Anthropic-compatible gateways, set `anthropic_base_url` to the gateway root without `/v1`, because the Claude client appends its own API path.
- Some gateways reject Claude beta headers, so `disable_experimental_betas: true` is the compatibility switch for that case.
- When you use MindRoom's OpenAI-compatible API, keep the same `X-Session-Id` across requests so the same Claude session key is reused.
- See [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/index.md) for request-level session continuity details.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Agent Configuration](https://docs.mindroom.chat/configuration/agents/index.md)
- [OpenClaw Workspace Import](https://docs.mindroom.chat/openclaw/index.md)
- [OpenAI-Compatible API](https://docs.mindroom.chat/openai-api/index.md)
