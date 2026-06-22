# Sync Restart Shutdown Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace scattered restart-cancellation string plumbing with one typed shutdown intent and one typed stale-cleanup scan policy.

**Architecture:** Runtime shutdown code decides intent once at the lifecycle boundary, then lower layers consume that intent without knowing whether an entity is being restarted, removed, or stopped. Matrix stale-stream cleanup gets a local scan policy object so old terminal interrupted resume behavior is separated from active stale stream editing. Non-streaming visible tool trace ordering stays a tiny response-runner integration point and does not drive the shutdown architecture.

**Tech Stack:** Python 3.13, asyncio task cancellation, dataclasses, Matrix/Nio, pytest, pre-commit, existing MindRoom runtime modules.

---

## Why Current PR Is Growing

The PR started small because the desired behavior is small: when a Matrix sync loop restarts, visible interrupted responses should be marked as sync-restart interruptions and retried once.

Complexity grew because the current patch passes raw `cancel_msg` strings through many unrelated layers:

- `src/mindroom/orchestration/runtime.py` decides restart vs removal in `stop_entities()`.
- `src/mindroom/bot.py` forwards `cancel_msg` through `stop()` and `prepare_for_sync_shutdown()`.
- `src/mindroom/background_tasks.py` forwards `cancel_msg` while draining owned background tasks.
- `src/mindroom/response_runner.py` forwards `cancel_msg` while draining detached inbox responses.
- `src/mindroom/cancellation.py` classifies raw task-cancel messages after the fact.
- `src/mindroom/matrix/stale_stream_cleanup.py` handles a different problem: startup cleanup and optional old terminal interrupted resume scan.

That is architectural smell: restart policy is encoded as a string and threaded through APIs whose real job is lifecycle shutdown or task draining.

Target design: make restart semantics first-class at the top, pass typed intent down, and keep stale Matrix history scan policy local to stale cleanup.

## File Structure

- Create: `src/mindroom/runtime_shutdown.py`
  - Own high-level shutdown intent.
  - Provide canonical intents for sync restart, entity removal, and generic stop.
  - Map intent to task-cancel source and lifecycle stop reason.

- Modify: `src/mindroom/cancellation.py`
  - Keep low-level task cancellation helpers.
  - Rename public cancellation input from raw `cancel_msg` to typed `cancel_source`.
  - Keep task message constants private to this module, except where tests need canonical values.

- Modify: `src/mindroom/orchestration/runtime.py`
  - Compute `RuntimeShutdownIntent` once per entity or sync-loop iteration.
  - Remove `_prepare_for_sync_shutdown(..., sync_restart=bool)`.
  - Pass `shutdown_intent` instead of `reason` plus `cancel_msg`.

- Modify: `src/mindroom/bot.py`
  - `AgentBot.stop()` accepts `shutdown_intent`.
  - `AgentBot.prepare_for_sync_shutdown()` accepts `shutdown_intent`.
  - Background drains and response drains receive only `shutdown_intent.cancel_source`.

- Modify: `src/mindroom/background_tasks.py`
  - `wait_for_background_tasks()` accepts `cancel_source`.
  - Internal cancellation calls use `request_task_cancel(task, cancel_source=...)`.

- Modify: `src/mindroom/response_runner.py`
  - `drain_inbox_responses()` accepts `cancel_source`.
  - Keep existing response cancellation classification logic unchanged except imports after moving helpers.
  - Keep `collect_streamed_response=show_tool_calls` as a small, separate response behavior fix.

- Modify: `src/mindroom/response_attempt.py`, `src/mindroom/delivery_gateway.py`, `src/mindroom/streaming.py`, `src/mindroom/stop.py`, `src/mindroom/coalescing.py`
  - Update imports/call sites to use typed cancellation helpers from `cancellation.py`.
  - No behavior change intended.

- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
  - Introduce `_CleanupScanPolicy`.
  - Replace threaded flags `collect_old_interrupted_threads`, `scan_past_cleanup_window`, and `extra_lookback_pages`.

