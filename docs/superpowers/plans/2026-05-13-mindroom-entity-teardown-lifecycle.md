# MindRoom Entity Teardown Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MindRoom entity teardown close ingress, discard queued coalesced work, and cancel already-admitted responses with provenance that matches the actual shutdown reason.

**Architecture:** Separate sync retry from entity teardown.
Sync retry may clean sync-local buffers and checkpoints, but must not cancel active responses.
Entity teardown first closes response-producing ingress, then prevents queued coalescing from dispatching new turns, then cancels or waits for already-admitted response tasks with an explicit cancel source.

**Tech Stack:** Python 3.13, asyncio, nio Matrix callbacks, pytest, Ruff, existing MindRoom Matrix streaming metadata.

---

## Core Invariant

Once entity teardown begins:

1. No new Matrix text event, media event, coalesced batch, or interactive selection may start a response task.
2. Queued coalesced work must be closed or discarded without dispatching response-producing turns.
3. Every response task already admitted before teardown must be cancelled or waited on.
4. The cancellation source must match the lifecycle:
   - service/process restart: restart-resumable
   - config reload or MCP entity restart: non-resumable entity teardown
   - entity removal: non-resumable entity teardown
   - user stop: user-cancelled and never reclassified
5. Startup stale-stream cleanup may auto-resume restart-resumable interruptions, but must not auto-resume entity-teardown or user-cancelled interruptions.

## File Structure

- Modify `src/mindroom/cancellation.py`
  - Own canonical task cancellation source strings and failure-reason mapping.
  - Add a non-resumable entity-teardown cancellation source.

- Modify `src/mindroom/constants.py`
  - Own Matrix content metadata keys.
  - Add one content key for response cancellation source metadata.

- Modify `src/mindroom/streaming.py`
  - Own visible terminal stream body and stream status construction.
  - Persist cancellation source metadata on terminal stream edits.

- Modify `src/mindroom/matrix/stale_stream_cleanup.py`
  - Own startup stale stream detection and auto-resume candidate selection.
  - Read cancellation source metadata and only resume restart-resumable terminal interruptions.

- Modify `src/mindroom/coalescing.py`
  - Own queued live message coalescing.
  - Add a teardown discard operation that closes queued metadata and prevents pending queued work from dispatching.

- Modify `src/mindroom/coalescing_batch.py`
  - Own metadata cleanup helpers for coalesced batches.
  - Add a helper that closes metadata carried by a flushed batch that is rejected during teardown.

- Modify `src/mindroom/bot.py`
  - Own Matrix callback admission and entity lifecycle coordination.
  - Add an explicit response-ingress closed flag.
  - Guard direct Matrix callbacks and coalesced batch dispatch.
  - Split sync retry cleanup from entity teardown cleanup.

- Modify `src/mindroom/turn_controller.py`
  - Own turn pipeline admission after async preparation steps.
  - Add a hard guard before any response-producing call to `ResponseRunner`.

- Modify `src/mindroom/orchestration/runtime.py`
  - Own runtime stop ordering for entity restarts.
  - Pass the correct response cancellation source for config-driven entity restarts.

- Modify `src/mindroom/orchestrator.py`
  - Own top-level service shutdown and entity removal calls.
  - Pass service-restart provenance only for full service shutdown and non-resumable provenance for entity removal.

- Test `tests/test_sync_task_cancellation.py`
  - Cover lifecycle ordering, ingress closure, and cancellation provenance.

- Test `tests/test_stale_stream_cleanup.py`
  - Cover auto-resume eligibility based on cancellation source metadata.

- Test `tests/test_live_message_coalescing.py`
  - Cover queued coalescing discard during entity teardown.

---

### Task 1: Add Non-Resumable Entity-Teardown Cancellation Provenance

**Files:**
- Modify: `src/mindroom/cancellation.py`
- Modify: `src/mindroom/constants.py`
- Test: `tests/test_sync_task_cancellation.py`

- [x] **Step 1: Write failing provenance tests**

Add these imports in `tests/test_sync_task_cancellation.py`:

```python
from mindroom.cancellation import ENTITY_TEARDOWN_CANCEL_MSG
from mindroom.constants import RESPONSE_CANCEL_SOURCE_KEY
```

Add this test near the existing cancellation-source tests:

```python
@pytest.mark.asyncio
async def test_classify_cancel_source_entity_teardown() -> None:
    assert classify_cancel_source(asyncio.CancelledError(ENTITY_TEARDOWN_CANCEL_MSG)) == "entity_teardown"
```

Extend `test_cancel_failure_reason_matches_cancel_source()`:

```python
assert _cancel_failure_reason("entity_teardown") == "entity_teardown_cancelled"
```

Extend `test_cancel_source_from_failure_reason_matches_canonical_reasons()` params:

```python
("entity_teardown_cancelled", "entity_teardown"),
```

Add this constant smoke test:

```python
def test_response_cancel_source_key_is_namespaced() -> None:
    assert RESPONSE_CANCEL_SOURCE_KEY == "io.mindroom.cancel_source"
```

