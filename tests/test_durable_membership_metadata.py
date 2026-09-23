"""Real nio member records carry tenure effects alongside their event disposition."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import nio
import pytest
from nio.durable import open_durable_sync

from mindroom.config.access import ResponderAccessConfig
from mindroom.event_journal import DeliveryProjectionPendingError, EventKind
from mindroom.event_journal import journal as journal_ops
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from mindroom.matrix.state import MatrixState
from tests.conftest import install_call_manager_mock, make_matrix_client_mock
from tests.test_bot_ready_hook import _router_bot_with_orchestrator

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import AdmissionFacts, EventJournalStore, IngestionRecordAdmission
    from mindroom.event_journal.backend import Transaction

ROOM = "!grant:localhost"
SENDER = "@alice:localhost"


def _member(event_id: str, user_id: str, membership: str) -> dict[str, object]:
    return {
        "type": "m.room.member",
        "event_id": event_id,
        "sender": user_id,
        "state_key": user_id,
        "origin_server_ts": 100,
        "content": {"membership": membership},
    }


def _message(event_id: str) -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": SENDER,
        "origin_server_ts": 100,
        "content": {"msgtype": "m.text", "body": "hello"},
    }


def _response(cursor: str, state: list[dict[str, object]], timeline: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "next_batch": cursor,
            "rooms": {
                "join": {
                    ROOM: {
                        "state": {"events": state},
                        "timeline": {"events": timeline, "limited": False},
                    },
                },
            },
        },
    ).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_leave_once", [False, True])
async def test_real_member_state_and_timeline_preserve_tenure_and_hooks(  # noqa: C901, PLR0915 - complete real source lifecycle
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    block_leave_once: bool,
) -> None:
    """State join and semantic leave/rejoin each carry their own atomic tenure effect."""
    bot, _orchestrator = _router_bot_with_orchestrator(tmp_path)
    account = bot.agent_user.user_id
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", ROOM, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    principal = bot.journal_principal()
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    client = nio.AsyncClient("https://localhost", account, device_id="DEVICE")
    client.restore_login(account, "DEVICE", "token")
    session = open_durable_sync(client, consumer_id=consumer.generation, store_path=tmp_path / "crypto")
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=session.stream_id)
    bot.client = client
    call_manager = AsyncMock()
    install_call_manager_mock(bot, call_manager)
    source: asyncio.Queue[bytes] = asyncio.Queue()

    async def request(*_args: object, **_kwargs: object) -> bytes:
        return await source.get()

    session._transport.request = request
    session._maintain_crypto = AsyncMock()
    completed = 0
    observed: list[tuple[str, bool, str, int]] = []
    blocked_leaves = 0
    apply_record = journal_ops._apply_ingestion_disposition

    def fail_after_leave(transaction: Transaction, principal_id: str, record: IngestionRecordAdmission) -> bool:
        nonlocal blocked_leaves
        semantic_new = apply_record(transaction, principal_id, record)
        if block_leave_once and record.event is not None and record.event.event_id == "$leave" and blocked_leaves == 0:
            blocked_leaves += 1
            message = "own leave transaction interrupted"
            raise DeliveryProjectionPendingError(message)
        return semantic_new

    monkeypatch.setattr(journal_ops, "_apply_ingestion_disposition", fail_after_leave)

    async def complete() -> None:
        nonlocal completed
        completed += 1

    async def after(
        record: IngestionRecordAdmission,
        facts: AdmissionFacts,
        provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        await bot._after_ingestion_admission(record, facts, provenance)
        event = record.event
        if event is not None:
            position = await principal.membership_position(ROOM)
            allowed = bot._runtime_view.agent_reply_memberships.is_allowed(
                SENDER,
                ["grant"],
                bot.config,
                bot.runtime_paths,
            )
            observed.append((event.event_id, allowed, position.membership, position.membership_epoch))

    async def drain_until(target: int) -> None:
        async with asyncio.timeout(3):
            while completed < target:
                try:
                    facts = await consume_one_ingestion_batch(
                        session,
                        principal,
                        account_id=account,
                        before_admission=bot._before_ingestion_admission,
                        after_admission=after,
                        after_sync=complete,
                    )
                except DeliveryProjectionPendingError:
                    position = await principal.membership_position(ROOM)
                    assert (position.membership, position.membership_epoch) == ("join", 0)
                    assert await principal.load_event("$leave") is None
                    assert "$before" in [event.event_id for event in await principal.pending()]
                    continue
                if facts is None:
                    await session.wait_for_work()

    runner = asyncio.create_task(session.run())
    try:
        await source.put(_response("one", [_member("$own-state", account, "join")], []))
        await drain_until(1)
        position = await principal.membership_position(ROOM)
        assert (position.membership, position.membership_epoch) == ("join", 0)
        call_manager.on_sync_room_membership.assert_awaited_once_with(joined_room_ids={ROOM}, left_room_ids=set())
        snapshot = make_matrix_client_mock(user_id=account)
        snapshot.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM])
        snapshot.joined_members.return_value = nio.JoinedMembersResponse(
            members=[nio.RoomMember(account, None, None)],
            room_id=ROOM,
        )
        await bot._runtime_view.agent_reply_memberships.refresh(bot.config, bot.runtime_paths, snapshot)
        await source.put(
            _response(
                "two",
                [],
                [
                    _member("$grant", SENDER, "join"),
                    _message("$before"),
                    _member("$leave", account, "leave"),
                    _message("$fenced"),
                    _member("$rejoin", account, "join"),
                    _message("$after"),
                ],
            ),
        )
        await drain_until(2)
        assert ("$before", True, "join", 0) in observed
        assert ("$leave", False, "leave", 1) in observed
        assert ("$fenced", False, "leave", 1) in observed
        assert ("$rejoin", False, "join", 1) in observed
        position = await principal.membership_position(ROOM)
        assert (position.membership, position.membership_epoch) == ("join", 1)
        pending = await principal.pending()
        # Rejoining does not authorize the remaining captured history as live.
        assert [event.event_id for event in pending if event.kind is EventKind.MESSAGE] == []
        assert (await principal.load_event("$leave")).kind is EventKind.ROOM_LIFECYCLE
        assert (await principal.load_event("$rejoin")).kind is EventKind.ROOM_LIFECYCLE
        assert call_manager.on_sync_room_membership.await_count == 3
        assert blocked_leaves == int(block_leave_once)
        await source.put(
            _response("three", [_member("$rejoin", account, "join")], []),
        )
        await drain_until(3)
        await bot._runtime_view.agent_reply_memberships.refresh(bot.config, bot.runtime_paths, snapshot)
        await source.put(_response("four", [], [_member("$live-grant", SENDER, "join"), _message("$live")]))
        await drain_until(4)
        assert ("$live", True, "join", 1) in observed
        assert [event.event_id for event in await principal.pending() if event.kind is EventKind.MESSAGE] == ["$live"]
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
        await session.close()
        await client.close()


@pytest.mark.asyncio
async def test_real_local_echoes_do_not_hide_later_departure(  # noqa: C901, PLR0915 - complete real source lifecycle
    tmp_path: Path,
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """Producer epochs fence both departures and admit work after the final rejoin."""
    account = "@bot:localhost"
    principal = journal_database().principal(account)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    client = nio.AsyncClient("https://localhost", account, device_id="DEVICE")
    client.restore_login(account, "DEVICE", "token")
    session = open_durable_sync(client, consumer_id=consumer.generation, store_path=tmp_path / "crypto")
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=session.stream_id)
    source: asyncio.Queue[bytes] = asyncio.Queue()
    local_calls: list[str] = []
    effects: list[tuple[str, int]] = []

    async def request(_method: str, path: str, *_args: object, **_kwargs: object) -> bytes:
        if "/sync" in path:
            return await source.get()
        local_calls.append(path)
        return b"{}"

    async def after(record: IngestionRecordAdmission, _facts: AdmissionFacts, _provenance: object) -> None:
        if record.membership is not None:
            position = await principal.membership_position(ROOM)
            effects.append((position.membership, position.membership_epoch))

    session._transport.request = request
    session._maintain_crypto = AsyncMock()
    runner = asyncio.create_task(session.run())

    async def consume() -> bool:
        batch = await session.next_batch()
        if batch is None:
            await session.wait_for_work()
            return False
        await consume_one_ingestion_batch(session, principal, account_id=account, after_admission=after)
        return batch.completes_sync

    async def sync(cursor: str, events: list[dict[str, object]], *, leave: bool = False) -> None:
        body = json.loads(_response(cursor, [], events))
        if leave:
            body["rooms"]["leave"] = body["rooms"].pop("join")
        await source.put(json.dumps(body).encode())
        async with asyncio.timeout(5):
            while not await consume():
                pass

    async def local(target: str) -> None:
        await session.wait_for_membership_idle()
        position = await principal.membership_position(ROOM)
        assert await session.change_membership(
            operation_id=uuid4(),
            room_id=ROOM,
            previous_membership=position.membership,
            previous_epoch=position.membership_epoch,
            current_membership=target,
        )
        await consume()

    async def assert_fenced(epoch: int) -> None:
        position = await principal.membership_position(ROOM)
        assert (position.membership, position.membership_epoch) == ("leave", epoch)
        assert not await principal.pending()
        page = await principal.read_conversation(room_id=ROOM, thread_id=None, limit=10)
        assert not page.messages

    try:
        await sync("initial", [_member("$initial", account, "join")])
        await sync("live-zero", [_message("$live-zero")])
        assert [event.event_id for event in await principal.pending()] == ["$live-zero"]
        await local("leave")
        await assert_fenced(1)
        await sync("leave-echo", [_member("$leave-echo", account, "leave")], leave=True)
        await local("join")
        await sync("join-echo", [_member("$join-echo", account, "join")])
        await sync("live-one", [_message("$live-one")])
        assert [event.event_id for event in await principal.pending()] == ["$live-one"]
        await sync("real-leave", [_member("$real-leave", account, "leave")], leave=True)
        await assert_fenced(2)
        await sync("real-rejoin", [_member("$real-rejoin", account, "join")])
        await sync("baseline", [_member("$real-rejoin", account, "join")])
        await sync("live-two", [_message("$live-two")])
        assert [event.event_id for event in await principal.pending()] == ["$live-two"]
        assert effects == [("join", 0), ("leave", 1), ("join", 1), ("leave", 2), ("join", 2)]
        assert len(local_calls) == 2
        await session.quiesce()
        await runner
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
        await session.close()
        await client.close()