- Modify tests:
  - `tests/test_sync_task_cancellation.py`
  - `tests/test_matrix_sync_tokens.py`
  - `tests/test_ingress_lanes.py`
  - `tests/test_threading_error.py`
  - `tests/test_ai_error_message_display.py`
  - `tests/test_stale_stream_cleanup.py`
  - Keep `tests/test_ai_stream_collection.py` and the one `tests/test_multi_agent_bot.py` assertion for visible tool ordering.

## Task 1: Add Typed Shutdown Intent

**Files:**
- Modify: `src/mindroom/cancellation.py`
- Create: `src/mindroom/runtime_shutdown.py`
- Test: `tests/test_sync_task_cancellation.py`

- [ ] **Step 1: Write failing tests for intent mapping**

Add these imports in `tests/test_sync_task_cancellation.py`:

```python
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    GENERIC_SHUTDOWN,
    SYNC_RESTART_SHUTDOWN,
    RuntimeShutdownIntent,
    shutdown_intent_for_entity,
)
```

Add these tests near the cancellation source tests:

```python
def test_shutdown_intent_for_restarted_entity() -> None:
    intent = shutdown_intent_for_entity("agent1", restart_entities={"agent1", "agent2"})

    assert intent == SYNC_RESTART_SHUTDOWN
    assert intent.stop_reason == "restart"
    assert intent.cancel_source == "sync_restart"


def test_shutdown_intent_for_removed_entity() -> None:
    intent = shutdown_intent_for_entity("removed", restart_entities={"agent1"})

    assert intent == ENTITY_REMOVED_SHUTDOWN
    assert intent.stop_reason == "entity_removed"
    assert intent.cancel_source is None


def test_generic_shutdown_has_no_restart_provenance() -> None:
    assert GENERIC_SHUTDOWN == RuntimeShutdownIntent(stop_reason=None, cancel_source=None)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_shutdown_intent_for_restarted_entity tests/test_sync_task_cancellation.py::test_shutdown_intent_for_removed_entity tests/test_sync_task_cancellation.py::test_generic_shutdown_has_no_restart_provenance -q -n 0 --no-cov
```

Expected: FAIL because `mindroom.runtime_shutdown` does not exist.

- [ ] **Step 3: Create shutdown intent module**

First add a narrower task-cancel source type in `src/mindroom/cancellation.py`:

```python
TaskCancelSource = Literal["user_stop", "sync_restart"]
CancelSource = Literal["user_stop", "sync_restart", "interrupted"]
```

This is deliberately not a behavior change yet.

Create `src/mindroom/runtime_shutdown.py`:

```python
"""Typed runtime shutdown intent shared by sync, bot, and response drains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mindroom.cancellation import TaskCancelSource

StopReason = Literal["restart", "entity_removed"]


@dataclass(frozen=True)
class RuntimeShutdownIntent:
    """One lifecycle shutdown decision made at the runtime boundary."""

    stop_reason: StopReason | None
    cancel_source: TaskCancelSource | None = None


GENERIC_SHUTDOWN = RuntimeShutdownIntent(stop_reason=None, cancel_source=None)
ENTITY_REMOVED_SHUTDOWN = RuntimeShutdownIntent(stop_reason="entity_removed", cancel_source=None)
SYNC_RESTART_SHUTDOWN = RuntimeShutdownIntent(stop_reason="restart", cancel_source="sync_restart")


def shutdown_intent_for_entity(
    entity_name: str,
    *,
    restart_entities: set[str],
) -> RuntimeShutdownIntent:
    """Return shutdown intent for one stopped entity."""
    if entity_name in restart_entities:
        return SYNC_RESTART_SHUTDOWN
    return ENTITY_REMOVED_SHUTDOWN
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_shutdown_intent_for_restarted_entity tests/test_sync_task_cancellation.py::test_shutdown_intent_for_removed_entity tests/test_sync_task_cancellation.py::test_generic_shutdown_has_no_restart_provenance -q -n 0 --no-cov
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/cancellation.py src/mindroom/runtime_shutdown.py tests/test_sync_task_cancellation.py
git commit -m "Add typed runtime shutdown intent"
```