- [x] **Step 2: Run the focused failing tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_classify_cancel_source_entity_teardown tests/test_sync_task_cancellation.py::test_cancel_failure_reason_matches_cancel_source tests/test_sync_task_cancellation.py::test_cancel_source_from_failure_reason_matches_canonical_reasons tests/test_sync_task_cancellation.py::test_response_cancel_source_key_is_namespaced -q -x
```

Expected: fail because `ENTITY_TEARDOWN_CANCEL_MSG`, the new cancel source, and `RESPONSE_CANCEL_SOURCE_KEY` do not exist yet.

- [x] **Step 3: Implement cancellation source and content key**

In `src/mindroom/constants.py`, add this near the other Matrix metadata constants:

```python
RESPONSE_CANCEL_SOURCE_KEY = "io.mindroom.cancel_source"
```

In `src/mindroom/cancellation.py`, update the type and constants:

```python
CancelSource = Literal["user_stop", "sync_restart", "entity_teardown", "interrupted"]
USER_STOP_CANCEL_MSG = "user_stop"
SYNC_RESTART_CANCEL_MSG = "sync_restart"
ENTITY_TEARDOWN_CANCEL_MSG = "entity_teardown"
```

Update `classify_cancel_source()`:

```python
def classify_cancel_source(exc: asyncio.CancelledError) -> CancelSource:
    """Return the visible cancellation provenance for one CancelledError."""
    if len(exc.args) == 0:
        return "interrupted"
    if exc.args[0] == USER_STOP_CANCEL_MSG:
        return "user_stop"
    if exc.args[0] == SYNC_RESTART_CANCEL_MSG:
        return "sync_restart"
    if exc.args[0] == ENTITY_TEARDOWN_CANCEL_MSG:
        return "entity_teardown"
    return "interrupted"
```

Update `_cancel_failure_reason()`:

```python
def _cancel_failure_reason(cancel_source: CancelSource) -> str:
    """Return the canonical failure reason for one cancellation provenance."""
    if cancel_source == "sync_restart":
        return "sync_restart_cancelled"
    if cancel_source == "entity_teardown":
        return "entity_teardown_cancelled"
    if cancel_source == "user_stop":
        return "cancelled_by_user"
    return "interrupted"
```

Update `cancel_source_from_failure_reason()`:

```python
def cancel_source_from_failure_reason(failure_reason: str | None) -> CancelSource:
    """Return cancellation provenance from one canonical failure reason."""
    if failure_reason == "sync_restart_cancelled":
        return "sync_restart"
    if failure_reason == "entity_teardown_cancelled":
        return "entity_teardown"
    if failure_reason == "cancelled_by_user":
        return "user_stop"
    return "interrupted"
```

- [x] **Step 4: Run the focused tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_classify_cancel_source_entity_teardown tests/test_sync_task_cancellation.py::test_cancel_failure_reason_matches_cancel_source tests/test_sync_task_cancellation.py::test_cancel_source_from_failure_reason_matches_canonical_reasons tests/test_sync_task_cancellation.py::test_response_cancel_source_key_is_namespaced -q -x
```

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/mindroom/cancellation.py src/mindroom/constants.py tests/test_sync_task_cancellation.py
git commit -m "Add entity teardown cancellation provenance"
```

---

### Task 2: Persist Cancellation Source Metadata on Terminal Stream Updates

**Files:**
- Modify: `src/mindroom/streaming.py`
- Test: `tests/test_sync_task_cancellation.py`
- Test: `tests/test_streaming_finalize.py`

- [x] **Step 1: Write failing streaming metadata tests**

Add this test in `tests/test_sync_task_cancellation.py` near the stop-manager tests:

```python
@pytest.mark.asyncio
async def test_entity_teardown_cancel_finalizes_as_non_resumable_interruption() -> None:
    from mindroom.streaming import build_cancelled_response_update

    body, stream_status = build_cancelled_response_update(
        "Partial answer",
        cancel_source="entity_teardown",
    )

    assert body == "Partial answer\n\n**[Response interrupted]**"
    assert stream_status == "error"
```

Add these imports in `tests/test_streaming_finalize.py`:

```python
from mindroom.constants import RESPONSE_CANCEL_SOURCE_KEY
```

Update the existing typing import in `tests/test_streaming_finalize.py`:

```python
from typing import TYPE_CHECKING, Any, cast
```

Add this test near the existing `StreamingResponse.finalize()` terminal update tests:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_source", ["sync_restart", "entity_teardown"])
async def test_cancelled_stream_terminal_edit_persists_cancel_source_metadata(
    tmp_path: Path,
    cancel_source: str,
) -> None:
    """Terminal stream edits should persist explicit cancellation provenance."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "partial answer"
    captured_edits: list[dict[str, object]] = []

    async def record_edit(*args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = cast("dict[str, object]", args[3])
        captured_edits.append(dict(content))
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await streaming.finalize(_client(), cancel_source=cancel_source)

    assert outcome.terminal_status == "cancelled"
    assert captured_edits[-1][RESPONSE_CANCEL_SOURCE_KEY] == cancel_source
    new_content = cast("dict[str, object]", captured_edits[-1]["m.new_content"])
    assert new_content[RESPONSE_CANCEL_SOURCE_KEY] == cancel_source
```

- [x] **Step 2: Run the focused failing test**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_entity_teardown_cancel_finalizes_as_non_resumable_interruption tests/test_streaming_finalize.py::test_cancelled_stream_terminal_edit_persists_cancel_source_metadata -q -x
```

Expected: fail because `build_cancelled_response_update()` does not accept `entity_teardown` and terminal Matrix content does not persist `io.mindroom.cancel_source`.

- [x] **Step 3: Update streaming cancel-source typing and body handling**

In `src/mindroom/streaming.py`, import the metadata key:

```python
from mindroom.constants import RESPONSE_CANCEL_SOURCE_KEY
```

Replace `Literal["user_stop", "sync_restart", "interrupted"]` annotations in this file with `CancelSource` imported from `mindroom.cancellation`.

Update `build_cancelled_response_update()`:

```python
def build_cancelled_response_update(
    text: str,
    *,
    cancel_source: CancelSource,
) -> tuple[str, _TerminalStreamStatus]:
    """Return the final visible body and stream status for one cancellation source."""
    if cancel_source == "sync_restart":
        return build_restart_interrupted_body(text), STREAM_STATUS_ERROR

    note = _CANCELLED_RESPONSE_NOTE if cancel_source == "user_stop" else _INTERRUPTED_RESPONSE_NOTE
    stream_status = STREAM_STATUS_CANCELLED if cancel_source == "user_stop" else STREAM_STATUS_ERROR
    stripped_text = text.rstrip()
    if not stripped_text or stripped_text == _PROGRESS_PLACEHOLDER:
        return note, stream_status
    return f"{stripped_text}\n\n{note}", stream_status
