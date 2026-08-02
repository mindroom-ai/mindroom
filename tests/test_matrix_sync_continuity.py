"""Tests for Matrix sync continuity and recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import nio
import pytest
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot, TeamBot, _create_best_effort_task_wrapper
from mindroom.coalescing import CoalescingDrainResult, CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescedBatch, CoalescingKey, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.delivery_gateway import FinalizeStreamedResponseRequest, ResponseIdentity
from mindroom.dispatch_admission import DispatchSourceAdmission
from mindroom.dispatch_handoff import PendingDispatchMetadata
from mindroom.dispatch_obligations import DispatchCallbackKind
from mindroom.dispatch_obligations.events import DispatchCallbackResult
from mindroom.dispatch_obligations.storage import DispatchObligationCorruptionError
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.handled_turns import TurnRecord
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.decrypt_failure import e2ee_stats
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.response_runner import ResponseRequest
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    GENERIC_SHUTDOWN,
    ORDERLY_SHUTDOWN,
    SYNC_RESTART_SHUTDOWN,
    RuntimeShutdownIntent,
)
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, StreamingResponse
from tests.bot_helpers import _configured_team_test_config, _configured_team_user
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    install_shutdown_drain_mocks,
    make_matrix_client_mock,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.sync_continuity_helpers import clear_sync_token, load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from mindroom.coalescing import LaneSlot, _GateEntry
    from mindroom.final_delivery import FinalDeliveryOutcome

_CACHE_GENERATION = "test-cache-generation"


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )


def _agent_bot(tmp_path: Path, *, agent_name: str = "code") -> AgentBot:
    config = _config(tmp_path)
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name=agent_name,
            password=TEST_PASSWORD,
            display_name=agent_name.title(),
            user_id=f"@mindroom_{agent_name}:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    install_runtime_cache_support(bot)
    return bot


def test_dispatch_recovery_room_contract_prefers_cache_and_guarantees_room_id(tmp_path: Path) -> None:
    """Recovery room state is best-effort, but its exact room ID is always available."""
    bot = _agent_bot(tmp_path)
    cached_room = nio.MatrixRoom("!cached:localhost", bot.agent_user.user_id)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.rooms = {cached_room.room_id: cached_room}

    assert bot._room_for_dispatch_obligation(cached_room.room_id) is cached_room
    assert bot._room_for_dispatch_obligation("!missing:localhost").room_id == "!missing:localhost"


def test_terminal_turn_settlement_hands_sqlite_work_to_event_loop_owner(
    tmp_path: Path,
) -> None:
    """Handled-turn persistence delegates settlement to its dedicated retry owner."""
    bot = _agent_bot(tmp_path)
    callback = bot._turn_store.deps.on_terminal_turn_persisted

    assert callback == bot._turn_settlement_retry.retry


def _install_fast_response_drain(bot: AgentBot) -> None:
    """Keep real response draining while shortening its bounded waits."""
    drain_inbox_responses = bot._response_runner.drain_inbox_responses

    async def fast_drain(*, cancel_after_seconds: float | None, shutdown_intent: RuntimeShutdownIntent) -> bool:
        assert cancel_after_seconds == 5.0
        return await drain_inbox_responses(cancel_after_seconds=0.01, shutdown_intent=shutdown_intent)

    bot._response_runner.drain_inbox_responses = fast_drain


def _certified_shutdown_bot(tmp_path: Path) -> AgentBot:
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_previous", cache_generation=_CACHE_GENERATION)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_shutdown")
    wrap_extracted_collaborators(bot, "_coalescing_gate", "_response_runner")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    return bot


def _token_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_continuity" / f"{agent_name}.json"


def _legacy_token_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_tokens" / f"{agent_name}.token"


def _load_sync_token_value(tmp_path: Path, agent_name: str) -> str | None:
    checkpoint = load_sync_checkpoint(tmp_path, agent_name)
    if checkpoint is None:
        return None
    return checkpoint.token


async def _admit_and_dispatch_decrypt(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.MegolmEvent,
) -> None:
    """Drive one Megolm event through nio's two callback phases."""
    await bot._dispatch_obligation_runner._admit_source_event(
        room,
        event,
        nio.TimelineEventProvenance.LIVE,
    )
    await bot._dispatch_obligation_runner.task_wrapper(
        DispatchCallbackKind.DECRYPTION_FAILURE,
        owner=bot._runtime_view,
    )(room, event)


@pytest.mark.asyncio
async def test_warm_join_decrypt_notice_waits_for_trusted_sync_containing_room(
    tmp_path: Path,
) -> None:
    """Fenced Megolm events request recovery but stay visibly silent until trusted sync."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.outgoing_key_requests = {}
    bot._first_sync_done = True
    room = nio.MatrixRoom(room_id, bot.agent_user.user_id)

    def megolm_event(event_id: str) -> nio.MegolmEvent:
        event = nio.MegolmEvent.from_dict(
            {
                "event_id": event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "type": "m.room.encrypted",
                "room_id": room_id,
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "cipher",
                    "sender_key": "sender-key",
                    "session_id": "pre-join-session",
                    "device_id": "DEVICE",
                },
            },
        )
        assert isinstance(event, nio.MegolmEvent)
        return event

    def sync_response(*, joined_room_id: str, next_batch: str) -> nio.SyncResponse:
        response = MagicMock(spec=nio.SyncResponse)
        response.next_batch = next_batch
        response.unrecovered_room_ids = frozenset()
        response.rooms = MagicMock(
            join={
                joined_room_id: MagicMock(
                    state=[],
                    timeline=MagicMock(events=[], limited=False),
                ),
            },
            leave={},
        )
        return response

    notice = AsyncMock(return_value=True)
    cache_result = AsyncMock(
        side_effect=[
            SyncCacheWriteResult(complete=False),
            SyncCacheWriteResult(complete=True),
            SyncCacheWriteResult(complete=True),
        ],
    )
    admitted_during_join: list[DispatchSourceAdmission] = []

    async def join_while_sync_is_live(_client: object, joining_room_id: str) -> bool:
        admitted_during_join.append(
            await bot._cold_history_fence.admit_source(
                joining_room_id,
                "$during-join",
                DispatchCallbackKind.DECRYPTION_FAILURE,
            ),
        )
        return True

    before_failures = e2ee_stats().decrypt_failures
    with (
        capture_logs() as logs,
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[])),
        patch("mindroom.bot_room_lifecycle.join_room", new=join_while_sync_is_live),
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.matrix.decrypt_failure._send_decrypt_failure_notice", new=notice),
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            new=cache_result,
        ),
    ):
        await bot.join_configured_rooms()
        assert admitted_during_join == [DispatchSourceAdmission.DECRYPT_NOTICE_FENCED]
        assert (
            await bot._cold_history_fence.admit_source(
                room_id,
                "$ordinary-message",
                DispatchCallbackKind.MESSAGE,
            )
            is DispatchSourceAdmission.ACCEPTED
        )
        assert (
            await bot._cold_history_fence.admit_source(
                room_id,
                "$pre-join",
                DispatchCallbackKind.DECRYPTION_FAILURE,
            )
            is DispatchSourceAdmission.DECRYPT_NOTICE_FENCED
        )
        pre_join_event = megolm_event("$pre-join")
        await _admit_and_dispatch_decrypt(bot, room, pre_join_event)
        assert not bot._dispatch_obligation_store.has_pending(
            "$pre-join",
            DispatchCallbackKind.DECRYPTION_FAILURE,
        )
        notice.assert_not_awaited()
        bot.client.request_room_key.assert_awaited_once_with(pre_join_event)
        assert e2ee_stats().decrypt_failures == before_failures + 1
        assert any(
            entry["event"] == "matrix_dispatch_source_fenced"
            and entry["reason"] == DispatchSourceAdmission.DECRYPT_NOTICE_FENCED
            and entry["source_event_id"] == "$pre-join"
            for entry in logs
        )

        await bot._on_sync_response(
            sync_response(
                joined_room_id=room_id,
                next_batch="s_uncertified_room",
            ),
        )
        uncertified_event = megolm_event("$after-uncertified-room")
        await _admit_and_dispatch_decrypt(bot, room, uncertified_event)
        assert not bot._dispatch_obligation_store.has_pending(
            "$after-uncertified-room",
            DispatchCallbackKind.DECRYPTION_FAILURE,
        )
        notice.assert_not_awaited()

        await bot._on_sync_response(
            sync_response(
                joined_room_id="!other:localhost",
                next_batch="s_certified_other",
            ),
        )
        certified_other_event = megolm_event("$after-certified-other")
        await _admit_and_dispatch_decrypt(bot, room, certified_other_event)
        assert not bot._dispatch_obligation_store.has_pending(
            "$after-certified-other",
            DispatchCallbackKind.DECRYPTION_FAILURE,
        )
        notice.assert_not_awaited()

        await bot._on_sync_response(
            sync_response(
                joined_room_id=room_id,
                next_batch="s_certified_room",
            ),
        )
        certified_room_event = megolm_event("$after-certified-room")
        await _admit_and_dispatch_decrypt(bot, room, certified_room_event)
        assert await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    notice.assert_awaited_once()


def test_stale_continuity_publication_cannot_restore_removed_join_fence(
    tmp_path: Path,
) -> None:
    """Runtime join fences must follow durable revision order, not task completion order."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    older = bot._sync_continuity_store.update_join_fences(add=(room_id,))
    newer = bot._sync_continuity_store.update_join_fences(remove=(room_id,))

    bot._room_lifecycle.apply_continuity_record(newer)
    bot._room_lifecycle.apply_continuity_record(older)

    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


@pytest.mark.asyncio
async def test_join_fence_restore_keeps_durable_fences_when_inventory_unavailable(
    tmp_path: Path,
) -> None:
    """Transient joined-room failure must preserve safe fences without aborting startup."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._sync_continuity_store.update_join_fences(add=(room_id,))

    with (
        capture_logs() as logs,
        patch(
            "mindroom.bot_room_lifecycle.get_joined_rooms",
            new=AsyncMock(return_value=None),
        ),
    ):
        await bot._room_lifecycle.restore_pending_join_decrypt_fences()

    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    assert any(entry["event"] == "matrix_join_fence_restore_joined_rooms_unavailable" for entry in logs)


@pytest.mark.asyncio
async def test_sliding_response_skips_continuity_write_without_join_fences(
    tmp_path: Path,
) -> None:
    """Steady Sliding responses must avoid off-loop store work when no fence exists."""
    bot = _agent_bot(tmp_path)
    bot.config.matrix_sync = MatrixSyncConfig(mode="sliding")
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._first_sync_done = True
    response = nio.SlidingSyncResponse(
        "pos_after",
        rooms={"!room:localhost": nio.SlidingSyncRoom(membership="join")},
    )

    with (
        patch.object(
            bot,
            "_apply_own_room_membership_from_sliding_sync",
            new=AsyncMock(),
        ),
        patch.object(
            bot._sync_continuity_store,
            "update_join_fences",
            side_effect=AssertionError("unexpected continuity write"),
        ),
    ):
        await bot._handle_sliding_sync_response(response)


@pytest.mark.asyncio
async def test_classic_unrecovered_gap_rejects_checkpoint(
    tmp_path: Path,
) -> None:
    """Classic next_batch cannot certify while nio reports an open recovery gap."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_with_gap"
    response.unrecovered_room_ids = frozenset({"!gap:localhost"})
    response.rooms = MagicMock(join={}, leave={})

    with patch.object(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        new=AsyncMock(
            return_value=SyncCacheWriteResult(
                complete=True,
                unrecovered_room_ids=("!gap:localhost",),
            ),
        ),
    ):
        await bot._on_sync_response(response)

    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot.client.next_batch == "s_test_token"


@pytest.mark.asyncio
async def test_classic_incomplete_cache_rejects_checkpoint(
    tmp_path: Path,
) -> None:
    """An incomplete cache write cannot advance the Classic checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_gap"
    bot._first_sync_done = True
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_gap"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={}, leave={})

    with patch.object(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        new=AsyncMock(
            return_value=SyncCacheWriteResult(
                complete=False,
                limited_room_ids=("!gap:localhost",),
            ),
        ),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch == "s_after_gap"


@pytest.mark.asyncio
async def test_tokenless_pre_certification_failure_rewinds_cursor(
    tmp_path: Path,
) -> None:
    """Failed first response must rewind its uncertified cursor."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_failed"
    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_failed"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={}, leave={})

    with (
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            new=AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
        ),
        patch.object(
            bot,
            "_run_pre_certification_sync_response_side_effects",
            new=AsyncMock(side_effect=RuntimeError("side effect failed")),
        ),
        pytest.raises(RuntimeError, match="side effect failed"),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch is None


