# Workspace Automations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build agent-owned declarative workspace automations that run deterministic cron checks through the existing worker backend and only invoke Matrix, hooks, or LLM work when explicit trigger rules match.

**Architecture:** Automation definitions live in `<agent-workspace>/.mindroom/automations.yaml`, while the primary MindRoom runtime owns discovery, validation, scheduling, execution, and supervision.
Worker containers are provisioned on demand through the same worker routing path used by shell/file/python tools.
In Kubernetes, workers may scale to zero between runs while persistent worker state may survive; automation correctness must not depend on a live container.
The first implementation supports shared agent workspaces and leaves requester-private workspace automations blocked until a durable execution-identity registry exists.

**Tech Stack:** Python 3.13, Pydantic, croniter, PyYAML, asyncio, existing MindRoom worker routing, existing hook registry, existing Matrix delivery helpers, pytest.

---

## Scope

This plan adds a new workspace automation system.
It does not replace `!schedule` or the existing scheduler tool.
It adds deterministic polling jobs for cheap checks that may optionally escalate to an LLM, Matrix message, or plugin hook.

The first version supports shared agents only.
Private agents and requester-scoped materializations require a follow-up registry because the runtime cannot safely reconstruct a private requester execution identity from workspace paths alone.

The first version supports `shell` checks only.
The data model should leave room for future check types, but no other check type should be implemented in this PR.

## File Structure

Create `src/mindroom/workspace_automations/__init__.py`.
This package exposes the public service and typed models.

Create `src/mindroom/workspace_automations/models.py`.
This file owns Pydantic models for the YAML file, normalized runtime records, trigger rules, action rules, policy, and run results.

Create `src/mindroom/workspace_automations/loader.py`.
This file reads and validates `.mindroom/automations.yaml` files from resolved agent workspaces.

Create `src/mindroom/workspace_automations/targets.py`.
This file resolves enabled shared agents, workspaces, worker scope, and Matrix action targets.

Create `src/mindroom/workspace_automations/executor.py`.
This file executes deterministic checks through the shell toolkit's structured command function with existing worker routing.

Create `src/mindroom/workspace_automations/triggers.py`.
This file evaluates exit code and stdout/stderr regex trigger rules.

Create `src/mindroom/workspace_automations/actions.py`.
This file performs `agent_message`, `matrix_message`, and `hook` actions after a trigger matches using router-bot Matrix bindings supplied by the service.

Create `src/mindroom/workspace_automations/service.py`.
This file owns the runtime scanner, cron loop, task lifecycle, active-service accessor, config refresh, and shutdown.

Create `src/mindroom/custom_tools/workspace_automation.py`.
This file provides the optional agent-facing tool for validating, listing, and reloading workspace automations.

Create `src/mindroom/tools/workspace_automation.py`.
This file registers the tool metadata for `workspace_automation`.

Modify `src/mindroom/config/models.py`.
Add defaults-level automation policy.

Modify `src/mindroom/config/agent.py`.
Add per-agent automation policy overrides.

Modify `src/mindroom/config/main.py`.
Add an effective policy helper for one agent.

Modify `src/mindroom/workspaces.py`.
Allow workspace creation for agents with workspace automations enabled even when file-backed memory is not enabled.

Inspect `src/mindroom/runtime_resolution.py`.
Verify normal runtime resolution passes through automation-only workspaces without file memory roots.

Modify `src/mindroom/orchestrator.py`.
Wire the service lifecycle and hook registry snapshot into the primary runtime.

Modify `src/mindroom/tools/__init__.py`.
Export the new tool registration function.

Create `docs/workspace-automations.md`.
Add a short deployment note to `docs/deployment/sandbox-proxy.md`.
Modify `docs/scheduling.md`.

Add focused tests under `tests/`.
Prefer new files over growing unrelated large tests.

## YAML Contract

Use this first-version schema.

```yaml
version: 1
automations:
  urgent_email_poll:
    enabled: true
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "./scripts/check_urgent_email.sh"
      timeout_seconds: 20
      tail: 100
    trigger:
      exit_code: 42
    action:
      type: agent_message
      room: "Lobby"
      thread_id: null
      message: "Urgent email condition matched. Investigate and summarize."
```

Rules:

- `version` is required and must be `1`.
- `automations` is required and must be a mapping.
- Automation IDs must be single path-safe identifiers matching `^[A-Za-z0-9_.-]+$`.
- `schedule` is a five-field cron expression.
- `check.type` must be `shell` in the first version.
- `check.command` runs from the owning agent workspace.
- `check.timeout_seconds` must be at least `1` and no larger than the effective policy limit.
- `trigger` must be present for visible actions.
- `action.type` may be `none`, `agent_message`, `matrix_message`, or `hook`.
- `none` is always permitted and is not listed in `allowed_actions`.
- `agent_message` and `matrix_message` require an explicit `room` unless the owning agent has exactly one configured room.
- `thread_id` is optional and must be literal Matrix thread/root event ID when provided.
- Environment variables are not declared in automation YAML.
- Shell env exposure continues to use existing shell tool policy and agent tool overrides.

## Policy Contract

Add this shape to `defaults` and `agents.<name>`.

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
    workspace_automations:
      enabled: true
      allowed_actions: [agent_message]
```

Per-agent values override defaults field-by-field.
If `enabled` is false, the runtime ignores that agent's automation file and the automation tool reports that automations are disabled.
If an action is not in `allowed_actions`, the loader rejects that automation entry and logs the validation error.
Agents that need `matrix_message` or `hook` must opt into those actions explicitly.
`max_output_bytes` caps the returned, trigger-evaluated, and persisted command output for one check run.
The first version should default to disabled at both defaults and agent levels.

## Task 1: Add Config Policy Models

**Files:**

- Modify: `src/mindroom/config/models.py`
- Modify: `src/mindroom/config/agent.py`
- Modify: `src/mindroom/config/main.py`
- Test: `tests/test_workspace_automations_config.py`

- [ ] **Step 1: Write failing config tests**

Add tests for default disabled policy, per-agent enabled policy, field-by-field merge, invalid action names, duplicate actions, and min/max bounds.

```python
def test_workspace_automation_policy_defaults_disabled(runtime_paths):
    config = Config.validate_with_runtime({"agents": {"ops": {"display_name": "Ops"}}}, runtime_paths)
    policy = config.get_agent_workspace_automation_policy("ops")
    assert policy.enabled is False
    assert policy.allowed_actions == []
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_config.py -q`.
Expected: FAIL because the policy helper and models do not exist.

- [ ] **Step 3: Add typed policy models**

In `src/mindroom/config/models.py`, add:

```python
WorkspaceAutomationActionName = Literal["agent_message", "matrix_message", "hook"]

class WorkspaceAutomationPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    min_interval_seconds: int | None = Field(default=None, ge=60)
    max_timeout_seconds: int | None = Field(default=None, ge=1)
    max_output_bytes: int | None = Field(default=None, ge=1024)
    allowed_actions: list[WorkspaceAutomationActionName] | None = None
```

Normalize duplicates in `allowed_actions` with the existing duplicate validation style.
Do not include `none` in this policy type because `none` is always allowed and never visible.

- [ ] **Step 4: Add defaults and agent fields**

Add `workspace_automations: WorkspaceAutomationPolicyConfig` to `DefaultsConfig` with concrete defaults.
Add `workspace_automations: WorkspaceAutomationPolicyConfig | None` to `AgentConfig`.
Use descriptions that clearly say this gates workspace-authored unattended automation and not normal scheduled tasks.

- [ ] **Step 5: Add effective policy helper**

Add `Config.get_agent_workspace_automation_policy(agent_name: str) -> WorkspaceAutomationPolicyConfig`.
Return a concrete merged config with no `None` fields.
Keep the helper near other effective per-agent helpers.

- [ ] **Step 6: Run focused config tests**

Run: `uv run pytest tests/test_workspace_automations_config.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/mindroom/config/models.py src/mindroom/config/agent.py src/mindroom/config/main.py tests/test_workspace_automations_config.py
git commit -m "feat: add workspace automation policy config"
```

## Task 2: Ensure Enabled Agents Have a Workspace

**Files:**

- Modify: `src/mindroom/workspaces.py`
- Test: `tests/test_workspace_automations_workspace.py`

- [ ] **Step 1: Write failing workspace tests**

Add tests that a shared agent with workspace automations enabled gets `agents/<name>/workspace` even when memory backend is not `file`.
Add tests that disabled agents keep current behavior.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_workspace.py -q`.
Expected: FAIL because shared workspaces still depend on file-backed memory.