```

In `StreamingResponse.finalize()`, persist the cancel source immediately after `resolved_cancel_source` is derived and before `_prepare_terminal_text_and_status()` builds terminal content:

```python
if resolved_cancel_source is not None:
    extra_content = dict(self.extra_content or {})
    extra_content[RESPONSE_CANCEL_SOURCE_KEY] = resolved_cancel_source
    self.extra_content = extra_content
```

- [x] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_entity_teardown_cancel_finalizes_as_non_resumable_interruption tests/test_streaming_finalize.py::test_cancelled_stream_terminal_edit_persists_cancel_source_metadata -q -x
```

Expected: pass.

- [x] **Step 5: Run streaming type-adjacent tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_stop_manager_cancels_active_responses_as_sync_restart tests/test_sync_task_cancellation.py::test_stop_manager_waits_for_user_cancelled_responses_without_reclassifying tests/test_streaming_finalize.py::test_cancelled_stream_terminal_edit_persists_cancel_source_metadata -q -x
```

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add src/mindroom/streaming.py tests/test_sync_task_cancellation.py tests/test_streaming_finalize.py
git commit -m "Persist response cancellation source metadata"
```

---

### Task 3: Restrict Startup Auto-Resume to Restart-Resumable Interruptions

**Files:**
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Test: `tests/test_stale_stream_cleanup.py`

- [x] **Step 1: Write failing stale-cleanup metadata tests**

In `tests/test_stale_stream_cleanup.py`, import the metadata key:

```python
from mindroom.constants import RESPONSE_CANCEL_SOURCE_KEY
```

Add this test next to `test_cleanup_returns_generic_interrupted_thread_from_graceful_restart()`:

```python
@pytest.mark.asyncio
async def test_cleanup_skips_entity_teardown_interrupted_thread(tmp_path: Path) -> None:
    """Config reload or entity removal interruptions are not startup-auto-resumable."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$entity-teardown",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: "error",
                RESPONSE_CANCEL_SOURCE_KEY: "entity_teardown",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
```

Add this restart metadata test:

```python
@pytest.mark.asyncio
async def test_cleanup_resumes_generic_interrupted_thread_with_restart_metadata(tmp_path: Path) -> None:
    """Generic interrupted notes remain resumable when terminal metadata says sync restart."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$restart",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: "error",
                RESPONSE_CANCEL_SOURCE_KEY: "sync_restart",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert [thread.target_event_id for thread in interrupted] == ["$restart"]
```

- [x] **Step 2: Run the focused failing tests**

Run:

```bash
uv run pytest tests/test_stale_stream_cleanup.py::test_cleanup_skips_entity_teardown_interrupted_thread tests/test_stale_stream_cleanup.py::test_cleanup_resumes_generic_interrupted_thread_with_restart_metadata -q -n 0 --no-cov -x
```

Expected: first test fails because generic interrupted messages with `STREAM_STATUS_ERROR` are still resumable regardless of metadata.

- [x] **Step 3: Store cancel-source metadata in `_MessageState`**

In `src/mindroom/matrix/stale_stream_cleanup.py`, import:

```python
from mindroom.constants import RESPONSE_CANCEL_SOURCE_KEY
```

Add a field to `_MessageState`:

```python
cancel_source: str | None = None
```

In `_merge_resolved_message_state()`, after `state.stream_status = message.stream_status`, add:

```python
cancel_source = normalized_latest_content.get(RESPONSE_CANCEL_SOURCE_KEY)
state.cancel_source = cancel_source if isinstance(cancel_source, str) else None
```

- [x] **Step 4: Gate generic interrupted auto-resume by cancel source**

Replace `_has_resumable_interrupted_note()` with:

```python
def _has_resumable_interrupted_note(state: _MessageState) -> bool:
    """Return whether the visible body represents a restart-resumable interruption."""
    assert state.latest_body is not None
    if _has_restart_interrupted_note(state.latest_body):
        return True
    if not _has_generic_interrupted_note(state.latest_body):
        return False
    if state.cancel_source == "sync_restart":
        return state.stream_status in {
            STREAM_STATUS_ERROR,
            STREAM_STATUS_INTERRUPTED,
        }
    if state.cancel_source in {"entity_teardown", "user_stop"}:
        return False
    return state.stream_status in {
        STREAM_STATUS_ERROR,
        STREAM_STATUS_INTERRUPTED,
    }
```

The final fallback preserves the original PR behavior for legacy terminal generic interruptions that have no metadata.

- [x] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/test_stale_stream_cleanup.py::test_cleanup_skips_entity_teardown_interrupted_thread tests/test_stale_stream_cleanup.py::test_cleanup_resumes_generic_interrupted_thread_with_restart_metadata tests/test_stale_stream_cleanup.py::test_cleanup_returns_generic_interrupted_thread_from_graceful_restart -q -n 0 --no-cov -x
```

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add src/mindroom/matrix/stale_stream_cleanup.py tests/test_stale_stream_cleanup.py
git commit -m "Gate restart auto-resume by cancellation metadata"
```

---

### Task 4: Add Coalescing Discard for Entity Teardown

**Files:**
- Modify: `src/mindroom/coalescing.py`
- Modify: `src/mindroom/coalescing_batch.py`
- Test: `tests/test_live_message_coalescing.py`

- [x] **Step 1: Write failing coalescing discard test**

Add this test in `tests/test_live_message_coalescing.py` near the existing `drain_all()` shutdown tests:

```python
@pytest.mark.asyncio
async def test_coalescing_discard_all_closes_pending_metadata_without_dispatch() -> None:
    dispatched: list[CoalescedBatch] = []
    closed: list[str] = []
    metadata = PendingDispatchMetadata(
        kind="test",
        payload=None,
        close=lambda: closed.append("metadata"),
    )

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        dispatched.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    room = MagicMock()
    room.room_id = "!room:example.com"
    event = _text_event(event_id="$event", sender="@user:example.com", body="hello")
    pending = PendingEvent(
        event=event,
        room=room,
        source_kind="text",
        dispatch_metadata=(metadata,),
    )

    await gate.enqueue(("!room:example.com", "$thread", "@user:example.com"), pending)
    await gate.discard_all()

    assert dispatched == []
    assert closed == ["metadata"]
```

Use the local test helpers already present in `tests/test_live_message_coalescing.py`; if the helper for text events has a different name, use the existing helper in that file rather than adding a new event factory.

Add this in-flight ownership test in the same file:

```python
@pytest.mark.asyncio
async def test_coalescing_discard_all_does_not_cancel_inflight_dispatch() -> None:
    dispatch_started = asyncio.Event()
    allow_dispatch_to_finish = asyncio.Event()
    closed: list[str] = []
    metadata = PendingDispatchMetadata(
        kind="test",
        payload=None,
        close=lambda: closed.append("metadata"),
    )

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_started.set()
        await allow_dispatch_to_finish.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    room = MagicMock()
    room.room_id = "!room:example.com"
    event = _text_event(event_id="$event", body="hello")
    await gate.enqueue(
        ("!room:example.com", "$thread", "@user:example.com"),
        PendingEvent(event=event, room=room, source_kind="text", dispatch_metadata=(metadata,)),
    )

    await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)
    await gate.discard_all()

    assert closed == []
    allow_dispatch_to_finish.set()
    await _wait_for(lambda: _coalescing_gate_is_idle(gate))
```

- [x] **Step 2: Run the focused failing test**

Run:

```bash
uv run pytest tests/test_live_message_coalescing.py::test_coalescing_discard_all_closes_pending_metadata_without_dispatch tests/test_live_message_coalescing.py::test_coalescing_discard_all_does_not_cancel_inflight_dispatch -q -x
```

Expected: fail because `CoalescingGate.discard_all()` does not exist.

- [x] **Step 3: Add coalesced batch metadata cleanup helper**

In `src/mindroom/coalescing_batch.py`, add:

```python
def close_coalesced_batch_metadata(batch: CoalescedBatch) -> None:
    """Close PendingDispatchMetadata items owned by a coalesced batch that will not dispatch."""
    for item in batch.dispatch_metadata:
        item.close()
```

Add `"close_coalesced_batch_metadata"` to any local import list where needed.
This file does not currently define `__all__`.

- [x] **Step 4: Add `CoalescingGate.discard_all()`**

In `src/mindroom/coalescing.py`, add this method after `drain_all()`:

```python
async def discard_all(self) -> None:
    """Drop queued coalescing work without cancelling in-flight dispatch."""
    for key, gate in list(self._gates.items()):
        close_pending_event_metadata([queued.pending_event for queued in gate.queue])
        gate.queue.clear()
        gate.drain_all_requested = True
        gate.deadline = time.monotonic()
        gate.grace_deadline = None
        self._wake(gate)
        if gate.phase is not GatePhase.IN_FLIGHT:
            self._gates.pop(key, None)
```

Claimed batch metadata ownership rule:

- Metadata still in `gate.queue` is closed by `discard_all()`.
- Metadata already claimed into a `CoalescedBatch` is closed by the closed-ingress rejection guard.
- In-flight dispatch tasks are not plain-cancelled by `discard_all()`.
- Already-admitted response tasks are cancelled by `StopManager.cancel_active_responses(cancel_msg=...)`.

- [x] **Step 5: Run focused coalescing test**

Run:

```bash
uv run pytest tests/test_live_message_coalescing.py::test_coalescing_discard_all_closes_pending_metadata_without_dispatch tests/test_live_message_coalescing.py::test_coalescing_discard_all_does_not_cancel_inflight_dispatch -q -x
```

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add src/mindroom/coalescing.py src/mindroom/coalescing_batch.py tests/test_live_message_coalescing.py
git commit -m "Discard queued coalescing work during teardown"
```

---

### Task 5: Close Response-Producing Ingress During Entity Teardown

**Files:**
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/coalescing_batch.py`
- Test: `tests/test_sync_task_cancellation.py`
- Test: `tests/test_live_message_coalescing.py`
- Test: `tests/test_turn_controller.py`

- [x] **Step 1: Write failing direct-ingress guard test**

Add this test in `tests/test_sync_task_cancellation.py`:

```python
@pytest.mark.asyncio
async def test_on_message_drops_text_after_entity_shutdown_starts() -> None:
    bot = object.__new__(AgentBot)
    bot.agent_user = MagicMock(agent_name="test_agent")
    bot._entity_shutdown_prepared = True
    bot.logger = MagicMock()
    bot.config = MagicMock()
    bot.runtime_paths = _fake_runtime_paths()
    bot.orchestrator = None
    bot._turn_controller = MagicMock()
    bot._turn_controller.handle_text_event = AsyncMock()

    room = MagicMock()
    room.room_id = "!room:example.com"
    event = MagicMock()
    event.event_id = "$event"
    event.sender = "@user:example.com"
    event.source = {
        "origin_server_ts": int(time.time() * 1000),
        "content": {"body": "hello", "msgtype": "m.text"},
    }
    event.body = "hello"

    with patch("mindroom.bot.maybe_handle_tool_approval_reply", new=AsyncMock(return_value=False)):
        await bot._on_message(room, event)

    bot._turn_controller.handle_text_event.assert_not_awaited()
```

Add this test in `tests/test_sync_task_cancellation.py`:

```python
@pytest.mark.asyncio
async def test_on_media_message_drops_media_after_entity_shutdown_starts() -> None:
    bot = object.__new__(AgentBot)
    bot.agent_user = MagicMock(agent_name="test_agent")
    bot._entity_shutdown_prepared = True
    bot.logger = MagicMock()
    bot._turn_controller = MagicMock()
    bot._turn_controller.handle_media_event = AsyncMock()

    room = MagicMock()
    room.room_id = "!room:example.com"
    event = MagicMock()
    event.event_id = "$media"
    event.sender = "@user:example.com"
    event.source = {
        "origin_server_ts": int(time.time() * 1000),
        "content": {"body": "image", "msgtype": "m.image"},
    }

    await bot._on_media_message(room, event)

    bot._turn_controller.handle_media_event.assert_not_awaited()
```

- [x] **Step 2: Write failing coalesced-batch and interactive guard tests**

Add this test in `tests/test_live_message_coalescing.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_coalesced_batch_drops_after_entity_shutdown_starts() -> None:
    from mindroom.coalescing_batch import CoalescedBatch

    closed: list[str] = []
    metadata = PendingDispatchMetadata(
        kind="test",
        payload=None,
        close=lambda: closed.append("closed"),
    )

    bot = object.__new__(AgentBot)
    bot._entity_shutdown_prepared = True
    bot.logger = MagicMock()
    bot._turn_controller = MagicMock()
    bot._turn_controller.handle_coalesced_batch = AsyncMock()
    room = MagicMock()
    room.room_id = "!room:example.com"
    event = _text_event(event_id="$event", sender="@user:example.com", body="hello")
    batch = CoalescedBatch(
        room=room,
        primary_event=event,
        requester_user_id="@user:example.com",
        pending_events=(),
        prompt="hello",
        source_kind="text",
        dispatch_policy_source_kind=None,
        hook_source=None,
        message_received_depth=0,
        attachment_ids=[],
        source_event_ids=["$event"],
        source_event_prompts={"$event": "hello"},
        media_events=[],
        dispatch_metadata=(metadata,),
    )

    await bot._dispatch_coalesced_batch(batch)

    bot._turn_controller.handle_coalesced_batch.assert_not_awaited()
    assert closed == ["closed"]
```

Add this test in `tests/test_turn_controller.py`:

```python
@pytest.mark.asyncio
async def test_handle_media_event_drops_when_response_ingress_closed(tmp_path: Path) -> None:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    replace_turn_controller_deps(bot, accepts_response_work=lambda: False)
    bot._turn_controller._enqueue_media_for_dispatch = AsyncMock()
    room = MagicMock()
    room.room_id = "!test:localhost"
    event = MagicMock()
    event.event_id = "$media:localhost"
    event.sender = "@user:localhost"
    event.source = {"content": {"msgtype": "m.image", "body": "image"}}

    await bot._turn_controller.handle_media_event(room, event)

    bot._turn_controller._enqueue_media_for_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_selection_drops_before_ack_when_response_ingress_closed(
    tmp_path: Path,
) -> None:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    bot._entity_shutdown_prepared = True
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$ack:localhost")
    replace_turn_controller_deps(
        bot,
        delivery_gateway=bot._delivery_gateway,
        accepts_response_work=lambda: False,
    )
    generate_response_mock = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, generate_response_mock)
    room = MagicMock()
    room.room_id = "!test:localhost"
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        selection_key="1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )

    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id="@user:localhost",
    )

    bot._delivery_gateway.send_text.assert_not_awaited()
    generate_response_mock.assert_not_awaited()
```

- [x] **Step 3: Run the failing ingress tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_on_message_drops_text_after_entity_shutdown_starts tests/test_sync_task_cancellation.py::test_on_media_message_drops_media_after_entity_shutdown_starts tests/test_live_message_coalescing.py::test_dispatch_coalesced_batch_drops_after_entity_shutdown_starts tests/test_turn_controller.py::test_handle_media_event_drops_when_response_ingress_closed tests/test_turn_controller.py::test_interactive_selection_drops_before_ack_when_response_ingress_closed -q -x
```

Expected: fail because `_on_message()`, `_on_media_message()`, `_dispatch_coalesced_batch()`, and `handle_interactive_selection()` do not guard closed response ingress.

- [x] **Step 4: Implement bot-level ingress guards**

In `src/mindroom/bot.py`, import:

```python
from mindroom.coalescing_batch import close_coalesced_batch_metadata
```

Add helper:

```python
def _response_ingress_closed(self) -> bool:
    """Return whether this bot must not admit new response-producing work."""
    return self._entity_shutdown_prepared
```

At the top of `_dispatch_coalesced_batch()`:

```python
if self._response_ingress_closed():
    close_coalesced_batch_metadata(batch)
    self.logger.info(
        "Dropping coalesced batch during entity shutdown",
        source_event_ids=batch.source_event_ids,
        room_id=batch.room.room_id,
    )
    return
```

At the top of `_on_message()` after `_log_matrix_event_callback_started()`:

```python
if self._response_ingress_closed():
    self.logger.info(
        "Dropping inbound message during entity shutdown",
        event_id=event.event_id,
        room_id=room.room_id,
    )
    return
```

At the top of `_on_media_message()` after `_log_matrix_event_callback_started()`:

```python
if self._response_ingress_closed():
    self.logger.info(
        "Dropping inbound media during entity shutdown",
        event_id=event.event_id,
        room_id=room.room_id,
    )
    return
```

- [x] **Step 5: Add turn-controller guard for already-dispatched callbacks**

In `src/mindroom/turn_controller.py`, import `Callable` under `TYPE_CHECKING` or from `collections.abc`, then add a field to `TurnControllerDeps`:

```python
accepts_response_work: Callable[[], bool]
```

In `src/mindroom/bot.py`, pass it when constructing `TurnControllerDeps`:

```python
accepts_response_work=lambda: not self._response_ingress_closed(),
```

In `TurnController`, add:

```python
def _accepts_response_work(self) -> bool:
    return self.deps.accepts_response_work()
```

At the start of `handle_text_event()`:

```python
if not self._accepts_response_work():
    self.deps.logger.info("Dropping text event because response ingress is closed")
    return
