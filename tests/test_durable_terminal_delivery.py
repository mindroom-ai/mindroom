"""Invariants for the single durable terminal-delivery authority."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
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
    TerminalDeliveryCoordinator,
    TerminalDeliveryCoordinatorDeps,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
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

    async def register_interactive_delivery(self, **kwargs: object) -> None:
        key = str(kwargs["idempotency_key"])
        self.interactive_keys.append(key)
        if self.fail_interactive:
            self.fail_interactive = False
            message = "reaction failed"
            raise OSError(message)

    @staticmethod
    def should_queue_thread_summary(_room: str, _thread: str, _hint: int | None) -> bool:
        return True

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


def test_same_turn_reentry_reuses_frozen_revision_and_new_regeneration_replaces_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.record(_intent(wire_content={"body": "original"}))
    repeated = store.record(_intent(wire_content={"body": "drifted"}))
    newer = store.record(_intent(correlation_id="corr-2", wire_content={"body": "new"}))

    assert original is not None
    assert repeated == original
    assert newer is not None
    assert newer.revision == original.revision + 1
    assert store.items() == (newer,)


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


def test_malformed_row_does_not_discard_valid_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    valid = store.record(_intent())
    assert valid is not None
    path = tmp_path / "tracking" / "code_pending_terminal_deliveries" / "broken.json"
    path.write_text(json.dumps({"schema_version": 7, "item": {"delivery_id": "broken"}}))

    assert _store(tmp_path).warm() == (valid,)


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
    path.write_text(json.dumps({"schema_version": 7, "item": malformed}))

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

    async def send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        send_started.set()
        await release_send.wait()
        return _delivered()

    with patch("mindroom.terminal_delivery.send_message_result", new=send):
        attempt_task = asyncio.create_task(coordinator._drain_item(item))
        await send_started.wait()
        redaction_task = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id=SOURCE))
        await asyncio.sleep(0.05)

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
async def test_stop_awaits_shielded_settlement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    coordinator, _hooks = _coordinator(store)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def settle() -> None:
        entered.set()
        await finish.wait()

    coordinator._settlement = asyncio.create_task(settle())
    await entered.wait()
    stopping = asyncio.create_task(coordinator.stop())
    await asyncio.sleep(0)

    assert not stopping.done()
    finish.set()
    await stopping
