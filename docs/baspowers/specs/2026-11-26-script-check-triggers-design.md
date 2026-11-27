# Durable Zero-LLM Scheduled `script_check` Triggers

Date: 2026-11-26. Status: designed and vetted with Gemini 4 Neon (`gemini4_neon@xhigh`) and GPT-6.2 Astra (`astra`); security-policy defaults confirmed by Bas (`workflow_placement` + live `tool_approval` fail-closed + optional `script_check_allowed_tools`).

Tracking issue: https://github.com/mindroom-ai/mindroom/issues/4422

## Intent

Provide durable, restart-surviving, zero-LLM-while-idle scheduled Python/tool check triggers by bridging `src/mindroom/script_runs/` into `src/mindroom/reactive_triggers/`.

An agent registers a short Python `check()` function (inline `source` or workspace `path`, snapshotted once) on a poll interval. On each due tick, MindRoom runs a fresh short-lived `script_runs/` process that calls any permitted owner-scoped tool via `MindRoomTools` (`src/mindroom/script_sdk.py`) with persisted bounded JSON `state` across ticks:

```python
import json

def check():
    page = json.loads(tool("gmail", "gmail_read", action="search_emails", query="from:alice is:unread"))
    msgs = page["emails"]
    seen = set(state.get("seen_ids", []))
    new = [m["id"] for m in msgs if m["id"] not in seen]
    state.set("seen_ids", [m["id"] for m in msgs])
    return {"items": new} if new else None  # None = 0 LLM tokens
```

When `check()` returns `None`, updated `state` persists and zero LLM calls happen. When `check()` returns a JSON object, it feeds directly into the existing `reactive_triggers` pipeline — inheriting `reopen_on_fire`, optional cheap triage (`llm`/`typesafe`/`openai_decisions`), dedupe, rate caps, cooldowns, expiry, `once`, firing history, dry-run `test`, and Room Triggers UI (`/api/rooms/{room_id}/triggers`).

## Explicit Non-Goals (v1)

- Do **not** retire standalone `script` (`Background Scripts`) in this PR.
- Do **not** add a 4th client SDK or bump `SCRIPT_PROTOCOL_VERSION` (stays `2`; zero worker-image changes).
- Do **not** allow `agent()` inside `check()` ticks (LLM reasoning belongs in `triggers` triage/wake).
- Do **not** support warm reusable worker pools, cron expressions, multi-event list fan-out, per-trigger `timeout_seconds` / `allowed_tools` / `initial_state` tool parameters, or source-code editors in Room Triggers UI.

---

## Architecture

### 1. Security, Capability & Approval Model

1. **`triggers` stays `executes_code=False`:** Flipping `executes_code=True` on `triggers` would escalate `triggers`-only monitoring agents and expose workspace write permissions in `room_agent_access.py`.
2. **Creation authority (`watch_script`; `update` is metadata-only):**
   - Require `external_trigger_policy.enabled`, `reactive_enabled`, and `script_check_enabled`.
   - Require calling `context.agent_name` in `config.agents` with `"triggers"` **and** at least one of `"script"` or `"workflow"` in its resolved tools (future-proof if standalone `script` is later retired).
   - Require a direct-agent Matrix turn (`identity.recipient == context.agent_name`, refusing team-transport authoring while still permitting `target_agent` to target a team), and reject narrowed/filtered turns (`context.tool_function_filter is not None`, persona tool restrictions excluding both `script` and `workflow`, detached/background-script turns, event-source watch turns, or unattended automation turns including router-relayed scheduled turns tracked via `com.mindroom.automation_origin`).
   - Check placement via `workflow_placement(config, runtime_paths)` (`worker` when dedicated backend/proxy configured; `local` only when `defaults.allow_shell_without_worker: true` or explicit local execution mode — never silent fallback from a broken worker backend to the host) and verify `script_runtime.tool_manager is not None` **before** reading `path` or saving.
   - Register `ApprovalAlias(judged_as="start_script", judge=_judge_triggers_script_check, allow_non_resumable_passthrough=True)` on `src/mindroom/tools/triggers.py` for function `"triggers"`:
     - `action == "watch_script"` -> `[{"source": args.get("source"), "path": args.get("path"), "name": args.get("trigger_id") or args.get("description")}]`
     - Missing/non-string/unknown `action` -> `None` (fail closed)
     - All other known actions (`watch_room`, `watch_thread`, `watch_pr`, `subscribe_event`, `create`, `list`, `update`, `set_enabled`, `delete`, `rotate_key`, `history`, `test`) -> `[]` (no `start_script` approval prompt, and allowed on non-resumable surfaces and voice calls via shared `non_resumable_alias_refusal_message`).
