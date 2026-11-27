# Durable Zero-LLM Scheduled `script_check` Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with `gemini4_neon@xhigh` (`minimal: true`) following the commit boundaries and verification steps below.

**Execution:** Subagent-driven (`gemini4_neon@xhigh`, `minimal: true`) across 5 logical commits on branch `feat/script-check-triggers`, followed by full-diff review from Opus and Astra — chosen so each layer (`config+models+store`, `script_runs+gateway`, `orchestration+poller`, `triggers+tools`, `docs+schemas+eval`) is independently verified with tests before wiring the next.

**Goal:** Bridge `src/mindroom/script_runs/` into `src/mindroom/reactive_triggers/` so agents can register durable, zero-LLM-while-idle scheduled Python `check()` triggers via `triggers(action="watch_script", ...)`.

**Architecture:** `reactive_triggers` owns the rule metadata (`ScriptCheckSourceSpec`), private checkpoint (`script_check_checkpoints`: snapshotted `source_utf8`, serialized `identity_json`, persisted `state_json`, `revision`, schedule/backoff), and bounded `ScriptCheckPoller`. `script_runs` owns the short-lived `entry="script"` worker/local process, non-interactive fail-closed tool approval, gateway `/check-input` & `/check-result` routes, and immediate resource + SQLite pruning. `orchestration/script_checks.py` bridges the two domains via an injected tick-runner callable so `tach.toml` stays strictly acyclic.

**Tech Stack:** Python 3.12+, Pydantic v2, SQLite WAL, FastAPI, stdlib `MindRoomTools` + `ScriptGatewayTransport`, pytest/pytest-asyncio, ruff/ty/mypy/tach/vulture/privata.

**Spec:** `docs/baspowers/specs/2026-11-26-script-check-triggers-design.md`

## Global Constraints

- `SCRIPT_PROTOCOL_VERSION` stays `2`; `script_runs/{wire,shim,worker_client,supervised,transport}.py` and `script_sdk.py` remain wire-compatible with zero protocol bumps and no 4th SDK.
- `triggers` tool metadata stays `executes_code=False`; `watch_script` and source-bearing `update` require the calling agent to have `"triggers"` plus `"script"` or `"workflow"`, pass `workflow_placement`, and carry `ApprovalAlias(judged_as="start_script", ...)`.
- Background check ticks **never** post Matrix approval cards or consult transient `!auto_approve` windows (`human_gate`); both `ToolGate.CONFIRM` and policy `require_approval` fail closed immediately.
- Bounds: source `<= 64 KiB`, persisted state `<= 32 KiB`, check result `<= 64 KiB`, HTTP `/check-result` stream `<= 128 KiB`, default tick timeout `30 s` (`5..120`), default poll interval `300 s`, default floor `60 s` (`30..3600`), default quota `10` active per owner.
- Only **2 new top-level parameters** on `TriggersTools.triggers(...)`: `source` and `path`.
- Soft budget: **~700–900 net prod LOC**, **~750–950 test LOC** (~1:1 ratio, real flows and invariants).
- Every commit ends with `--trailer "Assisted-by: MindRoom <display name>:<model id> <https://github.com/mindroom-ai/mindroom>"`.

## Review Focus

1. **Approval denial swallowed in user Python:** If user code catches `MindRoomToolCallError` after a non-interactive approval denial and returns a dict or mutates `state`, the broker tracks `approval_denied` on the tick slot so the poller treats the tick as failed and refuses to commit state or fire.
2. **Stale in-flight tick vs concurrent `update`/`set_enabled`/`delete`:** A tick that finishes after an in-place `source` edit or disable/re-enable must not overwrite newer state or enqueue events (guarded by comparing both `rule_uid` and `revision` inside the SQLite write transaction).
3. **v1 and v2 database upgrades:** Existing `reactive_triggers.db` files at schema v1 or v2 must upgrade cleanly to v3 without losing existing triggers, cursors, or `github_pr_checkpoints`.
4. **Module-level `tool()`/`state` calls & non-finite JSON:** Calling `tool()`/`state.set()` at import time (outside `check()`), returning `NaN`/`Infinity`, or returning non-`None`/non-`dict` values (`False`, `[]`, `"hi"`) must fail cleanly without mutating persisted state.
5. **Shutdown & reload races:** Calling `stop_script_checks()` before `script_runtime.shutdown()` cancels in-flight ticks without spurious backoff errors, and hot-reloading config to disable `script_check_enabled` or remove `script`/`workflow`/`triggers` revokes active tick runs immediately.

