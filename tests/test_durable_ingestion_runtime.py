"""Runtime regressions across the real nio producer and MindRoom consumer."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import nio
import pytest
from nio.durable import DurableSyncConfig, RecordKind, SyncRecord
from nio.durable.transport import HttpError

from mindroom.bot import AgentBot
from mindroom.bot_room_lifecycle import BotRoomLifecycle
from mindroom.config.access import ResponderAccessConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DeliveryStage, RoomMembershipPosition
from mindroom.matrix._owned_session import MatrixCredentials, open_owned_matrix_session
from mindroom.matrix.client_session import create_authenticated_client
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from mindroom.matrix.state import MatrixState
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.bot_helpers import make_matrix_client_mock
from tests.test_bot_ready_hook import _agent_bot
from tests.test_durable_ingestion_admission import ROOM
from tests.test_event_journal_store import admit, interactive_edit, interactive_prompt
from tests.test_room_invites import _handle_invite, _live_router_invite_scenario, _pending_room_invites

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from nio.durable import DurableSync

    from mindroom.event_journal.store import PrincipalStore


@asynccontextmanager
async def _owned_session(bot: AgentBot) -> AsyncIterator[DurableSync]:
    opened = await open_owned_matrix_session(
        "https://localhost",
        MatrixCredentials(bot.agent_user.user_id, "DEVICE", "token"),
        bot.runtime_paths,
        consumer_store=bot.journal_principal(),
        new_consumer_generation=uuid4(),
        config=DurableSyncConfig(),
    )
    bot.client = opened.client
    bot._ingestion_session = opened.session
    try:
        yield opened.session
    finally:
        await opened.session.close()
        await opened.client.close()


def _joined_frame(events: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "next_batch": "next",
            "rooms": {"join": {ROOM: {"state": {"events": []}, "timeline": {"limited": False, "events": events}}}},
        },
    ).encode()


async def _consume_frame(bot: AgentBot, session: DurableSync, frame: bytes) -> None:
    frames: asyncio.Queue[bytes] = asyncio.Queue()
    frames.put_nowait(frame)

    async def request(*_args: object, **_kwargs: object) -> bytes:
        return await frames.get()

    session._transport.request = request
    session._maintain_crypto = AsyncMock()
    runner = asyncio.create_task(session.run())
    completed = False

    async def after_sync() -> None:
        nonlocal completed
        completed = True

    try:
        async with asyncio.timeout(2):
            while not completed:
                facts = await consume_one_ingestion_batch(
                    session,
                    bot.journal_principal(),
                    account_id=bot.agent_user.user_id,
                    before_admission=bot._before_ingestion_admission,
                    after_admission=bot._after_ingestion_admission,
                    after_sync=after_sync,
                )
                if facts is None:
                    await session.wait_for_work()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def _consume_repaired_frame(
    bot: AgentBot,
    session: DurableSync,
    frame: dict[str, object],
    recovered: list[dict[str, object]],
) -> list[SyncRecord]:
    """Consume a limited sync using a real Nio gap-recovery response."""
    frames: asyncio.Queue[bytes] = asyncio.Queue()
    frames.put_nowait(json.dumps(frame).encode())

    async def request(_method: str, path: str, *_args: object, **_kwargs: object) -> bytes:
        if "/messages" in path:
            return json.dumps({"start": "next", "end": "next2", "chunk": recovered}).encode()
        return await frames.get()

    session._transport.request = request
    runner = asyncio.create_task(session.run())
    complete = False
    seen: list[SyncRecord] = []

    async def after_sync() -> None:
        nonlocal complete
        complete = True

    try:
        async with asyncio.timeout(3):
            while not complete:
                batch = await session.next_batch()
                if batch is not None:
                    seen.extend(batch.records)
                facts = await consume_one_ingestion_batch(
                    session,
                    bot.journal_principal(),
                    account_id=bot.matrix_id.full_id,
                    before_admission=bot._before_ingestion_admission,
                    after_admission=bot._after_ingestion_admission,
                    after_sync=after_sync,
                )
                if facts is None:
                    await session.wait_for_work()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("rejoined", [False, True])
async def test_repaired_gap_fences_grants_until_authoritative_refresh(tmp_path: Path, rejoined: bool) -> None:
    """Recovered membership closes stale grants without granting from historical joins."""
    bot = _agent_bot(tmp_path, agent_name=ROUTER_AGENT_NAME)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", ROOM, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    sender = "@alice:localhost"
    join = {
        "type": "m.room.member",
        "event_id": "$join",
        "sender": sender,
        "state_key": sender,
        "origin_server_ts": 1,
        "content": {"membership": "join"},
    }
    leave = {**join, "event_id": "$leave", "origin_server_ts": 2, "content": {"membership": "leave"}}
    tail = {
        "type": "m.room.message",
        "event_id": "$tail",
        "sender": sender,
        "origin_server_ts": 4,
        "content": {"msgtype": "m.text", "body": "hello"},
    }
    recovered = [leave]
    if rejoined:
        recovered.append({**join, "event_id": "$rejoin", "origin_server_ts": 3})
    index = bot._runtime_view.agent_reply_memberships
    bot._schedule_reply_authorized_call_revocation = MagicMock()
    async with _owned_session(bot) as session:
        await _consume_frame(bot, session, _joined_frame([join]))
        client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM])
        control = nio.RoomMember(bot.agent_user.user_id, None, None)
        member = nio.RoomMember(sender, None, None)
        client.joined_members.return_value = nio.JoinedMembersResponse(members=[control, member], room_id=ROOM)
        await index.refresh(bot.config, bot.runtime_paths, client)
        assert index.is_allowed(sender, ["grant"], bot.config, bot.runtime_paths)
        frame = {
            "next_batch": "next2",
            "rooms": {
                "join": {
                    ROOM: {
                        "state": {"events": []},
                        "timeline": {
                            "limited": True,
                            "prev_batch": "boundary",
                            "events": [tail],
                        },
                    },
                },
            },
        }
        seen = await _consume_repaired_frame(bot, session, frame, [*recovered, tail])
        assert not index.is_allowed(sender, ["grant"], bot.config, bot.runtime_paths)
        assert not any(record.kind is RecordKind.LOSS for record in seen)
        assert [record.provenance for record in seen if record.source.get("event_id") == "$leave"] == [
            nio.TimelineEventProvenance.RECOVERED,
        ]
        bot._schedule_reply_authorized_call_revocation.assert_called()
        client.joined_members.return_value = nio.JoinedMembersResponse(
            members=[control, member] if rejoined else [control],
            room_id=ROOM,
        )
        await bot._router_reply_membership_sync.refresh_if_needed(
            bot.config,
            lambda: index.refresh(bot.config, bot.runtime_paths, client),
        )
        assert client.joined_members.await_count == 2
        assert index.is_allowed(sender, ["grant"], bot.config, bot.runtime_paths) is rejoined


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_failed_durable_join_retains_pending_invitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """An ambiguous HTTP failure must retain both the invitation and decrypt fence."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    bot.change_local_membership = AgentBot.change_local_membership.__get__(bot)
    bot._room_lifecycle.deps = replace(bot._room_lifecycle.deps, change_membership=bot.change_local_membership)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    async with _owned_session(bot) as session:
        bot.client.invited_rooms[room.room_id] = room
        session._transport.request = AsyncMock(side_effect=HttpError(status))
        with pytest.raises(RuntimeError, match="Failed to join invited room"):
            await _handle_invite(bot, room, event)
        assert room.room_id in _pending_room_invites(config, ROUTER_AGENT_NAME)
        assert bot._room_lifecycle.decrypt_notice_is_fenced(room.room_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_at", ["lock", "producer", "admission"])
@pytest.mark.parametrize("replacement", [None, "@disallowed:localhost"])
async def test_invite_authorization_is_rechecked_after_membership_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_at: str,
    replacement: str | None,
) -> None:
    """A queued join cannot use an invitation withdrawn or replaced while it waited."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    assert room.inviter is not None
    config.router.accept_invites = [room.inviter]
    reached = asyncio.Event()
    release = asyncio.Event()

    async def change(room_id: str, target: str, *, is_authorized: Callable[[], bool] | None = None) -> bool:
        if wait_at == "lock":
            reached.set()
        return await AgentBot.change_local_membership(bot, room_id, target, is_authorized=is_authorized)

    bot._room_lifecycle.deps = replace(bot._room_lifecycle.deps, change_membership=change)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    async with _owned_session(bot) as session:
        bot.client.invited_rooms[room.room_id] = room
        request = AsyncMock(return_value=b"{}")
        session._transport.request = request
        if wait_at == "lock":
            await bot._local_membership_lock.acquire()
        elif wait_at == "producer":

            async def wait_for_producer() -> None:
                reached.set()
                await release.wait()

            monkeypatch.setattr(session, "wait_for_membership_idle", wait_for_producer)
        else:

            async def wait_for_admission() -> None:
                reached.set()
                await release.wait()

            monkeypatch.setattr(session, "next_batch", wait_for_admission)
        task = asyncio.create_task(_handle_invite(bot, room, event))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            if replacement is None:
                bot.client.invited_rooms.pop(room.room_id)
            else:
                room.inviter = replacement
            if wait_at == "lock":
                bot._local_membership_lock.release()
            release.set()
            await asyncio.wait_for(task, 2)
            request.assert_not_awaited()
            assert room.room_id not in bot._room_lifecycle.invited_rooms
            assert not bot._room_lifecycle.decrypt_notice_is_fenced(room.room_id)
        finally:
            release.set()
            if bot._local_membership_lock.locked():
                bot._local_membership_lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["persistence", "welcome"])
@pytest.mark.parametrize("policy_allows_recovery", [True, False])
async def test_joined_invitation_recovers_without_an_invite_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
    policy_allows_recovery: bool,
) -> None:
    """An accepted join survives unfinished app work and startup room cleanup."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=AgentBot.change_local_membership.__get__(bot),
    )
    failure = OSError("interrupted invitation completion")
    if failure_at == "persistence":
        monkeypatch.setattr(bot._room_lifecycle, "_remember_invited_room", MagicMock(side_effect=failure))
    else:
        monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock(side_effect=failure))
    async with _owned_session(bot) as session:
        bot.client.invited_rooms[room.room_id] = room
        session._transport.request = AsyncMock(return_value=b"{}")
        with pytest.raises(OSError, match="interrupted invitation completion"):
            await _handle_invite(bot, room, event)
        assert room.room_id in bot.client.rooms
        assert room.room_id not in bot.client.invited_rooms
        await consume_one_ingestion_batch(session, bot.journal_principal(), account_id=bot.agent_user.user_id)

    config.router.accept_invites = policy_allows_recovery
    bot._room_lifecycle = BotRoomLifecycle(bot._room_lifecycle.deps)
    welcome = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", welcome)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[room.room_id]))
    async with _owned_session(bot) as session:
        request = AsyncMock()
        session._transport.request = request
        assert room.room_id in bot.client.rooms
        assert room.room_id not in bot.client.invited_rooms
        # Startup computes cleanup before welcome admission opens.
        assert await bot._room_lifecycle._rooms_to_leave() == ([] if policy_allows_recovery else [room.room_id])
        welcome.assert_not_awaited()
        await bot._room_lifecycle.reconcile_pending_invites()
        assert (room.room_id in bot._room_lifecycle.invited_rooms) is policy_allows_recovery
        assert welcome.await_count == int(policy_allows_recovery)
        assert (room.room_id not in _pending_room_invites(config, ROUTER_AGENT_NAME)) is policy_allows_recovery
        request.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_completion_keeps_a_replacement_pending_inviter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing one welcome must not erase a later invitation's durable work."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=AgentBot.change_local_membership.__get__(bot),
    )
    replacement = "@replacement:localhost"

    async def welcome(_room_id: str, _sender: str) -> None:
        bot._room_lifecycle.record_pending_room_invite(room.room_id, replacement)

    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", welcome)
    async with _owned_session(bot) as session:
        bot.client.invited_rooms[room.room_id] = room
        session._transport.request = AsyncMock(return_value=b"{}")
        await _handle_invite(bot, room, event)
        assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {room.room_id: replacement}


