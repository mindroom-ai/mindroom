"""Independent root preparation remains bounded and owned by coalescing."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.coalescing import CoalescingGate, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescingKey, PreparedTurn, RequesterCoalescingOwner
from mindroom.dispatch_handoff import PendingDispatchMetadata
from mindroom.matrix.conversation_reads import ThreadReadMode
from tests.conftest import make_pending_event
from tests.test_turn_controller_focused import _build_harness, _room_with_members, _single_agent_config, _text_event

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.conversation_resolver import DispatchContextResult
    from mindroom.dispatch_handoff import DispatchEvent, DispatchPayloadMetadata
    from mindroom.matrix.media import MatrixMediaEvent


_KEY = CoalescingKey("!room:localhost", None, RequesterCoalescingOwner("@user:localhost"))


async def _admit_root(
    gate: CoalescingGate,
    event_id: str,
    *,
    key: CoalescingKey = _KEY,
    close: Callable[[], None] | None = None,
) -> None:
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1_000_000,
            "content": {"msgtype": "m.text", "body": event_id},
        },
    )
    pending = make_pending_event(event, nio.MatrixRoom(key.room_id, "@mindroom:localhost"), source_kind="message")
    if close is not None:
        pending.dispatch_metadata = (PendingDispatchMetadata(kind="test", payload=object(), close=close),)
    await gate.admit(
        key,
        source_event_id=event_id,
        source_kind="message",
        ready_result=ReadyPendingEvent(pending_event=pending),
    )


@pytest.mark.asyncio
async def test_independent_root_starts_while_same_key_preparation_is_blocked() -> None:
    """One root's preparation must not serialize unrelated roots from its sender."""
    started = {event_id: asyncio.Event() for event_id in ("$a", "$b")}
    release = asyncio.Event()
    turns: list[PreparedTurn] = []

    async def dispatch(turn: PreparedTurn) -> None:
        turns.append(turn)
        started[turn.event.event_id].set()
        await release.wait()

    gate = CoalescingGate(dispatch_turn=dispatch, debounce_seconds=lambda: 0.0, is_shutting_down=lambda: False)
    try:
        await _admit_root(gate, "$a")
        await asyncio.wait_for(started["$a"].wait(), timeout=1)
        await _admit_root(gate, "$b")
        await asyncio.wait_for(started["$b"].wait(), timeout=1)
        assert gate.has_pending_source_event("$a")
        assert gate.has_pending_source_event("$b")
        assert [turn.ingress.coalescing_key for turn in turns] == [_KEY, _KEY]
    finally:
        release.set()
        await gate.drain_all()

    assert not gate.has_pending_source_event("$a")
    assert not gate.has_pending_source_event("$b")


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_rooms", [False, True])
async def test_ninth_root_waits_for_one_of_eight_active_preparations(separate_rooms: bool) -> None:
    """Root fan-out bounds active preparation without losing queued ownership."""
    started = [asyncio.Event() for _ in range(9)]
    release = [asyncio.Event() for _ in range(9)]
    active: set[int] = set()
    peak = 0

    async def dispatch(turn: PreparedTurn) -> None:
        nonlocal peak
        index = int(turn.event.event_id[1:])
        active.add(index)
        peak = max(peak, len(active))
        started[index].set()
        try:
            await release[index].wait()
        finally:
            active.remove(index)

    gate = CoalescingGate(dispatch_turn=dispatch, debounce_seconds=lambda: 0.0, is_shutting_down=lambda: False)
    try:
        for index in range(9):
            key = CoalescingKey(f"!room{index}:localhost", None, _KEY.owner) if separate_rooms else _KEY
            await _admit_root(gate, f"${index}", key=key)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started[:8])), timeout=1)
        assert not started[8].is_set()
        assert all(gate.has_pending_source_event(f"${index}") for index in range(9))
        release[3].set()
        await asyncio.wait_for(started[8].wait(), timeout=1)
        assert peak == 8
    finally:
        for event in release:
            event.set()
        await gate.drain_all()
    assert not active
    assert not any(gate.has_pending_source_event(f"${index}") for index in range(9))


