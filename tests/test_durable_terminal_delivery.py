"""Invariants for the single durable terminal-delivery authority."""

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
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.response_identity import FrozenThreadSummary, ResponseIdentity
from mindroom.terminal_delivery import (
    PendingTerminalDelivery,
    TerminalDeliveryCoordinator,
    TerminalDeliveryCoordinatorDeps,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
    terminal_delivery_id,
)
from tests.conftest import message_origin

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:localhost"
SOURCE = "$source"
TARGET = "$visible"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Effects:
    def __init__(self) -> None:
        self.interactive_keys: list[str] = []
        self.summary_keys: list[str] = []
        self.summary_payloads: list[FrozenThreadSummary] = []
        self.summary_prepares = 0
        self.fail_interactive = False
        self.fail_summary = False
        self.fail_summary_delivery = False
        self.summary_eligible = True

    async def register_interactive_delivery(self, **kwargs: object) -> None:
        key = str(kwargs["idempotency_key"])
        self.interactive_keys.append(key)
        if self.fail_interactive:
            self.fail_interactive = False
            message = "reaction failed"
            raise OSError(message)

    def should_queue_thread_summary(self, _room: str, _thread: str, _hint: int | None) -> bool:
        return self.summary_eligible

    async def prepare_thread_summary(
        self,
        _room: str,
        _thread: str,
        _entity: str | None,
    ) -> FrozenThreadSummary:
        self.summary_prepares += 1
        if self.fail_summary:
            self.fail_summary = False
            message = "summary failed"
            raise OSError(message)
        return FrozenThreadSummary({"body": "summary"}, 12)

    async def deliver_thread_summary(
        self,
        _room: str,
        _thread: str,
        _frozen: FrozenThreadSummary,
        *,
        transaction_id: str,
    ) -> None:
        self.summary_keys.append(transaction_id)
        self.summary_payloads.append(_frozen)
        if self.fail_summary_delivery:
            self.fail_summary_delivery = False
            message = "summary delivery failed"
            raise OSError(message)


class _TurnStore:
    def __init__(self) -> None:
        self.redacted: set[str] = set()
        self.handled: set[str] = set()

    def mark_source_redacted(self, event_id: str) -> None:
        self.redacted.add(event_id)

    def any_source_redacted(self, event_ids: tuple[str, ...]) -> bool:
        return bool(self.redacted.intersection(event_ids))

    def flush(self) -> None:
        return

    def is_handled(self, event_id: str) -> bool:
        return event_id in self.handled


def _target(room: str = ROOM, thread: str | None = "$thread") -> MessageTarget:
    return MessageTarget.resolve(room, thread, SOURCE)


def _envelope(target: MessageTarget | None = None) -> MessageEnvelope:
    target = target or _target()
    return MessageEnvelope(
        source_event_id=SOURCE,
        target=target,
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
    correlation_id: str = "corr-1",
    target: MessageTarget | None = None,
    source_event_ids: tuple[str, ...] = (SOURCE,),
    wire_content: dict[str, object] | None = None,
    interactive: bool = False,
) -> TerminalDeliveryIntent:
    target = target or _target()
    return TerminalDeliveryIntent(
        target_event_id=TARGET,
        target_was_placeholder=True,
        identity=ResponseIdentity(
            response_kind="ai",
            response_envelope=_envelope(target),
            correlation_id=correlation_id,
            source_event_ids=source_event_ids,
            thread_summary_message_count_hint=12,
        ),
        interactive_metadata=_metadata() if interactive else None,
        body="final answer",
        wire_content=wire_content or {"body": "frozen", "m.new_content": {"body": "final answer"}},
    )


def _store(tmp_path: Path, clock: _Clock | None = None) -> TerminalDeliveryStore:
    return TerminalDeliveryStore("code", tmp_path / "tracking", clock=clock or _Clock())


def _coordinator(
    store: TerminalDeliveryStore,
    *,
    effects: _Effects | None = None,
) -> tuple[TerminalDeliveryCoordinator, AsyncMock]:
    client = AsyncMock()
    hooks = AsyncMock()
    turn_store = _TurnStore()
    coordinator = TerminalDeliveryCoordinator(
        TerminalDeliveryCoordinatorDeps(
            runtime=SimpleNamespace(client=client),
            store=store,
            turn_store=turn_store,  # type: ignore[arg-type]
            conversation_cache=MagicMock(notify_outbound_message=MagicMock()),
            response_hooks=hooks,
            post_response_effects=effects or _Effects(),  # type: ignore[arg-type]
            is_ready=lambda: True,
            logger=MagicMock(),
            poll_interval_seconds=0.01,
        ),
    )
    return coordinator, hooks