@pytest.mark.asyncio
async def test_cold_history_drop_emits_operator_telemetry(tmp_path: Path) -> None:
    """Rejected history identifies exact source and fence reason."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
    event = _text_event("$cold-history", "old", 1)
    admission = bot._dispatch_obligation_runner._admit_source_event

    with capture_logs() as logs:
        await admission(
            room,
            event,
            nio.TimelineEventProvenance.HISTORY,
        )

    assert not bot._dispatch_obligation_store.has_pending(
        event.event_id,
        DispatchCallbackKind.MESSAGE,
    )
    assert any(
        entry["event"] == "matrix_dispatch_source_fenced"
        and entry["reason"] == DispatchSourceAdmission.COLD_HISTORY_FENCED
        and entry["source_event_id"] == event.event_id
        for entry in logs
    )


@pytest.mark.asyncio
async def test_sliding_trusted_sync_clears_joined_room_decrypt_notice_fence(
    tmp_path: Path,
) -> None:
    """A Sliding invite keeps the fence until membership becomes joined."""
    bot = _agent_bot(tmp_path)
    bot.config.matrix_sync = MatrixSyncConfig(mode="sliding")
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._first_sync_done = True

    with (
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[])),
        patch("mindroom.bot_room_lifecycle.join_room", AsyncMock(return_value=True)),
    ):
        await bot.join_configured_rooms()

    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room:localhost")

    await bot._on_sync_response(
        nio.SlidingSyncResponse(
            "pos_after_invite",
            rooms={"!room:localhost": nio.SlidingSyncRoom(membership="invite")},
        ),
    )

    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room:localhost")

    await bot._on_sync_response(
        nio.SlidingSyncResponse(
            "pos_after_join",
            rooms={"!room:localhost": nio.SlidingSyncRoom(membership="join")},
        ),
    )

    assert not bot._room_lifecycle.decrypt_notice_is_fenced("!room:localhost")


@pytest.mark.asyncio
async def test_sliding_join_fence_settlement_survives_restart(
    tmp_path: Path,
) -> None:
    """Sliding join settlement preserves the unrelated Classic checkpoint."""
    room_id = "!room:localhost"
    store = SyncContinuityStore(tmp_path, "code")
    store.replace_checkpoint(SyncCheckpoint("s_classic", cache_generation=_CACHE_GENERATION))
    store.update_join_fences(add=(room_id,))
    bot = _agent_bot(tmp_path)
    bot.config.matrix_sync = MatrixSyncConfig(mode="sliding")
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._first_sync_done = True
    bot._room_lifecycle.apply_continuity_record(store.load())

    await bot._on_sync_response(
        nio.SlidingSyncResponse(
            "pos_after_join",
            rooms={room_id: nio.SlidingSyncRoom(membership="join")},
        ),
    )

    assert store.load() == SyncContinuityRecord(
        revision=3,
        checkpoint=SyncCheckpoint("s_classic", cache_generation=_CACHE_GENERATION),
    )
    restarted = _agent_bot(tmp_path)
    restarted.config.matrix_sync = MatrixSyncConfig(mode="sliding")
    restarted.client = make_matrix_client_mock(user_id=restarted.agent_user.user_id)

    await restarted._prepare_matrix_sync_continuity()

    assert not restarted._room_lifecycle.decrypt_notice_is_fenced(room_id)


@pytest.mark.asyncio
async def test_restart_loads_only_exact_unfinished_join_decrypt_fence(
    tmp_path: Path,
) -> None:
    """Restart must distinguish an unfinished join from a long-trusted room."""
    room_id = "!room:localhost"
    trusted_room_id = "!trusted:localhost"
    first_bot = _agent_bot(tmp_path)
    first_bot.client = make_matrix_client_mock(user_id=first_bot.agent_user.user_id)

    with (
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[])),
        patch("mindroom.bot_room_lifecycle.join_room", AsyncMock(return_value=True)),
    ):
        await first_bot.join_configured_rooms()

    assert first_bot._room_lifecycle.decrypt_notice_is_fenced(room_id)

    restarted_bot = _agent_bot(tmp_path)
    restarted_client = make_matrix_client_mock(user_id=restarted_bot.agent_user.user_id)
    with (
        patch.object(restarted_bot, "ensure_user_account", AsyncMock()),
        patch(
            "mindroom.bot.login_agent_user",
            AsyncMock(return_value=restarted_client),
        ),
        patch.object(restarted_bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(restarted_bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
        patch(
            "mindroom.bot_room_lifecycle.get_joined_rooms",
            AsyncMock(return_value=[room_id, trusted_room_id]),
        ),
    ):
        await restarted_bot.start()

    assert restarted_bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    assert not restarted_bot._room_lifecycle.decrypt_notice_is_fenced(trusted_room_id)


@pytest.mark.asyncio
async def test_join_cancellation_after_server_side_effect_retains_decrypt_notice_fence(
    tmp_path: Path,
) -> None:
    """An ambiguous cancelled join must remain fenced for later sync confirmation."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)

    async def join_then_cancel(client: nio.AsyncClient, joining_room_id: str) -> bool:
        client.rooms[joining_room_id] = nio.MatrixRoom(
            room_id=joining_room_id,
            own_user_id=bot.agent_user.user_id,
        )
        raise asyncio.CancelledError

    with (
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[])),
        patch("mindroom.bot_room_lifecycle.join_room", new=join_then_cancel),
        pytest.raises(asyncio.CancelledError),
    ):
        await bot.join_configured_rooms()

    assert room_id in bot.client.rooms
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


def _text_event(event_id: str, body: str, origin_server_ts: int) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


def _image_event(event_id: str, body: str, origin_server_ts: int) -> nio.RoomMessageImage:
    event = nio.RoomMessageImage.from_dict(
        {
            "content": {
                "body": body,
                "info": {"mimetype": "image/png", "size": 4},
                "msgtype": "m.image",
                "url": "mxc://localhost/history-image",
            },
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )
    assert isinstance(event, nio.RoomMessageImage)
    return event


def _limited_empty_classic_response(room_id: str) -> nio.SyncResponse:
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s_after_gap",
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "rooms": {
                "invite": {},
                "leave": {},
                "join": {
                    room_id: {
                        "timeline": {
                            "events": [],
                            "limited": True,
                            "prev_batch": "p_gap_start",
                        },
                        "state": {"events": []},
                        "ephemeral": {"events": []},
                        "account_data": {"events": []},
                    },
                },
            },
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


def _newly_joined_world_readable_response(
    room_id: str,
    user_id: str,
    *,
    limited: bool,
    next_batch: str,
) -> nio.SyncResponse:
    own_join = {
        "type": "m.room.member",
        "event_id": "$own-join",
        "sender": user_id,
        "state_key": user_id,
        "origin_server_ts": 3,
        "content": {"membership": "join"},
        "unsigned": {"prev_content": {"membership": "leave"}},
    }
    state_events = [
        {
            "type": "m.room.history_visibility",
            "event_id": "$world-readable",
            "sender": "@user:localhost",
            "state_key": "",
            "origin_server_ts": 0,
            "content": {"history_visibility": "world_readable"},
        },
    ]
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": next_batch,
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "rooms": {
                "invite": {},
                "leave": {},
                "join": {
                    room_id: {
                        "timeline": {
                            "events": [own_join] if limited else [],
                            "limited": limited,
                            "prev_batch": "p_before_join" if limited else "p_after_join",
                        },
                        "state": {"events": state_events},
                        "ephemeral": {"events": []},
                        "account_data": {"events": []},
                    },
                },
            },
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


def _register_counted_source_callbacks(bot: AgentBot, client: nio.AsyncClient) -> MagicMock:
    with patch.object(
        client,
        "add_event_admission_callback",
        wraps=client.add_event_admission_callback,
    ) as add_admission:
        bot._dispatch_obligation_runner.register_source_callbacks(
            client,
            owner=bot._runtime_view,
        )
    return add_admission


def _timeline_response(
    transport: str,
    room_id: str,
    event: nio.Event,
) -> nio.SyncResponse | nio.SlidingSyncResponse:
    if transport == "classic":
        response = nio.SyncResponse.from_dict(
            {
                "next_batch": "s_after_failure",
                "device_one_time_keys_count": {},
                "device_lists": {"changed": [], "left": []},
                "rooms": {
                    "invite": {},
                    "leave": {},
                    "join": {
                        room_id: {
                            "timeline": {
                                "events": [event.source],
                                "limited": False,
                                "prev_batch": "p0",
                            },
                            "state": {"events": []},
                            "ephemeral": {"events": []},
                            "account_data": {"events": []},
                        },
                    },
                },
                "to_device": {"events": []},
                "presence": {"events": []},
                "account_data": {"events": []},
            },
        )
        assert isinstance(response, nio.SyncResponse)
        return response
    response = nio.SlidingSyncResponse.from_dict(
        {
            "pos": "s_after_failure",
            "rooms": {
                room_id: {
                    "membership": "join",
                    "timeline": [event.source],
                },
            },
        },
    )
    assert isinstance(response, nio.SlidingSyncResponse)
    return response


def _room_member_event(event_id: str = "$member-join") -> nio.RoomMemberEvent:
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": event_id,
            "sender": "@alice:localhost",
            "state_key": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"membership": "join"},
            "unsigned": {"prev_content": {"membership": "leave"}},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    return event


def _pending(event: nio.RoomMessageText) -> PendingEvent:
    return PendingEvent(
        event=event,
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind="message",
    )


def test_load_sync_token_returns_none_when_missing(tmp_path: Path) -> None:
    """First-run agents should have no saved sync token."""
    assert _load_sync_token_value(tmp_path, "code") is None


def test_whitespace_only_continuity_record_fails_closed(tmp_path: Path) -> None:
    """Whitespace-only continuity cannot silently become a cold restart."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(" \n\t ", encoding="utf-8")

    with pytest.raises(RuntimeError, match="continuity"):
        _load_sync_token_value(tmp_path, "code")


def test_save_sync_token_round_trip(tmp_path: Path) -> None:
    """Saving and loading should round-trip the token value."""
    save_sync_token(tmp_path, "code", "s12345", cache_generation=_CACHE_GENERATION)

    token_path = _token_path(tmp_path)
    assert json.loads(token_path.read_text(encoding="utf-8")) == {
        "checkpoint": {
            "cache_generation": _CACHE_GENERATION,
            "token": "s12345",
        },
        "pending_join_decrypt_fences": [],
        "revision": 1,
        "version": "mindroom-sync-continuity-v2",
    }
    assert _load_sync_token_value(tmp_path, "code") == "s12345"
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == "s12345"  # noqa: S105
    assert checkpoint.cache_generation == _CACHE_GENERATION


def test_obsolete_certified_record_fails_closed(tmp_path: Path) -> None:
    """Obsolete records cannot silently establish or discard continuity."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        '{"token":"s_old","version":"mindroom-sync-token-v1"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="continuity"):
        load_sync_checkpoint(tmp_path, "code")


def test_clear_sync_token_preserves_empty_continuity_record(tmp_path: Path) -> None:
    """Clearing a checkpoint keeps the unified record available for join fences."""
    save_sync_token(tmp_path, "code", "s12345", cache_generation=_CACHE_GENERATION)

    clear_sync_token(tmp_path, "code")

    assert _load_sync_token_value(tmp_path, "code") is None
    assert SyncContinuityStore(tmp_path, "code").load() == SyncContinuityRecord(
        revision=2,
    )


def test_clear_sync_token_is_idempotent(tmp_path: Path) -> None:
    """Clearing a missing token should be a no-op."""
    clear_sync_token(tmp_path, "code")

    assert _load_sync_token_value(tmp_path, "code") is None
    assert not _token_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_bot_start_restores_saved_sync_token(tmp_path: Path) -> None:
    """Startup should hydrate the nio client from the previously saved token."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_saved",
        cache_generation=bot.event_cache.cache_generation,
    )

    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch == "s_saved"


@pytest.mark.asyncio
async def test_bot_start_leaves_trusted_joined_room_unfenced_for_catch_up(
    tmp_path: Path,
) -> None:
    """A joined room without unfinished join state may report real Megolm loss."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_saved",
        cache_generation=bot.event_cache.cache_generation,
    )
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None
    get_joined_rooms = AsyncMock(return_value=[room_id])

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", get_joined_rooms),
    ):
        await bot.start()

    get_joined_rooms.assert_not_awaited()
    assert client.next_batch == "s_saved"
    assert (
        await bot._cold_history_fence.admit_source(
            room_id,
            "$trusted-catch-up",
            DispatchCallbackKind.DECRYPTION_FAILURE,
        )
        is DispatchSourceAdmission.ACCEPTED
    )


