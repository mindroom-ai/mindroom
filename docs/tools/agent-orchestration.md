---
icon: lucide/wrench
---

# Agent Orchestration

These built-in tools let an agent hand tasks to other agents, build and run reusable Dynamic Workflows, publish reports through public links, change MindRoom's configuration from chat, and keep Claude Code sessions alive across turns.

## Tools On This Page

- [`delegate`](#delegate) - Run a configured agent as a fresh subagent and wait for its answer.
- [`dynamic_workflow`](#dynamic_workflow) - Create, update, run, and inspect saved Dynamic Workflows.
- [`report_publishing`](#report_publishing) - Publish workflow reports or workspace static sites through revocable public links.
- [`config_manager`](#config_manager) - Inspect and patch the full configuration, and create, update, or validate agents and teams.
- [`self_config`](#self_config) - Let an agent read and update only its own configuration.
- [`claude_agent`](#claude_agent) - Run persistent Claude Agent SDK coding sessions.

Related tools documented elsewhere:

- [`oauth_connections`](../oauth-framework.md#oauth_connections) - Issue a browser-confirmed reset for one OAuth connection.
- [`openclaw_compat`](../openclaw.md#openclaw_compat) - Config-only preset that expands to native MindRoom tools.
- [`usage_stats`](../usage.md#usage_stats) - Read-only summaries of retained usage.
- [`matrix_message`](matrix-message.md#agent-conversations) - Start and continue visible Matrix conversations with other agents; messages return immediately instead of waiting for an answer.

## [`delegate`]

`delegate` gives an agent `run_subagent` and `continue_subagent`, which run another configured agent in a fresh session and return its answer inside the same tool call.
Use it when the caller needs a specialist's result before continuing; use [`matrix_message`](matrix-message.md#agent-conversations) for a conversation that should be visible in Matrix.

### Agent Delegation

Set `delegate_to` on the calling agent to the agent names it may run as subagents; the dashboard labels this list **Allowed subagents**.
MindRoom adds the `delegate` tool automatically when `delegate_to` is non-empty, so you do not list it in `tools:`.
Every target must be a configured agent, or the config fails to load.
An agent may run a fresh copy of itself only when its own name is in `delegate_to`.
Subagents may delegate further through their own `delegate_to`, up to a maximum depth of 3.

```yaml
agents:
  lead:
    display_name: Lead
    role: Coordinate specialist agents
    model: sonnet
    delegate_to: [lead, code, research]

  code:
    display_name: Code
    role: Implement and debug code changes
    model: sonnet
    tools: [coding, shell]
    delegate_to: [research]  # can delegate further

  research:
    display_name: Research
    role: Gather sources and summarize findings
    model: sonnet
    tools: [duckduckgo]
```

### Running Subagents

```python
run_subagent(task: str, agent_name: str | None = None, model: str | None = None, minimal: bool = False) -> str
continue_subagent(subagent_id: str, message: str) -> str
```

The child starts with no history from the caller's conversation but keeps its own configured tools, workspace, memory, and tool policy.
Include the relevant facts, constraints, and expected output in `task`, because the child cannot see the caller's conversation.
Omitting `agent_name` selects the caller itself, which still requires its own name in `delegate_to`.
Set `model` to an alias from `models:` to override the child's model for that session; it takes precedence over thread and room model choices, and omitting it uses normal model selection.
Set `minimal=True` to run the child in [minimal mode](agent-cli.md#minimal-subagents), which saves tokens when the child does not need its full system prompt.
The caller waits for the child to finish and receives its answer, a stable `Subagent ID`, and an audit reference.
Delegated runs do not create a Matrix thread and cannot ask the user interactive questions.
When a child in a Matrix conversation calls a tool that needs approval, the approval card appears in the source room and thread, and the delegation continues after the decision.
In Matrix conversations, a parent runs its subagents one at a time.
Delegated runs through the OpenAI-compatible API have fewer tools available; see [OpenAI-Compatible API](../openai-api.md).
When the caller has a workspace, both calls accept the standard `mindroom_output_path` argument described in [Execution & Coding](execution-and-coding.md#workspace-and-tool-output-files).

Use `continue_subagent` with the returned ID to send a follow-up into the same child session after its previous turn returns.
The ID works across parent turns and restarts, but only for the same caller, requester, and originating conversation.
Follow-ups keep the child's history, model, and mode, and recheck current permissions.
A follow-up sent while the child is still running or awaiting approval is refused; finish that turn first.
If MindRoom restarts during a child turn, that turn is reported as interrupted instead of being rerun, and a pending approval stays pending.

```python
run_subagent(task="Independently review the proposed design and return its three main risks.", model="sonnet")

run_subagent(
    agent_name="research",
    task="Compare SQLite and PostgreSQL for a single-host task queue with 20 concurrent writers. Return three risks and cite sources.",
)

# Copy the Subagent ID from the result.
continue_subagent(
    subagent_id="<returned-subagent-id>",
    message="Now assess how your recommendation changes with multiple hosts.",
)
```

Common errors:

- `Cannot delegate to '<name>'. Allowed subagents: ...` - the target is not in the caller's `delegate_to`.
- `Cannot delegate to '<name>': that agent is not allowed to reply to you.` - the requester is not allowed to use the target agent under its [access settings](../authorization.md).
- `Cannot delegate: Unknown model '<model>'. Available models: ...` - `model` is not an alias in `models:`.
- `Cannot delegate: the maximum delegation depth was reached.` - the chain of subagents is already 3 deep.
- `Subagent is busy or awaiting approval. Finish its current turn before sending a follow-up.` - the child's previous turn has not finished.
- `Cannot delegate an empty task. Please provide a task description.` - `task` or `message` is empty.

### Delegation Records

Each child turn writes `run.json`, `events.jsonl`, and `transcript.md` to `.mindroom/delegations/YYYY-MM-DD/<delegation-id>/` in the child's workspace, dated by the delegation's start in UTC.
`run.json` and `events.jsonl` update as the child runs, and `transcript.md` is written when the turn finishes.
The caller receives a receipt at `.mindroom/delegation_receipts/YYYY-MM-DD/<delegation-id>.json` in its own workspace.
Sensitive fields are redacted, and large outputs are stored as referenced artifacts.
Each follow-up turn gets its own record, linked to earlier turns by `subagent_id` and `previous_delegation_id`.
These files are audit exports that MindRoom never reads, so editing or deleting them does not affect the delegation.

## [`dynamic_workflow`]

`dynamic_workflow` lets an agent save a reusable multi-step workflow, publish new revisions, run it, and inspect run records.
Add it to the `tools:` of each agent that should create and run workflows.

```yaml
agents:
  coordinator:
    display_name: Coordinator
    role: Build and run reusable Dynamic Workflows
    model: sonnet
    tools:
      - dynamic_workflow
```

The tool exposes `create_workflow()`, `validate_workflow()`, `update_workflow()`, `run_workflow()`, `get_workflow_run()`, `list_workflows()`, and `list_workflow_revisions()`, each returning JSON with a `status` field.
`validate_workflow()` reports every validation error in a spec without saving it.
Workflows belong to the calling agent; `scope="agent"` is the default and the only scope agent tools can use, and `room` or `tenant` returns an error.
`update_workflow(workflow_id, patch, reason)` merges `patch` into the current spec: nested objects merge, but a list such as `workflow` or `participants` replaces the old list, so pass the complete list when changing one entry.
Each `update_workflow()` publishes a new immutable revision, and each run uses the revision active when it starts.
Saved workflows and run records live under `MINDROOM_STORAGE_PATH/dynamic_workflows/`.
A run completes inside the tool call and writes `report.md`, `report.html`, and `step_outputs.json`, which the run payload lists.
Only the user who requested a run can read its record, publish it, or open its private report.
When `MINDROOM_PUBLIC_URL` is set, run payloads include a private report URL under `/reports/private/...` that opens for that user in the dashboard.
Use [`report_publishing`](#report_publishing) to share a completed run's report through a public link.

### Spec Shape

Workflow specs are JSON or YAML objects with `schema_version: 1` and `kind: workflow`.
The top-level fields are `id`, `name`, `description`, `kind`, `inputs`, `participants`, `workflow`, `outputs`, and `permissions`, and `id`, `name`, `participants`, and `workflow` are required.

- **`inputs`**: An object schema with `required` and `properties`; each property supports `type`, `description`, and `enum`.
- **`participants`**: Up to 8 entries with `kind` set to `ephemeral_agent` (the default) or `room_agent`.
  - An `ephemeral_agent` declares `id`, `name`, `role`, `description`, `model`, `tools`, and `instructions`.
    Its `model`, given as an alias or model ID, defaults to the caller's current model and must be that model; when `permissions.models` is set, it must also list it.
    Its `tools` may include any registered tool except `memory`, `delegate`, `self_config`, `skill_manage`, `compact_context`, `dynamic_workflow`, `dynamic_tools`, and `invite_router`, and each tool must also appear in `permissions.tools`.
    Granted tools run with the caller's credentials, worker routing, and plugin hooks.
  - A `room_agent` declares `id` and `agent` and reuses a configured agent that the requester can already use in the current room.
    It runs with its configured model and without tools, skills, knowledge, durable state, or context files.
- **`workflow`**: Up to 64 steps, each with a unique `id` and a `type`, run one at a time in order.
  - `agent_step` sends its rendered `prompt` to the named `participant`.
  - `transform_step` renders a `template` without calling a model.
  - `report_step` renders Markdown from `body_template` or copies a prior step via `from_step`, with an optional `title`.
  - Templates can reference `{input.<field>}` and earlier steps as `{steps.<step-id>}`.
- **`outputs`**: Entries with `id`, `type`, and `from_step`, where `type` is `text`, `markdown`, `json`, or `html_report`.
- **`permissions`**: Run limits and grants.
  - `max_runtime_seconds` is 1 to 3600 and defaults to 3600; a run that exceeds it fails.
  - `max_total_agents` is 1 to 16, defaults to 16, and caps the number of `agent_step` entries.
  - `max_concurrent_agents` is 1 to 8 and is only validated, because steps never run in parallel.
  - `models` lists the models participants may use, and `tools` lists the tools participants may be granted.
  - `data` must keep `matrix_history: none`, `attachments: none`, and `knowledge_bases: []`, because direct workflow data grants are not supported yet; participants can still reach such data through granted tools such as `matrix_message`.

```python
create_workflow(
    spec={
        "schema_version": 1,
        "id": "brief-report",
        "name": "Brief Report",
        "description": "Create a short HTML report from one topic.",
        "kind": "workflow",
        "inputs": {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string"}},
        },
        "participants": [
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "model": "claude-sonnet-5-5",
                "tools": ["duckduckgo", "website"],
            },
        ],
        "workflow": [
            {
                "id": "write",
                "type": "agent_step",
                "participant": "writer",
                "prompt": "Research the web and write a concise cited report about {input.topic}.",
            },
        ],
        "outputs": [{"id": "report_html", "type": "html_report", "from_step": "write"}],
        "permissions": {
            "max_runtime_seconds": 1800,
            "max_concurrent_agents": 4,
            "max_total_agents": 8,
            "models": ["claude-sonnet-5-5"],
            "tools": ["duckduckgo", "website"],
            "data": {"matrix_history": "none", "attachments": "none", "knowledge_bases": []},
        },
    },
    reason="initial report workflow",
)
run_workflow("brief-report", {"topic": "Agno factories"})
list_workflows()
get_workflow_run("brief-report", "run_...")
```

### Allowing participant tools

Workflow participants cannot pause for human approval, so a workflow is rejected when any function of a granted tool would require approval.
Inside a workflow, a function that no approval rule matches requires approval, even when `tool_approval.default` is `auto_approve`.
Set `allowed_tools` on the caller's `dynamic_workflow` entry to auto-approve the functions of listed toolkits for participants, or use `["*"]` for every eligible toolkit.

```yaml
agents:
  builder:
    display_name: Workflow Builder
    tools:
      - dynamic_workflow:
          allowed_tools: [duckduckgo, website]
```

Operator-authored [`tool_approval`](../tool-approval.md) rules are checked first and the first match wins.
A matching `auto_approve` rule makes a function usable even outside `allowed_tools`, and a matching `require_approval` or script rule makes it unavailable.
`allowed_tools`, including `"*"`, never auto-approves `claude_agent`, `config_manager`, or `scheduler`, but an explicit operator `auto_approve` rule can.
Functions that ask for their own confirmation stay unavailable even under an operator `auto_approve` rule.
A function name shared by several granted toolkits is auto-approved only when every owning toolkit is eligible.

## [`report_publishing`]

`report_publishing` lets an agent publish a completed Dynamic Workflow report or a workspace static site through a revocable public link.
Add it to the agent's `tools:`; it is often enabled next to `dynamic_workflow`.

```yaml
agents:
  coordinator:
    display_name: Coordinator
    role: Build workflows and publish report links
    model: sonnet
    tools:
      - dynamic_workflow
      - report_publishing
```

The tool exposes `publish_report(source_type, source, confirm_public)` and `revoke_public_report(slug)`, each returning JSON with a `status` field.
`confirm_public=True` is required, so an accidental call publishes nothing.
A successful publish returns a `slug` and the public path `/reports/public/<slug>`, plus an absolute `public_url` when `MINDROOM_PUBLIC_URL` is set.
Anyone with the link can open it without signing in until it is revoked.
Only the user who requested the source run or published the link can revoke it.
The tool never accepts arbitrary filesystem paths; it publishes only these sources, and only when the current requester may read them:

- **`dynamic_workflow_run`**: `source` takes `workflow_id`, `run_id`, and optional `scope` (default `agent`), and the run must be completed.
- **`static_site`**: `source` takes a workspace-relative `path` and a required `title`.
  The path is a directory containing `index.html` plus optional CSS, JavaScript, images, fonts, or JSON, or a single HTML file served as `index.html`.
  The agent needs a workspace, which exists when it uses `memory_backend: file` or a `private:` configuration.
  Publishing copies the site, so later workspace edits need a new `publish_report()` call.
  A site may contain at most 200 files, 200 directories nested at most 32 levels deep, and 10 MiB in total, and it may contain only regular files, not symlinks.
  Static sites are served at `/reports/public/<slug>/` with a trailing slash so relative asset URLs resolve.
  Scripts can make the page interactive but cannot act as the signed-in dashboard user or call MindRoom APIs.
  Scripts, stylesheets, and fonts must be bundled in the site, while images may also load from external HTTPS URLs.
  Pages cannot use `fetch()` or other API connections, even for the site's own JSON files, so embed data in the HTML or a bundled script.

```python
publish_report(
    source_type="dynamic_workflow_run",
    source={"workflow_id": "brief-report", "run_id": "run_..."},
    confirm_public=True,
)
publish_report(
    source_type="static_site",
    source={"path": "public-demo", "title": "Public Demo"},
    confirm_public=True,
)
revoke_public_report("pub_...")
```

Published copies and link records live under `MINDROOM_STORAGE_PATH/report_publishing/`.
Set `MINDROOM_PUBLIC_URL` to the externally reachable dashboard origin, such as `https://mindroom.example.com`, so results include clickable absolute URLs.
If the dashboard frontend and the Python backend sit behind separate upstreams, route `/reports/public/*` to the backend without dashboard-login middleware.

## [`config_manager`]

`config_manager` is the full configuration control plane: it reads and patches any authored config field and creates or updates agents and teams.
It can change every agent and team, so give it only to agents that administrators drive; use [`self_config`](#self_config) for narrow self-tuning.

```yaml
agents:
  builder:
    display_name: Builder
    role: Create and maintain MindRoom agents and teams
    model: sonnet
    tools:
      - config_manager
```

The tool exposes four functions:

- **`get_info(info_type, name=None, agent_scope="current_room")`**: `info_type` is `mindroom_docs`, `config_schema`, `available_models`, `agents`, `teams`, `available_tools`, `tool_details`, `agent_config`, or `agent_template`.
  `agents` lists agents in the current room unless `agent_scope="all"`.
  `tool_details` takes a tool name and shows its config fields and status.
  `agent_config` returns one agent's redacted YAML.
  `agent_template` takes `researcher`, `developer`, `social`, `communicator`, `analyst`, or `productivity` and returns starter YAML.
- **`manage_config(operation, path="", changes=None, dry_run=False)`**: Works on the authored document in `config.yaml`, without unset defaults, using RFC 6901 JSON Pointer paths where `""` is the root.
  `operation="inspect"` returns one subtree as redacted YAML; a subtree too large to return asks for a narrower path.
  `operation="patch"` applies a batch of RFC 6902 `add`, `replace`, and `remove` changes all at once, using `-` as the last token to append to a list.
  `dry_run=True` validates a patch without saving it.
  The receipt lists the changed paths without echoing their values.
  When `config.yaml` uses `!include`, inspection works but patching is refused; edit the source files instead.
- **`manage_agent(operation, agent_name, ...)`**: `operation` is `create`, `update`, or `validate`, and the optional fields are `display_name`, `role`, `tools`, `instructions`, `model`, `rooms`, `knowledge_bases`, `include_default_tools`, `markdown`, `learning`, and `learning_mode`.
  Creating requires `display_name`, takes a lowercase name of letters, digits, and underscores, defaults `model` to `default`, and defaults `include_default_tools` to `true`.
  Tool names and knowledge base IDs must exist.
- **`manage_team(team_name, display_name, role, agents, mode="coordinate")`**: Creates a new team in `coordinate` or `collaborate` mode; it rejects unknown member agents and existing team names, and it cannot update a team.

```python
get_info("tool_details", name="claude_agent")
manage_config(operation="inspect", path="/authorization")
manage_config(
    operation="patch",
    changes=[
        {"op": "replace", "path": "/models/default/id", "value": "claude-sonnet-5-5"},
        {"op": "add", "path": "/agents/triage/instructions/-", "value": "Escalate anything urgent."},
    ],
)
manage_agent(
    operation="create",
    agent_name="triage",
    display_name="Triage",
    role="Sort incoming requests and hand them to the right specialist.",
    tools=["duckduckgo", "matrix_message"],
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

### Access, Redaction, and Saving

These rules apply to both `config_manager` and `self_config`.
Reading configuration (`manage_config` inspection, `get_info("agent_config")`, `get_own_config()`) and every write require a requester listed in `administrators`; other requesters get an authorization error.
Redacted output masks every value inside fields the config schema marks secret, such as MCP server `env` and `headers`, plugin `settings`, and model `extra_kwargs`, except `${NAME}` environment references, which are shown as written.
It also masks free-form map entries whose key names look like credentials, and credential patterns such as URL passwords and bearer tokens in any text.
A write whose value contains the `***redacted***` marker or a masked URL password such as `user:***@host` is refused, so copying redacted output back cannot overwrite a hidden real value.
Every write is validated against the full runtime config before `config.yaml` is saved, and an invalid change saves nothing.
Saved changes take effect through the normal config hot reload after the current response.
When a plain list of tool names replaces an agent's tools, inline overrides for tools that stay in the list are kept.

## [`self_config`]

`self_config` lets an agent read and update only its own entry under `agents:`.
Enable it with `allow_self_config: true` on the agent, or for all agents with `defaults.allow_self_config: true`; MindRoom then adds the tool automatically.

```yaml
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

`get_own_config()` returns the agent's redacted YAML.
`update_own_config()` changes only the fields you pass: `display_name`, `role`, `instructions`, `tools`, `model`, `rooms`, `markdown`, `learning`, `learning_mode`, `knowledge_bases`, `skills`, `include_default_tools`, `show_tool_calls`, `thread_mode`, `num_history_runs`, `num_history_messages`, `compress_tool_results`, `max_tool_calls_from_history`, and `context_files`.
Every `update_own_config()` call shows an approval card, even when `tool_approval.default` is `auto_approve`.
An agent cannot give itself `config_manager`, and `include_default_tools=True` is refused when `defaults.tools` contains `config_manager`.
Access, redaction, and saving follow the [shared rules above](#access-redaction-and-saving).

```python
get_own_config()
update_own_config(
    instructions=[
        "Cite sources for factual claims.",
        "Prefer concise summaries with clear takeaways.",
    ],
    tools=["duckduckgo", "wikipedia", "matrix_message"],
    thread_mode="room",
    context_files=["SOUL.md", "USER.md"],
)
```

## [`claude_agent`]

`claude_agent` keeps Claude Agent SDK coding sessions alive across turns, so an agent can hand multi-step coding work to Claude Code and keep talking to the same session.
It runs code in the primary runtime and cannot run in a worker, so enable it only for agents trusted with everything the primary runtime can reach; see [Security Posture](../architecture/security-posture.md#file-access).

The tool exposes `claude_start_session()`, `claude_send()`, `claude_session_status()`, `claude_interrupt()`, and `claude_end_session()`.
`claude_send()` creates the session if needed, so `claude_start_session()` is optional.
Each agent gets one session per conversation, and a `session_label` opens additional independent sessions in the same conversation.
Calls to the same session run one after another.
`resume` (a Claude session ID) and `fork_session=True` apply only when a session is created; `fork_session` requires `resume`, and passing either for an existing session returns an error asking for another `session_label` or `claude_end_session()` first.
`claude_session_status()` reports age, idle time, and the Claude session ID once Claude has returned a result.
Errors from the Claude SDK include the last lines of Claude CLI stderr to help debug gateway or CLI problems.
Through the OpenAI-compatible API, keep the same `X-Session-Id` across requests to reuse one Claude session; see [Session continuity](../openai-api.md#session-continuity).

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | password | `null` | Anthropic API key or gateway key; usually set in the dashboard or credentials rather than inline YAML. |
| `anthropic_base_url` | url | `null` | Anthropic-compatible gateway root URL, without a `/v1` suffix, because the Claude client appends its own API path. |
| `anthropic_auth_token` | password | `null` | Bearer token for Anthropic-compatible gateways. |
| `disable_experimental_betas` | boolean | `false` | Sets `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` for gateways that reject Claude beta headers. |
| `cwd` | text | `null` | Working directory for Claude. |
| `model` | text | `null` | Claude model; defaults to the agent's own model ID. |
| `permission_mode` | text | `default` | `default`, `acceptEdits`, `plan`, or `bypassPermissions`; other values fall back to `default`. |
| `continue_conversation` | boolean | `false` | Continue the same Claude conversation context across queries in one session. |
| `allowed_tools` | text | `null` | Comma-separated Claude Code tool names to allow. |
| `disallowed_tools` | text | `null` | Comma-separated Claude Code tool names to deny. |
| `max_turns` | number | `null` | Maximum Claude turns per query; minimum 1. |
| `system_prompt` | text | `null` | Extra system prompt passed to Claude. |
| `cli_path` | text | `null` | Path to the Claude CLI executable. |
| `session_ttl_minutes` | number | `60` | Idle sessions close after this many minutes; minimum 1. |
| `max_sessions` | number | `200` | Maximum live sessions per agent; at the limit, the least recently used idle session closes. Minimum 1. |

Credentials set in the dashboard and in `mindroom_data/credentials/claude_agent_credentials.json` fill the same fields.

```yaml
agents:
  code:
    display_name: Code Agent
    role: Coding assistant with persistent Claude sessions
    model: default
    tools:
      - claude_agent:
          model: claude-sonnet-5-5
          cwd: /workspace/project
          permission_mode: acceptEdits
          continue_conversation: true
          session_ttl_minutes: 180
          max_sessions: 20
```

Credentials for an Anthropic-compatible gateway such as LiteLLM:

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
