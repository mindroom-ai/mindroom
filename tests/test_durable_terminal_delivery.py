"""Retry invariants for canonical TurnRecord terminal checkpoints."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import threading
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
) -> TerminalDeliveryIntent:
    return TerminalDeliveryIntent(
        target_event_id=target_event_id,
        target_was_placeholder=True,
        identity=ResponseIdentity(
            response_kind="ai",
            response_envelope=_envelope(room_id=room_id, source_event_id=source_event_id),
            correlation_id="corr-1",
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
            runtime=SimpleNamespace(client=AsyncMock()),
            turn_store=store,
            conversation_cache=MagicMock(notify_outbound_message=MagicMock()),
            response_hooks=hooks,
            post_response_effects=resolved_effects,  # type: ignore[arg-type]
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
async def test_persist_failure_leaves_thinking_and_never_calls_matrix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks, _effects = _coordinator(store)
    send = AsyncMock()

    with (
        patch.object(store._ledger, "_persist_records", side_effect=OSError("disk full")),
        patch("mindroom.terminal_delivery.send_message_result", send),
        pytest.raises(OSError, match="disk full"),
    ):
        await coordinator.commit_and_attempt(_intent())

    send.assert_not_awaited()
    assert store.terminal_checkpoint_records() == ()


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