@pytest.mark.asyncio
async def test_bot_start_keeps_fences_when_joined_rooms_query_is_unavailable(
    tmp_path: Path,
) -> None:
    """Startup must remain available while preserving fences on an inventory miss."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    with (
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[])),
        patch("mindroom.bot_room_lifecycle.join_room", AsyncMock(return_value=True)),
    ):
        await bot.join_configured_rooms()
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room:localhost")

    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    get_joined_rooms = AsyncMock(return_value=None)

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", get_joined_rooms),
    ):
        await bot.start()

    get_joined_rooms.assert_awaited_once_with(client)
    client.close.assert_not_awaited()
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room:localhost")
    assert bot.client is client
    assert bot.running


@pytest.mark.asyncio
async def test_bot_start_skips_joined_rooms_query_without_pending_join_fences(
    tmp_path: Path,
) -> None:
    """No durable unfinished joins means no membership reconciliation is needed."""
    bot = _agent_bot(tmp_path)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    get_joined_rooms = AsyncMock(side_effect=AssertionError("unexpected joined_rooms query"))

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", get_joined_rooms),
    ):
        await bot.start()

    get_joined_rooms.assert_not_awaited()
    assert bot.running


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [ROUTER_AGENT_NAME, "code"])
async def test_orchestrated_entity_start_defers_turn_recovery_to_coordinator(
    tmp_path: Path,
    agent_name: str,
) -> None:
    """Orchestrated entity startup must leave turn-backed replay to fleet readiness."""
    bot = _agent_bot(tmp_path, agent_name=agent_name)
    bot.orchestrator = MagicMock()
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    recover_pending = AsyncMock()
    bot._dispatch_obligation_runner.recover_pending = recover_pending

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    recover_pending.assert_awaited_once_with(turn_backed=False)


@pytest.mark.asyncio
async def test_start_runs_pending_invite_recovery_after_callbacks_and_running(
    tmp_path: Path,
) -> None:
    """A blocked invite retry must not block bot or fleet startup."""
    bot = _agent_bot(tmp_path)
    bot.orchestrator = MagicMock()
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()

    async def recover_pending(*, turn_backed: bool | None = None) -> None:
        assert turn_backed is False
        recovery_started.set()
        await release_recovery.wait()

    bot._dispatch_obligation_runner.recover_pending = recover_pending

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        start_task = asyncio.create_task(bot.start())
        try:
            await asyncio.wait_for(recovery_started.wait(), timeout=1)
            assert bot.running
            assert client.add_response_callback.call_count == 2
        finally:
            release_recovery.set()
            await start_task


@pytest.mark.asyncio
async def test_orchestrated_team_start_gates_turn_recovery_on_responder_fleet(
    tmp_path: Path,
) -> None:
    """Team startup must leave turn-backed replay gated until its member fleet starts."""
    config = _configured_team_test_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    bot = TeamBot(
        _configured_team_user(config, runtime_paths),
        tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        team_mode="coordinate",
    )
    install_runtime_cache_support(bot)
    bot.orchestrator = MagicMock(running=False)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    recover_pending = AsyncMock()
    bot._dispatch_obligation_runner.recover_pending = recover_pending

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    recover_pending.assert_awaited_once_with(turn_backed=False)


@pytest.mark.asyncio
async def test_leave_cleanup_restart_purges_only_current_sqlite_principal(tmp_path: Path) -> None:
    """A restart after leave cleanup interruption must discard only the departed principal."""
    principal_id = "@mindroom_code:localhost"
    other_principal_id = "@mindroom_other:localhost"
    room_id = "!room:localhost"
    event_id = "$stale"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "stale", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    principal_cache = root.for_principal(principal_id)
    other_cache = root.for_principal(other_principal_id)
    await principal_cache.store_event(event_id, room_id, event)
    await other_cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = principal_cache
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=principal_cache.cache_generation,
    )
    leave_response = MagicMock(spec=nio.SyncResponse)
    leave_response.rooms = MagicMock(join={}, leave={room_id: MagicMock()})
    interrupted_cleanup = asyncio.CancelledError("process stopped during leave cleanup")
    with (
        patch.object(bot._conversation_cache, "purge_rooms", AsyncMock(side_effect=interrupted_cleanup)),
        pytest.raises(asyncio.CancelledError, match="process stopped"),
    ):
        await bot._apply_own_room_membership_from_sync(leave_response)
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert bot._sync_cache_trust.checkpoint is None
    await root.close()

    reopened_root = SqliteEventCache(tmp_path / "event-cache.db")
    await reopened_root.initialize()
    principal_cache = reopened_root.for_principal(principal_id)
    other_cache = reopened_root.for_principal(other_principal_id)
    bot.event_cache = principal_cache
    client = make_matrix_client_mock(user_id=principal_id)
    client.next_batch = None

    try:
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await bot.start()

        assert await principal_cache.get_event(room_id, event_id) is None
        assert await other_cache.get_event(room_id, event_id) == event
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_login_identity_change_rebinds_principal_cache_view(tmp_path: Path) -> None:
    """Authenticated identity replacement must not retain the old principal's cache view."""
    old_principal_id = "@mindroom_code:localhost"
    new_principal_id = "@mindroom_code:new.example"
    room_id = "!room:localhost"
    event_id = "$old-principal"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "old principal", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    old_cache = root.for_principal(old_principal_id)
    await old_cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = old_cache
    admission_gate = ResponseAdmissionGate()
    bot.admission_gate = admission_gate
    matrix_id_before_login = bot.matrix_id

    try:
        bot.agent_user.user_id = new_principal_id
        bot._rebuild_runtime_components_after_login_if_identity_changed(matrix_id_before_login)

        assert bot.event_cache.principal_id == new_principal_id
        assert bot.admission_gate is admission_gate
        assert await bot.event_cache.get_event(room_id, event_id) is None
        assert await old_cache.get_event(room_id, event_id) == event
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_authoritative_leave_clears_checkpoint_before_cache_cleanup(tmp_path: Path) -> None:
    """A crash during leave cleanup must force principal cleanup on the next startup."""
    bot = _agent_bot(tmp_path)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=bot.event_cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={"!left:localhost": MagicMock()})

    await bot._apply_own_room_membership_from_sync(response)

    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert bot._sync_cache_trust.checkpoint is None


@pytest.mark.asyncio
async def test_leave_fence_rejects_delayed_write_before_new_checkpoint(tmp_path: Path) -> None:
    """Certification after leave must not preserve a delayed callback's recreated rows."""
    principal_id = "@mindroom_code:localhost"
    room_id = "!left:localhost"
    event_id = "$event"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "stale", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    cache = root.for_principal(principal_id)
    await cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = cache
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={room_id: MagicMock()})
    try:
        await bot._apply_own_room_membership_from_sync(response)
        await cache.store_event("$late", room_id, {**event, "event_id": "$late"})
        await bot._sync_cache_trust.certify_response(
            next_batch="s_after_leave",
            cache_result=SyncCacheWriteResult(complete=True),
            first_sync=False,
        )

        assert await cache.get_event(room_id, "$late") is None
        assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_leave"
    finally:
        await root.close()

    reopened_root = SqliteEventCache(tmp_path / "event-cache.db")
    await reopened_root.initialize()
    try:
        reopened_cache = reopened_root.for_principal(principal_id)
        assert await reopened_cache.get_event(room_id, event_id) is None
        assert await reopened_cache.get_event(room_id, "$late") is None
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_cleanup_failure",
    [
        RuntimeError("call cleanup interrupted"),
        asyncio.CancelledError("call cleanup interrupted"),
    ],
)
async def test_leave_purges_before_failing_call_reconciliation(
    tmp_path: Path,
    call_cleanup_failure: BaseException,
) -> None:
    """Call cleanup cannot suspend or fail before authoritative cache cleanup."""
    bot = _agent_bot(tmp_path)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=bot.event_cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={"!left:localhost": MagicMock()})
    operation_order: list[str] = []

    async def purge_rooms(_room_ids: object) -> None:
        operation_order.append("purge")

    async def fail_call_cleanup(**_kwargs: object) -> None:
        operation_order.append("call")
        raise call_cleanup_failure

    bot._call_manager = MagicMock()
    bot._call_manager.on_sync_room_membership = AsyncMock(side_effect=fail_call_cleanup)

    with (
        patch.object(bot._conversation_cache, "purge_rooms", side_effect=purge_rooms),
        pytest.raises(type(call_cleanup_failure), match="call cleanup interrupted"),
    ):
        await bot._apply_own_room_membership_from_sync(response)

    assert operation_order == ["purge", "call"]
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert bot._sync_cache_trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_checkpoint_clear_failure_defers_durable_leave_cleanup_for_replay(tmp_path: Path) -> None:
    """A failed checkpoint unlink must preserve old durable rows and disable cache use."""
    principal_id = "@mindroom_code:localhost"
    room_id = "!left:localhost"
    event_id = "$stale"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "stale", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    principal_cache = root.for_principal(principal_id)
    await principal_cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = principal_cache
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=principal_cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={room_id: MagicMock()})
    clear_failure = OSError("checkpoint directory unavailable")

    with patch.object(bot._sync_continuity_store, "clear_checkpoint", side_effect=clear_failure):
        await bot._apply_own_room_membership_from_sync(response)

    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert bot._sync_cache_trust.checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_leave"
    await root.close()

    reopened_root = SqliteEventCache(tmp_path / "event-cache.db")
    await reopened_root.initialize()
    reopened_cache = reopened_root.for_principal(principal_id)
    bot.event_cache = reopened_cache
    bot.client = make_matrix_client_mock(user_id=principal_id)
    bot.client.next_batch = None
    try:
        bot.client.next_batch = await bot._sync_cache_trust.prepare_startup()

        assert bot.client.next_batch == "s_before_leave"
        assert await reopened_cache.get_event(room_id, event_id) == event

        await bot._apply_own_room_membership_from_sync(response)

        assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
        assert await reopened_cache.get_event(room_id, event_id) is None
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_bot_start_initializes_postgres_principal_before_restoring_checkpoint(
    tmp_path: Path,
    postgres_event_cache_url: str,
) -> None:
    """A matching principal namespace generation must preserve restart continuity."""
    namespace = f"sync_restore_{uuid.uuid4().hex}"
    principal_id = "@mindroom_code:localhost"
    seed_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    seed_view = seed_root.for_principal(principal_id)
    await seed_view.initialize()
    generation = seed_view.cache_generation
    assert generation is not None
    await seed_root.close()

    bot = _agent_bot(tmp_path)
    reopened_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    bot.event_cache = reopened_root.for_principal(principal_id)
    assert bot.event_cache.cache_generation is None
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_postgres_restart",
        cache_generation=generation,
    )
    client = make_matrix_client_mock(user_id=principal_id)
    client.next_batch = None

    try:
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await bot.start()

        assert client.next_batch == "s_postgres_restart"
        assert bot.event_cache.cache_generation == generation
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_postgres_outage_clears_unverifiable_checkpoint_and_recovers_cold(
    tmp_path: Path,
    postgres_event_cache_url: str,
) -> None:
    """An unavailable cache generation must force a later cold restart."""
    namespace = f"sync_restore_outage_{uuid.uuid4().hex}"
    principal_id = "@mindroom_code:localhost"
    room_id = "!room:localhost"
    event_id = "$cached-before-outage"
    event = {
        "content": {"body": "cached", "msgtype": "m.text"},
        "event_id": event_id,
        "origin_server_ts": 1,
        "room_id": room_id,
        "sender": "@user:localhost",
        "type": "m.room.message",
    }
    seed_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    seed_view = seed_root.for_principal(principal_id)
    await seed_view.initialize()
    await seed_view.store_event(event_id, room_id, event)
    generation = seed_view.cache_generation
    assert generation is not None
    await seed_root.close()
    save_sync_token(
        tmp_path,
        "code",
        "s_before_outage",
        cache_generation=generation,
    )

    unavailable_bot = _agent_bot(tmp_path)
    unavailable_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    unavailable_bot.event_cache = unavailable_root.for_principal(principal_id)
    unavailable_client = make_matrix_client_mock(user_id=principal_id)
    unavailable_client.next_batch = None
    empty_response = MagicMock(
        spec=nio.SyncResponse,
        next_batch="s_empty_during_outage",
        unrecovered_room_ids=frozenset(),
    )
    empty_response.rooms = MagicMock(join={}, leave={})
    message_event = nio.RoomMessageText.from_dict(event)
    event_response = MagicMock(
        spec=nio.SyncResponse,
        next_batch="s_event_during_outage",
        unrecovered_room_ids=frozenset(),
    )
    event_response.rooms = MagicMock(
        join={room_id: MagicMock(timeline=MagicMock(events=[message_event], limited=False))},
        leave={},
    )
    try:
        with (
            patch.object(unavailable_bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=unavailable_client)),
            patch.object(unavailable_bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(unavailable_bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
            patch(
                "mindroom.matrix.cache.postgres_event_cache._initialize_postgres_event_cache_db",
                AsyncMock(side_effect=EventCacheBackendUnavailableError("database unavailable")),
            ),
        ):
            await unavailable_bot.start()
            await unavailable_bot._on_sync_response(empty_response)
            await unavailable_bot._on_sync_response(event_response)

        assert unavailable_client.next_batch is None
        assert load_sync_checkpoint(tmp_path, unavailable_bot.agent_name) is None
    finally:
        await unavailable_root.close()

    recovered_bot = _agent_bot(tmp_path)
    recovered_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    recovered_view = recovered_root.for_principal(principal_id)
    recovered_bot.event_cache = recovered_view
    recovered_client = make_matrix_client_mock(user_id=principal_id)
    recovered_client.next_batch = None
    try:
        with (
            patch.object(recovered_bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=recovered_client)),
            patch.object(recovered_bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(recovered_bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await recovered_bot.start()

        assert recovered_client.next_batch is None
        assert await recovered_view.get_event(room_id, event_id) is None
    finally:
        await recovered_root.close()


@pytest.mark.asyncio
async def test_sqlite_checkpoint_generation_rejects_matrix_principal_rebind(tmp_path: Path) -> None:
    """A retained agent token must not cross a Matrix account or homeserver change."""
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    old_principal = root.for_principal("@mindroom_code:old.example")
    new_principal = root.for_principal("@mindroom_code:new.example")
    old_generation = old_principal.cache_generation
    assert old_generation is not None
    assert new_principal.cache_generation != old_generation
    save_sync_token(
        tmp_path,
        "code",
        "s_old_principal",
        cache_generation=old_generation,
    )
    bot = _agent_bot(tmp_path)
    bot.event_cache = new_principal
    bot.client = make_matrix_client_mock(user_id=new_principal.principal_id)
    bot.client.next_batch = None

    try:
        bot.client.next_batch = await bot._sync_cache_trust.prepare_startup()

        assert bot.client.next_batch is None
        assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_bot_start_rejects_checkpoint_from_reset_cache_generation(tmp_path: Path) -> None:
    """A certified token cannot skip history after its backing cache was reset."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_stale",
        cache_generation="stale-cache-generation",
    )
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_bot_start_clears_checkpoint_when_cache_generation_is_unavailable(tmp_path: Path) -> None:
    """An unavailable generation cannot prove a saved checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.event_cache.cache_generation = None
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_stale",
        cache_generation="old-cache-generation",
    )
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    bot.event_cache.purge_principal.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_start_purges_untrusted_cache_without_checkpoint_when_generation_is_unavailable(
    tmp_path: Path,
) -> None:
    """Generation failure cannot excuse stale rows when no checkpoint proves their sync position."""
    bot = _agent_bot(tmp_path)
    bot.event_cache.cache_generation = None
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    bot.event_cache.purge_principal.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_v2_sync_token_path_is_not_parsed(tmp_path: Path) -> None:
    """Legacy token files are outside the unified continuity namespace."""
    token_path = _legacy_token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        '{"version":"mindroom-sync-token-v2","token":"s_old","cache_generation":"old"}',
        encoding="utf-8",
    )
    bot = _agent_bot(tmp_path)

    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    assert token_path.exists()


@pytest.mark.asyncio
async def test_cache_generation_rejects_token_after_reset_crash_window(tmp_path: Path) -> None:
    """A committed reset remains a principal-bound token barrier after a crash window."""
    db_path = tmp_path / "event_cache.db"
    principal_id = "@mindroom_code:localhost"
    first_root = SqliteEventCache(db_path)
    await first_root.initialize()
    first_cache = first_root.for_principal(principal_id)
    first_generation = first_cache.cache_generation
    assert first_generation is not None
    save_sync_token(
        tmp_path,
        "code",
        "s_before_reset",
        cache_generation=first_generation,
    )
    await first_root.close()

    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("DROP TABLE event_edits")
        await db.commit()
    finally:
        await db.close()

    reset_root = SqliteEventCache(db_path)
    await reset_root.initialize()
    reset_generation = reset_root.for_principal(principal_id).cache_generation
    assert reset_generation is not None
    assert reset_generation != first_generation
    await reset_root.close()

    restarted_root = SqliteEventCache(db_path)
    await restarted_root.initialize()
    try:
        restarted_cache = restarted_root.for_principal(principal_id)
        assert restarted_cache.cache_generation == reset_generation

        bot = _agent_bot(tmp_path)
        bot.event_cache = restarted_cache
        bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
        bot.client.next_batch = None
        bot.client.next_batch = await bot._sync_cache_trust.prepare_startup()

        assert bot.client.next_batch is None
        assert bot._sync_cache_trust.state is SyncTrustState.COLD
        assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    finally:
        await restarted_root.close()


@pytest.mark.asyncio
async def test_invalid_utf8_continuity_record_repairs_and_starts_cold(tmp_path: Path) -> None:
    """Malformed bytes must repair to a cold record instead of bricking startup."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = None

    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(b"\xff\xfe\xfd")

    assert await bot._sync_cache_trust.prepare_startup() is None
    assert bot._sync_cache_trust.state is SyncTrustState.COLD
    assert bot._sync_continuity_store.load() == SyncContinuityRecord(revision=1)


@pytest.mark.asyncio
async def test_unknown_pos_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Rejected first-sync saved tokens should be removed before nio retries."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected", cache_generation=_CACHE_GENERATION)
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_restored_first_sync_saves_later_checkpoint(tmp_path: Path) -> None:
    """After M_UNKNOWN_POS, later successful sync responses can save a fresh checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected", cache_generation=_CACHE_GENERATION)
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={})
    await bot._on_sync_response(response)

    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_later"  # noqa: S105