3. **Live tick authorization & dispatch source access:**
   - Recheck on every tick and before triage/dispatch: policy toggles enabled, owner is human, `source.agent` in `config.agents` with `"triggers"` and (`"script"` or `"workflow"`), owner authorized for `source.agent` in `source.room_id` (`is_sender_allowed_for_agent_reply_in_room(..., require_resolved_membership=True)`), and `workflow_placement` still permits execution.
   - `source.agent` and `source.room_id` (and captured `execution_identity`) are immutable; `target.agent` only determines who wakes up, never whose credentials run `check()`.
4. **Non-interactive fail-closed tool approval inside ticks (no approval spam):**
   - `ScriptToolBroker` evaluates live `context.current_config` (`tool_approval` default & rules) for `script_check` runs (matching `workflow`), so read tools like `gmail_read`, Google Tasks, Calendar, and MCP reads work out of the box under default `auto_approve`, while deployments with `default: require_approval` or explicit `require_approval` rules enforce operator policy.
   - In **both** `_request_authored_confirmation` (`function.gate is ToolGate.CONFIRM`) **and** `_build_background_approval_gate` (`policy_requires_approval=True`), a `script_check` run **immediately returns `ToolApprovalDecision(approved=False, reason=...)`** without calling `_request_approval`, `human_gate` (ignoring transient `!auto_approve` chat windows), or creating any approval card.
   - `broker.cancel_run` skips `settle_run_approvals` and `script_runtime` skips `prune_approvals` for `script_check` runs.
5. **Toolkit restrictions inside ticks (`src/mindroom/script_runs/policy.py`):**
   - `_SCRIPT_CHECK_RESTRICTED_TOOLKITS = _SCRIPT_RESTRICTED_TOOLKITS | {"browser_mcp", "callback_manager", "config_manager", "desktop", "oauth_connections", "scheduler", "shared_directories", "terminal", "triggers"}` (already inherits `browser`, `script`, `workflow`, `delegate`, `claude_agent`, `slack`, `agent_manager`, `compact_context`, `contacts`, `dynamic_tools`, `memory`, `self_config`, `skill_manage`).
   - If `config.external_trigger_policy.script_check_allowed_tools` is set (non-`None`), additionally restrict ticks to those toolkit names only.

---

### 2. Configuration Policy (`src/mindroom/config/external_trigger_policy.py`)

Add to `ExternalTriggerPolicyConfig`:
- `script_check_enabled: bool = Field(default=True, description="Enable scheduled script_check triggers")`
- `min_script_check_poll_interval_seconds: int = Field(default=60, ge=30, le=3600, description="Min script_check poll interval (s)")`
- `max_script_checks_per_owner: int = Field(default=10, ge=1, le=200, description="Max active script_check triggers/owner")`
- `script_check_timeout_seconds: int = Field(default=30, ge=5, le=120, description="Max runtime per script_check tick (s)")`
- `script_check_allowed_tools: list[str] | None = Field(default=None, description="Optional toolkit allowlist for script_check ticks")` (validated non-empty stripped names when set).

---

### 3. Tick Execution, Bootstrap & Gateway Transport

