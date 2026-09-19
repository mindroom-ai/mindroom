---
icon: lucide/wrench
---

# Agent Orchestration

Use these tools and presets to recover scoped OAuth connections, coordinate other agents, save reusable Dynamic Workflows, change runtime configuration, import OpenClaw-style workspaces, and keep long-lived Claude coding sessions alive across turns.

## What This Page Covers

This page documents the built-in tools in the `agent-orchestration` group.
Use these tools when you need OAuth recovery, multi-agent coordination, reusable workflow runs, runtime config changes, config-only presets, persistent Claude Agent SDK sessions, or retained usage statistics.

## Tools On This Page

- [`oauth_connections`] - Issue a browser-confirmed reset for one authorized OAuth connection.
- [`delegate`] - Run a configured agent as a fresh subagent and wait for its answer.
- [`dynamic_workflow`] - Create, update, run, and inspect saved Dynamic Workflows with persisted report artifacts.
- [`report_publishing`] - Publish authorized report artifacts through revocable public links.
- [`config_manager`] - Inspect and patch the full MindRoom configuration, and create, update, validate, or template agents and teams.
- [`self_config`] - Let an agent read and update only its own configuration.
- [`openclaw_compat`] - Config-only preset that expands to native MindRoom tools.
- [`claude_agent`] - Persistent Claude Agent SDK sessions with optional gateway support and per-session labels.
- [`usage_stats`] - Local, read-only summaries of retained Agno run usage.

## Common Setup Notes

All nine entries on this page are MindRoom-native orchestration features rather than third-party toolkits.
[`oauth_connections`] manages connections used by other provider-backed tools and has no credentials of its own.
Only [`claude_agent`] has tool-specific credential fields.
[`delegate`] and [`self_config`] can be added automatically based on agent config, so they are not limited to explicit `tools:` entries.
`agents.<name>.delegate_to` auto-enables [`delegate`] when the list is non-empty and the current delegation depth is below the hard limit of 3.
`agents.<name>.allow_self_config` or `defaults.allow_self_config` auto-enables [`self_config`].
[`config_manager`] and [`self_config`] both save changes by revalidating the full runtime config before rewriting `config.yaml`.
[`dynamic_workflow`] requires a live tool runtime context, a writable storage path, and a configured agent model.
[`report_publishing`] requires a live tool runtime context, a writable storage path, and an authorized report source.
[`openclaw_compat`] is a config preset, not a runtime toolkit.
`Config.expand_tool_names()` expands presets and implied tools while deduping and preserving order.
For [`openclaw_compat`], that means `matrix_message` is added directly and `attachments` is added indirectly through `Config.IMPLIED_TOOLS`.

## [`oauth_connections`]

`oauth_connections` lets an agent recover a stuck or revoked MindRoom-managed OAuth connection without exposing broader credential-management controls.

### What It Does

The toolkit exposes only `reset_oauth_connection(provider_id)`.
The provider must back one of the current agent's configured tools.
The call returns a temporary browser link and does not change the connection by itself.
After you confirm, MindRoom removes its saved connection and opens the provider's sign-in page so you can reconnect.
It does not revoke access at the provider itself.

### Configuration

Enable the tool alongside the OAuth-backed tools the agent may recover.

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Work with connected documents and recover revoked connections
    model: sonnet
    worker_scope: user_agent
    tools:
      - oauth_connections
      - google_drive