@pytest.mark.asyncio
async def test_unknown_pos_after_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Post-start M_UNKNOWN_POS must not leave a poisoned sync token in place."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_after_start"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started()
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_rejected_after_start",
        cache_generation=_CACHE_GENERATION,
    )
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_non_restored_runtime_allows_later_checkpoint(tmp_path: Path) -> None:
    """M_UNKNOWN_POS should fail closed, then allow later certified tokens."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_cold"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started()
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot.client.next_batch = "s_later_after_unknown_pos"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later_after_unknown_pos"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={"!room:localhost": MagicMock(timeline=MagicMock(events=[], limited=False))})
    await bot._on_sync_response(response)

    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_later_after_unknown_pos"  # noqa: S105


@pytest.mark.asyncio
async def test_on_sync_response_persists_latest_sync_token(tmp_path: Path) -> None:
    """Successful sync responses should update the saved next_batch token."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_latest"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_latest"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={})

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_latest"
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_latest"  # noqa: S105


@pytest.mark.asyncio
async def test_limited_cache_failure_keeps_live_cursor_and_last_checkpoint(tmp_path: Path) -> None:
    """Limited status must preserve continuity even when cache persistence also fails."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_partial"
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_partial"
    response.rooms = MagicMock(join={})
    cache_result = SyncCacheWriteResult(
        complete=False,
        limited_room_ids=("!room:localhost",),
        errors=(RuntimeError("cache failed"),),
    )

    with (
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            AsyncMock(return_value=cache_result),
        ),
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch == "s_partial"
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_gap"


@pytest.mark.asyncio
async def test_unrecovered_gap_keeps_device_independent_checkpoint(tmp_path: Path) -> None:
    """A fresh Matrix device must restart from before any store-local recovery gap."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_gap"
    bot._first_sync_done = True
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_gap"
    response.unrecovered_room_ids = frozenset({"!room:localhost"})
    response.rooms = MagicMock(join={})

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)

    restarted = _agent_bot(tmp_path)
    assert bot.client.next_batch == "s_after_gap"
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert await restarted._sync_cache_trust.prepare_startup() == "s_before_gap"


@pytest.mark.asyncio
async def test_cache_scope_cleanup_between_plan_and_apply_forces_tokenless_recovery(tmp_path: Path) -> None:
    """Bot wiring must apply the invalidation epoch before advancing nio's cursor."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_stale_after_cleanup"
    cache_result = SyncCacheWriteResult(complete=True)
    decision = bot._sync_cache_trust.plan_response(
        next_batch="s_stale_after_cleanup",
        cache_result=cache_result,
        first_sync=False,
    )

    assert await bot._sync_cache_trust.invalidate_for_cache_scope_cleanup()
    await bot._apply_sync_response_decision(decision, cache_result=cache_result)

    assert bot.client.next_batch is None
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert bot._sync_cache_trust.checkpoint is None
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_sync_response_side_effect_failure_preserves_raw_cache_checkpoint(tmp_path: Path) -> None:
    """A non-cache side effect failure must not poison independently durable raw continuity."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_side_effect_failure"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_side_effect_failure"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={})
    bot._emit_agent_lifecycle_event = AsyncMock(side_effect=RuntimeError("bot ready failed"))  # type: ignore[method-assign]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(RuntimeError, match="bot ready failed"),
    ):
        await bot._on_sync_response(response)

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_after_side_effect_failure")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_side_effect_failure"


@pytest.mark.asyncio
async def test_membership_cancellation_rewinds_uncertified_classic_cursor(tmp_path: Path) -> None:
    """Cancellation before membership effects finish must replay the uncertified response."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_membership"
    bot._first_sync_done = True
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_membership")
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_membership"
    response.rooms = MagicMock(join={})
    bot._apply_own_room_membership_from_sync = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError("watchdog restart"),
    )

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(asyncio.CancelledError, match="watchdog restart"),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch == "s_before_membership"


@pytest.mark.asyncio
async def test_limited_cache_cancellation_preserves_positioned_continuity(tmp_path: Path) -> None:
    """Cancellation must retain limited status when certifying the interrupted cache write."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_partial"
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_gap",
        cache_generation=_CACHE_GENERATION,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_partial"
    response.rooms = MagicMock(
        join={
            "!room:localhost": MagicMock(
                timeline=MagicMock(events=[], limited=True),
            ),
        },
    )

    with (
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            AsyncMock(side_effect=asyncio.CancelledError("watchdog restart")),
        ),
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(asyncio.CancelledError, match="watchdog restart"),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch == "s_partial"
    assert bot._sync_cache_trust.state is SyncTrustState.UNCERTAIN
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_gap"


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the latest cache-certified sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_shutdown"  # noqa: S105


@pytest.mark.parametrize(
    ("shutdown_intent", "reason_category"),
    [
        (GENERIC_SHUTDOWN, "agent_shutdown"),
        (ENTITY_REMOVED_SHUTDOWN, "agent_shutdown"),
        (SYNC_RESTART_SHUTDOWN, "config_reload"),
        (ORDERLY_SHUTDOWN, "process_shutdown"),
    ],
)
@pytest.mark.asyncio
async def test_response_runtime_shutdown_log_names_its_agent_and_reason(
    tmp_path: Path,
    shutdown_intent: RuntimeShutdownIntent,
    reason_category: str,
) -> None:
    """Full shutdown logs its agent, action, and response count, but no conversation."""
    bot = _agent_bot(tmp_path)

    with capture_logs() as logs:
        await bot.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)

    shutdown_logs = [entry for entry in logs if entry["event"] == "matrix_agent_response_runtime_shutdown"]
    assert len(shutdown_logs) == 1
    assert shutdown_logs[0]["agent"] == bot.agent_name
    assert shutdown_logs[0]["active_response_count"] == 0
    assert shutdown_logs[0]["restart_reason_category"] == reason_category
    assert shutdown_logs[0]["resulting_action"] == "drain_then_cancel_response_runtime"
    assert not {"room_id", "event_id", "user_id"} & shutdown_logs[0].keys()


@pytest.mark.asyncio
async def test_shutdown_timeout_preserves_checkpoint_for_durable_ingress_recovery(tmp_path: Path) -> None:
    """Incomplete durable ingress drains must not poison raw cache continuity."""
    bot = _agent_bot(tmp_path)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=CoalescingDrainResult(completed=False, cancelled_unready_count=1),
    )

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.parametrize(
    ("coalescing_drain_result", "responses_drained", "response_recovery_complete"),
    [
        (CoalescingDrainResult(completed=True), False, False),
        (CoalescingDrainResult(completed=False, cancelled_unready_count=1), True, True),
    ],
)
@pytest.mark.asyncio
async def test_shutdown_recovery_warning_logs_exact_drain_predicates(
    tmp_path: Path,
    coalescing_drain_result: CoalescingDrainResult,
    responses_drained: bool,
    response_recovery_complete: bool,
) -> None:
    """Durable-recovery logs should identify which content-free drain predicate failed."""
    bot = _agent_bot(tmp_path)
    install_shutdown_drain_mocks(
        bot,
        coalescing_drain_result=coalescing_drain_result,
        responses_drained=responses_drained,
        response_recovery_complete=response_recovery_complete,
    )

    with capture_logs() as logs:
        await bot.prepare_for_sync_shutdown()

    warnings = [entry for entry in logs if entry["event"] == "runtime_drain_incomplete_with_durable_dispatch_recovery"]
    assert len(warnings) == 1
    assert warnings[0]["coalescing_drain_completed"] is coalescing_drain_result.completed
    assert warnings[0]["cancelled_unready_count"] == coalescing_drain_result.cancelled_unready_count
    assert warnings[0]["responses_drained"] is responses_drained
    assert warnings[0]["response_recovery_complete"] is response_recovery_complete
    assert not {"body", "content", "formatted_body", "message_content"} & warnings[0].keys()
    response_warnings = [entry for entry in logs if entry["event"] == "matrix_agent_response_drain_incomplete"]
    if responses_drained:
        assert response_warnings == []
    else:
        assert len(response_warnings) == 1
        assert response_warnings[0]["active_response_count"] == 0
        assert response_warnings[0]["pending_response_count"] == 0
        assert response_warnings[0]["response_recovery_complete"] is response_recovery_complete
        assert response_warnings[0]["restart_reason_category"] == "agent_shutdown"