def _delivered() -> DeliveredMatrixEvent:
    return DeliveredMatrixEvent(event_id="$edit", content_sent={"body": "frozen"})


def test_restart_restores_exact_frozen_payload_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None

    [restarted] = _store(tmp_path).warm()

    assert restarted.wire_content == item.wire_content
    assert restarted.transaction_id == item.transaction_id


def test_owner_identity_encoding_is_unambiguous_for_opaque_ids() -> None:
    identity = _intent().identity
    left = replace(
        identity,
        response_envelope=replace(identity.response_envelope, source_event_id="a\x1fb"),
        correlation_id="c",
    )
    right = replace(
        identity,
        response_envelope=replace(identity.response_envelope, source_event_id="a"),
        correlation_id="b\x1fc",
    )

    assert terminal_delivery_id(left) != terminal_delivery_id(right)


def test_restart_keeps_highest_revision_when_crash_leaves_same_target_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.record(_intent())
    assert old is not None
    old_path = store._row_file(old.delivery_id)
    old_record = old_path.read_text()
    newer = store.record(_intent(correlation_id="corr-2"))
    assert newer is not None
    old_path.write_text(old_record)

    recovered = _store(tmp_path).warm()

    assert recovered == (newer,)
    assert not old_path.exists()


def test_same_turn_reentry_reuses_original_target_and_new_regeneration_is_distinct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.record(_intent(wire_content={"body": "original"}))
    repeated = store.record(
        replace(
            _intent(wire_content={"body": "drifted"}),
            target_event_id="$replayed-placeholder",
        ),
    )
    newer = store.record(_intent(correlation_id="corr-2", wire_content={"body": "new"}))

    assert original is not None
    assert repeated == original
    assert newer is not None
    assert newer.delivery_id != original.delivery_id
    assert newer.revision == original.revision + 1
    assert store.items() == (newer,)


@pytest.mark.asyncio
async def test_owner_lookup_restores_settled_and_pending_targets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record(_intent())
    settled = store.record(
        replace(
            _intent(correlation_id="settled", target=_target("!settled:localhost")),
            target_event_id="$settled-visible",
        ),
    )
    assert pending is not None
    assert settled is not None
    settled = store.update(
        settled.delivery_id,
        revision=settled.revision,
        transport_delivered=True,
        settled=True,
    )
    assert settled is not None
    coordinator, _hooks = _coordinator(store)

    assert await coordinator.owned_delivery(pending.identity) == pending
    assert await coordinator.owned_delivery(settled.identity) == settled


@pytest.mark.asyncio
async def test_pending_non_placeholder_owner_preserves_target_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pending = store.record(replace(_intent(), target_was_placeholder=False))
    assert pending is not None
    coordinator, _hooks = _coordinator(store)

    owned = await coordinator.owned_delivery(pending.identity)

    assert owned is not None
    assert not owned.target_was_placeholder


@pytest.mark.asyncio
async def test_turn_store_redaction_rejects_later_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    await coordinator.redact(room_id=ROOM, event_id=SOURCE)

    assert (await coordinator.commit_and_attempt(_intent())).status == "superseded"