```

### Who Can Reset A Connection

| Connection type | Who confirms the reset | What the reset affects |
| --- | --- | --- |
| Shared (`shared`) | An administrator or configured credential manager can request the link. Anyone with the complete link can confirm it before it expires. | Everyone using this agent |
| Personal (`user`) | The same MindRoom user who requested the link, signed in to the dashboard | That user's connection across agents |
| Personal for one agent (`user_agent`) | The same MindRoom user who requested the link, signed in to the dashboard | That user's connection for this agent only |

A shared connection belongs to the agent rather than to one MindRoom user.
On a private agent, users can manage their own personal connections.
On other agents, requesting a reset requires platform `administrator` access or an entry in `agents.<name>.credential_managers`.
Some providers always use personal connections, regardless of the agent's configured scope.

### Reset A Connection

1. Ask the agent to call `reset_oauth_connection()` for the affected provider.
2. Open the returned link within 10 minutes.
3. Review which agent and connection type will be affected, then confirm the reset.
4. Sign in at the provider and retry the original request.

Keep a shared reset link private.
Anyone with the complete link can confirm it before it expires, and confirming it can disconnect the service for everyone using that agent until reconnection finishes.

### Notes

- `oauth_connections` always runs in the primary MindRoom runtime, even if it appears in `worker_tools`.
- Opening a reset link without confirming it does not change the connection.
- MindRoom refuses an expired, unauthorized, or outdated link before deleting credentials.
- A shared reset link works once. Run `reset_oauth_connection()` again if you need a new one.
- Installation-level connections that are not assigned to an agent scope must be reset from the dashboard.
- Normal `tool_approval` rules still apply when the agent creates the link.

For implementation details and lifecycle guarantees, see [OAuth Credential Lifecycle Design](../dev/oauth-credential-lifecycle-design.md#browser-reset).

## [`usage_stats`]

`usage_stats` returns aggregate retained usage for the caller without modifying session storage.
All operations are local and read-only.
`get_my_usage()` always scopes the result to the current agent and the canonical current requester.
For a private agent, `get_my_usage()` remains inside the current user's exact private instance.
`get_my_private_usage()` reports all configured private agents belonging to the current requester, including retained stores under known requester aliases.
It resolves only that requester's databases and does not scan other users' private instances.
Shared-agent self reports cover retained Agno runs, while private self reports use the isolated Agno session aggregate.
The result excludes the in-flight tool call, and shared-agent self reports can be incomplete after compaction.
Provider billing, embedding usage, and speech-to-text usage are outside version one.

### Self-Service Configuration

Configure a normal agent with `usage_stats` to make `get_my_usage()` and `get_my_private_usage()` available.

```yaml
agents:
  usage_assistant:
    display_name: Usage Assistant
    role: Report the caller's retained MindRoom usage
    tools:
      - usage_stats
```

### Admin Configuration

Configure an admin agent with the per-agent `admin_scope` override to make `get_all_usage()` available.
Admin access requires both `admin_scope: true` and platform-administrator authority.

```yaml
administrators:
  - "@usage-admin:example.com"

agents:
  usage_admin:
    display_name: Usage Administrator
    role: Report retained MindRoom usage across configured entities
    tools:
      - usage_stats:
          admin_scope: true