- [ ] **Step 3: Add workspace requirement helper**

Add a small helper in `workspaces.py`:

```python
def agent_requires_workspace(agent_name: str, config: Config) -> bool:
    return (
        config.get_agent_memory_backend(agent_name) == "file"
        or config.get_agent_workspace_automation_policy(agent_name).enabled
    )
```

Use this helper in `_resolve_workspace` for non-private agents instead of checking only file memory.

- [ ] **Step 4: Preserve file memory behavior**

Set `file_memory_path` to `root` only when file memory is enabled.
Set `file_memory_path` to `None` when the workspace exists only for automations.

- [ ] **Step 5: Verify runtime pass-through**

Confirm `src/mindroom/runtime_resolution.py` already uses `workspace.root` for tool base directories and `workspace.file_memory_path` only for file-memory roots.
Modify it only if the focused tests expose a pass-through gap.

- [ ] **Step 6: Run focused workspace tests**

Run: `uv run pytest tests/test_workspace_automations_workspace.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/mindroom/workspaces.py tests/test_workspace_automations_workspace.py
git commit -m "feat: resolve workspaces for workspace automations"
```

If `src/mindroom/runtime_resolution.py` changed because the pass-through test exposed a real gap, add it explicitly in the same commit.

## Task 3: Add Automation YAML Models and Loader

**Files:**

- Create: `src/mindroom/workspace_automations/__init__.py`
- Create: `src/mindroom/workspace_automations/models.py`
- Create: `src/mindroom/workspace_automations/loader.py`
- Test: `tests/test_workspace_automations_loader.py`

- [ ] **Step 1: Write failing loader tests**

Cover missing file, valid file, invalid YAML, invalid cron, interval below policy, timeout above policy, unsupported action, unsafe automation ID, and explicit room target parsing.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_loader.py -q`.
Expected: FAIL because package and loader do not exist.

- [ ] **Step 3: Add Pydantic YAML models**

Create models for:

- `WorkspaceAutomationFile`
- `WorkspaceAutomationDefinition`
- `ShellCheckDefinition`
- `TriggerDefinition`
- `AutomationActionDefinition`
- `LoadedWorkspaceAutomation`
- `AutomationValidationError`

Keep runtime records dataclass-based where they are not config input.

- [ ] **Step 4: Validate cron and policy**

Use `croniter` to validate five-field cron expressions.
Reject schedules that can fire more often than `min_interval_seconds` by comparing the first two future fire times from a fixed base time.

- [ ] **Step 5: Load from predictable path**

Expose:

```python
AUTOMATIONS_RELATIVE_PATH = Path(".mindroom") / "automations.yaml"

def load_workspace_automations(
    *,
    agent_name: str,
    workspace_root: Path,
    policy: WorkspaceAutomationPolicyConfig,
) -> tuple[list[LoadedWorkspaceAutomation], list[AutomationValidationError]]:
    ...
```

Return empty lists when the file does not exist.
Never create the file from the loader.

- [ ] **Step 6: Run focused loader tests**

Run: `uv run pytest tests/test_workspace_automations_loader.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/mindroom/workspace_automations/__init__.py src/mindroom/workspace_automations/models.py src/mindroom/workspace_automations/loader.py tests/test_workspace_automations_loader.py
git commit -m "feat: load workspace automation files"
```

## Task 4: Resolve Automation Targets

**Files:**

- Create: `src/mindroom/workspace_automations/targets.py`
- Modify: `src/mindroom/workspace_automations/models.py`
- Test: `tests/test_workspace_automations_targets.py`

- [ ] **Step 1: Write failing target tests**

Cover shared enabled agents, disabled agents, missing workspaces, explicit room target, single configured room fallback, multi-room ambiguity, and private agents being skipped with a clear reason.

- [ ] **Step 2: Run focused target tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_targets.py -q`.
Expected: FAIL because target resolution does not exist.

- [ ] **Step 3: Add shared agent target resolution**

Expose:

```python
def iter_workspace_automation_targets(
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[WorkspaceAutomationTarget]:
    ...
```