## Task 2: Rename Low-Level Cancellation API To Typed Source

**Files:**
- Modify: `src/mindroom/cancellation.py`
- Modify call sites found by: `grep -R "request_task_cancel\\|cancel_msg=" -n src/mindroom tests`
- Test: `tests/test_sync_task_cancellation.py`

- [ ] **Step 1: Write failing direct API tests**

In `tests/test_sync_task_cancellation.py`, replace tests that call `_cancel_failure_reason` directly with public helper coverage:

```python
from mindroom.cancellation import (
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    cancel_failure_reason,
    cancel_message_for_source,
)
```

Add:

```python
def test_cancel_message_for_source() -> None:
    assert cancel_message_for_source("sync_restart") == SYNC_RESTART_CANCEL_MSG
    assert cancel_message_for_source("user_stop") == USER_STOP_CANCEL_MSG
    assert cancel_message_for_source(None) is None
```

- [ ] **Step 2: Update `cancellation.py`**

Replace the top of `src/mindroom/cancellation.py` with:

```python
"""Task cancellation helpers shared across runtime and response paths."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

TaskCancelSource = Literal["user_stop", "sync_restart"]
CancelSource = Literal["user_stop", "sync_restart", "interrupted"]
USER_STOP_CANCEL_MSG = "user_stop"
SYNC_RESTART_CANCEL_MSG = "sync_restart"

_TASK_CANCEL_SOURCES: dict[asyncio.Task[Any], TaskCancelSource] = {}


def cancel_message_for_source(cancel_source: TaskCancelSource | None) -> str | None:
    """Return asyncio task-cancel message for one typed source."""
    if cancel_source == "user_stop":
        return USER_STOP_CANCEL_MSG
    if cancel_source == "sync_restart":
        return SYNC_RESTART_CANCEL_MSG
    return None


def _clear_task_cancel_source(task: asyncio.Task[Any]) -> None:
    """Drop recorded cancellation provenance once one task finishes."""
    _TASK_CANCEL_SOURCES.pop(task, None)


def request_task_cancel(task: asyncio.Task[Any], *, cancel_source: TaskCancelSource | None = None) -> None:
    """Cancel one task while preserving the first explicit cancellation source."""
    cancel_msg = cancel_message_for_source(cancel_source)
    if cancel_source is not None and task not in _TASK_CANCEL_SOURCES:
        _TASK_CANCEL_SOURCES[task] = cancel_source
        task.add_done_callback(_clear_task_cancel_source)
    if cancel_msg is None:
        task.cancel()
    else:
        task.cancel(msg=cancel_msg)
```

Keep the existing `build_cancelled_error()`, `classify_cancel_source()`, `cancel_failure_reason()`, and `cancel_source_from_failure_reason()` behavior, but update `_TASK_CANCEL_SOURCES.get(task)` handling to translate stored typed source through `cancel_message_for_source()`.

- [ ] **Step 3: Mechanical call-site update**

Change call sites:

```python
request_task_cancel(task, cancel_msg=SYNC_RESTART_CANCEL_MSG)
```

to:

```python
request_task_cancel(task, cancel_source="sync_restart")
```

Change:

```python
request_task_cancel(task, cancel_msg=USER_STOP_CANCEL_MSG)
```

to:

```python
request_task_cancel(task, cancel_source="user_stop")
```

Change forwarded optional values named `cancel_msg` only after Task 3 replaces them with `cancel_source`.

- [ ] **Step 4: Run cancellation tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py tests/test_ai_error_message_display.py -q -n 0 --no-cov
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/cancellation.py src/mindroom/stop.py src/mindroom/coalescing.py src/mindroom/response_attempt.py tests/test_sync_task_cancellation.py tests/test_ai_error_message_display.py
git commit -m "Use typed task cancellation sources"
```

## Task 3: Push Shutdown Intent Through Runtime And Bot Drains

**Files:**
- Modify: `src/mindroom/orchestration/runtime.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/background_tasks.py`
- Modify: `src/mindroom/response_runner.py`
- Modify tests listed in File Structure.

- [ ] **Step 1: Update tests to expect `shutdown_intent`**

In `tests/test_sync_task_cancellation.py`, update fake bot:

```python
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, RuntimeShutdownIntent, SYNC_RESTART_SHUTDOWN
```

Change `_FakeBot.prepare_for_sync_shutdown()` to:

```python
async def prepare_for_sync_shutdown(
    self,
    *,
    shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
) -> None:
    self._sync_shutting_down = True
    self.prepare_for_sync_shutdown_calls += 1
    self.prepare_for_sync_shutdown_cancel_messages.append(shutdown_intent.cancel_source)