@pytest.mark.asyncio
async def test_shutdown_timeout_preserves_checkpoint_for_unsettled_callbacks(tmp_path: Path) -> None:
    """Durably accepted callback timeouts must not poison raw cache continuity."""
    bot = _agent_bot(tmp_path)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    with patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(return_value=False)):
        await bot.prepare_for_sync_shutdown()

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_post_drain_background_timeout_preserves_raw_checkpoint(tmp_path: Path) -> None:
    """Post-drain callback recovery must remain independent from raw cache continuity."""
    bot = _agent_bot(tmp_path)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    wait_for_background_tasks = AsyncMock(side_effect=[True, False])

    with patch("mindroom.bot.wait_for_background_tasks", new=wait_for_background_tasks):
        await bot.prepare_for_sync_shutdown()

    assert wait_for_background_tasks.await_count == 2
    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_shutdown_cancellation_during_post_drain_wait_keeps_raw_checkpoint(tmp_path: Path) -> None:
    """Durable dispatch recovery keeps raw continuity independent from shutdown cancellation."""
    bot = _certified_shutdown_bot(tmp_path)
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=CoalescingDrainResult(completed=False, cancelled_unready_count=1),
    )
    post_drain_wait_started = asyncio.Event()
    wait_call_count = 0

    async def wait_with_post_drain_barrier(**_kwargs: object) -> bool:
        nonlocal wait_call_count
        wait_call_count += 1
        if wait_call_count == 1:
            return True
        post_drain_wait_started.set()
        await asyncio.Event().wait()
        return True

    with patch("mindroom.bot.wait_for_background_tasks", new=wait_with_post_drain_barrier):
        shutdown_task = asyncio.create_task(bot.prepare_for_sync_shutdown())
        await post_drain_wait_started.wait()
        shutdown_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

    assert wait_call_count == 2
    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_previous"


@pytest.mark.asyncio
async def test_generic_callback_failure_does_not_poison_raw_checkpoint(tmp_path: Path) -> None:
    """Best-effort callback failure must not affect independently durable raw cache state."""
    bot = _agent_bot(tmp_path)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_after_bad_callback")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    async def failing_callback() -> None:
        msg = "canonical key lookup failed"
        raise RuntimeError(msg)

    callback = _create_best_effort_task_wrapper(failing_callback, owner=bot._runtime_view)
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    with capture_logs() as logs:
        await bot.prepare_for_sync_shutdown()

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_after_bad_callback")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_bad_callback"
    assert not [entry for entry in logs if entry["event"] == "runtime_drain_incomplete_with_durable_dispatch_recovery"]


@pytest.mark.asyncio
async def test_callback_failure_preserves_saved_checkpoint_immediately(tmp_path: Path) -> None:
    """A failed best-effort callback must leave raw sync continuity unchanged."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_before_failure", cache_generation=_CACHE_GENERATION)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_failure")

    async def failing_callback() -> None:
        msg = "callback failed"
        raise RuntimeError(msg)

    callback = _create_best_effort_task_wrapper(failing_callback, owner=bot._runtime_view)
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_before_failure")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_failure"


@pytest.mark.asyncio
async def test_durably_accepted_invite_failure_does_not_rewind_classic_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted invite work must retry independently of raw sync continuity."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_invite"
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_invite")
    bot._room_lifecycle.on_invite = AsyncMock(side_effect=RuntimeError("join failed"))
    room = nio.MatrixRoom("!invited:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.matrix_id.full_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteEvent)
    schedule_retry = MagicMock()
    monkeypatch.setattr(bot._dispatch_obligation_runner, "_schedule_retry", schedule_retry)

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert bot.client.next_batch == "s_after_invite"
    pending = bot._dispatch_obligation_store.pending()
    assert len(pending) == 1
    assert pending[0].room_id == room.room_id
    assert pending[0].event_source == {
        **event.source,
        "content": event.content,
    }
    schedule_retry.assert_called_once_with(pending[0].key)

    recovered_invite = AsyncMock()
    bot._room_lifecycle.on_invite = recovered_invite
    await bot._dispatch_obligation_runner.recover_pending(turn_backed=False)

    recovered_invite.assert_awaited_once()
    recovered_event = recovered_invite.await_args.args[1]
    assert isinstance(recovered_event, nio.InviteMemberEvent)
    assert recovered_event.content == {"membership": "invite"}
    assert not bot._dispatch_obligation_store.pending()


@pytest.mark.asyncio
async def test_dispatch_persistence_failure_rewinds_classic_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work rejected before durable acceptance must replay from the certified cursor."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_failure"
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_failure")

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(bot._dispatch_obligation_store, "create_pending", fail_persist)
    admission = bot._dispatch_obligation_runner._admit_source_event

    with pytest.raises(
        nio.CallbackNotAcceptedError,
        match="dispatch database unavailable",
    ) as exc_info:
        await admission(
            nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id),
            _text_event("$unpersisted", "hello", 1),
            nio.TimelineEventProvenance.LIVE,
        )

    assert isinstance(exc_info.value.__cause__, OSError)
    assert bot.client.next_batch == "s_before_failure"


@pytest.mark.asyncio
async def test_tokenless_dispatch_persistence_failure_rewinds_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected work without a replay cursor rewinds to tokenless sync."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_unpersisted"

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(bot._dispatch_obligation_store, "create_pending", fail_persist)
    admission = bot._dispatch_obligation_runner._admit_source_event

    with pytest.raises(nio.CallbackNotAcceptedError, match="dispatch database unavailable"):
        await admission(
            nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id),
            _text_event("$unpersisted", "hello", 1),
            nio.TimelineEventProvenance.LIVE,
        )

    assert bot.client.next_batch is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_nio_replays_event_rejected_before_durable_dispatch_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    """Nio must not deduplicate an event whose durable admission failed."""
    bot = _agent_bot(tmp_path)
    bot.config.matrix_sync = MatrixSyncConfig(mode=transport)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    bot.client = client
    if transport == "classic":
        client.next_batch = "s_before_failure"
        bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
        bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_failure")
    room_id = "!room:localhost"
    event = _text_event(f"$lost-{transport}", "hello", 1)
    response = _timeline_response(transport, room_id, event)

    create_attempts = 0
    create_pending = bot._dispatch_obligation_store.create_pending

    def fail_first_create(obligation: object) -> object:
        nonlocal create_attempts
        create_attempts += 1
        if create_attempts == 1:
            message = "dispatch database unavailable"
            error_type = OSError if transport == "classic" else sqlite3.OperationalError
            raise error_type(message)
        return create_pending(cast("Any", obligation))

    monkeypatch.setattr(bot._dispatch_obligation_store, "create_pending", fail_first_create)
    monkeypatch.setattr(bot._dispatch_obligation_runner, "_run_persisted", AsyncMock())
    client.add_event_admission_callback(bot._dispatch_obligation_runner._admit_source_event, nio.RoomMessageText)
    client.add_event_callback(
        bot._dispatch_obligation_runner.task_wrapper(
            DispatchCallbackKind.MESSAGE,
            owner=bot._runtime_view,
        ),
        nio.RoomMessageText,
    )

    with pytest.raises(
        nio.CallbackNotAcceptedError,
        match="dispatch database unavailable",
    ) as exc_info:
        await client.receive_response(response)

    expected_cause = OSError if transport == "classic" else sqlite3.OperationalError
    assert isinstance(exc_info.value.__cause__, expected_cause)
    recovery = cast("Any", client)._recovery
    assert event.event_id not in recovery.completed.get(room_id, {})

    await client.receive_response(response)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert create_attempts == 2
    assert bot._dispatch_obligation_store.has_pending(
        event.event_id,
        DispatchCallbackKind.MESSAGE,
    )


@pytest.mark.asyncio
async def test_nio_limited_recovery_caches_history_before_cold_fence(tmp_path: Path) -> None:
    """Recovered text and media must reach the cache without becoming turns."""
    bot = _agent_bot(tmp_path)
    cache_root = SqliteEventCache(tmp_path / "history-event-cache.db")
    await cache_root.initialize()
    bot.event_cache = cache_root.for_principal(bot.matrix_id.full_id)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    bot.client = client
    client.next_batch = "s_before_gap"
    room_id = "!room:localhost"
    await bot._conversation_cache.mark_room_joined(room_id)
    history_text = _text_event("$history-text", "old text", 1)
    history_image = _image_event("$history-image", "old image", 2)
    client._recovery_room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[history_text, history_image],
            start="s_before_gap",
            end="p_gap_start",
        ),
    )
    turn_callback = AsyncMock(return_value=DispatchCallbackResult.SUCCEEDED)
    callbacks = cast("dict[DispatchCallbackKind, Any]", bot._dispatch_obligation_runner.callbacks)
    callbacks.update(
        {
            DispatchCallbackKind.MESSAGE: turn_callback,
            DispatchCallbackKind.MEDIA: turn_callback,
        },
    )

    try:
        add_admission = _register_counted_source_callbacks(bot, client)
        response = _limited_empty_classic_response(room_id)
        await client.receive_response(response)
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

        add_admission.assert_called_once()
        assert response.recovered_room_ids == frozenset({room_id})
        assert response.unrecovered_room_ids == frozenset()
        assert response.rooms.join[room_id].timeline.events == []
        assert await bot.event_cache.get_event(room_id, history_text.event_id) is not None
        assert await bot.event_cache.get_event(room_id, history_image.event_id) is not None
        assert bot._dispatch_obligation_store.pending() == ()
        turn_callback.assert_not_awaited()
    finally:
        await client.close()
        await cache_root.close()


@pytest.mark.asyncio
async def test_nio_retries_history_when_cache_admission_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed history cache write must leave nio recovery work retryable."""
    bot = _agent_bot(tmp_path)
    cache_root = SqliteEventCache(tmp_path / "history-event-cache.db")
    await cache_root.initialize()
    bot.event_cache = cache_root.for_principal(bot.matrix_id.full_id)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    bot.client = client
    client.next_batch = "s_before_gap"
    room_id = "!room:localhost"
    await bot._conversation_cache.mark_room_joined(room_id)
    history_text = _text_event("$history-retry", "old text", 1)
    client._recovery_room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[history_text],
            start="s_before_gap",
            end="p_gap_start",
        ),
    )
    original_store_events_batch = bot.event_cache.store_events_batch
    cache_attempts = 0

    async def fail_first_cache_write(
        events: list[tuple[str, str, dict[str, Any]]],
        *,
        expected_membership_epoch: int | None = None,
    ) -> None:
        nonlocal cache_attempts
        cache_attempts += 1
        if cache_attempts == 1:
            msg = "historical cache unavailable"
            raise EventCacheBackendUnavailableError(msg)
        await original_store_events_batch(
            events,
            expected_membership_epoch=expected_membership_epoch,
        )

    monkeypatch.setattr(bot.event_cache, "store_events_batch", fail_first_cache_write)
    bot._dispatch_obligation_runner.register_source_callbacks(
        client,
        owner=bot._runtime_view,
    )
    response = _limited_empty_classic_response(room_id)

    try:
        with pytest.raises(
            nio.CallbackNotAcceptedError,
            match="historical cache unavailable",
        ) as exc_info:
            await client.receive_response(response)
        recovery = cast("Any", client)._recovery
        assert isinstance(exc_info.value.__cause__, EventCacheBackendUnavailableError)
        assert history_text.event_id not in recovery.completed.get(room_id, {})
        assert await bot.event_cache.get_event(room_id, history_text.event_id) is None

        await client.receive_response(response)

        assert cache_attempts == 2
        assert response.recovered_room_ids == frozenset({room_id})
        assert await bot.event_cache.get_event(room_id, history_text.event_id) is not None
        assert bot._dispatch_obligation_store.pending() == ()
    finally:
        await client.close()
        await cache_root.close()