```

All three functions accept an optional `include_daily` boolean, which defaults to `false`.
`get_my_usage()` reports requester-attributed direct runs for a shared agent or the isolated session aggregate for a private agent.
`get_all_usage()` reports all retained Agno session aggregates across configured agents and teams.

### Daily Token Usage

Call `get_my_usage(include_daily=True)`, `get_my_private_usage(include_daily=True)`, or `get_all_usage(include_daily=True)` to include token usage per day.
The response adds `daily_breakdown`, with one row per UTC calendar date containing `date` (`YYYY-MM-DD`), token `totals`, `run_count`, and a `model_breakdown` grouped by provider and model.
Dates use each retained run's creation timestamp, are sorted oldest first, and omit days without usable retained usage.
The daily breakdown follows the same requester and administrator access rules as the rest of the report.
Each day's combined totals and each model's totals separately include `input_tokens`, `output_tokens`, `cache_read_tokens`, and `cache_write_tokens`, alongside total, reasoning, and audio tokens.
The report's overall totals and overall `model_breakdown` expose the same counters.
For `get_all_usage(include_daily=True)`, each `user_breakdown` entry also includes a `daily_breakdown` with the same daily totals, run counts, and per-model rows.
Requester aliases share one user's daily history, and `user_id: null` contains unattributed daily usage.

`daily_coverage` reports scanned sources, unavailable or partially readable sources, and the retained-history limitation.
Runs with missing or invalid creation timestamps are excluded from daily rows and mark their source as partially unavailable, while their tokens remain eligible for the other totals.
This coverage applies to both overall and per-user daily rows; a user with only undated runs has an empty daily breakdown.
Daily rows use retained top-level runs, so they do not necessarily sum to session totals that include compacted history or nested team-member usage.
Both daily fields are omitted unless `include_daily=True`.

### Response And Coverage

All three functions return a JSON custom-tool envelope with `status` and `tool` fields.
A successful response also includes `scope`, token `totals`, `session_count`, an entity `breakdown`, `coverage`, a `model_breakdown`, and `model_coverage`.
Token totals separately report input, output, cache-read, cache-write, reasoning, and audio dimensions.

The admin response groups session aggregates by configured agent or team ID.
Self reports leave the entity `breakdown` empty; they still include `model_breakdown` and `model_coverage`.
Admin breakdown rows include every configured entity with retained usage and are sorted by total tokens.
Both responses group stored top-level usage snapshots by provider and model in `model_breakdown`.
When a run stores detailed metrics for several models, each model receives its own tokens; repeated uses of the same provider and model within a run are combined.
Older runs without detailed metrics use their recorded provider and model, with missing identities reported as `unknown`.
Malformed model details or details that do not account for the run's token totals put the run's tokens under `unknown` and mark model and daily coverage as partially unavailable.
Model rows include token totals and run counts and are sorted by total tokens.
Each model counts a run once, while daily and user run counts count that run once across all its models.
Coverage reports scanned sources, unavailable or partially unreadable sources, and the retained-history limitation.
Model coverage is reported separately because history lost before usage migration and nested team-member usage can contribute to report totals without model attribution.
Consequently, model rows do not necessarily sum to the top-level totals.
The tool does not change Agno persistence settings.

### Private-Agent Accounting

`private_agent_breakdown` separates usage by `agent_name` and, for admin reports, canonical `user_id`.
Personal reports omit user identifiers and contain only the requester's own private agents.
`get_my_private_usage()` combines those agents in the overall totals; `get_all_usage()` includes private rows alongside the instance-wide report.

Each private row contains session `totals` and `session_count`, plus `retained_run_totals`, `run_count`, and `model_breakdown`.
With `include_daily=True`, it also contains `daily_breakdown` using the same UTC dates and model/token counters as the other daily views.
Session totals can include history compacted before usage migration that no longer has model or daily detail; the two totals are intentionally separate.

For admin reports, session ownership comes from a validated private-instance identity record, falling back to the session's recorded requester.
Retained runs keep their recorded requester, with the validated owner as fallback when requester metadata is missing.
Known aliases are combined; missing ownership remains `user_id: null`.
`private_agent_coverage` describes unavailable attribution or metrics.
All views share the same storage reader, run deduplication, token normalization, and daily grouping.
Admin team totals use Agno's member-inclusive session aggregate without reading nested response content.
Usage snapshots survive compaction, edits, and regeneration; explicit whole-session erasure removes them.
A one-time startup import in `legacy_usage_storage.py` reads available Agno 2.x session blobs and Agno 3 run rows, including partly migrated databases.
The importer retains unknown timestamps and attribution and reports malformed records as coverage gaps.
Reports and tools read the current usage table without migrating or scanning conversation payloads.
History lost before migration cannot be recovered by this report.
Missing counters are reported as zero, so zero does not prove that an older provider recorded that token category.
These counters support cost estimates, but do not guarantee exact billing: provider-specific charging rules and calls outside retained session storage are not captured.

Errors use the same envelope with a stable code.
Common codes are `authorization_error` and `context_unavailable`.

## Matrix Conversations

Use [matrix_message](matrix-message.md#agent-conversations) to start and continue conversations with agents in Matrix.
Enable `matrix_message` on the caller; it also enables `matrix_room` for discovering available agents.
`matrix_room(action="agents")` lists agents and teams eligible to answer this requester in the selected room, including the caller when eligible.
Send `matrix_message(recipient="agent_name", message="...")` to request a response in the current conversation.
For a separate conversation with a thread-mode agent, add `new_thread=True` and keep the returned `thread_id` for later reads and messages.
Room-mode agents use the room timeline and reject requests for separate threads.
Messages return immediately and the conversation remains visible in Matrix.
Use `run_subagent` below when you need a fresh child's result before continuing.

## [`delegate`]

`delegate` exposes `run_subagent` to start a configured agent with fresh conversation context and `continue_subagent` to send follow-ups in that child session.
Both return the child's response inline.
When the caller has a workspace, both calls also accept the standard `mindroom_output_path` argument to save its result and return a file receipt.
Automatic saving of large tool results uses the same configured policy as other tools, including after a child approval resumes.

### What It Does

```python
run_subagent(task: str, agent_name: str | None = None, model: str | None = None) -> str
continue_subagent(subagent_id: str, message: str) -> str
```
The delegated agent is created with `create_agent()` and runs independently with no shared session or chat history from the caller.
Fresh execution still uses the target agent's configured workspace, memory, requester scope, and tool policy.
Set `model` to an alias from `models:` to override the child's model without changing its agent identity.
The override takes precedence over thread and room model choices; omitting `model` or passing `None` uses normal model selection.
Unknown model aliases are rejected before the child starts, with available aliases included in the error.
The caller waits for the child to finish and receives its answer, stable `Subagent ID`, and an audit reference.
Include the relevant facts, constraints, and expected output in `task`, because the child cannot see the caller's conversation.
Selecting the caller's own name starts a fresh copy if that name is explicitly allowed in `delegate_to`.
Omitting `agent_name` or passing `None` selects the caller itself, subject to the same allowlist.
MindRoom gives the delegated agent any already-published last-good knowledge indexes and schedules missing or stale refresh work in the background.
Interactive questions are disabled for delegated runs.
`run_subagent` does not create a Matrix conversation thread.
If a delegated Matrix run requests approval, MindRoom posts the native approval request to the source room and thread while durably retaining the paused parent-child continuation.
Detached OpenAI-compatible runs retain their existing restriction on approval-gated and room-context tools.
If `agent_name` is not in the caller's allowed `delegate_to` list, the tool returns an error string.
Empty tasks and follow-up messages are rejected.

Use `continue_subagent` after the child returns to retain its own conversation history.
The stable ID remains usable across parent turns and restarts, within the same caller, requester, and originating conversation.
Follow-ups preserve the child session, selected model, and nesting depth, recheck current permissions, and require the original storage scope.
The model choice also survives approval pauses and restarts.
Each turn gets a fresh audit record linked by `subagent_id` and `previous_delegation_id`; earlier records remain intact.
Calls wait for a result and do not queue messages into a running child or one awaiting approval.
Finish that child's current turn before sending another message.
After a crash, MindRoom recovers the exact saved outcome; an unfinished turn is marked interrupted without replaying its tools, while a saved approval remains pending.
Runtime-owned handle records live under `MINDROOM_STORAGE_PATH/subagent_sessions/`; editable workspace receipts do not grant continuation authority.

Each child writes `run.json`, `events.jsonl`, and `transcript.md` under the resolved workspace at `.mindroom/delegations/YYYY-MM-DD/<delegation-id>/`.
The folder date is the delegation's start date in UTC, so approval continuations keep the same location across midnight and restarts.
Sensitive fields are redacted, and oversized output is retained through referenced artifacts.
The caller receives `.mindroom/delegation_receipts/YYYY-MM-DD/<delegation-id>.json` in its resolved workspace.
Completed task results include a reference to the child's record.

### Configuration

This tool has no tool-specific inline configuration fields.
Enable it by setting `delegate_to` on the agent config (the dashboard calls this **Allowed subagents**).
MindRoom adds the tool automatically when `delegate_to` is non-empty, so listing `delegate` in `tools:` is usually unnecessary.

### Example

```yaml
agents:
  lead:
    display_name: Lead
    role: Coordinate specialist agents
    model: sonnet
    delegate_to:
      - lead
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

