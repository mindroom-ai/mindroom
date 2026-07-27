"""Retry invariants for canonical TurnRecord terminal checkpoints."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.handled_turns import TurnRecord
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.response_identity import ResponseIdentity
from mindroom.terminal_delivery import (
    TerminalDeliveryCoordinator,
    TerminalDeliveryCoordinatorDeps,
    TerminalDeliveryIntent,
)
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import message_origin

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:localhost"
SOURCE = "$source"
TARGET = "$visible"


class _Effects:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.fail_once = False

    async def register_interactive_delivery(self, **kwargs: object) -> None:
        self.keys.append(str(kwargs["idempotency_key"]))
        if self.fail_once:
            self.fail_once = False
            message = "reaction failed"
            raise OSError(message)


def _target(*, room_id: str = ROOM, source_event_id: str = SOURCE) -> MessageTarget:
    return MessageTarget.resolve(room_id, f"$thread-{source_event_id}", source_event_id)


def _envelope(*, room_id: str = ROOM, source_event_id: str = SOURCE) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=source_event_id,
        target=_target(room_id=room_id, source_event_id=source_event_id),
        body="question",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="code",
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )


def _metadata() -> InteractiveMetadata:
    metadata = InteractiveMetadata.from_parts(
        {"✅": "yes", "1": "yes"},
        [{"emoji": "✅", "label": "Yes", "value": "yes"}],
        question_text="Proceed?",
    )
    assert metadata is not None
    return metadata


def _intent(
    *,
    source_event_id: str = SOURCE,
    target_event_id: str = TARGET,
    room_id: str = ROOM,
    interactive: bool = False,
    correlation_id: str = "corr-1",
) -> TerminalDeliveryIntent:
    return TerminalDeliveryIntent(
        target_event_id=target_event_id,
        target_was_placeholder=True,
        identity=ResponseIdentity(
            response_kind="ai",
            response_envelope=_envelope(room_id=room_id, source_event_id=source_event_id),
            correlation_id=correlation_id,
            source_event_ids=(source_event_id,),
        ),
        interactive_metadata=_metadata() if interactive else None,
        body=f"final answer {source_event_id}",
        wire_content={"body": f"* final {source_event_id}", "m.new_content": {"body": source_event_id}},
    )


def _record_pending(
    store: TurnStore,
    *,
    source_event_id: str = SOURCE,
    room_id: str = ROOM,
    discovery_event_ids: tuple[str, ...] = (),
) -> None:
    pending = store.record_pending_turn(
        TurnRecord.create(
            [source_event_id],
            discovery_event_ids=discovery_event_ids,
            completed=False,
            response_owner="code",
            requester_id="@user:localhost",
            correlation_id="corr-1",
            conversation_target=_target(room_id=room_id, source_event_id=source_event_id),
        ),
    )
    assert pending is not None


def _store(tmp_path: Path) -> TurnStore:
    store = TurnStore(
        TurnStoreDeps(
            agent_name="code",
            tracking_base_path=tmp_path / "tracking",
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    _record_pending(store)
    return store


def _coordinator(
    store: TurnStore,
    *,
    ready: bool = True,
    effects: _Effects | None = None,
) -> tuple[TerminalDeliveryCoordinator, AsyncMock, _Effects]:
    hooks = AsyncMock()
    resolved_effects = effects or _Effects()
    coordinator = TerminalDeliveryCoordinator(
        TerminalDeliveryCoordinatorDeps(
            runtime=SimpleNamespace(client=AsyncMock() if ready else None),
            turn_store=store,
            conversation_cache=MagicMock(notify_outbound_message=MagicMock()),
            response_hooks=hooks,
            post_response_effects=resolved_effects,  # type: ignore[arg-type]
            redact_message_event=AsyncMock(return_value=True),
            is_ready=lambda: ready,
            logger=MagicMock(),
            poll_interval_seconds=0.01,
        ),
    )
    return coordinator, hooks, resolved_effects


def _delivered() -> DeliveredMatrixEvent:
    return DeliveredMatrixEvent(event_id="$edit", content_sent={"body": "* final"})


@pytest.mark.asyncio
async def test_checkpoint_is_durable_before_first_matrix_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)

    async def assert_checkpoint_first(*args: object, **kwargs: object) -> DeliveredMatrixEvent:
        del args, kwargs
        [record] = store.terminal_checkpoint_records()
        assert record.response_event_id == TARGET
        return _delivered()

    with patch(
        "mindroom.terminal_delivery.send_message_result",
        side_effect=assert_checkpoint_first,
    ):
        result = await coordinator.commit_and_attempt(_intent())

    assert result.status == "delivered"
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_stale_cleanup_guard_serializes_with_delivery_and_rechecks_settled_receipt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)

    with patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()):
        async with coordinator.stale_cleanup_guard(TARGET) as may_clean:
            assert may_clean is True
            delivery = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
            await asyncio.sleep(0)
            assert not delivery.done()

        assert (await delivery).status == "delivered"

    async with coordinator.stale_cleanup_guard(TARGET) as may_clean:
        assert may_clean is False


@pytest.mark.asyncio
async def test_old_checkpoint_does_not_own_a_newer_response_episode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blocked, _hooks, _effects = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"

    old_owner = await blocked.owned_delivery(_intent().identity)
    new_owner = await blocked.owned_delivery(_intent(correlation_id="$new-edit").identity)

    assert old_owner is not None
    assert new_owner is None


@pytest.mark.asyncio
async def test_cleared_delivery_still_owns_the_same_response_episode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    intent = _intent()

    with patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()):
        assert (await coordinator.commit_and_attempt(intent)).status == "delivered"

    assert store.terminal_checkpoint_records() == ()
    restarted, _hooks, _effects = _coordinator(_store(tmp_path))
    owner = await restarted.owned_delivery(intent.identity)
    assert owner is not None
    assert owner.target_event_id == TARGET
    assert owner.target_was_placeholder is False


@pytest.mark.asyncio
async def test_plain_completed_turn_is_not_a_terminal_delivery_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.get_turn_record(SOURCE)
    assert pending is not None
    store.record_turn(
        replace(
            pending,
            completed=True,
            response_event_id=TARGET,
        ),
    )
    completed = store.get_turn_record(SOURCE)
    assert completed is not None
    coordinator, _hooks, _effects = _coordinator(store)

    assert await coordinator.owned_delivery(_intent().identity) is None


@pytest.mark.asyncio
async def test_fresh_response_retries_initial_checkpoint_persist_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    original_commit = store.commit_terminal_checkpoint
    attempts = 0

    def fail_once(*args: object, **kwargs: object) -> TurnRecord | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "transient disk failure"
            raise OSError(message)
        return original_commit(*args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(store, "commit_terminal_checkpoint", side_effect=fail_once),
        patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()) as send,
    ):
        result = await coordinator.commit_and_attempt(_intent())

    assert result.status == "delivered"
    assert attempts == 2
    send.assert_awaited_once()
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_cancellation_stops_fresh_checkpoint_persist_retries_without_matrix(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    attempted = threading.Event()

    def fail_persist(*_args: object, **_kwargs: object) -> TurnRecord | None:
        attempted.set()
        message = "disk full"
        raise OSError(message)

    with (
        patch.object(store, "commit_terminal_checkpoint", side_effect=fail_persist),
        patch("mindroom.terminal_delivery.send_message_result") as send,
    ):
        task = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        assert await asyncio.to_thread(attempted.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    send.assert_not_awaited()
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_edit_regeneration_retries_initial_checkpoint_persist_failure(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = store.get_turn_record(SOURCE)
    assert pending is not None
    store.record_turn(replace(pending, completed=True, response_event_id=TARGET))
    completed = store.get_turn_record(SOURCE)
    assert completed is not None
    candidate = replace(
        completed,
        correlation_id="$edit",
        source_event_revisions={SOURCE: (2, "$edit")},
    )
    intent = replace(
        _intent(correlation_id="$edit"),
        target_was_placeholder=False,
        identity=replace(
            _intent(correlation_id="$edit").identity,
            regeneration_turn_record=candidate,
        ),
    )
    coordinator, _hooks, _effects = _coordinator(store)
    original_commit = store.commit_terminal_checkpoint
    attempts = 0

    def fail_once(*args: object, **kwargs: object) -> TurnRecord | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "transient disk failure"
            raise OSError(message)
        return original_commit(*args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(store, "commit_terminal_checkpoint", side_effect=fail_once),
        patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()) as send,
    ):
        result = await coordinator.commit_and_attempt(intent)

    assert result.status == "delivered"
    assert attempts == 2
    send.assert_awaited_once()
    settled = store.get_turn_record(SOURCE)
    assert settled is not None
    assert settled.source_event_revisions == {SOURCE: (2, "$edit")}


@pytest.mark.asyncio
async def test_post_transport_persist_failure_stays_lifecycle_managed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks, _effects = _coordinator(store)
    send = AsyncMock(return_value=_delivered())

    with (
        patch.object(store, "update_terminal_checkpoint", side_effect=OSError("disk full")),
        patch("mindroom.terminal_delivery.send_message_result", send),
    ):
        result = await coordinator.commit_and_attempt(_intent())

    assert result.status == "deferred"
    assert result.reason == "lifecycle_persist_failed"
    assert result.lifecycle_managed
    send.assert_awaited_once()
    hooks.emit_after_response.assert_not_awaited()
    assert len(store.terminal_checkpoint_records()) == 1


@pytest.mark.asyncio
async def test_restart_retries_exact_wire_content_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blocked, _hooks, _effects = _coordinator(store, ready=False)
    send = AsyncMock(return_value=_delivered())
    with patch("mindroom.terminal_delivery.send_message_result", send):
        first = await blocked.commit_and_attempt(_intent())
    assert first.status == "deferred"

    restarted = _store(tmp_path)
    active, _hooks, _effects = _coordinator(restarted)
    with patch("mindroom.terminal_delivery.send_message_result", send):
        await active.retry_pending()

    assert send.await_count == 1
    call = send.await_args
    assert call.args[2] == dict(_intent().wire_content)
    assert call.kwargs["content_is_prepared"] is True
    assert call.kwargs["transaction_id"]
    assert restarted.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_retry_isolates_malformed_checkpoint_and_delivers_valid_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store, source_event_id="$valid-source", room_id="!valid:localhost")
    blocked, _hooks, _effects = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"
    assert (
        await blocked.commit_and_attempt(
            _intent(
                source_event_id="$valid-source",
                target_event_id="$valid-target",
                room_id="!valid:localhost",
            ),
        )
    ).status == "deferred"
    malformed = store.get_turn_record(SOURCE)
    assert malformed is not None
    malformed_checkpoint = malformed.terminal_edit_checkpoint
    assert malformed_checkpoint is not None
    malformed = store.update_terminal_checkpoint(
        malformed,
        expected_transaction_id=malformed_checkpoint.transaction_id,
        update=lambda checkpoint: replace(
            checkpoint,
            response_envelope={"source_event_id": SOURCE},
        ),
    )
    assert malformed is not None
    active, _hooks, _effects = _coordinator(store)

    with patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()) as send:
        await active.retry_pending()

    send.assert_awaited_once()
    remaining = store.terminal_checkpoint_records()
    assert len(remaining) == 1
    assert remaining[0].source_event_ids == (SOURCE,)


@pytest.mark.asyncio
async def test_accepted_edit_before_clear_reuses_tx_and_claims_hook_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    effects.fail_once = True
    coordinator, hooks, effects = _coordinator(store, effects=effects)
    send = AsyncMock(return_value=_delivered())

    with patch("mindroom.terminal_delivery.send_message_result", send):
        first = await coordinator.commit_and_attempt(_intent(interactive=True))
        await coordinator.retry_pending()

    assert first.status == "deferred"
    assert send.await_count == 2
    assert send.await_args_list[0].kwargs["transaction_id"] == send.await_args_list[1].kwargs["transaction_id"]
    assert hooks.emit_after_response.await_count == 1
    assert hooks.emit_after_response.await_args.kwargs["continue_on_cancelled"] is True
    assert effects.keys[0] == effects.keys[1]
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2, 3])
async def test_cancelled_checkpoint_writer_drains_without_network_or_stale_write(
    tmp_path: Path,
    cancel_count: int,
) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    writer_started = threading.Event()
    writer_release = threading.Event()
    original_commit = store.commit_terminal_checkpoint

    def blocked_commit(*args: object, **kwargs: object) -> object:
        writer_started.set()
        assert writer_release.wait(timeout=5)
        return original_commit(*args, **kwargs)

    send = AsyncMock()
    with (
        patch.object(store, "commit_terminal_checkpoint", side_effect=blocked_commit),
        patch("mindroom.terminal_delivery.send_message_result", send),
    ):
        task = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        assert await asyncio.to_thread(writer_started.wait, 5)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        writer_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    send.assert_not_awaited()
    assert len(store.terminal_checkpoint_records()) == 1


@pytest.mark.asyncio
async def test_cancelled_checkpoint_writer_failure_preserves_caller_cancellation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    writer_started = threading.Event()
    writer_release = threading.Event()

    def blocked_failure(*_args: object, **_kwargs: object) -> object:
        writer_started.set()
        assert writer_release.wait(timeout=5)
        message = "disk full"
        raise OSError(message)

    with patch.object(store, "commit_terminal_checkpoint", side_effect=blocked_failure):
        task = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        assert await asyncio.to_thread(writer_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        writer_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_stop_finishes_when_retry_work_swallows_worker_cancellation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    retry_started = asyncio.Event()
    never = asyncio.Event()

    async def swallow_first_cancellation() -> None:
        retry_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            return

    with patch.object(coordinator, "retry_pending", side_effect=swallow_first_cancellation):
        coordinator.start()
        await asyncio.wait_for(retry_started.wait(), timeout=1.0)
        stop = asyncio.create_task(coordinator.stop())
        await asyncio.sleep(0.05)
        try:
            assert stop.done()
        finally:
            if not stop.done():
                stop.cancel()
            await stop


@pytest.mark.asyncio
async def test_cancelled_multi_lock_acquisition_releases_acquired_subset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    record = store.get_turn_record(SOURCE)
    assert record is not None
    event_lock = asyncio.Lock()
    turn_lock = asyncio.Lock()
    event_key = f"event:{TARGET}"
    turn_key = f"turn:{json.dumps(record.indexed_event_ids, separators=(',', ':'))}"
    coordinator._locks[event_key] = event_lock
    coordinator._locks[turn_key] = turn_lock
    await turn_lock.acquire()

    async def acquire_both() -> None:
        async with coordinator._locked(record, TARGET):
            raise AssertionError

    task = asyncio.create_task(acquire_both())
    await asyncio.sleep(0)
    assert event_lock.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not event_lock.locked()
    turn_lock.release()


@pytest.mark.asyncio
async def test_retry_pending_is_bounded_but_one_stalled_turn_does_not_block_others(tmp_path: Path) -> None:
    store = _store(tmp_path)
    turns = [(SOURCE, TARGET, ROOM)]
    for index in range(1, 9):
        source = f"$source-{index}"
        room_id = f"!room-{index}:localhost"
        _record_pending(store, source_event_id=source, room_id=room_id)
        turns.append((source, f"$visible-{index}", room_id))

    blocked, _hooks, _effects = _coordinator(store, ready=False)
    for source, target, room_id in turns:
        result = await blocked.commit_and_attempt(
            _intent(source_event_id=source, target_event_id=target, room_id=room_id),
        )
        assert result.status == "deferred"

    active, _hooks, _effects = _coordinator(store)
    started: list[str] = []
    eight_started = asyncio.Event()
    release = asyncio.Event()

    async def stalled_send(_client: object, room_id: str, *_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        started.append(room_id)
        if len(started) == 8:
            eight_started.set()
        await release.wait()
        return _delivered()

    with patch("mindroom.terminal_delivery.send_message_result", side_effect=stalled_send):
        retry = asyncio.create_task(active.retry_pending())
        try:
            await asyncio.wait_for(eight_started.wait(), timeout=0.5)
            await asyncio.sleep(0.02)
            assert len(started) == 8
        finally:
            release.set()
            await retry

    assert len(started) == 9


@pytest.mark.asyncio
async def test_retry_pending_gives_a_healthy_room_a_slot_before_same_room_backlog(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    limited_room = "!limited:localhost"
    healthy_room = "!healthy:localhost"
    turns: list[tuple[str, str, str]] = []
    for index in range(8):
        source = f"$limited-source-{index}"
        target = f"$limited-target-{index}"
        _record_pending(store, source_event_id=source, room_id=limited_room)
        turns.append((source, target, limited_room))
    _record_pending(store, source_event_id="$healthy-source", room_id=healthy_room)
    turns.append(("$healthy-source", "$healthy-target", healthy_room))
    blocked, _hooks, _effects = _coordinator(store, ready=False)
    for source, target, room_id in turns:
        assert (
            await blocked.commit_and_attempt(
                _intent(source_event_id=source, target_event_id=target, room_id=room_id),
            )
        ).status == "deferred"

    active, _hooks, _effects = _coordinator(store)
    limited_started = 0
    healthy_started = asyncio.Event()
    release_limited = asyncio.Event()

    async def room_send(_client: object, room_id: str, *_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        nonlocal limited_started
        if room_id == limited_room:
            limited_started += 1
            await release_limited.wait()
        else:
            healthy_started.set()
        return _delivered()

    with patch("mindroom.terminal_delivery.send_message_result", side_effect=room_send):
        retry = asyncio.create_task(active.retry_pending())
        try:
            await asyncio.wait_for(healthy_started.wait(), timeout=0.5)
            assert limited_started <= 1
        finally:
            release_limited.set()
            await retry


@pytest.mark.asyncio
async def test_checkpoint_read_contention_does_not_block_event_loop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blocked, _hooks, _effects = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"
    [record] = store.terminal_checkpoint_records()
    active, _hooks, _effects = _coordinator(store, ready=False)
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    heartbeat_seen = threading.Event()
    heartbeat_before_release: list[bool] = []

    def hold_ledger_lock() -> None:
        with store._ledger._state.lock:
            lock_acquired.set()
            assert release_lock.wait(timeout=5)

    holder = threading.Thread(target=hold_ledger_lock)
    holder.start()
    assert lock_acquired.wait(timeout=5)

    async def heartbeat() -> None:
        await asyncio.sleep(0.01)
        heartbeat_seen.set()

    heartbeat_task = asyncio.create_task(heartbeat())

    def inspect_and_release() -> None:
        heartbeat_before_release.append(heartbeat_seen.is_set())
        release_lock.set()

    timer = threading.Timer(0.1, inspect_and_release)
    timer.start()
    try:
        await active._attempt_locked(record)
        await heartbeat_task
    finally:
        release_lock.set()
        timer.join()
        holder.join()

    assert heartbeat_before_release == [True]


@pytest.mark.asyncio
async def test_after_response_identity_excludes_discovery_aliases(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store, discovery_event_ids=("$relay-alias",))
    coordinator, hooks, _effects = _coordinator(store)

    with patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()):
        result = await coordinator.commit_and_attempt(_intent())

    assert result.status == "delivered"
    identity = hooks.emit_after_response.await_args.kwargs["identity"]
    assert identity.source_event_ids == (SOURCE,)


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted_event_id", [SOURCE, TARGET], ids=["source", "target"])
async def test_redaction_before_commit_prevents_network_and_effects(
    tmp_path: Path,
    redacted_event_id: str,
) -> None:
    store = _store(tmp_path)
    coordinator, hooks, effects = _coordinator(store)
    send = AsyncMock()

    await coordinator.redact(room_id=ROOM, event_id=redacted_event_id)
    with patch("mindroom.terminal_delivery.send_message_result", send):
        result = await coordinator.commit_and_attempt(_intent())

    assert result.status == "superseded"
    send.assert_not_awaited()
    hooks.emit_after_response.assert_not_awaited()
    assert effects.keys == []


@pytest.mark.asyncio
async def test_deferred_checkpoint_source_redaction_retries_target_cleanup_after_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    blocked, hooks, effects = _coordinator(store, ready=False)
    send = AsyncMock()
    with patch("mindroom.terminal_delivery.send_message_result", send):
        assert (await blocked.commit_and_attempt(_intent())).status == "deferred"
        await blocked.redact(room_id=ROOM, event_id=SOURCE)

    send.assert_not_awaited()
    blocked.deps.redact_message_event.assert_not_awaited()
    [debt] = store.terminal_checkpoint_records()
    assert debt.redacted_source_event_ids == (SOURCE,)
    assert debt.response_event_id == TARGET
    hooks.emit_after_response.assert_not_awaited()
    assert effects.keys == []


@pytest.mark.asyncio
async def test_source_redaction_cleanup_network_error_keeps_debt_without_escaping(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    coordinator, hooks, effects = _coordinator(store)
    with patch("mindroom.terminal_delivery.send_message_result", return_value=None):
        assert (await coordinator.commit_and_attempt(_intent())).status == "deferred"

    coordinator.deps.redact_message_event.side_effect = OSError("homeserver unavailable")
    await coordinator.redact(room_id=ROOM, event_id=SOURCE)

    [debt] = store.terminal_checkpoint_records()
    assert debt.redacted_source_event_ids == (SOURCE,)
    assert debt.response_event_id == TARGET
    hooks.emit_after_response.assert_not_awaited()
    assert effects.keys == []

    restarted = _store(tmp_path)
    active, hooks, effects = _coordinator(restarted)
    send = AsyncMock()
    with patch("mindroom.terminal_delivery.send_message_result", send):
        await active.retry_pending()

    send.assert_not_awaited()
    active.deps.redact_message_event.assert_awaited_once_with(
        room_id=ROOM,
        event_id=TARGET,
        reason="Source event was redacted",
    )
    owner = restarted.get_turn_record(SOURCE)
    assert owner is not None
    assert owner.terminal_edit_checkpoint is None
    assert owner.response_event_id is None
    hooks.emit_after_response.assert_not_awaited()
    assert effects.keys == []


@pytest.mark.asyncio
async def test_deferred_surviving_edit_retains_accepted_redactions_and_retries_edit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    redacted_source = "$redacted-source"
    surviving_source = "$surviving-source"
    target_event_id = "$coalesced-visible"
    target = _target(source_event_id=surviving_source)
    pending = store.record_pending_turn(
        TurnRecord.create(
            [redacted_source, surviving_source],
            redacted_source_event_ids=(redacted_source,),
            completed=False,
            response_owner="code",
            requester_id="@user:localhost",
            correlation_id="corr-original",
            conversation_target=target,
        ),
    )
    assert pending is not None
    store.record_turn(
        replace(
            pending,
            completed=True,
            response_event_id=target_event_id,
        ),
    )
    completed = store.get_turn_record(surviving_source)
    assert completed is not None
    candidate = replace(
        completed,
        correlation_id="$surviving-edit",
        source_event_revisions={
            surviving_source: (2, "$surviving-edit"),
        },
    )
    identity = ResponseIdentity(
        response_kind="ai",
        response_envelope=_envelope(source_event_id=surviving_source),
        correlation_id="$surviving-edit",
        source_event_ids=(surviving_source,),
        regeneration_turn_record=candidate,
    )
    intent = TerminalDeliveryIntent(
        target_event_id=target_event_id,
        target_was_placeholder=False,
        identity=identity,
        interactive_metadata=None,
        body="newest answer",
        wire_content={
            "body": "* newest answer",
            "m.new_content": {"body": "newest answer"},
        },
    )
    blocked, _hooks, _effects = _coordinator(store, ready=False)

    assert (await blocked.commit_and_attempt(intent)).status == "deferred"
    debt = store.get_turn_record(surviving_source)
    assert debt is not None
    assert debt.terminal_edit_checkpoint is not None
    assert debt.terminal_edit_checkpoint.accepted_redacted_source_event_ids == (redacted_source,)

    active, _hooks, _effects = _coordinator(store)
    with patch("mindroom.terminal_delivery.send_message_result", return_value=_delivered()) as send:
        await active.retry_pending()

    send.assert_awaited_once()
    active.deps.redact_message_event.assert_not_awaited()
    settled = store.get_turn_record(surviving_source)
    assert settled is not None
    assert settled.terminal_edit_checkpoint is None
    assert settled.source_event_revisions == {
        surviving_source: (2, "$surviving-edit"),
    }


@pytest.mark.asyncio
async def test_source_redaction_during_transport_removes_accepted_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks, effects = _coordinator(store)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def blocked_send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        send_started.set()
        await release_send.wait()
        return _delivered()

    with patch("mindroom.terminal_delivery.send_message_result", side_effect=blocked_send):
        delivery = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        await send_started.wait()
        redaction = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id=SOURCE))
        await asyncio.sleep(0)
        assert not redaction.done()
        release_send.set()
        assert (await delivery).status == "delivered"
        await redaction

    coordinator.deps.redact_message_event.assert_awaited_once()
    owner = store.get_turn_record(SOURCE)
    assert owner is not None
    assert owner.redacted_source_event_ids == (SOURCE,)
    assert owner.response_event_id is None
    assert owner.terminal_edit_checkpoint is None
    hooks.emit_after_response.assert_awaited_once()
    assert effects.keys == []


@pytest.mark.asyncio
async def test_target_redaction_waits_for_accepted_delivery_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks, _effects = _coordinator(store)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def blocked_send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        send_started.set()
        await release_send.wait()
        return _delivered()

    with patch("mindroom.terminal_delivery.send_message_result", side_effect=blocked_send):
        delivery = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        await send_started.wait()
        redaction = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id=TARGET))
        await asyncio.sleep(0)
        assert not redaction.done()
        release_send.set()
        result = await delivery
        await redaction

    assert result.status == "delivered"
    hooks.emit_after_response.assert_awaited_once()
    target_record = store.get_turn_record(TARGET)
    assert target_record is not None
    assert target_record.redacted_source_event_ids == (TARGET,)
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_stop_cancels_inflight_network_before_effects_and_keeps_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blocked, _hooks, _effects = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"

    active, hooks, effects = _coordinator(store)
    send_started = asyncio.Event()

    async def blocked_send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        send_started.set()
        await asyncio.Event().wait()
        raise AssertionError

    with patch("mindroom.terminal_delivery.send_message_result", side_effect=blocked_send):
        active.start()
        await send_started.wait()
        await active.stop()
        active.wake(reason="after-stop")
        await asyncio.sleep(0)

    hooks.emit_after_response.assert_not_awaited()
    assert effects.keys == []
    assert len(store.terminal_checkpoint_records()) == 1


@pytest.mark.asyncio
async def test_wake_during_retry_scan_triggers_an_immediate_second_scan(tmp_path: Path) -> None:
    coordinator, _hooks, _effects = _coordinator(_store(tmp_path))
    coordinator.deps = replace(coordinator.deps, poll_interval_seconds=60)
    entered = asyncio.Event()
    release = asyncio.Event()
    second_scan = asyncio.Event()
    calls = 0

    async def retry_pending() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        elif calls == 2:
            second_scan.set()

    coordinator.retry_pending = retry_pending  # type: ignore[method-assign]
    worker = asyncio.create_task(coordinator._run())
    try:
        await entered.wait()
        coordinator.wake(reason="checkpoint-added-during-scan")
        release.set()
        async with asyncio.timeout(1):
            await second_scan.wait()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert calls == 2