1. **Run discriminator on `script_runs`:**
   - Add nullable `script_check_rule_uid: str | None = None` to `ScriptRunRecord` (`script_runs/models.py`), `script_runs` table (`script_runs/store.py`), and additive migration in `script_runs/legacy_schema.py` (mutually exclusive with `workflow_run_id`).
   - Properties: `is_script_check` (`script_check_rule_uid is not None`), `is_plain_script` (`workflow_run_id is None and script_check_rule_uid is None`), `launch_allows(grant)` (`(not self.is_plain_script) or grant in self.grants`).
   - Keep wire `entry = "script"` (no `wire.py`, `shim.py`, or `worker_client.py` protocol changes; `SCRIPT_PROTOCOL_VERSION = 2` untouched).
   - `ScriptRunManager.run(...)` accepts optional `run_id`, `script_check_rule_uid`, and `max_runtime_seconds`:
     - Uses `workflow_placement`, skips eager `launch_grants`, sets `recovery_signature=None` (never adopted across restarts), and excludes `script_check` runs from `list()`, `_owned_run()`, and plain `max_concurrent_runs`.
2. **Pre-allocated gateway mailbox (`ScriptToolBroker` + `api/script_gateway.py`):**
   - `broker.check_tick(run_id, *, source: str, source_digest: str, initial_state: dict[str, object])` context manager registers an in-memory `_CheckTickSlot` **before** `manager.run(...)` spawns the process (eliminating startup races) and signals an `asyncio.Event` when the result arrives.
   - Two authenticated routes on `src/mindroom/api/script_gateway.py:router` (automatically available on both the primary API and isolated `MINDROOM_SCRIPT_GATEWAY_PORT` listener):
     - `GET /api/script-gateway/runs/{run_id}/check-input` -> returns `{"source": ..., "source_digest": ..., "state": ...}`.
     - `POST /api/script-gateway/runs/{run_id}/check-result` -> stream-bounded at `128 KiB`, strict Pydantic `ScriptCheckResultRequest` (`extra="forbid"`, `has_only_finite_numbers`, fields: `result: JsonValue = None`, `state: dict[str, JsonValue] = {}`, `state_changed: bool = False`, `error: str | None = None` max 8192 chars). Identical duplicate POST returns 200; conflicting second POST returns 409.
   - Both routes authenticate with `broker.authenticate(run_id, authorization)`, require `run.is_script_check`, and require an active slot for `run_id` (else `404`). Ordinary script and workflow tokens are rejected.
3. **Fixed stdlib bootstrap (`SCRIPT_CHECK_BOOTSTRAP_SOURCE` in `src/mindroom/reactive_triggers/script_check.py`):**
   - Snapshotted as `source.py` on launch (small fixed stdlib module importing only `MindRoomTools` from `mindroom.script_sdk` and `ScriptGatewayTransport` from `mindroom.script_runs.transport` — no 4th SDK).
   - Fetches `GET runs/{run_id}/check-input`, verifies SHA-256 of `source` against `source_digest`.
   - Builds `_CheckState` (`get`, `set`, `delete`, `pop`, `clear`, `keys` with strict finite JSON validation and 32 KiB UTF-8 byte cap), `tool = MindRoomTools(http_timeout_seconds=10.0, poll_interval_seconds=0.1).call` (refusing calls during module load outside `check()`), and stub `agent(*a, **kw)` raising `RuntimeError`.
   - Executes `source` with `exec(compile(source, "<script_check>", "exec"), namespace)`, invokes `check()`, and POSTs `{result, state, state_changed, error}` to `POST runs/{run_id}/check-result` before exiting.