@pytest.mark.asyncio
async def test_new_world_readable_join_caches_prejoin_history_before_fence_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An own-join boundary must cache readable history that nio intentionally skips."""
    bot = _agent_bot(tmp_path)
    cache_root = SqliteEventCache(tmp_path / "joined-history-event-cache.db")
    await cache_root.initialize()
    bot.event_cache = cache_root.for_principal(bot.matrix_id.full_id)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    bot.client = client
    client.user_id = bot.matrix_id.full_id
    bot._first_sync_done = True
    client.next_batch = "s_before_join"
    room_id = "!room:localhost"
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(room_id,)),
    )
    history_text = _text_event("$prejoin-text", "old text", 1)
    history_image = _image_event("$prejoin-image", "old image", 2)
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[history_image, history_text],
            start="p_before_join",
            end=None,
        ),
    )
    client._recovery_room_messages = AsyncMock()
    cached_while_fenced: list[bool] = []
    cache_historical_event = bot._conversation_cache.cache_historical_event

    async def cache_and_observe_fence(room: nio.MatrixRoom, event: nio.Event) -> None:
        cached_while_fenced.append(bot._room_lifecycle.decrypt_notice_is_fenced(room.room_id))
        await cache_historical_event(room, event)

    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_historical_event",
        cache_and_observe_fence,
    )
    turn_callback = AsyncMock(return_value=DispatchCallbackResult.SUCCEEDED)
    callbacks = cast("dict[DispatchCallbackKind, Any]", bot._dispatch_obligation_runner.callbacks)
    callbacks.update(
        {
            DispatchCallbackKind.MESSAGE: turn_callback,
            DispatchCallbackKind.MEDIA: turn_callback,
        },
    )

    try:
        add_admission = _register_counted_source_callbacks(bot, client)
        client.add_response_callback(bot._on_sync_response, nio.SyncResponse)
        response = _newly_joined_world_readable_response(
            room_id,
            bot.matrix_id.full_id,
            limited=True,
            next_batch="s_after_join",
        )
        await client.receive_response(response)
        await client.run_response_callbacks([response])
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

        add_admission.assert_called_once()
        client._recovery_room_messages.assert_not_awaited()
        client.room_messages.assert_awaited_once_with(
            room_id,
            start="p_before_join",
            direction=nio.MessageDirection.back,
            limit=50,
        )
        assert response.unrecovered_room_ids == frozenset()
        assert cached_while_fenced == [True, True]
        assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
        assert await bot.event_cache.get_event(room_id, history_text.event_id) is not None
        assert await bot.event_cache.get_event(room_id, history_image.event_id) is not None
        assert bot._dispatch_obligation_store.pending() == ()
        turn_callback.assert_not_awaited()

        complete_response = _newly_joined_world_readable_response(
            room_id,
            bot.matrix_id.full_id,
            limited=False,
            next_batch="s_after_open",
        )
        await client.receive_response(complete_response)
        await client.run_response_callbacks([complete_response])
        assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    finally:
        await client.close()
        await cache_root.close()


@pytest.mark.asyncio
async def test_new_world_readable_join_cache_failure_rewinds_and_keeps_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-join cache write must keep the join response retryable."""
    bot = _agent_bot(tmp_path)
    cache_root = SqliteEventCache(tmp_path / "joined-history-event-cache.db")
    await cache_root.initialize()
    bot.event_cache = cache_root.for_principal(bot.matrix_id.full_id)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    bot.client = client
    client.user_id = bot.matrix_id.full_id
    bot._first_sync_done = True
    client.next_batch = "s_before_join"
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_join")
    room_id = "!room:localhost"
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(room_id,)),
    )
    history_text = _text_event("$prejoin-retry", "old text", 1)
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[history_text],
            start="p_before_join",
            end=None,
        ),
    )
    client._recovery_room_messages = AsyncMock()

    original_store_events_batch = bot.event_cache.store_events_batch
    historical_cache_attempts = 0

    async def fail_first_cache_write(
        events: list[tuple[str, str, dict[str, Any]]],
        *,
        expected_membership_epoch: int | None = None,
    ) -> None:
        nonlocal historical_cache_attempts
        is_historical_write = any(event_id == history_text.event_id for event_id, _, _ in events)
        if is_historical_write:
            historical_cache_attempts += 1
        if is_historical_write and historical_cache_attempts == 1:
            msg = "joined history cache unavailable"
            raise EventCacheBackendUnavailableError(msg)
        await original_store_events_batch(
            events,
            expected_membership_epoch=expected_membership_epoch,
        )

    monkeypatch.setattr(bot.event_cache, "store_events_batch", fail_first_cache_write)
    bot._dispatch_obligation_runner.register_source_callbacks(
        client,
        owner=bot._runtime_view,
    )
    client.add_response_callback(bot._on_sync_response, nio.SyncResponse)

    try:
        response = _newly_joined_world_readable_response(
            room_id,
            bot.matrix_id.full_id,
            limited=True,
            next_batch="s_after_join",
        )
        await client.receive_response(response)
        with pytest.raises(
            EventCacheBackendUnavailableError,
            match="joined history cache unavailable",
        ):
            await client.run_response_callbacks([response])

        client._recovery_room_messages.assert_not_awaited()
        assert client.next_batch == "s_before_join"
        assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
        assert await bot.event_cache.get_event(room_id, history_text.event_id) is None
        assert bot._dispatch_obligation_store.pending() == ()

        await client.receive_response(response)
        await client.run_response_callbacks([response])

        assert historical_cache_attempts == 2
        assert await bot.event_cache.get_event(room_id, history_text.event_id) is not None
        assert bot._dispatch_obligation_store.pending() == ()
    finally:
        await client.close()
        await cache_root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_nio_rejects_event_when_existing_dispatch_payload_is_corrupt(
    tmp_path: Path,
    transport: str,
) -> None:
    """Corrupt pending work must remain visible to nio as rejected admission."""
    bot = _agent_bot(tmp_path)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    room_id = "!room:localhost"
    room = nio.MatrixRoom(room_id, bot.matrix_id.full_id)
    event = _text_event(f"$corrupt-{transport}", "hello", 1)
    response = _timeline_response(transport, room_id, event)
    await bot._dispatch_obligation_runner.persist(
        room,
        event,
        DispatchCallbackKind.MESSAGE,
    )
    with closing(sqlite3.connect(bot._dispatch_obligation_store._database_path)) as connection, connection:
        connection.execute(
            "UPDATE dispatch_obligations SET event_source_json = ? WHERE source_event_id = ?",
            ("{", event.event_id),
        )
    client.add_event_admission_callback(bot._dispatch_obligation_runner._admit_source_event, nio.RoomMessageText)

    with pytest.raises(nio.CallbackNotAcceptedError) as exc_info:
        await client.receive_response(response)

    assert isinstance(exc_info.value.__cause__, DispatchObligationCorruptionError)
    recovery = cast("Any", client)._recovery
    assert event.event_id not in recovery.completed.get(room_id, {})
    assert bot._dispatch_obligation_store.has_pending(
        event.event_id,
        DispatchCallbackKind.MESSAGE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_nio_accepts_late_non_acceptance_without_live_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    """The rejection signal is too late once ordinary event fanout begins."""
    bot = _agent_bot(tmp_path)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    bot.client = client
    if transport == "classic":
        client.next_batch = "s_before_late_rejection"
    room_id = "!room:localhost"
    event = _text_event(f"$late-{transport}", "hello", 1)
    response = _timeline_response(transport, room_id, event)
    callback_attempts = 0

    async def reject_too_late(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> object:
        nonlocal callback_attempts
        callback_attempts += 1
        message = "ordinary callback rejection"
        raise nio.CallbackNotAcceptedError(message)

    callbacks = cast("dict[DispatchCallbackKind, Any]", bot._dispatch_obligation_runner.callbacks)
    callbacks[DispatchCallbackKind.MESSAGE] = reject_too_late
    schedule_retry = MagicMock()
    monkeypatch.setattr(bot._dispatch_obligation_runner, "_schedule_retry", schedule_retry)
    client.add_event_admission_callback(bot._dispatch_obligation_runner._admit_source_event, nio.RoomMessageText)

    async def run_admitted(
        room: nio.MatrixRoom,
        callback_event: nio.Event,
    ) -> None:
        await bot._dispatch_obligation_runner._run_admitted(
            room,
            callback_event,
            DispatchCallbackKind.MESSAGE,
        )

    client.add_event_callback(run_admitted, nio.RoomMessageText)

    with pytest.raises(
        nio.CallbackNotAcceptedError,
        match="ordinary callback rejection",
    ):
        await client.receive_response(response)
    await client.receive_response(response)

    assert callback_attempts == 1
    assert bot._dispatch_obligation_store.has_pending(
        event.event_id,
        DispatchCallbackKind.MESSAGE,
    )
    schedule_retry.assert_called_once()


@pytest.mark.asyncio
async def test_swallowed_dispatch_persistence_failure_cannot_certify_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected response cannot certify its token or clear its joined-room fence."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_failure"
    bot._first_sync_done = True
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_failure")
    room_id = "!room:localhost"
    bot._room_lifecycle.apply_continuity_record(bot._sync_continuity_store.update_join_fences(add=(room_id,)))
    cache_generation = bot.event_cache.cache_generation
    assert cache_generation is not None
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_failure",
        cache_generation=cache_generation,
    )

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(bot._dispatch_obligation_store, "create_pending", fail_persist)
    admission = bot._dispatch_obligation_runner._admit_source_event
    with pytest.raises(
        nio.CallbackNotAcceptedError,
        match="dispatch database unavailable",
    ) as exc_info:
        await admission(
            nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id),
            _text_event("$unpersisted-response", "hello", 1),
            nio.TimelineEventProvenance.LIVE,
        )
    assert isinstance(exc_info.value.__cause__, OSError)
    assert bot.client.next_batch == "s_before_failure"

    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_failure"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={room_id: MagicMock()})
    with (
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            new=AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
        ),
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
    ):
        await bot._on_sync_response(response)

    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_before_failure"  # noqa: S105

    restarted = _agent_bot(tmp_path)
    assert await restarted._sync_cache_trust.prepare_startup() == "s_before_failure"


@pytest.mark.asyncio
async def test_continuity_write_failure_preserves_prior_pair_and_runtime_trust(
    tmp_path: Path,
) -> None:
    """Apply failure preserves disk state without undoing clean transport continuity."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    cache_generation = bot.event_cache.cache_generation
    assert cache_generation is not None
    old_checkpoint = SyncCheckpoint("s_before_failure", cache_generation=cache_generation)
    bot._sync_continuity_store.replace_checkpoint(old_checkpoint)
    bot._room_lifecycle.apply_continuity_record(bot._sync_continuity_store.update_join_fences(add=(room_id,)))
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_failure")
    bot.client.next_batch = "s_after_failure"
    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_failure"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={room_id: MagicMock()})

    with (
        capture_logs() as logs,
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            new=AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
        ),
        patch(
            "mindroom.matrix.sync_continuity.write_json_file_durable",
            side_effect=OSError("continuity unavailable"),
        ),
        pytest.raises(OSError, match="continuity unavailable"),
    ):
        await bot._on_sync_response(response)

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_before_failure")
    assert bot.client.next_batch == "s_after_failure"
    assert bot._sync_continuity_store.load() == SyncContinuityRecord(
        revision=2,
        checkpoint=old_checkpoint,
        pending_join_decrypt_fences=frozenset({room_id}),
    )
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    assert any(entry["event"] == "matrix_sync_certification_apply_failed" for entry in logs)
    assert not any(entry["event"] == "pre_certification_sync_side_effect_failed_replaying_sync" for entry in logs)


@pytest.mark.asyncio
async def test_certification_cancellation_is_not_logged_as_durability_failure(
    tmp_path: Path,
) -> None:
    """Routine task cancellation must propagate without false durability telemetry."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_cancelled"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={}, leave={})

    with (
        capture_logs() as logs,
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            new=AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
        ),
        patch.object(
            bot,
            "_apply_sync_response_after_dispatch_acceptance",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await bot._handle_classic_sync_response(
            response,
            first_sync_response=False,
            room_member_join_hooks_were_armed=True,
        )

    assert not any(entry["event"] == "matrix_sync_certification_apply_failed" for entry in logs)


@pytest.mark.asyncio
async def test_continuity_acceptance_runs_off_event_loop(tmp_path: Path) -> None:
    """Classic continuity persistence cannot block Matrix callback progress."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={}, leave={})
    write_thread: threading.Thread | None = None
    accept_response = bot._sync_continuity_store.accept_classic_response

    def record_write_thread(
        checkpoint: SyncCheckpoint,
        *,
        joined_room_ids: Iterable[str],
    ) -> SyncContinuityRecord:
        nonlocal write_thread
        write_thread = threading.current_thread()
        return accept_response(checkpoint, joined_room_ids=joined_room_ids)

    with (
        patch.object(
            bot._conversation_cache,
            "cache_sync_timeline_for_certification",
            new=AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
        ),
        patch.object(
            bot._sync_continuity_store,
            "accept_classic_response",
            side_effect=record_write_thread,
        ),
    ):
        await bot._on_sync_response(response)

    assert write_thread is not None
    assert write_thread is not threading.main_thread()


@pytest.mark.asyncio
async def test_dispatch_creation_drains_repeated_cancellation_before_rewind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot escape while its create worker may still commit unseen work."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_cancel"
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_cancel")
    create_started = threading.Event()
    release_create = threading.Event()
    original_create = bot._dispatch_obligation_store.create_pending

    def blocking_create(obligation: object) -> object:
        create_started.set()
        assert release_create.wait(timeout=2)
        return original_create(obligation)  # type: ignore[arg-type]

    monkeypatch.setattr(bot._dispatch_obligation_store, "create_pending", blocking_create)
    run_persisted = AsyncMock()
    monkeypatch.setattr(bot._dispatch_obligation_runner, "_run_persisted", run_persisted)
    admission = bot._dispatch_obligation_runner._admit_source_event
    event = _text_event("$cancelled-create", "hello", 1)
    task = asyncio.create_task(
        admission(
            nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id),
            event,
            nio.TimelineEventProvenance.LIVE,
        ),
    )

    assert await asyncio.to_thread(create_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    escaped_before_worker = task.done()
    release_create.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not escaped_before_worker
    assert bot._dispatch_obligation_store.has_pending(event.event_id, DispatchCallbackKind.MESSAGE)
    assert bot.client.next_batch == "s_before_cancel"
    run_persisted.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_obligation_waits_for_terminal_turn_durability(tmp_path: Path) -> None:
    """In-memory terminal state must not retire exact work before its ledger fsync."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
    event = _text_event("$write-behind", "hello", 1)
    obligation = await bot._dispatch_obligation_runner.persist(
        room,
        event,
        DispatchCallbackKind.MESSAGE,
    )
    assert obligation is not None

    persist_started = threading.Event()
    release_persist = threading.Event()
    original_persist = bot._turn_store._ledger._persist_records

    def blocking_persist(records: tuple[TurnRecord, ...]) -> None:
        persist_started.set()
        assert release_persist.wait(timeout=2)
        original_persist(records)

    with patch.object(bot._turn_store._ledger, "_persist_records", side_effect=blocking_persist):
        bot._turn_store.record_turn(TurnRecord.create([event.event_id], response_event_id="$response"))
        assert await asyncio.to_thread(persist_started.wait, 2)
        run_task = asyncio.create_task(
            bot._dispatch_obligation_runner._run_persisted(
                obligation,
                room=room,
                event=event,
            ),
        )
        try:
            await asyncio.sleep(0.05)
            pending_before_durable_turn = bot._dispatch_obligation_store.has_pending(
                event.event_id,
                DispatchCallbackKind.MESSAGE,
            )
            task_done_before_durable_turn = run_task.done()
        finally:
            release_persist.set()
            await asyncio.gather(run_task, return_exceptions=True)

    assert pending_before_durable_turn
    assert not task_done_before_durable_turn
    assert not bot._dispatch_obligation_store.has_pending(
        event.event_id,
        DispatchCallbackKind.MESSAGE,
    )
    with closing(sqlite3.connect(bot._dispatch_obligation_store._database_path)) as connection, connection:
        terminal_kinds = connection.execute(
            """
            SELECT callback_kind
            FROM dispatch_obligations
            WHERE source_event_id = ?
            ORDER BY callback_kind
            """,
            (event.event_id,),
        ).fetchall()
    assert terminal_kinds == [(DispatchCallbackKind.MESSAGE.value,)]


@pytest.mark.asyncio
async def test_incomplete_shutdown_drain_remains_recoverable_across_repeated_shutdown(tmp_path: Path) -> None:
    """Repeated shutdown keeps raw continuity while durable callbacks own retry."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_previous", cache_generation=_CACHE_GENERATION)
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        side_effect=[
            CoalescingDrainResult(completed=False, cancelled_unready_count=1),
            CoalescingDrainResult(completed=True),
        ],
    )

    await bot.prepare_for_sync_shutdown()
    await bot.prepare_for_sync_shutdown()

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_skips_precallback_uncertified_token(tmp_path: Path) -> None:
    """Shutdown must not flush a nio-advanced token before sync-response certification starts."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_precallback",
        cache_generation=bot.event_cache.cache_generation,
    )
    bot._runtime_view.mark_runtime_started()
    bot.client.next_batch = await bot._sync_cache_trust.prepare_startup()

    bot.client.next_batch = "s_after_precallback"

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_precallback"