Resolve `agent_runtime = resolve_agent_runtime(agent_name, config, runtime_paths, execution_identity=None, create=True)`.
Skip private agents in the first version.
Skip disabled agents.
Skip agents with no workspace after resolution.

- [ ] **Step 4: Resolve Matrix room targets separately**

Expose:

```python
def resolve_action_room(
    *,
    action_room: str | None,
    agent_configured_rooms: list[str],
) -> str | None:
    ...
```

Return the explicit room when present.
Return the only configured room when exactly one exists.
Return `None` when ambiguous or missing.
Defer alias-to-room resolution to the action layer where a Matrix client exists.

- [ ] **Step 5: Run focused target tests**

Run: `uv run pytest tests/test_workspace_automations_targets.py -q`.
Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/mindroom/workspace_automations/models.py src/mindroom/workspace_automations/targets.py tests/test_workspace_automations_targets.py
git commit -m "feat: resolve workspace automation targets"
```

## Task 5: Execute Shell Checks Through Worker Routing

**Files:**

- Create: `src/mindroom/workspace_automations/executor.py`
- Modify: `src/mindroom/tools/shell.py`
- Test: `tests/test_workspace_automations_executor.py`
- Test: `tests/test_shell_tool.py`

- [ ] **Step 1: Write failing executor tests**

Use strict fakes around toolkit construction.
Verify the shell check runs with `cwd` equal to the workspace through the shell `base_dir` override.
Verify worker scope is the resolved agent execution scope.
Verify timeout, tail, and byte cap are passed to `run_shell_command_structured`.
Verify execution errors are returned as failed check results, not raised out of the service loop.

- [ ] **Step 2: Run focused executor tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_executor.py -q`.
Expected: FAIL because executor does not exist.

- [ ] **Step 3: Build automation execution identity**

For shared agents, call `build_tool_execution_identity(...)` from `src/mindroom/tool_system/worker_routing.py`.
Pass:

- `channel="matrix"`
- `agent_name=<agent>`
- `transport_agent_name=<agent>`
- `runtime_paths=runtime_paths`
- `requester_id=None`
- `room_id=None`
- `thread_id=None`
- `resolved_thread_id=None`
- `session_id=f"workspace-automation:{agent}:{automation_id}"`

Tenant and account IDs come from `runtime_paths.env_value("CUSTOMER_ID")` and `runtime_paths.env_value("ACCOUNT_ID")` through this helper.
Keep requester-scoped execution out of scope until private automation support exists.
Do not place a room display name or alias into `ToolExecutionIdentity.room_id`; Matrix target resolution belongs to the action layer.

- [ ] **Step 4: Reuse existing toolkit construction**

Call `build_agent_toolkit("shell", ...)` rather than manually constructing proxy payloads.
Pass the resolved `agent_runtime` and `execution_identity`.
Use `config.get_agent_tool_runtime_overrides(agent_name, "shell")` so existing shell env passthrough policy still applies.
Invoke the structured shell function described below rather than `run_shell_command`.

- [ ] **Step 5: Normalize shell result**

The worker-routed shell function must return a plain JSON-serializable `dict`, not a dataclass.
The sandbox runner serializes tool results through `to_json_compatible`, and non-mapping custom objects would be stringified.
The primary executor reconstructs `ShellCheckResult` from that dict after the proxy call returns.

The dict must contain:

- `ok`
- `exit_code`
- `stdout`
- `stderr`
- `raw_output`
- `timed_out`
- `error`

The public `run_shell_command` response remains unchanged.
The automation executor must never parse the human-readable `run_shell_command` text to infer exit codes.
The structured helper must enforce the effective `max_output_bytes` cap before returning stdout, stderr, and raw output.

- [ ] **Step 6: Add shell structured execution helper**

Add `run_shell_command_structured` to `src/mindroom/tools/shell.py`.
Register it in the toolkit's `tools=[...]` list and in the tool metadata `function_names`.
Accept that agents with the shell tool can call this function because it grants no new capability beyond shell execution and only changes result shape.
Return a JSON-safe mapping with numeric `exit_code`.
Accept the effective `max_output_bytes` cap and truncate returned stdout, stderr, and raw output by bytes.
Do not return a dataclass across the worker boundary.
Keep `run_shell_command` behavior unchanged.
Test this helper in `tests/test_shell_tool.py` or a new focused file.