@pytest.mark.asyncio
async def test_drain_waits_for_detached_root_preparation() -> None:
    """Shutdown cannot report completion while a detached root owns its source."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def dispatch(_turn: PreparedTurn) -> None:
        started.set()
        await release.wait()
        finished.set()

    gate = CoalescingGate(dispatch_turn=dispatch, debounce_seconds=lambda: 0.0, is_shutting_down=lambda: False)
    await _admit_root(gate, "$a")
    await asyncio.wait_for(started.wait(), timeout=1)
    drain = asyncio.create_task(gate.drain_all())
    try:
        await asyncio.sleep(0)
        assert not drain.done()
        assert gate.has_pending_source_event("$a")
    finally:
        release.set()
        result = await asyncio.wait_for(drain, timeout=1)
    assert result.completed
    assert finished.is_set()
    assert not gate.has_pending_source_event("$a")


@pytest.mark.asyncio
async def test_drain_started_before_root_dispatch_registration_waits_for_child() -> None:
    """A queue drain may create children after shutdown took its first task snapshot."""
    admission_waiting = asyncio.Event()
    allow_dispatch = asyncio.Event()
    preparation_started = asyncio.Event()
    release = asyncio.Event()

    async def wait_allowed(_key: CoalescingKey) -> None:
        admission_waiting.set()
        await allow_dispatch.wait()

    async def dispatch(_turn: PreparedTurn) -> None:
        preparation_started.set()
        await release.wait()

    gate = CoalescingGate(
        dispatch_turn=dispatch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_allowed,
    )
    await _admit_root(gate, "$a")
    await asyncio.wait_for(admission_waiting.wait(), timeout=1)
    drain = asyncio.create_task(gate.drain_all())
    try:
        await asyncio.sleep(0)
        allow_dispatch.set()
        await asyncio.wait_for(preparation_started.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(drain), timeout=0.02)
        assert gate.has_pending_source_event("$a")
    finally:
        allow_dispatch.set()
        release.set()
        result = await asyncio.wait_for(drain, timeout=1)
    assert result.completed


@pytest.mark.asyncio
async def test_bounded_drain_cancels_detached_roots_and_closes_metadata_once() -> None:
    """Every detached owner participates in the shared shutdown budget."""
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    cancelled: list[str] = []
    closed: list[str] = []

    async def dispatch(turn: PreparedTurn) -> None:
        started[int(turn.event.event_id[1:])].set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(turn.event.event_id)
            raise

    gate = CoalescingGate(dispatch_turn=dispatch, debounce_seconds=lambda: 0.0, is_shutting_down=lambda: False)
    try:
        await _admit_root(gate, "$0", close=lambda: closed.append("$0"))
        await _admit_root(gate, "$1", close=lambda: closed.append("$1"))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=1)
        result = await asyncio.wait_for(gate.drain_all(ready_timeout_seconds=0.01), timeout=1)
        assert not result.completed
        assert result.dispatch_cancelled_count == 2
        assert sorted(cancelled) == ["$0", "$1"]
        assert sorted(closed) == ["$0", "$1"]
        await gate.drain_all()
        assert sorted(closed) == ["$0", "$1"]
    finally:
        release.set()
        await gate.drain_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(("thread_id", "single_conversation"), [("$thread", False), (None, True)])
async def test_shared_conversation_preparation_keeps_fifo_order(
    thread_id: str | None,
    single_conversation: bool,
) -> None:
    """Explicit threads and configured room conversations remain sequential."""
    key = CoalescingKey(_KEY.room_id, thread_id, _KEY.owner)
    started: list[str] = []
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(turn: PreparedTurn) -> None:
        started.append(turn.event.event_id)
        if turn.event.event_id == "$a":
            first_started.set()
            await release.wait()
        else:
            second_started.set()

    gate = CoalescingGate(
        dispatch_turn=dispatch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        room_scope_is_single_conversation=lambda _room: single_conversation,
    )
    try:
        await _admit_root(gate, "$a", key=key)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await _admit_root(gate, "$b", key=key)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_started.wait(), timeout=0.02)
        assert gate.has_pending_source_event("$b")
    finally:
        release.set()
        await gate.drain_all()
    assert started == ["$a", "$b"]


@pytest.mark.asyncio
async def test_expired_shutdown_budget_closes_waiting_root_metadata_once() -> None:
    """Shutdown closes a root waiting for preparation capacity as well as active roots."""
    started: list[str] = []
    closed: list[str] = []
    capacity_full = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(turn: PreparedTurn) -> None:
        started.append(turn.event.event_id)
        if len(started) == 8:
            capacity_full.set()
        await release.wait()

    gate = CoalescingGate(dispatch_turn=dispatch, debounce_seconds=lambda: 0.0, is_shutting_down=lambda: False)
    try:
        for index in range(9):
            event_id = f"${index}"
            await _admit_root(gate, event_id, close=lambda event_id=event_id: closed.append(event_id))
        await asyncio.wait_for(capacity_full.wait(), timeout=1)
        async with asyncio.timeout(1):
            result = await gate.drain_all(ready_timeout_seconds=0)
        assert not result.completed
        assert "$8" not in started
        assert sorted(closed) == ["$0", "$1", "$2", "$3", "$4", "$5", "$6", "$7", "$8"]
        assert not gate.has_pending_source_event("$8")
        await gate.drain_all()
        assert len(closed) == 9
    finally:
        release.set()
        await gate.drain_all()


@pytest.mark.asyncio
async def test_failed_root_releases_ownership_and_requests_retry_once() -> None:
    """Detached dispatch errors retain the existing retry and metadata contract."""
    closed: list[str] = []
    retry_sources: list[tuple[str, ...]] = []

    async def dispatch(_turn: PreparedTurn) -> None:
        message = "preparation failed"
        raise RuntimeError(message)

    gate = CoalescingGate(
        dispatch_turn=dispatch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        on_dispatch_failure=lambda events: retry_sources.append(tuple(event.event.event_id for event in events)),
    )
    await _admit_root(gate, "$a", close=lambda: closed.append("$a"))
    result = await gate.drain_all()
    assert not result.completed
    assert result.dispatch_failure_count == 1
    assert retry_sources == [("$a",)]
    assert closed == ["$a"]
    assert not gate.has_pending_source_event("$a")


@pytest.mark.asyncio
async def test_caption_joins_room_media_while_an_earlier_root_prepares() -> None:
    """Independent preparation does not split a later attachment from its caption."""
    first_started = asyncio.Event()
    media_started = asyncio.Event()
    release = asyncio.Event()
    batches: list[PreparedTurn] = []

    async def dispatch(turn: PreparedTurn) -> None:
        batches.append(turn)
        if turn.event.event_id == "$a":
            first_started.set()
            await release.wait()
        else:
            media_started.set()

    gate = CoalescingGate(dispatch_turn=dispatch, debounce_seconds=lambda: 60.0, is_shutting_down=lambda: False)
    image = nio.RoomMessageImage.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$image",
            "sender": "@user:localhost",
            "origin_server_ts": 1_000_000,
            "content": {"msgtype": "m.image", "body": "photo.jpg", "url": "mxc://localhost/photo"},
        },
    )
    pending = make_pending_event(image, nio.MatrixRoom(_KEY.room_id, "@mindroom:localhost"), source_kind="image")
    try:
        await _admit_root(gate, "$a")
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await gate.admit(
            _KEY,
            source_event_id="$image",
            source_kind="image",
            ready_result=ReadyPendingEvent(pending_event=pending),
        )
        await _admit_root(gate, "$caption")
        await asyncio.wait_for(media_started.wait(), timeout=1)
        assert [batch.handled_turn.source_event_ids for batch in batches] == [("$a",), ("$image", "$caption")]
        assert [event.event_id for event in batches[1].media_events] == ["$image"]
    finally:
        release.set()
        await gate.drain_all()


@pytest.mark.asyncio
async def test_controller_prepares_independent_roots_without_changing_reply_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real controller preparation can overlap while each response keeps its own root."""
    config = _single_agent_config(tmp_path, "thread")
    harness = _build_harness(config, tmp_path)
    room = _room_with_members(config, "general")
    started = {event_id: asyncio.Event() for event_id in ("$a", "$b")}
    release = asyncio.Event()
    resolver = harness.controller.deps.resolver
    original_extract = resolver.extract_dispatch_context

    async def extract(
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
        mode: ThreadReadMode = ThreadReadMode.NONBLOCKING,
    ) -> DispatchContextResult:
        started[event.event_id].set()
        if event.event_id == "$a":
            await release.wait()
        return await original_extract(room, event, payload_metadata=payload_metadata, mode=mode)

    monkeypatch.setattr(resolver, "extract_dispatch_context", extract)
    try:
        await harness.controller.handle_text_event(room, _text_event("@general first", event_id="$a"))
        await asyncio.wait_for(started["$a"].wait(), timeout=1)
        await harness.controller.handle_text_event(room, _text_event("@general second", event_id="$b"))
        await asyncio.wait_for(started["$b"].wait(), timeout=1)
        assert harness.gate.has_pending_source_event("$a")
    finally:
        release.set()
        await harness.gate.drain_all()
        await harness.runner.settle_inbox_responses()
        await harness.journal_store.close()

    targets = {request.response_envelope.target.resolved_thread_id for request in harness.runner.requests}
    assert targets == {"$a", "$b"}
    for turn in harness.gate_batches:
        assert turn.ingress.coalescing_key is not None
        assert turn.ingress.coalescing_key.thread_id is None