```

Update assertions:

```python
restart_bot.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
restart_bot.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
removed_bot.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
removed_bot.stop.assert_awaited_once_with(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
```

- [ ] **Step 2: Make `background_tasks.py` consume typed source**

Change signatures:

```python
from mindroom.cancellation import TaskCancelSource, request_task_cancel


async def _cancel_and_drain_background_tasks(
    tasks: tuple[asyncio.Task[Any], ...],
    *,
    owner: object | None,
    cancel_source: TaskCancelSource | None,
) -> None:
```

and:

```python
async def wait_for_background_tasks(
    timeout: float | None = None,
    *,
    owner: object | None = None,
    cancel_source: TaskCancelSource | None = None,
) -> bool:
```

Use:

```python
request_task_cancel(task, cancel_source=cancel_source)
```

- [ ] **Step 3: Make `response_runner.py` consume typed source**

Change signature:

```python
from mindroom.cancellation import TaskCancelSource


async def drain_inbox_responses(
    self,
    *,
    cancel_after_seconds: float | None = None,
    cancel_source: TaskCancelSource | None = None,
) -> bool:
```

Use:

```python
request_task_cancel(task, cancel_source=cancel_source)
```

Keep this existing response behavior:

```python
collect_streamed_response=show_tool_calls,
```

- [ ] **Step 4: Make `bot.py` consume `RuntimeShutdownIntent`**

Add imports:

```python
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, RuntimeShutdownIntent
```

Change `stop()`:

```python
async def stop(
    self,
    *,
    shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
) -> None:
    """Stop the agent bot."""
    self.running = False
    ...
    await self._emit_agent_lifecycle_event(EVENT_AGENT_STOPPED, stop_reason=shutdown_intent.stop_reason)

    await self.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)
```

Change `cleanup()`:

```python
from mindroom.runtime_shutdown import ENTITY_REMOVED_SHUTDOWN

...
await self.stop(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
```

Change `prepare_for_sync_shutdown()`:

```python
async def prepare_for_sync_shutdown(
    self,
    *,
    shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
) -> None:
    """Cancel work that must not outlive the Matrix sync loop."""
    self._sync_shutting_down = True
    ...
    background_tasks_completed = await wait_for_background_tasks(
        timeout=5.0,
        owner=self._runtime_view,
        cancel_source=shutdown_intent.cancel_source,
    )
    ...
    responses_drained = await self._response_runner.drain_inbox_responses(
        cancel_after_seconds=5.0,
        cancel_source=shutdown_intent.cancel_source,
    )
```

- [ ] **Step 5: Make `runtime.py` decide intent once**

Add imports:

```python
from mindroom.runtime_shutdown import (
    GENERIC_SHUTDOWN,
    SYNC_RESTART_SHUTDOWN,
    shutdown_intent_for_entity,
)
```

Delete `_prepare_for_sync_shutdown()`.

Change `stop_entities()` core:

```python
restart_entities = set() if restart_entities is None else restart_entities
shutdown_intents = {
    entity_name: shutdown_intent_for_entity(entity_name, restart_entities=restart_entities)
    for entity_name in entities_to_stop
}

for entity_name in entities_to_stop:
    await cancel_sync_task(
        entity_name,
        sync_tasks,
        cancel_source=shutdown_intents[entity_name].cancel_source,
    )

for entity_name in entities_to_stop:
    bot = agent_bots.get(entity_name)
    if bot is not None:
        await bot.prepare_for_sync_shutdown(shutdown_intent=shutdown_intents[entity_name])

stop_tasks = [
    agent_bots[entity_name].stop(shutdown_intent=shutdown_intents[entity_name])
    for entity_name in entities_to_stop
    if entity_name in agent_bots
]
```

Change `sync_forever_with_restart()` final drain:

```python
will_retry = retry_after_cleanup and bot.running and (max_retries < 0 or retry_count < max_retries)
shutdown_intent = SYNC_RESTART_SHUTDOWN if will_retry or sync_restart_cancelled else GENERIC_SHUTDOWN
await bot.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)
```

- [ ] **Step 6: Run focused runtime tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py tests/test_matrix_sync_tokens.py tests/test_ingress_lanes.py tests/test_threading_error.py -q -n 0 --no-cov
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mindroom/orchestration/runtime.py src/mindroom/bot.py src/mindroom/background_tasks.py src/mindroom/response_runner.py tests/test_sync_task_cancellation.py tests/test_matrix_sync_tokens.py tests/test_ingress_lanes.py tests/test_threading_error.py
git commit -m "Thread shutdown intent through runtime drains"
```