### Notes

- `Config.validate_delegate_to()` accepts explicit self-delegation and rejects unknown target agents at config-load time.
- Recursive delegation is supported, but only up to a maximum depth of 3.
- Native Matrix delegation runs one child at a time per parent; direct tool calls can run children in parallel.
- Use `continue_subagent` for another answer from an existing child; use [matrix_message](matrix-message.md#agent-conversations) for a conversation visible in Matrix.
- Use [`delegate`] when you need a synchronous specialist answer inside the current run.

## [`dynamic_workflow`]

`dynamic_workflow` lets an agent save a reusable workflow, publish immutable revisions, run the active revision, and inspect stored run records.

### What It Does

`dynamic_workflow` exposes `create_workflow()`, `validate_workflow()`, `update_workflow()`, `run_workflow()`, `get_workflow_run()`, `list_workflows()`, and `list_workflow_revisions()`.
All calls return JSON strings with a `status` field and operation-specific payload data.
Saved specs live under `MINDROOM_STORAGE_PATH/dynamic_workflows/`.
Each update creates a new immutable `revisions/<revision>.yaml` file and updates the small `workflow.yaml` pointer file.
Each run pins the active revision at start time, writes a `runs/<run_id>.json` record, and writes `report.md`, `report.html`, and `step_outputs.json` under that run's artifact directory.
If `MINDROOM_PUBLIC_URL` is set, successful and failed run payloads include a private report URL under `/reports/private/...`.
Private report routes authorize the dashboard requester against the run's `requested_by` identity.
Use [`report_publishing`] to publish a completed Dynamic Workflow run report through a revocable public URL under `/reports/public/<slug>`.
If `MINDROOM_PUBLIC_URL` is unset, the report artifacts are still persisted on disk and listed in the run payload.

### Configuration

Enable the tool by adding `dynamic_workflow` to the agent that should be allowed to create and run workflows.
The current implementation supports agent-scoped workflows from agent tools.
Room and tenant scopes are reserved for a future approval policy, so tool calls with `scope="room"` or `scope="tenant"` return an error today.

```yaml
agents:
  coordinator:
    display_name: Coordinator
    role: Build and run reusable Dynamic Workflows
    model: sonnet
    tools:
      - dynamic_workflow
```

### Spec Shape

Workflow specs are declarative JSON/YAML objects with `schema_version: 1`.
The top-level fields are `id`, `name`, `description`, `kind`, `inputs`, `participants`, `workflow`, `outputs`, and `permissions`.
`kind` must be `workflow`.
`inputs` supports an object schema with `required`, `properties`, property `type`, property `description`, and property `enum`.
Participants can be `ephemeral_agent` or `room_agent`.
An `ephemeral_agent` can declare `id`, `name`, `role`, `description`, `model`, `tools`, and `instructions`.
Ephemeral participant `tools` may grant any registered tool except agent-infrastructure tools (`memory`, `delegate`, `self_config`, `compact_context`, `dynamic_workflow`, `dynamic_tools`).
Every participant tool must also be listed in `permissions.tools`.
Dynamic Workflow participants cannot suspend and resume a model run for human approval.
A participant grant is rejected when any exposed function would require approval under the operator's `tool_approval` policy and the caller's `dynamic_workflow` `allowed_tools` config.
Setting `allowed_tools` to `["*"]` makes every granted tool eligible except system-mutating tools and functions still gated by an operator-authored approval rule.
A `room_agent` can declare `id`, `agent`, and an empty `tools` list.
Room-agent participants must already be available to the requester in the current room, use their configured model, and run without tools, skills, knowledge, durable state, or preloaded context files.
Step types are `transform_step`, `agent_step`, and `report_step`.
`transform_step` renders a template without calling a model.
`agent_step` renders a prompt and sends it to the selected participant.
`report_step` renders Markdown report content from input and prior step outputs.
Outputs declare `id`, `type`, and `from_step`.
Output `type` may be `text`, `markdown`, `json`, or `html_report`.
Permissions support runtime caps, model caps, and tool grants.
`permissions.data` must keep `matrix_history`, `attachments`, and `knowledge_bases` disabled until approval-backed data grants exist.

### Example

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
                "model": "claude-sonnet-5",
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
            "models": ["claude-sonnet-5"],
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

Configure `allowed_tools` on the calling agent's `dynamic_workflow` tool entry to make trusted tools eligible for embedded participants.

```yaml
agents:
  builder:
    display_name: Workflow Builder
    tools:
      - dynamic_workflow:
          allowed_tools: [duckduckgo, website]
```

Use `allowed_tools: ["*"]` to make every granted non-system-mutating tool eligible.
Tools outside `allowed_tools` are rejected because Dynamic Workflow has no resumable Matrix approval lifecycle.
Operator-authored approval rules retain precedence, so a matching `require_approval` rule still makes that function unavailable.
System-mutating tools (`claude_agent`, `config_manager`, `scheduler`) are always unavailable to embedded participants.

### Notes

- Dynamic Workflow runs execute synchronously on the current tool call path today.
- Long-running background workflow management, workflow-activation approval cards, Matrix history grants, attachment grants, and knowledge-base grants are future work.
- Ephemeral agents can only use models allowed by both the workflow permissions and the caller's current model policy.
- Granted tools run with the calling agent's tool routing (credentials, worker sandboxing, and egress proxying), and the tool-hook bridge applies plugin gating.
- Room-agent participants can reuse only agents that normal room routing would expose to the requester.
- Runtime caps are enforced for sync and async runs, and async runs are marked failed at the deadline even if participant cancellation is delayed.

## [`report_publishing`]

`report_publishing` lets an agent intentionally publish authorized report artifacts through revocable public links.

### What It Does

`report_publishing` exposes `publish_report()` and `revoke_public_report(slug)`.
All calls return JSON strings with a `status` field and operation-specific payload data.
The tool does not accept arbitrary filesystem paths.
It publishes only registered source references that the current Matrix requester is authorized to read.
The current source types are `dynamic_workflow_run` and `static_site`.
Use `dynamic_workflow_run` to publish a completed Dynamic Workflow HTML report.
Use `static_site` to publish a copied workspace directory that contains `index.html` and optional CSS, JavaScript, images, fonts, or JSON assets.
A `static_site` source path may also point at one workspace HTML file, which is copied and served as `index.html`.
The static site source path is workspace-relative and the published copy is stored under `MINDROOM_STORAGE_PATH/report_publishing/artifacts/<slug>/`.
A static site snapshot may contain at most 200 files and 10 MiB of total data, and publishing fails with an explanatory error beyond either limit.
Static site links serve under the trailing-slash form `/reports/public/<slug>/`, and the slash-less form redirects there so relative asset URLs resolve.
JavaScript is allowed for static sites, but the public route serves static sites with a sandbox CSP that omits `allow-same-origin` and sets `connect-src 'none'`.
That means scripts can drive local page interactivity, but they cannot act as logged-in MindRoom dashboard code or call MindRoom APIs.
Published link records live under `MINDROOM_STORAGE_PATH/report_publishing/`.
Public report links serve the registered artifact without dashboard authentication until `revoke_public_report(slug)` revokes the slug.
The `slug` is the public-report identifier returned by `publish_report()`.
If `MINDROOM_PUBLIC_URL` is set, successful publish payloads include the absolute public URL.

### Configuration

Enable the tool by adding `report_publishing` to any agent that should be allowed to publish report artifacts.

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

### Example

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

### Notes

- `confirm_public=True` is required so accidental publish calls fail closed.
- Dynamic Workflow source references default to `scope="agent"` and may include an explicit `scope`.
- Static site publishing requires an agent workspace and publishes an immutable copy, so later workspace edits need a new `publish_report()` call.
- An agent has a workspace when it uses `memory_backend: file` or a `private:` workspace configuration, and the source path resolves against that canonical workspace root.
- Only the run requester or the user who published the link may revoke it.
- Additional registered report sources can be added without changing Dynamic Workflow storage.
- No extra proxy route is needed when `/reports/public/*` already reaches the MindRoom backend.
- If the dashboard frontend and Python backend are split across upstreams, route `/reports/public/*` to the Python backend and do not put dashboard-login middleware on that path.
- Set `MINDROOM_PUBLIC_URL` to the externally reachable dashboard origin, such as `https://mindroom.lab.mindroom.chat`, so publish payloads include clickable absolute URLs.

## [`config_manager`]

`config_manager` is the full configuration control plane: it inspects and patches any authored `Config` field and creates or updates agents and teams through curated helpers.

### What It Does

`config_manager` exposes `get_info()`, `manage_config()`, `manage_agent()`, and `manage_team()`.
`get_info(info_type, name=None)` supports `mindroom_docs`, `config_schema`, `available_models`, `agents`, `teams`, `available_tools`, `tool_details`, `agent_config`, and `agent_template`.
`tool_details` requires `name` and reads from live `TOOL_METADATA`, so it includes real config fields and statuses from the current worktree.
`agent_config` returns the authored YAML for a specific agent.
`agent_template` generates starter YAML for one of the built-in template types: `researcher`, `developer`, `social`, `communicator`, `analyst`, or `productivity`.
`manage_config(operation, path, changes, dry_run)` addresses the authored document written to `config.yaml` with RFC 6901 JSON Pointer paths.
`manage_config(operation="inspect", path=...)` returns one authored subtree as YAML with secret-bearing values redacted at every pointer depth.
`manage_config(operation="patch", changes=[...])` applies an atomic batch of RFC 6902 `add`, `replace`, and `remove` entries across the full `Config` schema, validates the result against the active runtime, and persists only when validation passes.
`dry_run=True` validates a patch and returns its receipt without writing.
Patch receipts report the config path, changed paths, and validation and persistence status without echoing changed values.
When the configuration is composed from multiple files via `!include`, inspection still works but structured patching is refused so source files are never flattened.
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
manage_config(operation="inspect", path="/authorization")
manage_config(
    operation="patch",
    changes=[
        {"op": "replace", "path": "/models/default/id", "value": "claude-sonnet-5"},
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

### Notes

- [`config_manager`] is broader and more privileged than [`self_config`] because it can inspect and modify other agents and teams.
- `manage_team()` creates teams, but it does not expose a separate update operation on this branch.
- Use [Agent Configuration](../configuration/agents.md) for the full authored schema outside the tool's curated helper surface.

## [`self_config`]

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
    tools=["duckduckgo", "wikipedia", "matrix_message"],
    thread_mode="room",
    context_files=["SOUL.md", "USER.md"],
)
```

### Notes

- `self_config` blocks privileged self-escalation by rejecting `config_manager` in its `tools` update list.
- `include_default_tools=True` is also rejected when `defaults.tools` contains blocked privileged tools such as `config_manager`.
- Use [`self_config`] for narrow self-tuning at runtime and [`config_manager`] for full config-authoring workflows.

## [`openclaw_compat`]

`openclaw_compat` is a config-only preset for OpenClaw-style workspace portability.

### What It Does

`openclaw_compat` is not a runtime toolkit.
The registered factory returns an empty `Toolkit`, and the real behavior comes from `Config.TOOL_PRESETS`.
`Config.expand_tool_names()` expands `openclaw_compat` into `shell`, `coding`, `duckduckgo`, `website`, `browser`, `scheduler`, `matrix_message`.
`matrix_message` then implies `attachments` and `matrix_room`, so the effective enabled set includes both companion toolkits even though the preset does not list them directly.
Preset expansion dedupes while preserving order, so adding `openclaw_compat` alongside one of its member tools does not create duplicates.
This preset is meant for OpenClaw-compatible workspace behavior inside MindRoom rather than for cloning the full OpenClaw gateway control plane.

### Configuration

This preset has no inline configuration fields and cannot use `defer` or `initial`.
Configure individual member tools directly when they need lazy loading.

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

- [`openclaw_compat`] is a preset name that belongs in `tools:` but does not expose callable runtime methods of its own.
- Use the dedicated [OpenClaw Workspace Import](../openclaw.md) guide for workspace layout, file memory behavior, and migration details.
- If you only need one or two of the member tools, configure those tools directly instead of using the preset.

## [`claude_agent`]

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

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `no` | `null` | Anthropic API key or gateway-compatible key material. Usually stored in credentials JSON or dashboard setup instead of inline YAML. |
| `anthropic_base_url` | `url` | `no` | `null` | Optional Anthropic-compatible gateway root URL. Use the host root, not a `/v1` suffix. |
| `anthropic_auth_token` | `password` | `no` | `null` | Optional bearer token for Anthropic-compatible gateways. |
| `disable_experimental_betas` | `boolean` | `no` | `false` | Sets `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` for gateway compatibility. |
| `cwd` | `text` | `no` | `null` | Working directory passed to the Claude Agent SDK client. |
| `model` | `text` | `no` | `null` | Claude model override. When omitted, the tool falls back to the current agent model ID when one is available. |
| `permission_mode` | `text` | `no` | `default` | One of `default`, `acceptEdits`, `plan`, or `bypassPermissions`. Invalid values fall back to `default`. |
| `continue_conversation` | `boolean` | `no` | `false` | Continue the same Claude conversation context across queries in one session. |
| `allowed_tools` | `text` | `no` | `null` | Comma-separated Claude Code tool names to allow. |
| `disallowed_tools` | `text` | `no` | `null` | Comma-separated Claude Code tool names to deny. |
| `max_turns` | `number` | `no` | `null` | Maximum Claude turns per query. Values below 1 are normalized up to 1. |
| `system_prompt` | `text` | `no` | `null` | Extra system prompt passed directly to the Claude Agent SDK. |
| `cli_path` | `text` | `no` | `null` | Optional path to the Claude CLI executable. |
| `session_ttl_minutes` | `number` | `no` | `60` | Idle-session expiration window in minutes. Values below 1 are normalized up to 1. |
| `max_sessions` | `number` | `no` | `200` | Maximum live sessions per agent namespace. Values below 1 are normalized up to 1. |

### Example

```yaml
agents:
  code:
    display_name: Code Agent
    role: Coding assistant with persistent Claude sessions
    model: default
    tools:
      - claude_agent:
          model: claude-sonnet-5
          cwd: /workspace/project
          permission_mode: acceptEdits
          continue_conversation: true
          session_ttl_minutes: 180
          max_sessions: 20
```

```json
{
  "api_key": "sk-ant-or-proxy-key",
  "model": "claude-sonnet-5",
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
- See [OpenAI-Compatible API](../openai-api.md) for request-level session continuity details.

## Related Docs

- [Tools Overview](index.md)
- [Agent Configuration](../configuration/agents.md)
- [OpenClaw Workspace Import](../openclaw.md)
- [OpenAI-Compatible API](../openai-api.md)
