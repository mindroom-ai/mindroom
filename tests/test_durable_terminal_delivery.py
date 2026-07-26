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
from mindroom.response_identity import ResponseIdentity
from mindroom.terminal_delivery import (
    PendingTerminalDelivery,
    TerminalDeliveryAttempt,
    TerminalDeliveryCoordinator,
    TerminalDeliveryCoordinatorDeps,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
    _reset_terminal_delivery_store_runtime,
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
        self.fail_interactive = False
        self.fail_summary = False

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

    async def complete_thread_summary(
        self,
        _room: str,
        _thread: str,
        _entity: str | None,
        *,
        idempotency_key: str,
    ) -> None:
        self.summary_keys.append(idempotency_key)
        if self.fail_summary:
            self.fail_summary = False
            message = "summary failed"
            raise OSError(message)


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
        thread_summary_entity_name="code",
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
    coordinator = TerminalDeliveryCoordinator(
        TerminalDeliveryCoordinatorDeps(
            runtime=SimpleNamespace(client=client),
            store=store,
            conversation_cache=MagicMock(notify_outbound_message=MagicMock()),
            response_hooks=hooks,
            post_response_effects=effects or _Effects(),  # type: ignore[arg-type]
            is_ready=lambda: True,
            logger=MagicMock(),
            poll_interval_seconds=0.01,
        ),
    )
    coordinator._inspect_target = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    return coordinator, hooks


def _delivered() -> DeliveredMatrixEvent:
    return DeliveredMatrixEvent(event_id="$edit", content_sent={"body": "frozen"})


def test_restart_restores_exact_frozen_payload_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None

    _reset_terminal_delivery_store_runtime()
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


def test_redaction_tombstone_rejects_later_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.redact(room_id=ROOM, event_id=SOURCE)

    assert store.record(_intent()) is None


def test_failed_write_never_publishes_candidate_to_shared_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.record(_intent())
    assert original is not None

    with (
        patch("mindroom.terminal_delivery.write_json_file_durable", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        store.redact(room_id=ROOM, event_id=SOURCE)

    assert store.items() == (original,)
    assert store.record(_intent(correlation_id="corr-2")) is not None


def test_malformed_row_does_not_discard_valid_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    valid = store.record(_intent())
    assert valid is not None
    path = tmp_path / "tracking" / "code_pending_terminal_deliveries.json"
    payload = json.loads(path.read_text())
    payload["items"].append({"delivery_id": "broken"})
    path.write_text(json.dumps(payload))

    _reset_terminal_delivery_store_runtime()
    assert _store(tmp_path).warm() == (valid,)


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
        retry = await coordinator.attempt(item)

    assert retry.result == "delivered"
    assert [call.args[2] for call in sends.await_args_list] == [dict(item.wire_content), dict(item.wire_content)]
    assert {call.kwargs["transaction_id"] for call in sends.await_args_list} == {item.transaction_id}
    assert all(call.kwargs["content_is_prepared"] for call in sends.await_args_list)


@pytest.mark.asyncio
async def test_redaction_during_target_inspection_prevents_transport(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent(source_event_ids=(SOURCE, "$coalesced")))
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    redaction_task: asyncio.Task[None] | None = None

    async def inspect(_room: str, _event: str) -> str:
        nonlocal redaction_task
        redaction_task = asyncio.create_task(coordinator.redact(room_id=ROOM, event_id="$coalesced"))
        while store.get(item.delivery_id) is not None:  # noqa: ASYNC110
            await asyncio.sleep(0)
        return "ok"

    coordinator._inspect_target = inspect  # type: ignore[method-assign]
    send = AsyncMock(return_value=_delivered())
    with patch("mindroom.terminal_delivery.send_message_result", send):
        attempt = await coordinator.attempt(item)
    assert redaction_task is not None
    await redaction_task

    assert attempt.result == "superseded"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_immediate_success_checkpoints_all_lifecycle_steps_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    coordinator, hooks = _coordinator(store, effects=effects)

    with patch("mindroom.terminal_delivery.send_message_result", new=AsyncMock(return_value=_delivered())):
        commit = await coordinator.commit_and_attempt(_intent(interactive=True))

    assert commit.settled
    assert store.items() == ()
    hooks.emit_after_response.assert_awaited_once()
    assert len(effects.interactive_keys) == 1
    assert len(effects.summary_keys) == 1


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
        await coordinator.settle_attempt(
            item,
            TerminalDeliveryAttempt.delivered_now("transport_already_delivered"),
            store.clock(),
        )

    send.assert_awaited_once()
    hooks.emit_after_response.assert_awaited_once()
    assert len(effects.interactive_keys) == 2
    assert len(set(effects.interactive_keys)) == 1
    assert len(effects.summary_keys) == 1
    assert store.items() == ()


@pytest.mark.asyncio
async def test_cancelled_retry_safe_lifecycle_claim_is_released(tmp_path: Path) -> None:
    store = _store(tmp_path)
    effects = _Effects()
    coordinator, _hooks = _coordinator(store, effects=effects)
    item = store.record(_intent(interactive=True))
    assert item is not None
    item = store.mark_transport_delivered(item.delivery_id, revision=item.revision)
    assert item is not None
    started = asyncio.Event()

    async def cancelled(**_kwargs: object) -> None:
        started.set()
        raise asyncio.CancelledError

    effects.register_interactive_delivery = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await coordinator.settle_attempt(item, TerminalDeliveryAttempt.delivered_now(), store.clock())

    current = store.get(item.delivery_id)
    assert current is not None
    assert "interactive" not in current.completed_lifecycle_steps


@pytest.mark.asyncio
async def test_stale_settlement_cannot_delete_newer_regeneration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = store.record(_intent())
    newer = store.record(_intent(correlation_id="corr-2"))
    assert old is not None
    assert newer is not None
    coordinator, _hooks = _coordinator(store)

    await coordinator.settle_attempt(old, TerminalDeliveryAttempt.delivered_now(), store.clock())

    assert store.items() == (newer,)


@pytest.mark.asyncio
async def test_stop_awaits_shielded_settlement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.record(_intent())
    assert item is not None
    coordinator, _hooks = _coordinator(store)
    coordinator.attempt = AsyncMock(return_value=TerminalDeliveryAttempt.delivered_now())  # type: ignore[method-assign]
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def settle(
        pending: PendingTerminalDelivery,
        _attempt: TerminalDeliveryAttempt,
        _next_attempt_at: float,
    ) -> None:
        entered.set()
        await finish.wait()
        store.finish(pending.delivery_id, revision=pending.revision)

    coordinator.settle_attempt = settle  # type: ignore[method-assign]
    drain = asyncio.create_task(coordinator.drain_once())
    coordinator._task = drain
    await entered.wait()
    stopping = asyncio.create_task(coordinator.stop())
    await asyncio.sleep(0)

    assert not stopping.done()
    finish.set()
    await stopping
    assert store.items() == ()