---

### Task 1: Policy Config, Reactive Models, SQLite v3 Store & Authoring Validation

**Files:**
- Modify: `src/mindroom/config/external_trigger_policy.py`
- Modify: `src/mindroom/reactive_triggers/models.py`
- Modify: `src/mindroom/reactive_triggers/store.py`
- Modify: `src/mindroom/reactive_triggers/management.py`
- Modify: `src/mindroom/reactive_triggers/__init__.py`
- Create: `src/mindroom/reactive_triggers/script_check.py` (validation, normalization, access check, `ScriptCheckPoller`)
- Modify: `tach.toml`
- Test: `tests/test_script_check_triggers.py`

**Interfaces:**
- Produces in `config/external_trigger_policy.py`: `ExternalTriggerPolicyConfig.{script_check_enabled, min_script_check_poll_interval_seconds, max_script_checks_per_owner, script_check_timeout_seconds, script_check_allowed_tools}`.
- Produces in `reactive_triggers/models.py`: `"script_check"` in `SourceType`, `ScriptCheckSourceSpec`, `ScriptCheckCheckpoint`.
- Produces in `reactive_triggers/store.py`: schema v3 `script_check_checkpoints` table + migrations from v1/v2; `get_script_check_checkpoint`, `upsert_script_check_checkpoint`, `list_due_script_check_triggers`, `update_script_check_checkpoint_and_enqueue`; quota checks in `create_trigger`, `update_trigger`, `set_enabled`.
- Produces in `reactive_triggers/script_check.py`: `validate_script_check_source(source_bytes: bytes) -> tuple[str, str]`, `normalize_script_check_event(...) -> TriggerEvent | None`, `check_script_check_source_access(...) -> str | None`, `ScriptCheckTickOutcome`, `ScriptCheckTickRunner` protocol, `ScriptCheckPoller`.

- [ ] **Step 1:** Write unit tests in `tests/test_script_check_triggers.py` covering policy validation, AST validation (`def check():`, rejects `async def`, wrong arity, syntax error, >64 KiB), event normalization (`None`, plain dict, envelope keys, collision rejection, dedupe key hashing, >64 KiB rejection), SQLite v1/v2->v3 migration, atomic checkpoint+enqueue revision guard, and quota enforcement on create/enable/expiry-extension.
- [ ] **Step 2:** Implement policy fields, models, store v3 schema/methods, management re-enable refresh (preserving `state`, resetting error counters, incrementing `revision`), and `reactive_triggers/script_check.py`.
- [ ] **Step 3:** Run `uv run pytest tests/test_script_check_triggers.py tests/test_reactive_triggers_store.py tests/test_external_trigger_policy_config.py -x -n 0 --no-cov` and `tach check --dependencies --interfaces`.
- [ ] **Step 4:** Commit (`feat(triggers): add script_check models, policy, SQLite v3 store, and poller`).

---

### Task 2: `script_runs` Non-Interactive Check Mode & Gateway Check Routes

**Files:**
- Modify: `src/mindroom/script_runs/models.py`
- Modify: `src/mindroom/script_runs/legacy_schema.py`
- Modify: `src/mindroom/script_runs/store.py`
- Modify: `src/mindroom/script_runs/policy.py`
- Modify: `src/mindroom/script_runs/broker.py`
- Modify: `src/mindroom/script_runs/manager.py`
- Modify: `src/mindroom/api/script_gateway.py`
- Modify: `tach.toml`
- Test: `tests/test_script_check_triggers.py`

