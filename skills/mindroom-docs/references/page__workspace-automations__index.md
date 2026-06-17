# Workspace Automations

Workspace automations are deterministic cron-like checks that live in an agent workspace.
They are useful for cheap polling work that should not invoke an LLM every time it runs.
A shell check can inspect files, call a local script, or query a service through the normal shell tool runtime, then only take an action when trigger predicates match.

Workspace automations are separate from Matrix [`!schedule`](https://docs.mindroom.chat/scheduling/).
`!schedule` persists Matrix scheduled tasks that are interpreted by agents and LLMs.
Workspace automations are workspace-local deterministic checks that can run without involving an LLM unless their action asks for one.

## File Location

Each eligible agent workspace may contain this file:

```text
.mindroom/automations.yaml
```

The file path is relative to the resolved agent workspace root.
MindRoom loads the file from the workspace and never authors it automatically.
Automation runtime status is stored separately under `mindroom_data/workspace_automations/state.json`.

## Eligibility

Workspace automations are disabled by default.
Enable them with `workspace_automations` in `defaults` or on an individual agent.
Shared agent workspaces are eligible when policy is enabled and the agent has a resolved workspace.
Private agent workspaces are eligible after a real requester interaction has materialized that private workspace and MindRoom has recorded the concrete workspace instance.
MindRoom does not proactively create private workspaces for every possible requester just because automations are enabled.
Shared agents that use requester-scoped `worker_scope: user` or `worker_scope: user_agent` are still skipped because they have no requester-owned workspace instance for unattended automation checks.

```yaml
defaults:
  workspace_automations:
    enabled: false
    min_interval_seconds: 60
    max_timeout_seconds: 30
    max_output_bytes: 65536
    allowed_actions: []

agents:
  ops:
    display_name: Ops
    tools: [shell, workspace_automation]
    rooms: [Lobby]
    workspace_automations:
      enabled: true
      allowed_actions: [hook, agent_message]
```

`enabled` controls whether MindRoom discovers and runs workspace automation files for the policy scope.
`min_interval_seconds` rejects cron schedules that can run more often than the configured interval.
`max_timeout_seconds` caps `check.timeout_seconds` for one shell check.
`max_output_bytes` caps command output returned from the check, used for trigger evaluation, and included in the `automation:triggered` hook check-result payload.
The value must be between 1024 and 65536 bytes.
`allowed_actions` lists visible or side-effecting action types that the workspace file may use.
`none` is always allowed and is not listed in `allowed_actions`.

## Automation File

This is a complete first-version automation file:

```yaml
version: 1
automations:
  urgent_email_poll:
    enabled: true
    schedule: "*/5 * * * *"
    check:
      type: shell
      command: "./scripts/check-urgent-email.sh"
      timeout_seconds: 20
      tail: 100
    trigger:
      exit_code: 42
      stdout_matches: "urgent"
      stderr_not_matches: "auth failed"
    action:
      type: agent_message
      room: "Lobby"
      message: "Urgent email condition matched, investigate and summarize the result."
```

`version` must be `1`.
Each automation id must be a single path-safe identifier.
`schedule` is a five-field cron expression.
`check.type` is `shell` in the first version.
`check.command` runs from the owning agent workspace.
`trigger` supports `exit_code`, `stdout_matches`, `stderr_matches`, `stdout_not_matches`, and `stderr_not_matches`.
Every action except `none` requires a trigger with at least one predicate.
`action.room` is required for `matrix_message` and `agent_message` unless the owning agent has exactly one configured room.
When `action.room` is present or inferred, it must be one of the owning agent's configured `rooms`.
`action.message` is required for `matrix_message` and `agent_message`.

## Actions

`none` records whether the check matched and performs no visible action.
`hook` emits `automation:triggered` with the automation metadata, check result, trigger payload, and action payload.
For `hook`, `action.room` is optional, but room-scoped hooks only match when the configured or inferred room resolves to a Matrix room ID.
`matrix_message` sends the configured message to Matrix without asking an agent to respond.
`agent_message` sends the configured Matrix message with dispatch enabled and an internal mention for the owning agent, which is the action path that can start an LLM response.

Every non-`none` action requires explicit policy opt-in through `allowed_actions`.
Deterministic shell checks do not start an LLM by themselves.
Hooks decide what happens after `automation:triggered`, so a hook can call an LLM, perform external work, send messages, or do nothing.
See [Hooks](https://docs.mindroom.chat/hooks/) for hook configuration.

## Management Tool

Agents can be given the `workspace_automation` tool.
The tool exposes `workspace_automation.validate_automations`, `workspace_automation.list_automations`, and `workspace_automation.reload_automations`.
`validate_automations` reads workspace files and returns loaded automation summaries plus validation errors.
`list_automations` reports automations currently loaded by the live service and their latest status.
`reload_automations` asks the live service to reload workspace files and reconcile its cron loops.
The management tool validates, lists, and reloads automation definitions, but it does not create or edit `.mindroom/automations.yaml`.

## Deployment Behavior

The workspace automation scheduler runs inside the primary MindRoom runtime.
There is one central scheduler, not one platform-native scheduler per automation.
Worker runtimes execute shell checks on demand when the central scheduler reaches a due time.
Shell checks use the same worker-routed `shell` tool path as ordinary shell calls.
This means worker backend, worker scope, workspace home, environment filtering, and credential lease behavior come from the normal shell tool deployment.
Dedicated worker backends may scale down between automation runs.
MindRoom re-ensures or recreates the worker on the next due run before executing the shell check.
MindRoom does not create platform-native cron resources or keep dedicated workers alive just because an automation exists.
On Kubernetes, this means MindRoom does not create CronJobs or keep-alive worker pods for workspace automations.
See [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/#workspace-automation-shell-checks) for deployment details.