## Task 4: Localize Stale Cleanup Scan Policy

**Files:**
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Test: `tests/test_stale_stream_cleanup.py`

- [ ] **Step 1: Add `_CleanupScanPolicy`**

In `src/mindroom/matrix/stale_stream_cleanup.py`, near existing private dataclasses:

```python
@dataclass(frozen=True)
class _CleanupScanPolicy:
    """History scan bounds for one startup stale-stream cleanup run."""

    startup_cutoff_ms: int | None
    collect_terminal_interrupted_for_resume: bool
    max_extra_old_pages: int


def _cleanup_scan_policy(config: Config, *, startup_cutoff_ms: int | None) -> _CleanupScanPolicy:
    """Return history scan policy for one stale-stream cleanup run."""
    collect_terminal_interrupted_for_resume = config.defaults.auto_resume_after_restart
    return _CleanupScanPolicy(
        startup_cutoff_ms=startup_cutoff_ms,
        collect_terminal_interrupted_for_resume=collect_terminal_interrupted_for_resume,
        max_extra_old_pages=(
            MAX_AUTO_RESUME_AFTER_RESTART_THREADS if collect_terminal_interrupted_for_resume else 0
        ),
    )
```

- [ ] **Step 2: Replace threaded booleans with policy**

In `cleanup_stale_streaming_messages()`:

```python
scan_policy = _cleanup_scan_policy(config, startup_cutoff_ms=startup_cutoff_ms)
```

Pass `scan_policy=scan_policy` into `_cleanup_room_stale_streaming_messages()`.

Change `_cleanup_room_stale_streaming_messages()` signature:

```python
scan_policy: _CleanupScanPolicy,
```

Pass `scan_policy=scan_policy` into `_scan_room_message_states()` and `_should_skip_for_startup_cleanup_window()`.

Change `_scan_room_message_states()` signature:

```python
scan_policy: _CleanupScanPolicy,
```

Change `_collect_room_history_events()` signature:

```python
scan_policy: _CleanupScanPolicy,
```

Change `_lookback_scan_state()` signature:

```python
def _lookback_scan_state(
    events: list[object],
    *,
    now_ms: int,
    scan_policy: _CleanupScanPolicy,
    lookback_pages_scanned: int,
) -> tuple[int, bool]:
```

Implementation:

```python
if not _chunk_reaches_cleanup_lookback_limit(events, now_ms=now_ms):
    return lookback_pages_scanned, False
if not scan_policy.collect_terminal_interrupted_for_resume:
    return lookback_pages_scanned, True
updated_count = lookback_pages_scanned + 1
return updated_count, updated_count >= scan_policy.max_extra_old_pages
```

- [ ] **Step 3: Update startup skip helper**

Replace `_should_skip_for_startup_cleanup_window()` with:

```python
def _should_skip_for_startup_cleanup_window(
    state: _MessageState,
    *,
    now_ms: int,
    scan_policy: _CleanupScanPolicy,
) -> bool:
    """Return whether startup cleanup should ignore one candidate by age."""
    timestamp_ms = state.latest_timestamp
    if _is_at_or_after_startup_cutoff(timestamp_ms, startup_cutoff_ms=scan_policy.startup_cutoff_ms):
        return True
    if _is_recent_timestamp(timestamp_ms, now_ms=now_ms):
        return True
    if _is_older_than_cleanup_window(timestamp_ms, now_ms=now_ms):
        return not (
            scan_policy.collect_terminal_interrupted_for_resume
            and _has_resumable_interrupted_note(state)
        )
    return False
```

- [ ] **Step 4: Run stale cleanup tests**

Run:

```bash
uv run pytest tests/test_stale_stream_cleanup.py -q -n 0 --no-cov
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/stale_stream_cleanup.py tests/test_stale_stream_cleanup.py
git commit -m "Localize stale cleanup scan policy"
```

## Task 5: Re-check Scope And Remove String Plumbing

**Files:**
- Modify any file still containing stale API names.

- [ ] **Step 1: Search for stale plumbing**

Run:

```bash
grep -R "cancel_msg=" -n src/mindroom tests
grep -R "sync_restart: bool" -n src/mindroom tests
grep -R "collect_old_interrupted_threads\\|scan_past_cleanup_window\\|extra_lookback_pages" -n src/mindroom tests
```

Expected:

```text
no matches
```

Allowed remaining matches:

```text
src/mindroom/cancellation.py:SYNC_RESTART_CANCEL_MSG
src/mindroom/cancellation.py:USER_STOP_CANCEL_MSG
```

- [ ] **Step 2: Run focused behavior suites**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py tests/test_stale_stream_cleanup.py tests/test_config_reload.py tests/test_ingress_lanes.py tests/test_threading_error.py tests/test_matrix_sync_tokens.py -q -n 0 --no-cov
```

Expected: PASS.

- [ ] **Step 3: Run response/tool trace focused checks**

Run:

```bash
uv run pytest tests/test_ai_stream_collection.py tests/test_multi_agent_bot.py::TestAgentBot::test_agent_bot_on_message_mentioned tests/test_ai_user_id.py::TestUserIdPassthrough::test_ai_response_collects_tool_trace_when_tool_calls_hidden -q -n 0 --no-cov
```

Expected: PASS.

- [ ] **Step 4: Run pre-commit**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: all hooks pass.

- [ ] **Step 5: Commit final cleanup if needed**

Only if Step 1 or hooks require changes:

```bash
git add <exact-files>
git commit -m "Clean up shutdown architecture call sites"
```

## Task 6: Push And Review Loop

**Files:**
- No code files unless review finds real blockers.

- [ ] **Step 1: Push branch**

Run:

```bash
git push origin HEAD:refs/heads/bas/fix-sync-restart-stream-cancel
```

- [ ] **Step 2: Start two native Codex review agents**

Use native Codex subagents only, not agent-cli.

Prompt both agents:

```text
Review pull request https://github.com/mindroom-ai/mindroom/pull/1327 for merge readiness using the provided pr-review skill.
Fetch latest remote PR head and review the real diff against base.
Do not modify files.
Return only the verdict and concrete findings with file/line references, or APPROVE if no issues.
```

- [ ] **Step 3: Triage findings**

For every finding:

```text
Verify in code first.
If real and in scope, fix in main thread.
If false or scope creep, document reason and do not change code.
```

- [ ] **Step 4: Repeat until clean**

Loop:

```text
fix -> focused tests -> pre-commit -> commit -> push -> two native review agents
```

Stop when both latest agents approve or only non-code CI state is reported.

## Self-Review

- Spec coverage: restart cancellation provenance, entity removal vs restart, startup stale cleanup scan bounds, detached response/background drains, non-streaming visible tool order, push/review loop all covered.
- Placeholder scan: no TBD/TODO/fill-later steps.
- Type consistency: `RuntimeShutdownIntent.cancel_source` uses `TaskCancelSource | None`; low-level task cancellation accepts `cancel_source`; stale cleanup scan accepts `_CleanupScanPolicy`.