**Interfaces:**
- Consumes: Task 1 policy field `script_check_allowed_tools`.
- Produces in `script_runs/models.py`: `ScriptRunRecord.script_check_rule_uid`, `.is_script_check`, `.is_plain_script`, updated `.launch_allows`.
- Produces in `script_runs/policy.py`: `_SCRIPT_CHECK_RESTRICTED_TOOLKITS`, `resolve_current_script_tool(..., mode: Literal["script", "workflow", "script_check"] = "script")`.
- Produces in `script_runs/broker.py`: `ScriptCheckTickSlot`, `broker.check_tick(run_id, *, source, source_digest, initial_state)`, `broker.get_authenticated_check_input(run_id, authorization)`, `broker.submit_authenticated_check_result(run_id, authorization, ...)`; non-interactive fail-closed handling on both `ToolGate.CONFIRM` and policy approval (recording `approval_denied` on slot); skipping `settle_run_approvals`.
- Produces in `script_runs/manager.py`: optional `run_id`, `script_check_rule_uid`, `max_runtime_seconds` on `ScriptRunManager.run(...)`; hiding `script_check` from `list()`, `_owned_run()`, and plain `max_concurrent_runs`.
- Produces in `api/script_gateway.py`: `GET /api/script-gateway/runs/{run_id}/check-input` and `POST /api/script-gateway/runs/{run_id}/check-result`.

- [ ] **Step 1:** Add tests covering `script_runs` migration for `script_check_rule_uid`, restricted toolkits + optional `script_check_allowed_tools`, non-interactive fail-closed on both `ToolGate.CONFIRM` and `require_approval` (asserting 0 approval cards created and `human_gate` never consulted), gateway `/check-input` and `/check-result` auth/bounds/conflict/non-finite rejection, and hiding ticks from `list_scripts`/`get_script`/`cancel_script`/plain concurrency limits.
- [ ] **Step 2:** Implement `script_runs/` and `api/script_gateway.py` changes.
- [ ] **Step 3:** Run `uv run pytest tests/test_script_check_triggers.py tests/test_script_run_store.py tests/test_script_run_manager.py tests/test_script_tool_broker.py tests/api/test_script_gateway.py -x -n 0 --no-cov`.
- [ ] **Step 4:** Commit (`feat(script_runs): support non-interactive script_check ticks and gateway routes`).

---

### Task 3: Orchestration Bridge, Bootstrap, Service & Lifecycle Integration

**Files:**
- Create: `src/mindroom/orchestration/script_checks.py` (fixed stdlib bootstrap source, headless context reconstruction, `execute_script_check_tick` launch/wait/retire/prune)
- Modify: `src/mindroom/orchestration/script_runtime.py` (immediate pruning of terminal `script_check` rows without `prune_approvals`; config-reload revocation)
- Modify: `src/mindroom/reactive_triggers/dispatch.py` (`ScriptCheckSourceSpec` branch in `check_reactive_source_access`)
- Modify: `src/mindroom/reactive_triggers/service.py` (wire `ScriptCheckPoller` cadence task, `stop_script_checks()`, `poll_script_checks_once()`, `notify_work(poll_script_checks=...)`)
- Modify: `src/mindroom/orchestrator.py` (bind `execute_script_check_tick` into `ReactiveTriggerService`; call `await self.reactive_triggers.stop_script_checks()` before `self._script_runtime.shutdown()`)
- Modify: `tach.toml`
- Test: `tests/test_script_check_triggers.py`

**Interfaces:**
- Consumes: Task 1 `ScriptCheckPoller` / `ScriptCheckTickOutcome`, Task 2 `ScriptRunManager` / `ScriptToolBroker.check_tick`.
- Produces in `orchestration/script_checks.py`: `build_script_check_tick_runner(...) -> ScriptCheckTickRunner`, `SCRIPT_CHECK_BOOTSTRAP_SOURCE`.
- Produces in `reactive_triggers/service.py`: `ReactiveTriggerService.set_script_check_runner(...)`, `poll_script_checks_once()`, `stop_script_checks()`.

- [ ] **Step 1:** Add end-to-end tests exercising the real bootstrap script inside a real local subprocess against a live FastAPI gateway: `None` commits state with 0 LLM calls, fresh `tool()` read across ticks (no cache), returning dict flows through triage/claim/dispatch with `reopen_on_fire`, swallowed approval denial fails tick without committing state, `agent()` blocked, module-level `tool()` rejected, timeout kills process and prunes `script_runs` row, stale revision rejected on commit, and shutdown cancels active tick cleanly.
- [ ] **Step 2:** Implement `orchestration/script_checks.py`, `script_runtime.py`, `dispatch.py`, `service.py`, `orchestrator.py`, and `tach.toml`.
- [ ] **Step 3:** Run `uv run pytest tests/test_script_check_triggers.py tests/test_reactive_triggers_service.py tests/test_reactive_triggers_pipeline.py -x -n 0 --no-cov` and `tach check --dependencies --interfaces`.
- [ ] **Step 4:** Commit (`feat(triggers): wire script_check execution bridge, bootstrap, and lifecycle`).