def test_failed_write_never_publishes_candidate_to_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.record(_intent())
    assert original is not None

    with (
        patch("mindroom.terminal_delivery.write_json_file_durable", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        store.record(_intent(correlation_id="corr-2"))

    assert store.items() == (original,)


def test_failed_stale_row_cleanup_keeps_new_in_memory_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.record(_intent())
    assert original is not None

    with patch("pathlib.Path.unlink", side_effect=OSError("busy")):
        newer = store.record(_intent(correlation_id="corr-2"))

    assert newer is not None
    assert store.items() == (newer,)
    assert _store(tmp_path).warm() == (newer,)


def test_malformed_row_does_not_discard_valid_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    valid = store.record(_intent())
    assert valid is not None
    path = tmp_path / "tracking" / "code_pending_terminal_deliveries" / "broken.json"
    path.write_text(json.dumps({"schema_version": 8, "item": {"delivery_id": "broken"}}))

    assert _store(tmp_path).warm() == (valid,)


def test_row_read_error_fails_warm_without_forgetting_committed_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None

    with (
        patch("pathlib.Path.read_text", side_effect=OSError("input/output error")),
        pytest.raises(OSError, match="input/output error"),
    ):
        _store(tmp_path).warm()


def test_malformed_retry_timestamp_does_not_poison_valid_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    valid = store.record(_intent())
    assert valid is not None
    valid_path = tmp_path / "tracking" / "code_pending_terminal_deliveries" / f"{valid.delivery_id}.json"
    payload = json.loads(valid_path.read_text())
    malformed = dict(payload["item"])
    malformed["delivery_id"] = "malformed"
    malformed["target_event_id"] = "$malformed"
    malformed["next_attempt_at"] = "tomorrow"
    path = tmp_path / "tracking" / "code_pending_terminal_deliveries" / "malformed.json"
    path.write_text(json.dumps({"schema_version": 8, "item": malformed}))

    restarted = _store(tmp_path)

    assert restarted.warm() == (valid,)
    assert restarted.due(limit=8) == (valid,)


def test_due_batch_starts_with_distinct_rooms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.record(_intent(target=_target("!one:localhost"), correlation_id="one-a"))
    second = store.record(
        replace(
            _intent(target=_target("!one:localhost"), correlation_id="one-b"),
            target_event_id="$visible-2",
        ),
    )
    third = store.record(_intent(target=_target("!two:localhost"), correlation_id="two"))
    assert first is not None
    assert second is not None
    assert third is not None

    due = store.due(limit=2)

    assert {item.target.room_id for item in due} == {"!one:localhost", "!two:localhost"}


@pytest.mark.asyncio
async def test_first_attempt_and_retry_use_identical_wire_payload_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    sends = AsyncMock(side_effect=[None, _delivered()])
    intent = _intent()

    with patch("mindroom.terminal_delivery.send_message_result", sends):
        commit = await coordinator.commit_and_attempt(intent)
        assert commit.pending is not None
        item = commit.pending
        await coordinator._drain_item(item)

    assert [call.args[2] for call in sends.await_args_list] == [dict(item.wire_content), dict(item.wire_content)]
    assert {call.kwargs["transaction_id"] for call in sends.await_args_list} == {item.transaction_id}
    assert all(call.kwargs["content_is_prepared"] for call in sends.await_args_list)


@pytest.mark.asyncio
async def test_cancelled_first_attempt_returns_durable_ownership_for_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks = _coordinator(store)
    send_started = asyncio.Event()
    attempt_count = 0

    async def send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            send_started.set()
            await asyncio.Event().wait()
        return _delivered()

    with patch("mindroom.terminal_delivery.send_message_result", new=send):
        attempt = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        await send_started.wait()
        attempt.cancel()
        commit = await attempt

        assert commit.pending is not None
        assert commit.status == "deferred"
        assert commit.reason == "cancelled"
        await coordinator._drain_item(commit.pending)

    assert not attempt.cancelled()
    assert attempt_count == 2
    hooks.emit_after_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_announced_source_redaction_prevents_transport(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent(source_event_ids=(SOURCE, "$coalesced")))
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    coordinator._redacting.add("$coalesced")
    send = AsyncMock(return_value=_delivered())
    with patch("mindroom.terminal_delivery.send_message_result", send):
        await coordinator._drain_item(item)

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_redaction_and_transport_have_one_linearized_commit_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    tombstone_recorded = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_mark_source_redacted = coordinator.deps.turn_store.mark_source_redacted

    def mark_source_redacted(event_id: str) -> None:
        original_mark_source_redacted(event_id)
        loop.call_soon_threadsafe(tombstone_recorded.set)

    async def send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        send_started.set()
        await release_send.wait()
        return _delivered()

    coordinator.deps.turn_store.mark_source_redacted = mark_source_redacted  # type: ignore[method-assign]
    with patch("mindroom.terminal_delivery.send_message_result", new=send):
        attempt_task = asyncio.create_task(coordinator._drain_item(item))
        await send_started.wait()
        redaction_task = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id=SOURCE))
        await tombstone_recorded.wait()

        assert not redaction_task.done()
        assert SOURCE in coordinator.deps.turn_store.redacted
        assert store.get(item.delivery_id) == item

        release_send.set()
        await attempt_task
        await redaction_task

    assert (await coordinator.commit_and_attempt(_intent(correlation_id="corr-2"))).status == "superseded"


