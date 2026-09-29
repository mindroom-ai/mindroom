"""One stop reaction produces one terminal record and one visible cancellation.

These are pins on ``UserStopReconciler``, the owner of that convergence, with a
real ``TurnStore`` behind it. The runner and the gateway are represented by the
two things the reconciler actually needs from them -- serialization under the
target's lifecycle lock, and the one visible edit that commits the cancellation
note -- so that a duplicate visible cancellation is something the test can see
rather than something a broader harness would absorb.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.journal_dispatch import JournalDispatcher
from mindroom.message_target import MessageTarget
from mindroom.reaction_dispatch import ReactionDispatcher, ReactionDispatcherDeps
from mindroom.stop import StopManager
from mindroom.turn_store import TurnStore, TurnStoreDeps
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.conftest import test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import EventJournalStore
    from mindroom.response_runner import ResponseRunner

pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_SOURCE_EVENT_ID = "$source"
_RESPONSE_EVENT_ID = "$response"
_STOP_RECEIPT_ORDER = 7


@dataclass
class _SerializingRunner:
    """The part of ``ResponseRunner`` this reconciliation depends on.

    Only two behaviors matter here: the finalize callback runs under a lock
    held per target, and the live response is asked to cancel while waiting for
    it. Everything else the real runner does is unrelated to convergence.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_requests: int = 0

    async def finalize_user_stop(
        self,
        message_id: str,
        source_event_id: str,
        target: MessageTarget,
        stop_receipt_order: int,
        should_cancel: Callable[[], bool],
        finalize: Callable[[bool], Awaitable[bool]],
    ) -> bool:
        """Cancel the live response, then finalize under the target's lock."""
        del message_id, source_event_id, target, stop_receipt_order
        if should_cancel():
            self.cancel_requests += 1
        async with self.lock:
            return await finalize(False)


@dataclass
class _CountingGateway:
    """A gateway that records every visible cancellation it is asked for."""

    finalized: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def user_stop_scope(self, response_event_id: str) -> AsyncIterator[None]:
        """Represent a gateway with no outbox cleanup competing with STOP."""
        del response_event_id
        yield None

    async def finalize_user_stopped_response(self, target: MessageTarget, response_event_id: str) -> bool:
        """Commit the cancellation note for one response."""
        del target
        self.finalized.append(response_event_id)
        return True


def _reconciler(store: TurnStore, runner: _SerializingRunner, gateway: _CountingGateway) -> UserStopReconciler:
    return UserStopReconciler(
        UserStopReconcilerDeps(
            turn_store=store,
            response_runner=cast("ResponseRunner", runner),
            delivery_gateway=cast("DeliveryGateway", gateway),
        ),
    )


async def _store(journal_store: EventJournalStore) -> TurnStore:
    _reset_handled_turn_ledger_runtime()
    store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            turn_records=journal_store.turn_records("agent"),
            redacted_event_ids=journal_store.principal("agent@alice").redacted_event_ids,
            legacy_responses_file=None,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    return store


async def _record_answered_turn(store: TurnStore) -> None:
    """Record the turn a stop reaction can arrive for: answered, not terminal."""
    await store.record_turn(
        TurnRecord.create(
            (_SOURCE_EVENT_ID,),
            response_event_id=_RESPONSE_EVENT_ID,
            completed=False,
            conversation_target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        ),
    )


async def test_one_stop_makes_the_turn_terminal_and_commits_one_cancellation(journal_store: EventJournalStore) -> None:
    """The two sides of a stop have to agree, and there is only one of each."""
    store = await _store(journal_store)
    await _record_answered_turn(store)
    runner, gateway = _SerializingRunner(), _CountingGateway()

    finalized = await _reconciler(store, runner, gateway).finalize(
        _RESPONSE_EVENT_ID,
        _STOP_RECEIPT_ORDER,
        _noop,
    )

    assert finalized
    assert gateway.finalized == [_RESPONSE_EVENT_ID]
    stopped = store.get_turn_record(_SOURCE_EVENT_ID)
    assert stopped is not None
    assert stopped.completed
    assert stopped.user_stop_receipt_order == _STOP_RECEIPT_ORDER
    assert stopped.user_stop_settled_receipt_order == _STOP_RECEIPT_ORDER