@pytest.mark.asyncio
async def test_malformed_message_does_not_block_following_valid_message(tmp_path: Path) -> None:
    """An ordinary malformed payload must not poison durable batch replay."""
    bot = _agent_bot(tmp_path)
    async with _owned_session(bot) as session:
        await _consume_frame(
            bot,
            session,
            _joined_frame(
                [
                    {
                        "type": "m.room.message",
                        "event_id": "$bad",
                        "sender": "@alice:example.org",
                        "origin_server_ts": 100,
                        "content": {"msgtype": "m.image", "body": "missing URL"},
                    },
                    {
                        "type": "m.room.message",
                        "event_id": "$good",
                        "sender": "@alice:example.org",
                        "origin_server_ts": 101,
                        "content": {"msgtype": "m.text", "body": "hello"},
                    },
                ],
            ),
        )
        assert await bot.journal_principal().load_event("$bad") is None
        assert await bot.journal_principal().load_event("$good") is not None
    async with _owned_session(bot) as session:
        assert await session.next_batch() is None


@pytest.mark.asyncio
async def test_quiesce_is_bounded_when_delivery_projection_cannot_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source drain must let shutdown continue while retaining blocked input."""
    bot = _agent_bot(tmp_path)
    principal = bot.journal_principal()
    account = bot.agent_user.user_id
    await admit(principal, "$turn", sender="@alice:example.org")
    await admit(
        principal,
        "$prompt",
        sender=account,
        content=interactive_prompt("Old?", "old", source_event_id="$turn"),
    )
    await principal.enqueue_matrix_delivery(
        delivery_id="$edit",
        stage=DeliveryStage.FINAL,
        room_id=ROOM,
        thread_id=None,
        payload=interactive_edit("$prompt", "New?", "new", source_event_id="$turn"),
        edits_event_id="$prompt",
    )
    await principal.claim_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    async with _owned_session(bot) as session:
        with session._store.transaction():
            session._store.publish(
                (
                    SyncRecord(
                        RecordKind.TIMELINE,
                        ROOM,
                        {
                            "type": "m.reaction",
                            "event_id": "$reaction",
                            "sender": "@alice:example.org",
                            "origin_server_ts": 3000,
                            "content": {
                                "m.relates_to": {"rel_type": "m.annotation", "event_id": "$prompt", "key": "1"},
                            },
                        },
                        provenance=nio.TimelineEventProvenance.RECOVERED,
                    ),
                ),
            )
        recovery_attempted = asyncio.Event()

        async def unavailable_delivery() -> bool:
            recovery_attempted.set()
            return False

        monkeypatch.setattr(bot, "_recover_unacknowledged_matrix_deliveries", unavailable_delivery)
        sync = asyncio.create_task(bot.sync_forever())
        try:
            await asyncio.wait_for(recovery_attempted.wait(), timeout=2)
            await asyncio.wait_for(bot._quiesce_matrix_ingestion(), timeout=5.2)
        finally:
            bot._sync_shutting_down = True
            bot._delivery_recovery_wake.set()
            sync.cancel()
            await asyncio.gather(sync, return_exceptions=True)
            if bot._delivery_recovery_task is not None:
                await asyncio.gather(bot._delivery_recovery_task, return_exceptions=True)
    async with _owned_session(bot) as session:
        retained = await session.next_batch()
        assert retained is not None
        assert retained.records[0].source["event_id"] == "$reaction"
        assert await principal.load_event("$reaction") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_token", [None, "previous-sync"])
@pytest.mark.parametrize("legacy_journal", [False, True])
async def test_upgrade_keeps_old_history_silent_and_preserves_crypto(
    tmp_path: Path,
    saved_token: str | None,
    legacy_journal: bool,
) -> None:
    """Automatic upgrade preserves keys and never turns historical input into work."""
    if legacy_journal:
        tracking = tmp_path / "tracking"
        tracking.mkdir()
        with closing(sqlite3.connect(tracking / "event_journal.db")) as connection:
            connection.executescript((Path(__file__).parent / "fixtures" / "pre_nio1_journal.sql").read_text())
            connection.executescript("""
                INSERT INTO journal_identity VALUES (TRUE, 'old-journal');
                INSERT INTO journal_events (
                    principal_id, event_id, room_id, thread_id, kind, sender, origin_server_ts,
                    source_json, membership_epoch, state
                ) VALUES ('code@@mindroom_code:localhost', '$old-request', '!room:example.org', '',
                          'message', '@alice:example.org', 100, '{}', 9, 'pending');
                INSERT INTO room_membership VALUES ('code@@mindroom_code:localhost', '!room:example.org', 9, 0, 1);
            """)
        (tracking / "event_journal_binding.json").write_text(
            '{"generation":"old-journal","database":"sqlite tracking/event_journal.db"}',
        )
        continuity = tmp_path / "sync_continuity"
        continuity.mkdir()
        (continuity / "code.json").write_text(
            '{"version":"mindroom-sync-continuity-v3","revision":2,"pending_join_decrypt_fences":[], '
            '"checkpoint":{"store_generation":"old-journal","token":"old-checkpoint"}}',
        )
    bot = _agent_bot(tmp_path)
    principal = bot.journal_principal()
    legacy = create_authenticated_client(
        "https://localhost",
        bot.agent_user.user_id,
        "DEVICE",
        "token",
        bot.runtime_paths,
    )
    assert legacy.olm is not None
    identity_keys = legacy.olm.account.identity_keys
    assert legacy.store is not None
    if saved_token is not None:
        legacy.store.save_sync_token(saved_token)
    await legacy.close()
    legacy.store.database.close()

    def frame(event_id: str, token: str) -> bytes:
        body = json.loads(
            _joined_frame(
                [
                    {
                        "type": "m.room.message",
                        "event_id": event_id,
                        "sender": "@alice:example.org",
                        "origin_server_ts": 100,
                        "content": {"msgtype": "m.text", "body": "Please answer this request"},
                    },
                ],
            ),
        )
        body["next_batch"] = token
        return json.dumps(body).encode()

    async with _owned_session(bot) as session:
        await bot._room_lifecycle.restore_pending_join_decrypt_fences()
        assert bot.client is not None
        assert bot.client.olm is not None
        assert bot.client.olm.account.identity_keys == identity_keys
        await _consume_frame(bot, session, frame("$old-request", "baseline"))
        assert await principal.load_event("$old-request") is not None
        assert not await principal.pending()
        await _consume_frame(bot, session, frame("$new-request", "live"))
        assert [event.event_id for event in await principal.pending()] == ["$new-request"]
        await principal.settle("$new-request")

    assert bot._own_journal is not None
    await bot._own_journal.close()
    bot = _agent_bot(tmp_path)
    principal = bot.journal_principal()
    async with _owned_session(bot) as session:
        await _consume_frame(bot, session, frame("$old-request", "history-repeated"))
        assert not await principal.pending()
        await _consume_frame(bot, session, frame("$new-request", "duplicate"))
        assert not await principal.pending()
        await _consume_frame(bot, session, frame("$after-restart", "after-restart"))
        assert [event.event_id for event in await principal.pending()] == ["$after-restart"]
        assert await session.next_batch() is None


@pytest.mark.asyncio
async def test_startup_cleanup_leaves_room_before_first_membership_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown producer state cannot certify that a server-joined room was left."""
    bot = _agent_bot(tmp_path)
    principal = bot.journal_principal()
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=AgentBot.change_local_membership.__get__(bot),
    )
    monkeypatch.setattr("mindroom.matrix.rooms.is_dm_room", AsyncMock(return_value=False))
    first_poll = asyncio.Event()
    leave_requests: list[str] = []

    async def request(_method: str, path: str, *_args: object, **_kwargs: object) -> bytes:
        if "/sync" in path:
            first_poll.set()
            await asyncio.Event().wait()
        assert path.partition("?")[0].endswith("/leave")
        leave_requests.append(path)
        return b"{}"

    async with _owned_session(bot) as session:
        assert bot.client is not None
        monkeypatch.setattr(bot.client, "joined_rooms", AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM])))
        session._transport.request = request
        session._maintain_crypto = AsyncMock()
        runner = asyncio.create_task(session.run())
        try:
            await asyncio.wait_for(first_poll.wait(), timeout=2)
            await asyncio.wait_for(bot.leave_unconfigured_rooms(), timeout=2)
            assert len(leave_requests) == 1
            facts = await consume_one_ingestion_batch(session, principal, account_id=bot.agent_user.user_id)
            assert facts is not None
            assert await principal.membership_position(ROOM) == RoomMembershipPosition("leave", 0)
            assert await principal.ingestion_membership_position(ROOM) == RoomMembershipPosition("leave", 0)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