---

### Task 4: `triggers` Tool Surface (`watch_script`, `update`, `list`, `history`, `test`) & ApprovalAlias

**Files:**
- Modify: `src/mindroom/tools/triggers.py` (register `ApprovalAlias(judged_as="start_script", ...)`, update description)
- Modify: `src/mindroom/custom_tools/triggers.py` (add `source`, `path` params; `watch_script` action; `update` support for `source`/`path`/`poll_interval_seconds`; capability/placement/turn-ceiling checks; `set_enabled` capability check; `list` owner/admin redaction; `history` access check; synthetic `test` dry-run)
- Modify: `src/mindroom/api/room_triggers.py` (re-enable live capability check on `PATCH` for `script_check` if needed; verify redaction)
- Test: `tests/test_script_check_triggers.py`, `tests/test_triggers_tool.py`, `tests/api/test_room_triggers.py`

**Interfaces:**
- Consumes: Tasks 1–3 (`ScriptCheckSourceSpec`, `ScriptCheckCheckpoint`, `validate_script_check_source`, `check_script_check_source_access`, `workflow_placement`, `ScriptRunManager.resolve_source`).
- Produces: `triggers(action="watch_script", source=..., path=..., prompt=..., poll_interval_seconds=...)` and `ApprovalAlias` on `triggers`.

- [ ] **Step 1:** Add tests for `watch_script` creation (inline `source` & workspace `path`), triggers-only agent refusal, narrowed/filtered turn refusal, `ApprovalAlias` judging `watch_script` and code-bearing `update` as `start_script` while returning `[]` for `watch_pr`/`watch_room`/`list`, `update` resetting state & incrementing revision, `list` and `/api/rooms/{room_id}/triggers` never leaking `source_utf8`/`identity_json`/`state`, `history` access check, and synthetic `test` dry-run.
- [ ] **Step 2:** Implement `tools/triggers.py`, `custom_tools/triggers.py`, and `api/room_triggers.py`.
- [ ] **Step 3:** Run `uv run pytest tests/test_script_check_triggers.py tests/test_triggers_tool.py tests/api/test_room_triggers.py tests/test_tool_approval_aliases.py -x -n 0 --no-cov`.
- [ ] **Step 4:** Commit (`feat(triggers): expose watch_script action and start_script approval alias`).

---

### Task 5: Docs, Generated Metadata/Schemas, Full Static Gates & Weak-Model Schema A/B

**Files:**
- Modify: `docs/architecture/security-posture.md`, `docs/architecture/code-map.md`, `docs/external-triggers.md` (if present)
- Regenerate: `src/mindroom/tools_metadata.json`, `frontend/src/test/fixtures/config-schema.json`, `tests/fixtures/agno_parity/tools/schemas.json`, `skills/mindroom-docs/references/`
- Verify: `ruff`, `ty`, `mypy`, `tach`, `vulture`, `privata --methods .`, pytest with default addopts, and weak-model A/B check (`haiku`, `gemini_flash`, `deepseek_v4_1_flash_1017`, `gpt5mini`) on the updated `triggers` schema.

- [ ] **Step 1:** Update docs (`security-posture.md`, `code-map.md`, external-triggers docs) and regenerate `tools_metadata.json`, `config-schema.json`, `schemas.json`, and `skills/mindroom-docs/references/`.
- [ ] **Step 2:** Run all static checks (`ruff check`, `ruff format --check`, `ty`, `mypy`, `tach check --dependencies --interfaces`, `vulture`, `privata --methods .`) and pytest suite (including default addopts on new test file + schema/metadata/boundary tests).
- [ ] **Step 3:** Commit (`docs(triggers): document script_check and regenerate schemas/metadata`).
- [ ] **Step 4:** Run weak-model A/B evaluation against updated `triggers` schema and proceed to Opus + Astra full-diff review.