@pytest.mark.asyncio
async def test_summary_delivery_rechecks_redaction_after_freeze_persistence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    coordinator, _hooks = _coordinator(store, effects=effects)
    original_update = store.update

    def announce_redaction_after_update(
        delivery_id: str,
        *,
        revision: int,
        **changes: object,
    ) -> object:
        updated = original_update(delivery_id, revision=revision, **changes)
        if "thread_summary" in changes:
            coordinator._redacting.add(SOURCE)
        return updated

    store.update = MagicMock(side_effect=announce_redaction_after_update)  # type: ignore[method-assign]

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        await coordinator.commit_and_attempt(_intent())

    assert effects.summary_prepares == 1
    assert effects.summary_payloads == []


@pytest.mark.asyncio
async def test_immediate_success_checkpoints_all_lifecycle_steps_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    coordinator, hooks = _coordinator(store, effects=effects)

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent(interactive=True))

    assert commit.settled
    [receipt] = store.items()
    assert receipt.settled
    hooks.emit_after_response.assert_awaited_once()
    assert len(effects.interactive_keys) == 1
    assert len(effects.summary_keys) == 1


@pytest.mark.asyncio
async def test_slow_lifecycle_does_not_block_another_delivery_transport(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks = _coordinator(store)
    first_hook_started = asyncio.Event()
    release_first_hook = asyncio.Event()
    first_correlation_id = "corr-1"

    async def emit_after_response(**kwargs: object) -> None:
        identity = kwargs["identity"]
        assert isinstance(identity, ResponseIdentity)
        if identity.correlation_id == first_correlation_id:
            first_hook_started.set()
            await release_first_hook.wait()

    hooks.emit_after_response = AsyncMock(side_effect=emit_after_response)
    second_transport = asyncio.Event()

    async def send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        if sends.await_count >= 2:
            second_transport.set()
        return _delivered()

    sends = AsyncMock(side_effect=send)
    second_intent = replace(
        _intent(correlation_id="corr-2", target=_target("!two:localhost")),
        target_event_id="$visible-2",
    )

    with patch("mindroom.terminal_delivery.send_message_result", sends):
        first = asyncio.create_task(coordinator.commit_and_attempt(_intent(correlation_id=first_correlation_id)))
        await first_hook_started.wait()
        second = asyncio.create_task(coordinator.commit_and_attempt(second_intent))
        await asyncio.wait_for(second_transport.wait(), 1)

        assert sends.await_count == 2
        await asyncio.wait_for(asyncio.shield(second), 1)
        release_first_hook.set()
        await first


@pytest.mark.asyncio
async def test_due_drain_keeps_other_rooms_moving_with_more_than_one_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    for index in range(9):
        item = store.record(
            replace(
                _intent(
                    correlation_id=f"corr-{index}",
                    target=_target(f"!room-{index}:localhost"),
                ),
                target_event_id=f"$visible-{index}",
            ),
        )
        assert item is not None

    all_due = store.due(limit=9)
    blocked_id = all_due[0].delivery_id
    ninth_id = all_due[8].delivery_id
    blocked = asyncio.Event()
    release = asyncio.Event()
    ninth_finished = asyncio.Event()
    active = 0
    max_active = 0

    async def drain_item(item: PendingTerminalDelivery) -> None:
        nonlocal active, max_active
        delivery_id = item.delivery_id
        active += 1
        max_active = max(max_active, active)
        try:
            if delivery_id == blocked_id:
                blocked.set()
                await release.wait()
            else:
                await asyncio.sleep(0)
            if delivery_id == ninth_id:
                ninth_finished.set()
        finally:
            active -= 1

    coordinator._drain_item = drain_item  # type: ignore[method-assign]
    draining = asyncio.create_task(coordinator.drain_once())
    await blocked.wait()
    try:
        await asyncio.wait_for(ninth_finished.wait(), 0.5)
    finally:
        release.set()
        drained = await draining

    assert drained == 9
    assert 1 < max_active <= 8


@pytest.mark.asyncio
async def test_failed_redaction_persistence_remains_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    item = store.record(_intent())
    assert item is not None
    coordinator.deps.turn_store.mark_source_redacted = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="disk full"):
        await coordinator.redact(room_id=ROOM, event_id=SOURCE)

    assert SOURCE in coordinator._redacting
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())) as send:
        await coordinator._drain_item(item)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_redaction_barrier_survives_restart_until_reconciled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    coordinator.deps.turn_store.mark_source_redacted = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="disk full"):
        await coordinator.redact(room_id=ROOM, event_id=SOURCE)

    [barrier] = store.redaction_barriers()
    assert barrier.room_id == ROOM
    assert barrier.event_id == SOURCE
    assert store.get(item.delivery_id) == item

    restarted_store = _store(tmp_path)
    restarted, _hooks = _coordinator(restarted_store)
    restarted.deps.turn_store.mark_source_redacted = MagicMock(side_effect=OSError("still unavailable"))  # type: ignore[method-assign]
    recovered = await restarted.warm()

    assert recovered == (item,)
    assert restarted_store.due() == ()
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())) as send:
        assert await restarted.drain_once() == 0
    send.assert_not_awaited()

    def persist_tombstone(event_id: str) -> None:
        restarted.deps.turn_store.redacted.add(event_id)

    restarted.deps.turn_store.mark_source_redacted = MagicMock(side_effect=persist_tombstone)  # type: ignore[method-assign]
    await restarted.reconcile_redactions()

    assert restarted.deps.turn_store.redacted == {SOURCE}
    assert restarted_store.items() == ()
    assert restarted_store.redaction_barriers() == ()
    assert restarted.redaction_barriers_ready