```

At the start of `handle_media_event()`:

```python
if not self._accepts_response_work():
    self.deps.logger.info("Dropping media event because response ingress is closed")
    return
```

At the start of `handle_coalesced_batch()`:

```python
if not self._accepts_response_work():
    close_coalesced_batch_metadata(batch)
    self.deps.logger.info(
        "Dropping coalesced batch because response ingress is closed",
        source_event_ids=batch.source_event_ids,
    )
    return
```

Before each call to `self.deps.response_runner.generate_response()` and `self.deps.response_runner.generate_team_response_helper()` in `_dispatch_text_message()`:

```python
if not self._accepts_response_work():
    return
```

At the very start of `handle_interactive_selection()`, before fetching history or sending the visible acknowledgment:

```python
if not self._accepts_response_work():
    self.deps.logger.info("Dropping interactive selection because response ingress is closed")
    return
```

In `_handle_media_message_inner()`, add the same guard immediately before `_enqueue_media_for_dispatch()`:

```python
if not self._accepts_response_work():
    self.deps.logger.info("Dropping media dispatch because response ingress is closed")
    return
```

- [x] **Step 6: Run ingress tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_on_message_drops_text_after_entity_shutdown_starts tests/test_sync_task_cancellation.py::test_on_media_message_drops_media_after_entity_shutdown_starts tests/test_live_message_coalescing.py::test_dispatch_coalesced_batch_drops_after_entity_shutdown_starts tests/test_turn_controller.py::test_handle_media_event_drops_when_response_ingress_closed tests/test_turn_controller.py::test_interactive_selection_drops_before_ack_when_response_ingress_closed -q -x
```

Expected: pass.

- [x] **Step 7: Commit**

```bash
git add src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/coalescing_batch.py tests/test_sync_task_cancellation.py tests/test_live_message_coalescing.py tests/test_turn_controller.py
git commit -m "Close response ingress during entity teardown"
```

---

### Task 6: Reorder Entity Teardown Around the Closed-Ingress Boundary