@pytest.mark.asyncio
async def test_failed_coalesced_dispatch_returns_exact_source_to_durable_retry(tmp_path: Path) -> None:
    """Gate failure must retry its durable source without restart or another ready signal."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _text_event("$deferred-retry", "retry me", 1_000)
    retried = asyncio.Event()

    async def recovered_callback(_room: nio.MatrixRoom, recovered_event: nio.Event) -> DispatchCallbackResult:
        assert recovered_event.event_id == event.event_id
        retried.set()
        return DispatchCallbackResult.SUCCEEDED

    async def failing_dispatch(_batch: CoalescedBatch) -> None:
        msg = "coalesced dispatch failed"
        raise RuntimeError(msg)

    bot._dispatch_obligation_runner.callbacks = {DispatchCallbackKind.MESSAGE: recovered_callback}
    bot._dispatch_obligation_runner.room_for_id = lambda _room_id: room
    bot._dispatch_obligation_runner._retry_initial_delay_seconds = 0
    bot._dispatch_obligation_runner._retry_max_delay_seconds = 0
    bot._coalescing_gate._dispatch_batch = failing_dispatch
    await bot._dispatch_obligation_runner.persist(room, event, DispatchCallbackKind.MESSAGE)

    await bot._coalescing_gate.admit(
        CoalescingKey(room.room_id, None, event.sender),
        ready_result=ReadyPendingEvent(
            pending_event=PendingEvent(event=event, room=room, source_kind="message"),
        ),
        source_event_id=event.event_id,
        source_kind="message",
    )
    await asyncio.wait_for(retried.wait(), timeout=1)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert not bot._dispatch_obligation_store.has_pending(event.event_id, DispatchCallbackKind.MESSAGE)


@pytest.mark.parametrize(
    ("source_kind", "callback_kind"),
    [
        ("message", DispatchCallbackKind.MESSAGE),
        (IMAGE_SOURCE_KIND, DispatchCallbackKind.MEDIA),
        (MEDIA_SOURCE_KIND, DispatchCallbackKind.MEDIA),
        (VOICE_SOURCE_KIND, DispatchCallbackKind.MEDIA),
    ],
)
def test_failed_coalesced_dispatch_retries_exact_source_kind(
    tmp_path: Path,
    source_kind: str,
    callback_kind: DispatchCallbackKind,
) -> None:
    """Gate failure must return each source to its actual durable callback key."""
    bot = _agent_bot(tmp_path)
    event = _text_event("$retry-kind", "retry me", 1_000)
    bot._dispatch_obligation_runner.retry_pending_turn_source = MagicMock()

    bot._retry_failed_coalesced_dispatch(
        (
            PendingEvent(
                event=event,
                room=nio.MatrixRoom("!room:localhost", bot.agent_user.user_id),
                source_kind=source_kind,
            ),
        ),
    )

    bot._dispatch_obligation_runner.retry_pending_turn_source.assert_called_once_with(
        event.event_id,
        callback_kind,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["readiness_exception", "readiness_self_cancel", "readiness_none", "lane_delivery_failure"],
)
async def test_lane_terminal_drop_returns_deferred_source_to_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """A lane must either retry failed readiness or settle a successful empty result."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _text_event("$lane-retry", "retry me", 1_000)
    runner = bot._dispatch_obligation_runner
    runner._retry_initial_delay_seconds = 60
    runner._retry_max_delay_seconds = 60
    await runner.persist(room, event, DispatchCallbackKind.MESSAGE)

    async def resolve_readiness() -> ReadyPendingEvent | None:
        if failure_mode == "readiness_exception":
            msg = "readiness failed"
            raise RuntimeError(msg)
        if failure_mode == "readiness_self_cancel":
            current_task = asyncio.current_task()
            assert current_task is not None
            current_task.cancel()
            await asyncio.sleep(0)
        if failure_mode == "readiness_none":
            return None
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=event, room=room, source_kind="message"),
        )

    if failure_mode == "lane_delivery_failure":
        monkeypatch.setattr(
            bot._coalescing_gate,
            "admit",
            AsyncMock(side_effect=RuntimeError("lane delivery failed")),
        )

    slot = bot._coalescing_gate.enter_lane(room_id=room.room_id, sender_id=event.sender)
    bot._coalescing_gate.submit_lane_slot(
        slot,
        key=CoalescingKey(room.room_id, None, event.sender),
        source_event_id=event.event_id,
        source_kind="message",
        ready_task=asyncio.create_task(resolve_readiness()),
    )
    await asyncio.wait_for(slot.settled.wait(), timeout=1)

    if failure_mode == "readiness_none":
        assert not runner.store.has_pending(event.event_id, DispatchCallbackKind.MESSAGE)
        assert not bot._coalescing_gate.has_pending_source_event(event.event_id)
        assert not runner._retry_keys
        assert runner._retry_task is None
        return

    assert runner.store.has_pending(event.event_id, DispatchCallbackKind.MESSAGE)
    assert not bot._coalescing_gate.has_pending_source_event(event.event_id)
    assert len(runner._retry_keys) == 1
    retry_key = next(iter(runner._retry_keys))
    assert retry_key.source_event_id == event.event_id
    assert retry_key.callback_kind is DispatchCallbackKind.MESSAGE
    assert runner._retry_task is not None

    runner._retry_task.cancel()
    await asyncio.gather(runner._retry_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_receive_time_gate_shutdown_drains_unresolved_admission() -> None:
    """Sync shutdown should wait for an admitted prompt to become ready and dispatch it."""
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    event = cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": "$waiting",
                "sender": "@user:localhost",
                "origin_server_ts": 1000,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "waiting"},
            },
        ),
    )
    key = CoalescingKey(room.room_id, "$thread", "@user:localhost")
    release_ready = asyncio.Event()
    dispatched: list[list[str]] = []

    async def dispatch_batch(batch: object) -> None:
        dispatched.append(list(batch.source_event_ids))

    async def ready_event() -> object:
        await release_ready.wait()
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=event, room=room, source_kind="message"),
        )

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: True,
    )

    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$waiting",
        source_kind="message",
        ready_task=asyncio.create_task(ready_event()),
    )
    shutdown_task = asyncio.create_task(gate.drain_all())
    await asyncio.sleep(0)

    assert shutdown_task.done() is False

    release_ready.set()
    await shutdown_task

    assert dispatched == [["$waiting"]]


@pytest.mark.asyncio
async def test_receive_time_gate_shutdown_does_not_poison_later_generation() -> None:
    """A shutdown drain should not prevent a later clean sync generation from admitting prompts."""
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    key = CoalescingKey(room.room_id, "$thread", "@user:localhost")
    dispatched: list[list[str]] = []

    def text_event(event_id: str, body: str) -> nio.RoomMessageText:
        return cast(
            "nio.RoomMessageText",
            nio.RoomMessageText.from_dict(
                {
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": body},
                },
            ),
        )

    async def dispatch_batch(batch: object) -> None:
        dispatched.append(list(batch.source_event_ids))

    shutting_down = True
    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: shutting_down,
    )

    waiting_release = asyncio.Event()

    async def waiting_ready() -> object:
        await waiting_release.wait()
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=text_event("$waiting", "waiting"), room=room, source_kind="message"),
        )

    waiting_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        waiting_slot,
        key=key,
        source_event_id="$waiting",
        source_kind="message",
        ready_task=asyncio.create_task(waiting_ready()),
    )
    drain_task = asyncio.create_task(gate.drain_all())
    await asyncio.sleep(0)
    waiting_release.set()
    await drain_task

    shutting_down = False

    async def next_ready() -> object:
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=text_event("$next", "next"), room=room, source_kind="message"),
        )

    next_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        next_slot,
        key=key,
        source_event_id="$next",
        source_kind="message",
        ready_task=asyncio.create_task(next_ready()),
    )
    await gate.drain_all()

    assert dispatched == [["$waiting"], ["$next"]]


@pytest.mark.asyncio
async def test_shutdown_drain_cancels_stuck_ready_task_without_cancelling_dispatch() -> None:
    """Bounded drains should cancel unresolved ready work and report an unsafe result."""
    cancelled = asyncio.Event()

    async def stuck_ready() -> ReadyPendingEvent | None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(stuck_ready()),
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert result.cancelled_unready_count == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_shutdown_drain_counts_self_cancelled_ready_task_as_incomplete() -> None:
    """Undelivered ready work that cancelled itself still means ingress was not dispatched."""

    async def cancelled_ready() -> ReadyPendingEvent | None:
        raise asyncio.CancelledError

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    unresolved_front_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    ready_task = asyncio.create_task(cancelled_ready())
    await asyncio.gather(ready_task, return_exceptions=True)
    assert ready_task.cancelled()
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=ready_task,
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert unresolved_front_slot.released is True
    assert result.completed is False
    assert result.released_reservation_count == 2
    assert result.cancelled_unready_count == 1


@pytest.mark.asyncio
async def test_shutdown_drain_releases_stuck_pre_admission_lane_slot() -> None:
    """Bounded drains should release unresolved lane slots and reject late admission."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert slot.released is True
    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            source_event_id="$late:localhost",
            source_kind="message",
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
        )


@pytest.mark.asyncio
async def test_shutdown_ready_timeout_closes_ready_result_returned_during_cancellation() -> None:
    """Ready results produced while handling timeout cancellation should be closed once."""
    close_count = 0
    cancelled = asyncio.Event()

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$voice:localhost", "voice", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ReadyPendingEvent(pending_event=pending_event)

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(ready()),
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert cancelled.is_set()
    assert close_count == 1
    assert result.completed is False
    assert result.cancelled_unready_count == 1
    assert result.dropped_ready_count == 1


@pytest.mark.asyncio
async def test_shutdown_timeout_reaches_already_running_ready_wait() -> None:
    """Bounded shutdown should interrupt an already-running shielded ready wait."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck_ready() -> ReadyPendingEvent | None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(stuck_ready()),
    )
    await started.wait()

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert result.cancelled_unready_count == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_ready_task_self_cancellation_finishes_no_ready() -> None:
    """Ready tasks that cancel themselves should finish as no-ready work."""

    async def cancelled_ready() -> ReadyPendingEvent | None:
        raise asyncio.CancelledError

    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(cancelled_ready()),
    )

    await gate.drain_all()

    assert slot.settled.is_set()
    assert batches == []


@pytest.mark.asyncio
async def test_enter_lane_during_active_bounded_shutdown_returns_released_counted_slot() -> None:
    """New lane slots during bounded shutdown should be pre-released and counted."""
    shutting_down = False

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: shutting_down,
    )
    old_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    shutting_down = True
    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.05))
    await asyncio.sleep(0)

    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")

    assert slot.closed is True
    assert slot.released is True
    assert slot.settled.is_set()

    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            source_event_id="$late:localhost",
            source_kind="message",
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
        )

    result = await drain_task

    assert old_slot.released is True
    assert result.completed is False
    assert result.released_reservation_count == 2