@pytest.mark.asyncio
async def test_failed_redaction_barrier_rejects_later_new_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    coordinator.deps.turn_store.mark_source_redacted = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="disk full"):
        await coordinator.redact(room_id=ROOM, event_id=SOURCE)

    later = replace(_intent(correlation_id="later"), target_event_id="$later-visible")
    assert store.record(later) is None
    commit = await coordinator.commit_and_attempt(later)
    assert commit.status == "superseded"


def test_malformed_redaction_barrier_sibling_fails_closed_without_losing_valid_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    barrier = store.record_redaction(room_id=ROOM, event_id=SOURCE)
    malformed = tmp_path / "tracking" / "code_pending_terminal_deliveries" / "redactions" / "malformed.json"
    malformed.write_text("{not json")

    restarted = _store(tmp_path)

    assert restarted.warm() == (item,)
    assert restarted.redaction_barriers() == (barrier,)
    assert not restarted.redaction_barriers_valid
    assert restarted.due() == ()


@pytest.mark.asyncio
async def test_redaction_barrier_write_failure_marks_coordinator_not_ready(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, _hooks = _coordinator(store)

    with (
        patch("mindroom.terminal_delivery.write_json_file_durable", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await coordinator.redact(room_id=ROOM, event_id=SOURCE)

    assert not coordinator.redaction_barriers_ready
    assert SOURCE not in coordinator.deps.turn_store.redacted
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())) as send:
        await coordinator._drain_item(item)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_redaction_barrier_blocks_record_while_tombstone_is_in_flight(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    tombstone_started = threading.Event()
    release_tombstone = threading.Event()

    def persist_tombstone(event_id: str) -> None:
        tombstone_started.set()
        release_tombstone.wait()
        coordinator.deps.turn_store.redacted.add(event_id)

    coordinator.deps.turn_store.mark_source_redacted = MagicMock(side_effect=persist_tombstone)  # type: ignore[method-assign]
    redaction = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id=SOURCE))
    await asyncio.to_thread(tombstone_started.wait)
    try:
        [barrier] = store.redaction_barriers()
        assert barrier.event_id == SOURCE
        later = replace(_intent(correlation_id="later"), target_event_id="$later-visible")
        commit = await coordinator.commit_and_attempt(later)
        assert commit.status == "superseded"
    finally:
        release_tombstone.set()
        await redaction
    assert store.redaction_barriers() == ()