- [ ] **Step 7: Run focused executor tests**

Run: `uv run pytest tests/test_workspace_automations_executor.py tests/test_shell_tool.py -q`.
Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/mindroom/workspace_automations/executor.py src/mindroom/tools/shell.py tests/test_workspace_automations_executor.py tests/test_shell_tool.py
git commit -m "feat: execute workspace automation shell checks"
```

## Task 6: Add Trigger Evaluation

**Files:**

- Create: `src/mindroom/workspace_automations/triggers.py`
- Modify: `src/mindroom/workspace_automations/models.py`
- Test: `tests/test_workspace_automations_triggers.py`

- [ ] **Step 1: Write failing trigger tests**

Cover exit code match, exit code mismatch, stdout regex match, stderr regex match, missing output, invalid regex validation, and combined trigger semantics.
Assert exit-code triggers use the structured shell result and do not parse human output.

- [ ] **Step 2: Run focused trigger tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_triggers.py -q`.
Expected: FAIL because trigger evaluation does not exist.

- [ ] **Step 3: Implement trigger semantics**

Use AND semantics when multiple trigger fields are present.
Support first-version fields:

- `exit_code`
- `stdout_matches`
- `stderr_matches`
- `stdout_not_matches`
- `stderr_not_matches`

Leave JSON field triggers out of the first PR.

- [ ] **Step 4: Run focused trigger tests**

Run: `uv run pytest tests/test_workspace_automations_triggers.py -q`.
Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/mindroom/workspace_automations/models.py src/mindroom/workspace_automations/triggers.py tests/test_workspace_automations_triggers.py
git commit -m "feat: evaluate workspace automation triggers"
```

## Task 7: Add Actions and Hook Event

**Files:**

- Create: `src/mindroom/workspace_automations/actions.py`
- Modify: `src/mindroom/hooks/types.py`
- Modify: `src/mindroom/hooks/context.py`
- Modify: `src/mindroom/hooks/__init__.py`
- Test: `tests/test_workspace_automations_actions.py`

- [ ] **Step 1: Write failing action tests**

Cover `none`, `matrix_message`, `agent_message`, `hook`, unsupported action by policy, room resolution failure, and hook payload shape.

- [ ] **Step 2: Run focused action tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_actions.py -q`.
Expected: FAIL because actions do not exist.

- [ ] **Step 3: Add hook event type**

Add `EVENT_AUTOMATION_TRIGGERED = "automation:triggered"` to `src/mindroom/hooks/types.py`.
Add it to `BUILTIN_EVENT_NAMES` and default timeout map.
Add `"automation"` to reserved namespaces.

- [ ] **Step 4: Add hook context**

Add `AutomationTriggeredContext` to `src/mindroom/hooks/context.py`.
Fields should include:

- `agent_name`
- `automation_id`
- `workspace_root`
- `room_id`
- `thread_id`
- `check_result`
- `trigger_payload`
- `action_payload`

Do not expose secrets or raw environment values.

- [ ] **Step 5: Implement action execution**

For `matrix_message`, send a visible Matrix message without triggering dispatch.
For `agent_message`, send a Matrix message with dispatch-triggering metadata using existing hook sender semantics where possible.
For `hook`, emit `automation:triggered` with the context and no Matrix message by default.
For `none`, record the run and do nothing visible.
Use a router-bot `nio.AsyncClient` supplied by the service through a bot provider.
Build the hook message sender from that router client, the active config, runtime paths, and the orchestrator conversation cache.
Pass `trigger_dispatch=True` only for `agent_message`.
If the router bot or its client is not ready at fire time, record a transient action failure and retry on the next scheduled occurrence.

- [ ] **Step 6: Run focused action tests**

Run: `uv run pytest tests/test_workspace_automations_actions.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/mindroom/workspace_automations/actions.py src/mindroom/hooks/types.py src/mindroom/hooks/context.py src/mindroom/hooks/__init__.py tests/test_workspace_automations_actions.py
git commit -m "feat: add workspace automation actions"
```

## Task 8: Add Runtime Service

**Files:**

- Create: `src/mindroom/workspace_automations/service.py`
- Modify: `src/mindroom/orchestrator.py`
- Test: `tests/test_workspace_automations_service.py`

- [ ] **Step 1: Write failing service tests**