@pytest.mark.asyncio
async def test_shutdown_timeout_reaches_already_running_same_window_lane_slot_wait() -> None:
    """Bounded shutdown should interrupt same-window lane-slot waits already in progress."""
    shutting_down = False
    wait_entered = asyncio.Event()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.01,
        is_shutting_down=lambda: shutting_down,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    target_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)

    original_wait_for_lane_slots = gate._wait_for_lane_slots

    async def spy_wait_for_lane_slots(
        wait_gate: _GateEntry,
        slots: list[LaneSlot],
    ) -> None:
        if target_slot in slots:
            wait_entered.set()
        await original_wait_for_lane_slots(wait_gate, slots)

    gate._wait_for_lane_slots = spy_wait_for_lane_slots

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "typed", 1000))),
        source_event_id="$text:localhost",
        source_kind="message",
    )
    await asyncio.wait_for(wait_entered.wait(), timeout=5.0)

    shutting_down = True
    result = await gate.drain_all(ready_timeout_seconds=0.05)

    assert target_slot.released is True
    assert result.completed is False
    assert result.released_reservation_count == 1


@pytest.mark.asyncio
async def test_shutdown_in_flight_dispatch_failure_marks_drain_incomplete() -> None:
    """In-flight dispatch failures during bounded shutdown should make the result unsafe."""
    dispatch_entered = asyncio.Event()
    fail_dispatch = asyncio.Event()

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_entered.set()
        await fail_dispatch.wait()
        message = "dispatch failed"
        raise RuntimeError(message)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "typed", 1000))),
    )
    await dispatch_entered.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    for _ in range(100):
        if gate._active_drain_context is not None and gate._gates[key].drain_context is gate._active_drain_context:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("drain context was not installed before dispatch failure")
    fail_dispatch.set()
    result = await drain_task

    assert result.completed is False
    assert result.dispatch_failure_count == 1


@pytest.mark.asyncio
async def test_shutdown_in_flight_dispatch_cancellation_marks_drain_incomplete() -> None:
    """In-flight dispatch cancellation during bounded shutdown should make the result unsafe."""
    dispatch_entered = asyncio.Event()
    dispatch_raised_self_cancel = asyncio.Event()
    cancel_dispatch = asyncio.Event()

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_entered.set()
        await cancel_dispatch.wait()
        dispatch_raised_self_cancel.set()
        raise asyncio.CancelledError

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "typed", 1000))),
    )
    await dispatch_entered.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    for _ in range(100):
        if gate._active_drain_context is not None and gate._gates[key].drain_context is gate._active_drain_context:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("drain context was not installed before dispatch cancellation")
    cancel_dispatch.set()
    result = await drain_task

    assert dispatch_raised_self_cancel.is_set()
    assert result.completed is False
    assert result.dispatch_cancelled_count == 1


@pytest.mark.parametrize(
    (
        "response_rooms",
        "handoff_indexes",
        "remains_pending",
        "response_recovery_complete",
        "shutdown_intent",
        "restart_reason_category",
    ),
    [
        (("!one:localhost",), frozenset({0}), False, True, SYNC_RESTART_SHUTDOWN, "config_reload"),
        (("!one:localhost",), frozenset(), False, False, SYNC_RESTART_SHUTDOWN, "config_reload"),
        (
            ("!one:localhost", "!two:localhost"),
            frozenset({0}),
            False,
            False,
            SYNC_RESTART_SHUTDOWN,
            "config_reload",
        ),
        (
            ("!one:localhost", "!one:localhost"),
            frozenset({0}),
            False,
            False,
            SYNC_RESTART_SHUTDOWN,
            "config_reload",
        ),
        (
            ("!one:localhost", "!one:localhost"),
            frozenset({0, 1}),
            False,
            True,
            SYNC_RESTART_SHUTDOWN,
            "config_reload",
        ),
        (("!one:localhost",), frozenset(), True, False, SYNC_RESTART_SHUTDOWN, "config_reload"),
        (("!one:localhost",), frozenset({0}), False, True, ORDERLY_SHUTDOWN, "process_shutdown"),
    ],
)
@pytest.mark.asyncio
async def test_shutdown_tracks_exact_source_recovery_without_gating_raw_checkpoint(
    tmp_path: Path,
    response_rooms: tuple[str, ...],
    handoff_indexes: frozenset[int],
    remains_pending: bool,
    response_recovery_complete: bool,
    shutdown_intent: RuntimeShutdownIntent,
    restart_reason_category: str,  # noqa: ARG001
) -> None:
    """Terminal interruption proof remains exact while obligations own source replay."""
    bot = _certified_shutdown_bot(tmp_path)
    response_started = [asyncio.Event() for _room_id in response_rooms]
    release_response = asyncio.Event()

    async def interrupted_response(index: int, room_id: str) -> None:
        response_started[index].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if remains_pending:
                await release_response.wait()
            if index in handoff_indexes:
                bot._interrupted_turn_rooms.register(f"$source-{index}", room_id=room_id)
            if not remains_pending:
                raise

    response_tasks = [
        bot._response_runner.track_inbox_response(
            interrupted_response(index, room_id),
            name=f"test_interrupted_response_{index}",
            recovery_proof_ready=lambda index=index: bot._interrupted_turn_rooms.contains(f"$source-{index}"),
        )
        for index, room_id in enumerate(response_rooms)
    ]
    await asyncio.gather(*(event.wait() for event in response_started))
    _install_fast_response_drain(bot)
    await bot.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)

    release_response.set()
    await asyncio.gather(*response_tasks, return_exceptions=True)
    assert all(task.cancelled() for task in response_tasks) == (not remains_pending)
    assert bot._response_runner.incomplete_inbox_responses_recoverable is response_recovery_complete
    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"
    assert bot.pending_sync_restart_retry_room_ids == {response_rooms[index] for index in handoff_indexes}


@pytest.mark.asyncio
async def test_shutdown_keeps_raw_checkpoint_when_response_swallows_cancellation_without_handoff(
    tmp_path: Path,
) -> None:
    """Missing response proof cannot poison separately durable source continuity."""
    bot = _certified_shutdown_bot(tmp_path)
    response_started = asyncio.Event()

    async def swallowed_response_cancellation() -> None:
        response_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    response_task = bot._response_runner.track_inbox_response(
        swallowed_response_cancellation(),
        name="test_swallowed_response_cancellation",
        recovery_proof_ready=lambda: False,
    )
    await response_started.wait()
    _install_fast_response_drain(bot)

    await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    assert response_task.done()
    assert not response_task.cancelled()
    assert response_task.exception() is None
    assert bot._response_runner.incomplete_inbox_responses_recoverable is False
    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.parametrize(
    "terminal_edit_effect",
    [
        pytest.param(None, id="failed"),
        pytest.param(asyncio.CancelledError("terminal edit cancelled"), id="cancelled"),
    ],
)
@pytest.mark.parametrize(
    "prior_visible_body",
    [
        pytest.param("partial answer", id="ordinary-body"),
        pytest.param(RESTART_INTERRUPTED_RESPONSE_NOTE, id="marker-collision"),
    ],
)
@pytest.mark.asyncio
async def test_shutdown_keeps_raw_checkpoint_when_terminal_interruption_note_did_not_land(
    tmp_path: Path,
    terminal_edit_effect: object,
    prior_visible_body: str,
) -> None:
    """Even a colliding prior body cannot prove that the terminal note edit landed."""
    bot = _certified_shutdown_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room_id = "!room:localhost"
    source_event_id = "$source"
    target = MessageTarget.resolve(room_id, "$thread", source_event_id)
    envelope = request_envelope(
        room_id=room_id,
        reply_to_event_id=source_event_id,
        target=target,
        agent_name=bot.agent_name,
    )
    streaming = StreamingResponse(
        target=target,
        config=bot.config,
        runtime_paths=runtime_paths_for(bot.config),
    )
    streaming.event_id = "$response"
    streaming.accumulated_text = prior_visible_body
    response_started = asyncio.Event()
    final_outcomes: list[FinalDeliveryOutcome] = []

    async def interrupted_response() -> None:
        response_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            transport_outcome = await streaming.finalize(bot.client, restart_interrupted=True)
            final_outcome = await bot._response_runner.deps.delivery_gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=target,
                    stream_transport_outcome=transport_outcome,
                    initial_delivery_kind="sent",
                    identity=ResponseIdentity(
                        response_kind="ai",
                        response_envelope=envelope,
                        correlation_id=source_event_id,
                    ),
                    tool_trace=None,
                    extra_content=None,
                ),
            )
            final_outcomes.append(final_outcome)
            bot._response_runner._notify_interrupted_response_recoverable(
                ResponseRequest(
                    thread_history=(),
                    prompt="Hello",
                    response_envelope=envelope,
                    on_interrupted_response_recoverable=lambda: bot._interrupted_turn_rooms.register(
                        source_event_id,
                        room_id=room_id,
                    ),
                ),
                final_outcome,
            )
            raise

    edit_message = AsyncMock(
        side_effect=[
            DeliveredMatrixEvent(event_id="$partial-edit", content_sent={"body": prior_visible_body}),
            terminal_edit_effect,
        ],
    )
    with patch("mindroom.streaming.edit_message_result", new=edit_message):
        assert await streaming._send_or_edit_message(bot.client)
        response_task = bot._response_runner.track_inbox_response(
            interrupted_response(),
            name="test_unlanded_terminal_interruption",
            recovery_proof_ready=lambda: bot._interrupted_turn_rooms.contains(source_event_id),
        )
        await response_started.wait()
        _install_fast_response_drain(bot)
        await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    await asyncio.gather(response_task, return_exceptions=True)
    assert edit_message.await_count == 2
    assert response_task.cancelled()
    assert len(final_outcomes) == 1
    assert final_outcomes[0].mark_handled is True
    assert final_outcomes[0].final_visible_body == prior_visible_body
    assert not bot._interrupted_turn_rooms.contains(source_event_id)
    assert bot._response_runner.incomplete_inbox_responses_recoverable is False
    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_orderly_shutdown_keeps_raw_checkpoint_for_write_behind_handled_response_without_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write-behind handled state alone cannot preserve source continuity."""
    bot = _certified_shutdown_bot(tmp_path)
    source_event_id = "$orderly-source"
    response_started = asyncio.Event()
    persist_started = threading.Event()
    release_persist = threading.Event()
    real_persist = bot._turn_store._ledger._persist_records

    def persist_with_barrier(turn_records: tuple[TurnRecord, ...]) -> None:
        persist_started.set()
        if not release_persist.wait(timeout=5):
            msg = "test did not release terminal-turn persistence"
            raise TimeoutError(msg)
        real_persist(turn_records)

    monkeypatch.setattr(bot._turn_store._ledger, "_persist_records", persist_with_barrier)

    async def write_behind_handled_cancellation() -> None:
        response_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            bot._turn_store.record_turn(
                TurnRecord.create([source_event_id], response_event_id="$interrupted-response"),
            )

    response_task = bot._response_runner.track_inbox_response(
        write_behind_handled_cancellation(),
        name="test_write_behind_handled_orderly_cancellation",
        recovery_proof_ready=lambda: bot._interrupted_turn_rooms.contains(source_event_id),
    )
    await response_started.wait()
    _install_fast_response_drain(bot)

    try:
        await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)
        assert await asyncio.to_thread(persist_started.wait, 5)
        assert response_task.done()
        assert not response_task.cancelled()
        assert bot._turn_store.is_handled(source_event_id)
        assert bot._response_runner.incomplete_inbox_responses_recoverable is False
        assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
        assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"
    finally:
        release_persist.set()
        await asyncio.to_thread(bot._turn_store._ledger.flush)


@pytest.mark.asyncio
async def test_response_timeout_keeps_raw_checkpoint_when_dispatch_recovery_is_pending(
    tmp_path: Path,
) -> None:
    """Durable callback obligations keep raw continuity independent from drain state."""
    bot = _certified_shutdown_bot(tmp_path)
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=CoalescingDrainResult(
            completed=False,
            cancelled_unready_count=1,
        ),
    )
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=False)

    await bot.prepare_for_sync_shutdown()

    assert bot._sync_cache_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_cache_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_response_timeout_does_not_persist_uncertified_precallback_token(tmp_path: Path) -> None:
    """Response cancellation must not promote nio's token before callback certification."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_uncertified"
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    wrap_extracted_collaborators(bot, "_coalescing_gate", "_response_runner")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=False)

    await bot.prepare_for_sync_shutdown()

    assert bot._sync_cache_trust.state is SyncTrustState.PENDING
    assert bot._sync_cache_trust.checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_passes_cancel_source_to_inbox_drain(tmp_path: Path) -> None:
    """Sync-restart shutdown should preserve provenance for detached inbox responses."""
    bot = _agent_bot(tmp_path)
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=True)

    await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    bot._response_runner.drain_inbox_responses.assert_awaited_once_with(
        cancel_after_seconds=5.0,
        shutdown_intent=SYNC_RESTART_SHUTDOWN,
    )