@pytest.mark.asyncio
async def test_cancelled_redaction_drains_durable_tombstone_before_propagating(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    tombstone_started = threading.Event()
    release_tombstone = threading.Event()

    def persist_tombstone(event_id: str) -> None:
        tombstone_started.set()
        release_tombstone.wait()
        coordinator.deps.turn_store.redacted.add(event_id)

    coordinator.deps.turn_store.mark_source_redacted = MagicMock(side_effect=persist_tombstone)  # type: ignore[method-assign]
    redaction = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id=SOURCE))
    await asyncio.to_thread(tombstone_started.wait)
    redaction.cancel()
    await asyncio.sleep(0)
    returned_before_write = redaction.done()
    release_tombstone.set()

    with pytest.raises(asyncio.CancelledError):
        await redaction

    assert not returned_before_write
    assert SOURCE in coordinator.deps.turn_store.redacted
    assert tuple(barrier.event_id for barrier in store.redaction_barriers()) == (SOURCE,)


@pytest.mark.asyncio
async def test_settled_receipt_survives_until_handled_turn_is_durable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent())

    assert commit.settled
    assert store.get(commit.item.delivery_id) is not None


@pytest.mark.asyncio
async def test_settled_reentry_reuses_receipt_until_handled_flush_prunes_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks = _coordinator(store)
    send = AsyncMock(return_value=_delivered())

    with patch("mindroom.terminal_delivery.send_message_result", send):
        first = await coordinator.commit_and_attempt(_intent())
        repeated = await coordinator.commit_and_attempt(_intent(wire_content={"body": "drifted"}))

    assert first.item is not None
    assert repeated.item == store.get(first.item.delivery_id)
    send.assert_awaited_once()
    hooks.emit_after_response.assert_awaited_once()

    coordinator.deps.turn_store.handled.add(SOURCE)
    await coordinator._drain_item(repeated.item)

    assert store.get(first.item.delivery_id) is None


@pytest.mark.asyncio
async def test_retry_safe_lifecycle_failure_keeps_transport_checkpoint_and_stable_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    effects.fail_interactive = True
    coordinator, hooks = _coordinator(store, effects=effects)
    send = AsyncMock(return_value=_delivered())

    with patch("mindroom.terminal_delivery.send_message_result", send):
        commit = await coordinator.commit_and_attempt(_intent(interactive=True))
        assert commit.pending is not None
        item = store.get(commit.pending.delivery_id)
        assert item is not None
        assert item.transport_delivered
        await coordinator._drain_item(item)

    send.assert_awaited_once()
    hooks.emit_after_response.assert_awaited_once()
    assert len(effects.interactive_keys) == 2
    assert len(set(effects.interactive_keys)) == 1
    assert len(effects.summary_keys) == 1
    [receipt] = store.items()
    assert receipt.settled


@pytest.mark.asyncio
async def test_summary_retry_reuses_persisted_payload_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    effects.fail_summary_delivery = True
    coordinator, _hooks = _coordinator(store, effects=effects)

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent())
        assert commit.pending is not None
        pending = store.get(commit.pending.delivery_id)
        assert pending is not None
        assert pending.thread_summary is not None
        await coordinator._drain_item(pending)

    assert effects.summary_prepares == 1
    assert len(effects.summary_payloads) == 2
    assert effects.summary_payloads[0] == effects.summary_payloads[1]
    assert len(set(effects.summary_keys)) == 1


@pytest.mark.asyncio
async def test_persisted_summary_retry_bypasses_changed_eligibility(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    effects.fail_summary_delivery = True
    coordinator, _hooks = _coordinator(store, effects=effects)

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent())
        assert commit.pending is not None
        pending = store.get(commit.pending.delivery_id)
        assert pending is not None
        assert pending.thread_summary is not None
        effects.summary_eligible = False
        await coordinator._drain_item(pending)

    assert effects.summary_prepares == 1
    assert len(effects.summary_payloads) == 2
    [receipt] = store.items()
    assert receipt.settled