async def test_the_same_stop_delivered_twice_cancels_once(journal_store: EventJournalStore) -> None:
    """A reaction can be redelivered, and the room must not say so twice."""
    store = await _store(journal_store)
    await _record_answered_turn(store)
    runner, gateway = _SerializingRunner(), _CountingGateway()
    reconciler = _reconciler(store, runner, gateway)

    await reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop)
    await reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop)

    assert gateway.finalized == [_RESPONSE_EVENT_ID]


async def test_one_stop_delivered_concurrently_cancels_once(journal_store: EventJournalStore) -> None:
    """The same stop arriving twice at once is one intent, not two.

    Same receipt order deliberately: that is what a redelivery of one
    reaction looks like. Two *different* receipt orders are two distinct
    stop intents and each is entitled to settle, so racing those would
    assert nothing about convergence.
    """
    store = await _store(journal_store)
    await _record_answered_turn(store)
    runner, gateway = _SerializingRunner(), _CountingGateway()
    reconciler = _reconciler(store, runner, gateway)

    results = await asyncio.gather(
        reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop),
        reconciler.finalize(_RESPONSE_EVENT_ID, _STOP_RECEIPT_ORDER, _noop),
    )

    assert all(results)
    assert gateway.finalized == [_RESPONSE_EVENT_ID]
    stopped = store.get_turn_record(_SOURCE_EVENT_ID)
    assert stopped is not None
    assert stopped.user_stop_settled_receipt_order == _STOP_RECEIPT_ORDER


async def test_stop_reaction_on_a_voice_echo_is_not_claimed(journal_store: EventJournalStore, tmp_path: Path) -> None:
    """A voice echo owns a visible event but no response, so a stop on it is left for the other consumers."""
    store = await _store(journal_store)
    await store.record_visible_echo("$voice", "$echo")
    config = Config()
    runtime_paths = test_runtime_paths(tmp_path)
    entity_ids(config, runtime_paths)
    journal = MagicMock(spec=JournalDispatcher)
    stop_manager = MagicMock(spec=StopManager)
    stop_manager.can_handle_stop_reaction.return_value = False
    reconciler = MagicMock(spec=UserStopReconciler)
    dispatcher = ReactionDispatcher(
        ReactionDispatcherDeps(
            runtime=SimpleNamespace(config=config, client=None, orchestrator=None),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="agent",
            journal_dispatcher=journal,
            agent_reply_memberships=MagicMock(),
            turn_policy=MagicMock(),
            turn_store=store,
            stop_manager=stop_manager,
            user_stop_reconciler=reconciler,
            ingress=MagicMock(),
            reserve_prompt_ingress_order=MagicMock(),
            enqueue_interactive_selection=AsyncMock(),
            emit_reaction_received_hooks=AsyncMock(),
            wait_for_admission_or_shutdown=AsyncMock(),
            config_confirmation=MagicMock(),
        ),
    )
    event = nio.Event.parse_event(
        {
            "type": "m.reaction",
            "event_id": "$stop",
            "sender": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$echo", "key": "🛑"}},
        },
    )
    assert isinstance(event, nio.ReactionEvent)

    assert await dispatcher._maybe_handle_stop_reaction(event, None) is False

    journal.claim_semantic_consumer.assert_not_awaited()
    reconciler.finalize.assert_not_awaited()


async def _noop() -> None:
    """Stand in for the caller's post-finalization notification."""


@pytest.mark.parametrize("stop_already_written", [False, True])
async def test_stop_on_a_voice_echo_without_a_response_target_changes_nothing(
    journal_store: EventJournalStore,
    stop_already_written: bool,
) -> None:
    """A stop naming a visible voice echo has no response to finalize, so it must not write or raise.

    The written case is the durable state an earlier release left behind before
    it raised, which replays after an upgrade.
    """
    store = await _store(journal_store)
    await store.record_visible_echo("$voice", "$echo")
    if stop_already_written:
        await store.record_user_stopped_response("$echo", 5)
    before = store.get_turn_record("$voice")
    runner, gateway = _SerializingRunner(), _CountingGateway()

    finalized = await _reconciler(store, runner, gateway).finalize("$echo", _STOP_RECEIPT_ORDER, _noop)

    assert finalized is False
    assert store.get_turn_record("$voice") == before
    assert gateway.finalized == []
    assert runner.cancel_requests == 0