4. **Headless context reconstruction & ephemeral cleanup:**
   - Capture `serialize_tool_execution_identity(context.execution_identity)` in `script_check_checkpoints.identity_json` at creation.
   - On each tick, rebuild `ToolRuntimeContext` via the executing agent's live bot (`MessageTarget` from stored `identity_json`, fresh `correlation_id=f"script-check:{trigger_id}:{run_id}"`), matching `_LiveScriptRuntimeResolver.resolve` byte-for-byte.
   - Separate launch timeout budget (`_LAUNCH_TIMEOUT_SECONDS = 60s` in a tracked `asyncio.Task`) from script execution timeout (`script_check_timeout_seconds` started after launch). On launch timeout, revoke + `broker.cancel_run` + cancel task, hand cleanup/retirement to a tracked background task set (drained on `stop()`), and return `launch_timeout` immediately so per-owner and global poller slots release on time.
   - After `POST /check-result`, wait briefly (up to 2 s) for process exit via `ScriptRunManager.observe_process_exit(run_id)` (which records process exit durably without blocking on worker retirement), commit the result & state whenever `exit_code == 0`, `error is None`, and no approval denial occurred, and hand worker retirement (`reconcile_durable`) + `prune_terminal_run` to the tracked background cleanup set.
   - `ScriptRuntimeLifecycle._prune_pass` also prunes orphaned terminal `script_check` rows immediately without waiting 30 days, and `_apply_update` revokes active `script_check` runs if policy disables them or removes required tools from the agent.

---

### 4. Authoring Contract & Event Normalization (`src/mindroom/reactive_triggers/script_check.py`)

- **Source validation (synchronous on create):**
  - Exactly one of `source` or `path` (`path` resolved via `ScriptRunManager.resolve_source`, snapshotted once into `source_utf8`, never re-read on ticks).
  - UTF-8, non-empty, `<= 64 KiB`.
  - `ast.parse` checks valid syntax and a top-level synchronous `def check():` with 0 required/positional args (rejecting `async def check` and wrong arity with clear messages).
- **Return contract:**
  - `None` -> quiet tick (0 LLM tokens); **still commits modified `state`**.
  - `dict` -> 1 event (strict finite JSON, serialized payload `<= 64 KiB`).
    - Optional envelope keys: `title` (`str`, `<= 500`), `summary` (`str`, `<= 2000`), `text` (`str`, `<= 8000`), `actor` (`str`, `<= 256`), `dedupe_key` (`str`, `<= 256`), `data` (`dict`).
    - Plain dicts (e.g. `{"items": [...]}`) become `event.data`. Non-envelope keys are collected into `event.data`; if explicit `data` is combined with extra non-envelope keys, reject cleanly to avoid dropping fields.
    - Fixed `kind="script_check.match"` so untrusted user values cannot appear outside `<untrusted_event_data>` in `dispatch.py`.
    - Dedupe ID: if `dedupe_key` is supplied, `event_id = f"sc:key:{sha256(f'{trigger_id}:{dedupe_key}')[:32]}"`; otherwise `event_id = f"sc:tick:{run_id}"`.
  - Any other return type (`bool`, `int`, `str`, `list`) -> tick error (state not committed, backoff applied).
- **State bounds & atomicity:** `state` must be a `dict` of strict finite JSON `<= 32 KiB` UTF-8 bytes. Committed on clean return (`None` or valid `dict`); **never** committed on exception, timeout, approval denial, invalid result, or stale revision.

---

### 5. Data Models, Store & Poller

- **Models (`src/mindroom/reactive_triggers/models.py`):**
  - `SourceType = Literal["room_message", "thread_tag", "external_event", "github_pr", "script_check"]`
  - `ScriptCheckSourceSpec`: `source_type: Literal["script_check"] = "script_check"`, `agent: str`, `room_id: str`, `source_digest: str`, `poll_interval_seconds: StrictInt = Field(default=300, ge=30, le=86400)`. Keeps raw code, identity, and state out of `spec_json`, `list`, and Room Triggers API.
  - `ScriptCheckCheckpoint`: `revision: int = 1`, `source_utf8: str`, `identity_json: dict[str, Any]`, `state: dict[str, Any] = {}`, `next_poll_at: float`, `consecutive_errors: int = 0`, `last_error_code: str | None = None`, `last_run_at: float | None = None`, `updated_at: float`.