@pytest.mark.asyncio
async def test_failed_frozen_summary_reserves_thread_across_concurrent_delivery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    coordinator, _hooks = _coordinator(store, effects=effects)
    first_summary_started = asyncio.Event()
    release_first_summary = asyncio.Event()
    second_summary_delivered = asyncio.Event()
    summary_calls = 0

    async def deliver_summary(
        _room: str,
        _thread: str,
        frozen: FrozenThreadSummary,
        *,
        transaction_id: str,
    ) -> None:
        nonlocal summary_calls
        effects.summary_keys.append(transaction_id)
        effects.summary_payloads.append(frozen)
        summary_calls += 1
        if summary_calls == 1:
            first_summary_started.set()
            await release_first_summary.wait()
            message = "summary delivery failed"
            raise OSError(message)
        second_summary_delivered.set()

    effects.deliver_thread_summary = deliver_summary  # type: ignore[method-assign]
    second_intent = replace(
        _intent(correlation_id="corr-2"),
        target_event_id="$visible-2",
    )
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        first_attempt = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        await first_summary_started.wait()
        second_attempt = asyncio.create_task(coordinator.commit_and_attempt(second_intent))
        await asyncio.sleep(0.05)
        try:
            assert effects.summary_prepares == 1
            assert not second_summary_delivered.is_set()
        finally:
            release_first_summary.set()
        first, second = await asyncio.gather(first_attempt, second_attempt)

    assert first.pending is not None
    assert second.pending is not None
    assert effects.summary_prepares == 1
    assert len(effects.summary_payloads) == 1


@pytest.mark.asyncio
async def test_cancelled_retry_safe_lifecycle_claim_is_released(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    coordinator, _hooks = _coordinator(store, effects=effects)
    item = store.record(_intent(interactive=True))
    assert item is not None
    item = store.update(item.delivery_id, revision=item.revision, transport_delivered=True)
    assert item is not None
    started = asyncio.Event()

    async def cancelled(**_kwargs: object) -> None:
        started.set()
        raise asyncio.CancelledError

    effects.register_interactive_delivery = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await coordinator._settle_locked(item, "delivered", store.clock())

    current = store.get(item.delivery_id)
    assert current is not None
    assert "interactive" not in current.completed_lifecycle_steps


@pytest.mark.asyncio
async def test_first_attempt_cancellation_after_transport_returns_managed_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, hooks = _coordinator(store)
    hooks.emit_after_response.side_effect = asyncio.CancelledError

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent())

    assert commit.status == "delivered"
    assert commit.pending is not None
    current = store.get(commit.pending.delivery_id)
    assert current is not None
    assert current.transport_delivered
    assert current.after_response_claimed