Cover service startup, scan interval, cron scheduling, reload on config replacement, cancellation on disabled policy, cancellation on deleted file, no duplicate tasks after reload, and clean shutdown.

- [ ] **Step 2: Run focused service tests and verify failure**

Run: `uv run pytest tests/test_workspace_automations_service.py -q`.
Expected: FAIL because service does not exist.

- [ ] **Step 3: Implement service**

Create `WorkspaceAutomationService` with:

- `start(config, runtime_paths, hook_registry, bot_provider, conversation_cache)`
- `refresh(config, hook_registry, bot_provider, conversation_cache)`
- `shutdown()`
- `scan_now()`
- `list_loaded()`

Keep task dictionaries private to the service.
Use stable keys `(agent_name, automation_id, workspace_root)`.
The `bot_provider` follows the existing orchestrator pattern used by `ApprovalMatrixTransport`.
The service uses `bot_provider(ROUTER_AGENT_NAME)` for Matrix delivery and never embeds delivery logic in `orchestrator.py`.
The `conversation_cache` comes from the orchestrator runtime support event cache and is passed to `build_hook_message_sender`.

- [ ] **Step 4: Implement cron loop**

Each automation loop should:

1. Compute next fire with `croniter`.
2. Sleep in bounded chunks so shutdown and reload are responsive.
3. Re-read the current loaded automation before firing.
4. Execute the deterministic check.
5. Evaluate triggers.
6. Run action only when matched.
7. Record status in memory and a small JSON state file under `storage_root/workspace_automations/state.json`.

Protect state-file writes with an `asyncio.Lock`.
If the state shape grows beyond a small status map, shard by automation key or move to SQLite before adding concurrent writers.

- [ ] **Step 5: Wire orchestrator lifecycle**

Instantiate the service in `MultiAgentOrchestrator.__post_init__`.
Start it after config and hook registry activation.
Refresh it when config reload succeeds and hook registry changes.
Shutdown it before worker manager shutdown.
Keep `orchestrator.py` changes limited to lifecycle calls and dependency injection.
It may pass `bot_provider=lambda agent_name: self.agent_bots.get(agent_name)` just like existing runtime services.
It should pass the existing conversation cache rather than constructing a new cache.

- [ ] **Step 6: Run focused service tests**

Run: `uv run pytest tests/test_workspace_automations_service.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/mindroom/workspace_automations/service.py src/mindroom/orchestrator.py tests/test_workspace_automations_service.py
git commit -m "feat: supervise workspace automations"
```

## Task 9: Add Agent-Facing Management Tool

**Files:**

- Create: `src/mindroom/custom_tools/workspace_automation.py`
- Create: `src/mindroom/tools/workspace_automation.py`
- Modify: `src/mindroom/tools/__init__.py`
- Test: `tests/test_workspace_automation_tool.py`

- [ ] **Step 1: Write failing tool tests**

Cover disabled policy, validate current file, list loaded automations, reload request, and validation errors surfaced to the agent.

- [ ] **Step 2: Run focused tool tests and verify failure**

Run: `uv run pytest tests/test_workspace_automation_tool.py -q`.
Expected: FAIL because tool does not exist.

- [ ] **Step 3: Add toolkit**

Expose methods:

- `validate_automations()`
- `list_automations()`
- `reload_automations()`

Do not add a tool method that writes arbitrary YAML in the first version.
Agents can use the file tool to edit the file, then use this tool to validate and reload.

- [ ] **Step 4: Add live service accessor**

In `src/mindroom/workspace_automations/service.py`, expose module-level helpers:

```python
def set_active_workspace_automation_service(service: WorkspaceAutomationService | None) -> None: ...
def get_active_workspace_automation_service() -> WorkspaceAutomationService | None: ...
```

Update the orchestrator lifecycle to set the active service on start and clear it on shutdown.
The management tool uses this accessor for `list_automations()` and `reload_automations()`.
`validate_automations()` can run directly from the current tool runtime context without requiring a live service.

- [ ] **Step 5: Register metadata**

Register tool name `workspace_automation`.
Use category productivity or development.
Set setup type none.
Keep default execution target primary because it manages runtime service state.

- [ ] **Step 6: Run focused tool tests**