- **SQLite (`src/mindroom/reactive_triggers/store.py`):**
  - Schema version `4` with idempotent additive migration from `v1`, `v2`, and `v3` (preserving `applied_migrations` from #4423).
  - `script_check_checkpoints` table (`trigger_id PK FK ON DELETE CASCADE`, `revision`, `source_utf8`, `identity_json`, `state_json`, `next_poll_at`, `consecutive_errors`, `last_error_code`, `last_run_at`, `updated_at`) + `idx_script_check_checkpoints_due(next_poll_at)`.
  - `_source_index_fields` returns `(None, source.agent)` so `source_room_id` stays reserved for Matrix-room-watching rules.
  - Atomic `_check_script_check_quota` enforced on `create_trigger`, `update_trigger` (re-enable or expiry extension), and `set_enabled(..., enabled=True)`.
  - `update_script_check_checkpoint_and_enqueue(trigger_id, checkpoint, *, event=None, expected_rule_uid=..., expected_revision=...)` checks `rule_uid`, `revision`, `enabled=1`, and unexpired status, increments `revision`, upserts checkpoint, and inserts the pending firing inside one `BEGIN IMMEDIATE` transaction.
- **Poller & Lifecycle (`reactive_triggers/script_check.py`, `reactive_triggers/service.py`, `orchestrator.py`):**
  - `ScriptCheckPoller` runs on a 15s cadence task inside `ReactiveTriggerService._run` (plus `notify_work(poll_script_checks=True)`), with global `asyncio.Semaphore(4)`, per-owner concurrency 1, and per-trigger in-flight guard.
  - `ScriptCheckPoller` calls an injected `tick_runner` callable provided by `orchestration/script_checks.py` (keeping `reactive_triggers/` decoupled from `script_runs/` in `tach.toml`).
  - Backoff on error: `min(3600.0, effective_interval * (2 ** min(errors, 5)))` with `state` preserved; transient runtime/bot/membership unavailability defers 30 s without incrementing `consecutive_errors`.
  - `orchestrator.py` calls `await self.reactive_triggers.stop_script_checks()` **before** `self._script_runtime.shutdown()`.

---

### 6. `triggers(...)` Surface & API Redaction

- **2 new parameters on `TriggersTools.triggers(...)`:** `source: str | None = None`, `path: str | None = None`.
- **`action="watch_script"`:** requires `prompt` and exactly one of `source` or `path`; accepts `_COMMON_REACTIVE_PARAMS | _TRIAGE_PARAMS | {"source", "path", "poll_interval_seconds"}`; defaults `poll_interval_seconds=300` (clamped to `[policy.min_script_check_poll_interval_seconds, 86400]`), `once=False`; creates rule + checkpoint with `next_poll_at=now` and wakes poller.
- **`action="update"`:** metadata-only for `script_check` (`poll_interval_seconds`, `prompt`, `description`, `triage_*`, caps, `enabled`; rejects `source`/`path`). Updating `poll_interval_seconds` or re-enabling atomically bumps `checkpoint.revision` inside `store.update_trigger` so in-flight ticks cannot overwrite `next_poll_at`.
- **`action="set_enabled"` & Room Triggers `PATCH`:** enabling preserves `state`, resets error counters, increments `revision`, sets `next_poll_at=now`, enforces quota, and verifies live capability before activating.
- **`action="test"`:** fast synthetic dry-run using `sample_data`/`text`/`actor`/`kind` through optional triage (never executes Python or calls tools).
- **`action="list"` & `/api/rooms/{room_id}/triggers`:** metadata only (`source_digest`, `agent`, `poll_interval_seconds`, plus owner-only `consecutive_errors`/`last_error_code`/`last_run_at`); never exposes `source_utf8`, `identity_json`, or `state`. `api/room_triggers.py` automatically includes `kind="script_check"` via `SourceType`.
- **`action="history"`:** explicit `ScriptCheckSourceSpec` branch in `_has_source_access_for_history` and `_firing_allowed_for_history`.

---

## Soft Size Budget

- Target: **~700–900 net production LOC** across focused modules (`reactive_triggers/script_check.py`, `orchestration/script_checks.py`, and targeted additions in `script_runs/`, `reactive_triggers/`, `api/script_gateway.py`, `custom_tools/triggers.py`, `tools/triggers.py`, `config/external_trigger_policy.py`), plus **~750–950 test LOC** (~1:1 test:prod covering real user flows and security invariants).