@pytest.mark.asyncio
async def test_cancellation_during_record_returns_durable_managed_ownership(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    record_started = threading.Event()
    release_record = threading.Event()
    original_record = store.record

    def blocked_record(intent: TerminalDeliveryIntent) -> PendingTerminalDelivery | None:
        record_started.set()
        release_record.wait()
        return original_record(intent)

    store.record = MagicMock(side_effect=blocked_record)  # type: ignore[method-assign]
    attempt = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
    await asyncio.to_thread(record_started.wait)
    attempt.cancel()
    await asyncio.sleep(0)
    attempt.cancel()
    await asyncio.sleep(0)
    release_record.set()

    commit = await asyncio.wait_for(attempt, 1)

    assert not attempt.cancelled()
    assert commit.reason == "cancelled"
    assert commit.pending is not None
    assert store.get(commit.pending.delivery_id) == commit.pending


@pytest.mark.asyncio
async def test_cancelled_store_update_drains_disk_mutation_before_propagating(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    update_started = threading.Event()
    release_update = threading.Event()
    original_update = store.update

    def blocked_update(
        delivery_id: str,
        *,
        revision: int,
        **changes: object,
    ) -> PendingTerminalDelivery | None:
        update_started.set()
        release_update.wait()
        return original_update(delivery_id, revision=revision, **changes)

    store.update = MagicMock(side_effect=blocked_update)  # type: ignore[method-assign]
    mutation = asyncio.create_task(coordinator._defer(item, store.clock() + 1))
    await asyncio.to_thread(update_started.wait)
    mutation.cancel()
    await asyncio.sleep(0)
    returned_before_write = mutation.done()
    release_update.set()

    with pytest.raises(asyncio.CancelledError):
        await mutation

    current = store.get(item.delivery_id)
    assert not returned_before_write
    assert current is not None
    assert current.attempts == 1


@pytest.mark.asyncio
async def test_stale_settlement_cannot_delete_newer_regeneration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.record(_intent())
    newer = store.record(_intent(correlation_id="corr-2"))
    assert old is not None
    assert newer is not None
    coordinator, _hooks = _coordinator(store)

    await coordinator._settle_locked(old, "delivered", store.clock())

    assert store.items() == (newer,)


@pytest.mark.asyncio
async def test_captured_old_due_row_cannot_overwrite_new_same_target_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.record(_intent())
    assert old is not None
    coordinator, _hooks = _coordinator(store)
    coordinator.owned_delivery = AsyncMock(return_value=None)  # type: ignore[method-assign]
    target_lock = coordinator._lock_for(old.target.room_id, old.target_event_id)

    await target_lock.acquire()
    try:
        newer_attempt = asyncio.create_task(coordinator.commit_and_attempt(_intent(correlation_id="corr-2")))
        await asyncio.sleep(0)
        stale_drain = asyncio.create_task(coordinator._drain_item(old))
        await asyncio.sleep(0)
    finally:
        target_lock.release()

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())) as send:
        await stale_drain
        newer = await newer_attempt

    assert newer.item is not None
    assert store.items() == (newer.item,)
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_cancels_stalled_transport_and_restart_retries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    item = store.record(_intent())
    assert item is not None
    transport_started = asyncio.Event()

    async def stalled_transport(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        transport_started.set()
        await asyncio.Event().wait()
        raise AssertionError

    with patch("mindroom.terminal_delivery.send_message_result", new=stalled_transport):
        draining = asyncio.create_task(coordinator.drain_once())
        await transport_started.wait()
        await asyncio.wait_for(coordinator.stop(), 0.5)
        with pytest.raises(asyncio.CancelledError):
            await draining

    assert coordinator._settlement is None
    current = store.get(item.delivery_id)
    assert current is not None
    assert not current.transport_delivered

    restarted_store = _store(tmp_path)
    restarted, restarted_hooks = _coordinator(restarted_store)
    await restarted.warm()
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        assert await restarted.drain_once() == 1

    [receipt] = restarted_store.items()
    assert receipt.settled
    restarted_hooks.emit_after_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_cancels_stalled_hook_and_restart_retries_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, hooks = _coordinator(store)
    hook_started = asyncio.Event()

    async def stalled_hook(**_kwargs: object) -> None:
        hook_started.set()
        await asyncio.Event().wait()

    hooks.emit_after_response = AsyncMock(side_effect=stalled_hook)
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        draining = asyncio.create_task(coordinator.drain_once())
        await hook_started.wait()
        await asyncio.wait_for(coordinator.stop(), 0.5)
        with pytest.raises(asyncio.CancelledError):
            await draining

    assert coordinator._settlement is None
    current = store.get(item.delivery_id)
    assert current is not None
    assert current.transport_delivered
    assert current.after_response_claimed

    restarted_store = _store(tmp_path)
    restarted, restarted_hooks = _coordinator(restarted_store)
    await restarted.warm()
    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())) as send:
        assert await restarted.drain_once() == 1

    send.assert_not_awaited()
    restarted_hooks.emit_after_response.assert_not_awaited()
    [receipt] = restarted_store.items()
    assert receipt.settled


@pytest.mark.asyncio
async def test_stop_waits_for_cancelled_settlement_store_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    update_started = threading.Event()
    release_update = threading.Event()
    original_update = store.update

    def blocked_update(
        delivery_id: str,
        *,
        revision: int,
        **changes: object,
    ) -> PendingTerminalDelivery | None:
        update_started.set()
        release_update.wait()
        return original_update(delivery_id, revision=revision, **changes)

    store.update = MagicMock(side_effect=blocked_update)  # type: ignore[method-assign]
    settlement = asyncio.create_task(coordinator._defer(item, store.clock() + 1))
    coordinator._settlement = settlement
    await asyncio.to_thread(update_started.wait)
    stopping = asyncio.create_task(coordinator.stop())
    done, _pending = await asyncio.wait({stopping}, timeout=0.1)
    returned_before_write = stopping in done
    release_update.set()
    await asyncio.wait_for(stopping, 1)

    assert not returned_before_write
    current = store.get(item.delivery_id)
    assert current is not None
    assert current.attempts == 1


@pytest.mark.asyncio
async def test_completed_delivery_lock_is_released_from_registry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent())

    assert commit.item is not None
    assert commit.item.delivery_id not in coordinator._locks