Run: `uv run pytest tests/test_workspace_automation_tool.py -q`.
Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/mindroom/custom_tools/workspace_automation.py src/mindroom/tools/workspace_automation.py src/mindroom/tools/__init__.py tests/test_workspace_automation_tool.py
git commit -m "feat: add workspace automation tool"
```

## Task 10: Add Kubernetes and Backend-Neutral Coverage

**Files:**

- Modify: `tests/test_kubernetes_worker_backend.py`
- Create: `tests/test_workspace_automations_worker_routing.py`
- Test only unless a real bug is found.

- [ ] **Step 1: Write worker routing tests**

Assert automation shell checks call the same worker target as normal shell tools for shared agents.
Assert Kubernetes backend idle cleanup remains allowed between automation runs.
Assert a new due run calls `ensure_worker` again rather than assuming the old pod still exists.

- [ ] **Step 2: Run focused tests and verify failure or pass**

Run: `uv run pytest tests/test_workspace_automations_worker_routing.py tests/test_kubernetes_worker_backend.py -q`.
Expected: FAIL until service/executor wires worker routing cleanly.

- [ ] **Step 3: Fix only actual wiring issues**

Do not add Kubernetes-specific automation state.
Do not create Kubernetes CronJobs.
Do not keep worker pods alive just because automation YAML exists.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_workspace_automations_worker_routing.py tests/test_kubernetes_worker_backend.py -q`.
Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_workspace_automations_worker_routing.py tests/test_kubernetes_worker_backend.py
git commit -m "test: cover workspace automation worker routing"
```

## Task 11: Add Documentation

**Files:**

- Create: `docs/workspace-automations.md`
- Modify: `docs/scheduling.md`
- Modify: `docs/deployment/sandbox-proxy.md`

- [ ] **Step 1: Write docs**

Document the difference between scheduled agent tasks and deterministic workspace automations.
Document `.mindroom/automations.yaml`.
Document policy gates.
Document Kubernetes behavior.
Document that worker containers may scale to zero between runs while persistent worker state may survive.
Document that private workspace automations are not supported in the first version.

- [ ] **Step 2: Keep Markdown style**

Write one sentence per line.
Do not split a sentence across multiple lines.

- [ ] **Step 3: Run docs-adjacent tests if available**

Run: `uv run pytest tests/test_cron_natural_language.py tests/test_scheduler_tool.py -q`.
Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add docs/workspace-automations.md docs/scheduling.md docs/deployment/sandbox-proxy.md
git commit -m "docs: explain workspace automations"
```

## Task 12: Final Verification

**Files:**

- No new files unless failures require fixes.

- [ ] **Step 1: Run focused workspace automation suite**

Run:

```bash
uv run pytest \
  tests/test_workspace_automations_config.py \
  tests/test_workspace_automations_workspace.py \
  tests/test_workspace_automations_loader.py \
  tests/test_workspace_automations_targets.py \
  tests/test_workspace_automations_executor.py \
  tests/test_workspace_automations_triggers.py \
  tests/test_workspace_automations_actions.py \
  tests/test_workspace_automations_service.py \
  tests/test_workspace_automation_tool.py \
  tests/test_workspace_automations_worker_routing.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run scheduling and worker regression tests**

Run:

```bash
uv run pytest \
  tests/test_scheduling.py \
  tests/test_scheduling_executor.py \
  tests/test_scheduler_tool.py \
  tests/test_worker_runtime.py \
  tests/test_worker_lifecycle.py \
  tests/test_kubernetes_worker_backend.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run broader pytest before claiming completion**

Run: `uv run pytest`.
Expected: PASS.

- [ ] **Step 4: Run pre-commit before merge**

Run: `uv run pre-commit run --all-files`.
Expected: PASS.

- [ ] **Step 5: Commit any final fixes**

Use targeted adds only.
Never run `git add .`.

## Follow-Up Work Not In This PR

Private workspace automations need a durable execution identity registry.
That registry must be written when private workspaces are materialized and must survive restart.

Kubernetes CronJob provider support is not in scope.
It can be considered later only for firing automations while the primary MindRoom runtime is down.

Additional check types are not in scope.
Future check types may include HTTP, Python, Matrix state query, and typed tool calls.

Dashboard management is not in scope.
The initial UI is the file plus the optional agent-facing validation tool.