async def _retain_membership_before_admission(bot: AgentBot, retained: str) -> None:
    """Stop a real source after committing membership but before app admission."""
    principal = bot.journal_principal()
    async with _owned_session(bot) as session:
        frame = json.loads(_joined_frame([]))
        if retained == "leave":
            await _consume_frame(bot, session, _joined_frame([]))
            frame["rooms"]["leave"] = frame["rooms"].pop("join")
            frame["next_batch"] = "departed"
        session._transport.request = AsyncMock(return_value=json.dumps(frame).encode())
        session._maintain_crypto = AsyncMock()
        runner = asyncio.create_task(session.run())
        try:
            await asyncio.wait_for(session.wait_for_work(), timeout=2)
            assert await principal.ingestion_membership_position(ROOM) == (
                RoomMembershipPosition("join", 0) if retained == "leave" else None
            )
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_entity_removal_leaves_with_retained_input_before_closing_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal keeps the real source and pump available until the leave finishes."""
    bot = _agent_bot(tmp_path)
    await _retain_membership_before_admission(bot, "join")
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=AgentBot.change_local_membership.__get__(bot),
    )
    monkeypatch.setattr("mindroom.matrix.rooms.is_dm_room", AsyncMock(return_value=False))
    monkeypatch.setattr(bot, "_on_ingestion_frame_completion", AsyncMock())
    leaves: list[str] = []

    async def request(_method: str, path: str, *_args: object, **_kwargs: object) -> bytes:
        if "/sync" in path:
            await asyncio.Event().wait()
        assert path.partition("?")[0].endswith("/leave")
        leaves.append(path)
        return b"{}"

    async with _owned_session(bot) as session:
        assert bot.client is not None
        monkeypatch.setattr(bot.client, "joined_rooms", AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM])))
        session._transport.request = request
        session._maintain_crypto = AsyncMock()
        orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
        orchestrator.agent_bots[bot.agent_name] = bot
        orchestrator._approval_transport.reconcile_unavailable_entities = AsyncMock()
        sync = asyncio.create_task(bot.sync_forever())
        orchestrator._sync_tasks[bot.agent_name] = sync
        try:
            await asyncio.wait_for(orchestrator._remove_deleted_entities({bot.agent_name}), timeout=2)
            assert len(leaves) == 1
            assert bot.agent_name not in orchestrator.agent_bots
            assert bot.agent_name not in orchestrator._sync_tasks
            assert sync.done()
        finally:
            sync.cancel()
            await asyncio.gather(sync, return_exceptions=True)


@pytest.mark.asyncio
async def test_removal_cleanup_bounds_wait_for_stopped_ingestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecoverable retained input must not leave removal waiting forever."""
    bot = _agent_bot(tmp_path)
    await _retain_membership_before_admission(bot, "join")
    bot.change_local_membership = AgentBot.change_local_membership.__get__(bot)
    bot._room_lifecycle.deps = replace(bot._room_lifecycle.deps, change_membership=bot.change_local_membership)
    monkeypatch.setattr("mindroom.matrix.rooms.is_dm_room", AsyncMock(return_value=False))
    async with _owned_session(bot) as session:
        assert bot.client is not None
        monkeypatch.setattr(bot.client, "joined_rooms", AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM])))
        session._transport.request = AsyncMock()
        cleanup = asyncio.create_task(bot.leave_rooms())
        try:
            done, _pending = await asyncio.wait([cleanup], timeout=5.5)
            assert cleanup in done, "room cleanup waited forever for a stopped admission pump"
            await cleanup
            session._transport.request.assert_not_awaited()
            assert await session.next_batch() is not None
        finally:
            for task in asyncio.all_tasks():
                if task.get_name() == "matrix_leave_room":
                    task.cancel()
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["join", "leave"])
@pytest.mark.parametrize("retained", ["join", "leave"])
async def test_startup_reconciles_membership_saved_before_application_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    retained: str,
) -> None:
    """Restarted room maintenance must wait for retained producer membership."""
    bot = _agent_bot(tmp_path)
    principal = bot.journal_principal()
    await _retain_membership_before_admission(bot, retained)
    bot.rooms = [ROOM] if target == "join" else []
    configured_setup = AsyncMock()
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=AgentBot.change_local_membership.__get__(bot),
        on_configured_room_joined=configured_setup,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.is_dm_room", AsyncMock(return_value=False))
    monkeypatch.setattr(bot, "_on_ingestion_frame_completion", AsyncMock())
    position_read = asyncio.Event()
    read_position = type(principal).ingestion_membership_position

    async def observe_position(store: PrincipalStore, room_id: str) -> RoomMembershipPosition | None:
        position = await read_position(store, room_id)
        position_read.set()
        return position

    monkeypatch.setattr(type(principal), "ingestion_membership_position", observe_position)
    membership_requests: list[str] = []

    async def request(_method: str, path: str, *_args: object, **_kwargs: object) -> bytes:
        if "/sync" in path:
            await asyncio.Event().wait()
        action = "join" if "/join/" in path else path.partition("?")[0].rsplit("/", 1)[-1]
        assert action in {"join", "leave"}
        membership_requests.append(action)
        return b"{}"

    async with _owned_session(bot) as session:
        assert bot.client is not None
        monkeypatch.setattr(bot.client, "joined_rooms", AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM])))
        session._transport.request = request
        session._maintain_crypto = AsyncMock()
        maintenance = asyncio.create_task(bot.ensure_rooms())
        sync: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(position_read.wait(), timeout=2)
            sync = asyncio.create_task(bot.sync_forever())
            await asyncio.wait_for(maintenance, timeout=2)
            if target == "join":
                configured_setup.assert_awaited_once_with(ROOM)
            else:
                configured_setup.assert_not_awaited()
            assert membership_requests == ([target] if target != retained else [])
        finally:
            maintenance.cancel()
            if sync is not None:
                sync.cancel()
            await asyncio.gather(maintenance, *([sync] if sync is not None else []), return_exceptions=True)