**Files:**
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/orchestration/runtime.py`
- Modify: `src/mindroom/orchestrator.py`
- Test: `tests/test_sync_task_cancellation.py`

- [x] **Step 1: Write failing teardown ordering test**

Replace `test_prepare_for_entity_shutdown_cancels_active_responses_once()` with:

```python
@pytest.mark.asyncio
async def test_prepare_for_entity_shutdown_closes_ingress_discards_coalescing_then_cancels() -> None:
    bot = _prepare_only_agent_bot()
    calls: list[str] = []
    bot._coalescing_gate.discard_all = AsyncMock(side_effect=lambda: calls.append("discard_coalescing"))
    bot._coalescing_gate.drain_all = AsyncMock(side_effect=lambda: calls.append("drain_coalescing"))
    bot.stop_manager.cancel_active_responses = AsyncMock(side_effect=lambda **_: calls.append("cancel_responses") or 1)

    await bot.prepare_for_entity_shutdown(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
    await bot.prepare_for_entity_shutdown(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)

    assert bot._entity_shutdown_prepared is True
    assert calls == ["discard_coalescing", "cancel_responses"]
    bot.stop_manager.cancel_active_responses.assert_awaited_once_with(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
    bot._coalescing_gate.drain_all.assert_not_awaited()
```

Add this regression test in `tests/test_sync_task_cancellation.py`:

```python
@pytest.mark.asyncio
async def test_stop_entities_uses_entity_teardown_for_responses_and_sync_restart_for_supervisor() -> None:
    response_cancel_messages: list[str | None] = []
    sync_cancel_messages: list[str | None] = []

    mock_bot = AsyncMock()
    mock_bot.prepare_for_entity_shutdown = AsyncMock(
        side_effect=lambda *, cancel_msg: response_cancel_messages.append(cancel_msg),
    )
    mock_bot.stop = AsyncMock()
    sync_task = asyncio.create_task(asyncio.sleep(60))

    async def fake_cancel_sync_task(
        entity_name: str,
        sync_tasks: dict[str, asyncio.Task],
        *,
        cancel_msg: str | None = None,
    ) -> None:
        sync_cancel_messages.append(cancel_msg)
        task = sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with patch("mindroom.orchestration.runtime.cancel_sync_task", side_effect=fake_cancel_sync_task):
        await stop_entities({"agent1"}, {"agent1": mock_bot}, {"agent1": sync_task})

    assert response_cancel_messages == [ENTITY_TEARDOWN_CANCEL_MSG]
    assert sync_cancel_messages == [SYNC_RESTART_CANCEL_MSG]
```

Add this full-shutdown ordering test in `tests/test_sync_task_cancellation.py`:

```python
@pytest.mark.asyncio
async def test_orchestrator_stop_prepares_entity_shutdown_before_cancelling_sync_tasks() -> None:
    orchestrator = object.__new__(_MultiAgentOrchestrator)
    orchestrator.running = True
    orchestrator._runtime_shutdown_event = None
    orchestrator._sync_tasks = {"agent1": asyncio.create_task(asyncio.sleep(60))}
    orchestrator.agent_bots = {"agent1": AsyncMock()}
    orchestrator.agent_bots["agent1"].prepare_for_entity_shutdown = AsyncMock()
    orchestrator.agent_bots["agent1"].stop = AsyncMock()
    orchestrator._cancel_config_reload_task = AsyncMock()
    orchestrator._stop_memory_auto_flush_worker = AsyncMock()
    orchestrator._knowledge_source_watcher = AsyncMock()
    orchestrator._knowledge_refresh_scheduler = AsyncMock()
    orchestrator._cancel_bot_start_tasks = AsyncMock()
    orchestrator._stop_mcp_manager = AsyncMock()
    orchestrator._close_runtime_support_services = AsyncMock()
    call_order: list[str] = []

    orchestrator.agent_bots["agent1"].prepare_for_entity_shutdown.side_effect = (
        lambda *, cancel_msg: call_order.append(f"prepare:{cancel_msg}")
    )

    async def fake_cancel_sync_task(
        entity_name: str,
        sync_tasks: dict[str, asyncio.Task],
        *,
        cancel_msg: str | None = None,
    ) -> None:
        call_order.append(f"cancel_sync:{cancel_msg}")
        task = sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with (
        patch("mindroom.orchestrator.shutdown_approval_runtime", new=AsyncMock()),
        patch("mindroom.orchestrator.cancel_sync_task", side_effect=fake_cancel_sync_task),
    ):
        await orchestrator.stop()

    assert call_order == [
        f"prepare:{SYNC_RESTART_CANCEL_MSG}",
        f"cancel_sync:{SYNC_RESTART_CANCEL_MSG}",
    ]
```

- [x] **Step 2: Run the failing ordering test**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_prepare_for_entity_shutdown_closes_ingress_discards_coalescing_then_cancels tests/test_sync_task_cancellation.py::test_stop_entities_uses_entity_teardown_for_responses_and_sync_restart_for_supervisor tests/test_sync_task_cancellation.py::test_orchestrator_stop_prepares_entity_shutdown_before_cancelling_sync_tasks -q -x
```

Expected: fail because `prepare_for_entity_shutdown()` has no `cancel_msg` parameter, `stop_entities()` does not pass entity-teardown provenance, and `orchestrator.stop()` cancels sync tasks before closing entity ingress.

- [x] **Step 3: Update bot teardown methods**

In `src/mindroom/bot.py`, replace `prepare_for_sync_shutdown()` and `prepare_for_entity_shutdown()` with:

```python
async def prepare_for_sync_shutdown(self) -> None:
    """Cancel work that must not outlive one Matrix sync loop."""
    self._sync_shutting_down = True
    await self._cancel_startup_thread_prewarm()
    await self._coalescing_gate.drain_all()
    if self._sync_trust_state is SyncTrustState.CERTIFIED:
        self._save_sync_checkpoint(self._sync_checkpoint)
    if self.agent_name != ROUTER_AGENT_NAME:
        return

    await self._cancel_deferred_overdue_task_drain()

async def prepare_for_entity_shutdown(self, *, cancel_msg: str | None) -> None:
    """Close ingress and cancel work that must not outlive entity/client teardown."""
    if self._entity_shutdown_prepared:
        return
    self._entity_shutdown_prepared = True
    self._sync_shutting_down = True
    await self._cancel_startup_thread_prewarm()
    await self._coalescing_gate.discard_all()
    if self._sync_trust_state is SyncTrustState.CERTIFIED:
        self._save_sync_checkpoint(self._sync_checkpoint)
    if self.agent_name == ROUTER_AGENT_NAME:
        await self._cancel_deferred_overdue_task_drain()
    await self.stop_manager.cancel_active_responses(cancel_msg=cancel_msg)
```

Update the `stop()` signature so callers choose response provenance explicitly instead of deriving it from a broad reason string:

```python
async def stop(
    self,
    *,
    reason: str | None = None,
    response_cancel_msg: str | None = ENTITY_TEARDOWN_CANCEL_MSG,
) -> None:
```

Replace the current `await self.prepare_for_entity_shutdown()` call inside `stop()` with:

```python
await self.prepare_for_entity_shutdown(cancel_msg=response_cancel_msg)
```

This makes service shutdown, config reload, and entity removal call sites choose provenance at the lifecycle boundary.

- [x] **Step 4: Update runtime stop ordering and provenance**

In `src/mindroom/orchestration/runtime.py`, import `ENTITY_TEARDOWN_CANCEL_MSG`.

In `stop_entities()`, keep the existing order but pass the explicit non-resumable response cancel source:

```python
await bot.prepare_for_entity_shutdown(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
```

Keep sync task cancellation at:

```python
await cancel_sync_task(entity_name, sync_tasks, cancel_msg=SYNC_RESTART_CANCEL_MSG)
```

That cancel source is for the sync supervisor task only, not response tasks.

- [x] **Step 5: Update orchestrator shutdown and direct entity removal**

In `src/mindroom/orchestrator.py`, update full service shutdown so it prepares every bot with restart-resumable provenance before cancelling sync supervisors:

```python
for bot in self.agent_bots.values():
    await bot.prepare_for_entity_shutdown(cancel_msg=SYNC_RESTART_CANCEL_MSG)

# Cancel sync tasks after ingress is closed so sync-supervisor finally blocks cannot dispatch queued work.
for entity_name in list(self._sync_tasks.keys()):
    await cancel_sync_task(entity_name, self._sync_tasks, cancel_msg=SYNC_RESTART_CANCEL_MSG)
```

Update the final stop call to avoid reclassifying already-prepared response work:

```python
stop_tasks = [
    bot.stop(reason="shutdown", response_cancel_msg=SYNC_RESTART_CANCEL_MSG)
    for bot in self.agent_bots.values()
]
```

In `src/mindroom/orchestrator.py`, entity removal calls `bot.cleanup()`, and `cleanup()` calls `stop(reason="entity_removed")`.

Update `cleanup()` or its `stop()` call so removed entities use non-resumable provenance:

```python
await self.stop(reason="entity_removed", response_cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
```

- [x] **Step 6: Run focused lifecycle tests**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py::test_prepare_for_entity_shutdown_closes_ingress_discards_coalescing_then_cancels tests/test_sync_task_cancellation.py::test_stop_entities_uses_entity_teardown_for_responses_and_sync_restart_for_supervisor tests/test_sync_task_cancellation.py::test_orchestrator_stop_prepares_entity_shutdown_before_cancelling_sync_tasks tests/test_sync_task_cancellation.py::test_sync_forever_with_restart_restarts_stalled_sync tests/test_sync_task_cancellation.py::test_sync_forever_with_restart_retries_on_sync_restart_cancel tests/test_sync_task_cancellation.py::test_stop_entities_prepares_bots_before_cancelling_sync_tasks -q -x
```

Expected: pass.

- [x] **Step 7: Commit**

```bash
git add src/mindroom/bot.py src/mindroom/orchestration/runtime.py src/mindroom/orchestrator.py tests/test_sync_task_cancellation.py
git commit -m "Order entity teardown around closed ingress"
```

---

### Task 7: Verify the Whole Lifecycle Boundary and Remove Half-Refactor Traces

**Files:**
- Inspect: `src/mindroom/bot.py`
- Inspect: `src/mindroom/orchestration/runtime.py`
- Inspect: `src/mindroom/stop.py`
- Inspect: `src/mindroom/streaming.py`
- Inspect: `src/mindroom/matrix/stale_stream_cleanup.py`
- Inspect: `tests/test_sync_task_cancellation.py`
- Inspect: `tests/test_stale_stream_cleanup.py`
- Inspect: `tests/test_live_message_coalescing.py`
- Inspect: `tests/test_streaming_finalize.py`
- Inspect: `tests/test_turn_controller.py`

- [x] **Step 1: Search for stale helper and old wrapper traces**

Run:

```bash
rg "_append_interrupted_thread|_extract_partial_text" src tests
```

Expected: no output.

- [x] **Step 2: Search for response cancellation without explicit provenance**

Run:

```bash
rg "cancel_active_responses\\(" src tests
```

Expected production call sites:

```text
src/mindroom/bot.py: await self.stop_manager.cancel_active_responses(cancel_msg=cancel_msg)
```

Test call sites may pass `SYNC_RESTART_CANCEL_MSG`, `ENTITY_TEARDOWN_CANCEL_MSG`, or `USER_STOP_CANCEL_MSG` depending on the test.

- [x] **Step 3: Search for entity teardown calls without `cancel_msg`**

Run:

```bash
rg "prepare_for_entity_shutdown\\(" src tests
```

Expected production call sites must include `cancel_msg=`.

Also verify config/MCP restarts do not pass restart-resumable provenance to response cancellation:

```bash
rg "prepare_for_entity_shutdown\\(cancel_msg=SYNC_RESTART_CANCEL_MSG\\)" src/mindroom
```

Expected production call sites:

```text
src/mindroom/orchestrator.py: full service shutdown only
```

- [x] **Step 4: Run focused lifecycle suites**

Run:

```bash
uv run pytest tests/test_sync_task_cancellation.py -q -x
uv run pytest tests/test_stale_stream_cleanup.py -q -n 0 --no-cov
uv run pytest tests/test_live_message_coalescing.py -q -x
uv run pytest tests/test_streaming_finalize.py -q -x
uv run pytest tests/test_turn_controller.py -q -x
```

Expected:

```text
tests/test_sync_task_cancellation.py: all non-skipped tests pass
tests/test_stale_stream_cleanup.py: 50 or more tests pass
tests/test_live_message_coalescing.py: all non-skipped tests pass
tests/test_streaming_finalize.py: all non-skipped tests pass
tests/test_turn_controller.py: all non-skipped tests pass
```

- [x] **Step 5: Run touched-file lint and type checks**

Run:

```bash
uv run ruff check src/mindroom/cancellation.py src/mindroom/constants.py src/mindroom/streaming.py src/mindroom/matrix/stale_stream_cleanup.py src/mindroom/coalescing.py src/mindroom/coalescing_batch.py src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/orchestration/runtime.py src/mindroom/orchestrator.py tests/test_sync_task_cancellation.py tests/test_stale_stream_cleanup.py tests/test_live_message_coalescing.py tests/test_streaming_finalize.py tests/test_turn_controller.py
```

Expected:

```text
All checks passed!
```

- [x] **Step 6: Run diff hygiene**

Run:

```bash
git diff --check
git status --short
```

Expected:

```text
git diff --check: no output
git status --short: only intended files are modified
```

- [x] **Step 7: Commit final cleanup**

Only commit if Step 1 through Step 6 are clean.

```bash
git add src/mindroom/cancellation.py src/mindroom/constants.py src/mindroom/streaming.py src/mindroom/matrix/stale_stream_cleanup.py src/mindroom/coalescing.py src/mindroom/coalescing_batch.py src/mindroom/bot.py src/mindroom/turn_controller.py src/mindroom/orchestration/runtime.py src/mindroom/orchestrator.py tests/test_sync_task_cancellation.py tests/test_stale_stream_cleanup.py tests/test_live_message_coalescing.py tests/test_streaming_finalize.py tests/test_turn_controller.py
git commit -m "Verify entity teardown lifecycle boundary"
```

---

## Design Notes

- Do not auto-resume config reload cancellations unless this PR also adds an immediate same-process resume relay. This plan does not add that feature.
- Do not make `prepare_for_sync_shutdown()` cancel responses. Same-process sync retry must remain response-preserving.
- Do not use `SYNC_RESTART_CANCEL_MSG` for config reload or entity removal response tasks. That source is reserved for service/process restart work that startup cleanup may resume.
- Do not rely only on cancelling the Matrix sync task to stop ingress. nio callbacks are dispatched as background work, and queued coalesced batches can outlive the sync loop that received them.
- Preserve the legacy generic-interrupted startup resume fallback only when there is no explicit cancellation source metadata.

## Self-Review

- Spec coverage: The plan covers the repeated review findings: config reload provenance, entity removal provenance, sync retry preserving active responses, queued coalescing dispatch during teardown, and direct Matrix callback ingress during teardown.
- Placeholder scan: The plan contains no placeholder markers or unspecified edge-case instructions.
- Type consistency: The same `CancelSource`, `ENTITY_TEARDOWN_CANCEL_MSG`, and `RESPONSE_CANCEL_SOURCE_KEY` names are used consistently across production and test tasks.
