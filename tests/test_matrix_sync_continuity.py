"""Tests for Matrix sync continuity and recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot, TeamBot, _create_best_effort_task_wrapper
from mindroom.cancellation import request_task_cancel
from mindroom.coalescing import CoalescingDrainResult, CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescedBatch, CoalescingKey, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.delivery_gateway import FinalizeStreamedResponseRequest, ResponseIdentity
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import PendingDispatchMetadata
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind
from mindroom.handled_turns import TurnRecord
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.client_session import MatrixSyncStorage
from mindroom.matrix.decrypt_failure import e2ee_stats
from mindroom.matrix.sync_certification import SyncRecoveryOutcome, SyncTrustState
from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
from mindroom.matrix.sync_recovery_escape import _CLASSIC_SYNC_RECOVERY_STALL_LIMIT
from mindroom.matrix.sync_token_values import SyncCheckpoint
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    GENERIC_SHUTDOWN,
    ORDERLY_SHUTDOWN,
    SYNC_RESTART_SHUTDOWN,
    RuntimeShutdownIntent,
)
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, StreamingResponse
from tests.bot_helpers import (
    FencedRoomRecorder,
    _configured_team_test_config,
    _configured_team_user,
)
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_journal_support,
    install_shutdown_drain_mocks,
    make_matrix_client_mock,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.sync_continuity_helpers import certify_response, clear_sync_token, load_sync_checkpoint, save_sync_token

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from mindroom.cancellation import TaskCancelSource
    from mindroom.coalescing import LaneSlot, _GateEntry
    from mindroom.event_journal import PrincipalStore
    from mindroom.event_journal.models import DepartureOutcome, DepartureSource
    from mindroom.final_delivery import FinalDeliveryOutcome

_STORE_GENERATION = "test-store-generation"


def _config(tmp_path: Path, *, authorize_senders: bool = False) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=authorize_senders),
        ),
        runtime_paths,
    )


def _agent_bot(tmp_path: Path, *, agent_name: str = "code", authorize_senders: bool = False) -> AgentBot:
    config = _config(tmp_path, authorize_senders=authorize_senders)
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
    install_runtime_journal_support(bot)
    return bot


def test_dispatch_recovery_room_contract_prefers_cache_and_guarantees_room_id(tmp_path: Path) -> None:
    """Recovery room state is best-effort, but its exact room ID is always available."""
    bot = _agent_bot(tmp_path)
    cached_room = nio.MatrixRoom("!cached:localhost", bot.agent_user.user_id)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.rooms = {cached_room.room_id: cached_room}

    assert bot._room_for_journal_event(cached_room.room_id) is cached_room
    assert bot._room_for_journal_event("!missing:localhost").room_id == "!missing:localhost"


@contextmanager
def _cleanup_window_closed_only_by_the_cleanup() -> Iterator[None]:
    """Stop the loop clock when the drain cancels, so its cleanup window cannot expire.

    A bounded drain gives cancelled work one wall-clock window to finish
    cleaning up. A test that asserts what a *finished* cleanup did must not
    depend on that cleanup outrunning a real clock: any loaded machine can
    stall the loop past any bound, and then the assertion reports which
    process the kernel happened to schedule rather than what the drain did.
    Freezing `loop.time` at the instant the drain requests cancellation makes
    the window's deadline unreachable, because `asyncio.wait` derives it from
    that same clock. The drain then returns exactly when the cleanup finishes,
    which is the fact under test.
    """
    loop = asyncio.get_running_loop()
    monotonic = loop.time
    frozen_at: float | None = None

    def frozen_loop_time() -> float:
        return monotonic() if frozen_at is None else frozen_at

    def freeze_then_cancel(task: asyncio.Task[Any], *, cancel_source: TaskCancelSource | None = None) -> None:
        nonlocal frozen_at
        frozen_at = monotonic()
        request_task_cancel(task, cancel_source=cancel_source)

    loop.time = frozen_loop_time  # type: ignore[method-assign]
    try:
        with patch("mindroom.response_runner.request_task_cancel", freeze_then_cancel):
            yield
    finally:
        del loop.time  # type: ignore[method-assign]


def _install_bounded_response_drain(bot: AgentBot, *, cleanup_finishes: bool = True) -> None:
    """Keep the real bounded drain while making both of its windows deterministic.

    The drain waits one window for tracked responses to finish on their own and
    a second window for the ones it cancelled to clean up. The first window can
    only expire here, because every tracked response is parked on an event
    nobody sets, so shortening it just saves wall clock. The second window is
    the one that used to race the machine: `cleanup_finishes` says which
    outcome the test is pinning, and each is then reached without a wall clock
    deciding it -- a cleanup that runs is protected by a frozen clock, and a
    cleanup that parks forever cannot end the window whatever the clock does.
    """
    drain_inbox_responses = bot._response_runner.drain_inbox_responses

    async def bounded_drain(*, cancel_after_seconds: float | None, shutdown_intent: RuntimeShutdownIntent) -> bool:
        assert cancel_after_seconds == 5.0
        cleanup_window = _cleanup_window_closed_only_by_the_cleanup() if cleanup_finishes else nullcontext()
        with cleanup_window:
            return await drain_inbox_responses(cancel_after_seconds=0.01, shutdown_intent=shutdown_intent)

    bot._response_runner.drain_inbox_responses = bounded_drain


def _certified_shutdown_bot(tmp_path: Path) -> AgentBot:
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_previous", store_generation=_STORE_GENERATION)
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_shutdown")
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
    await bot._journal_dispatcher._ingress._admit(
        room,
        event,
        nio.TimelineEventProvenance.LIVE,
    )
    await bot._journal_dispatcher.drain_once()


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
    fenced_during_join: list[bool] = []

    async def join_while_sync_is_live(_client: object, joining_room_id: str) -> bool:
        fenced_during_join.append(bot._room_lifecycle.decrypt_notice_is_fenced(joining_room_id))
        return True

    before_failures = e2ee_stats().decrypt_failures
    with (
        capture_logs(),
        patch("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[])),
        patch("mindroom.bot_room_lifecycle.join_room", new=join_while_sync_is_live),
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.matrix.decrypt_failure._send_decrypt_failure_notice", new=notice),
    ):
        await bot.join_configured_rooms()
        # The fence now suppresses the user-facing notice rather than
        # refusing admission: the event is still journalled and still
        # requests its key, it just does not tell the room it failed.
        assert fenced_during_join == [True]
        assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
        pre_join_event = megolm_event("$pre-join")
        await _admit_and_dispatch_decrypt(bot, room, pre_join_event)
        assert not await bot._journal_dispatcher.store.is_pending("$pre-join")
        notice.assert_not_awaited()
        bot.client.request_room_key.assert_awaited_once_with(pre_join_event)
        assert e2ee_stats().decrypt_failures == before_failures + 1

        # The first response must not certify: a refused admission is the
        # durable-ownership failure that leaves this room's position untrusted.
        bot._sync_checkpoint_trust.record_dispatch_persist_failure()
        await bot._on_sync_response(
            sync_response(
                joined_room_id=room_id,
                next_batch="s_uncertified_room",
            ),
        )
        uncertified_event = megolm_event("$after-uncertified-room")
        await _admit_and_dispatch_decrypt(bot, room, uncertified_event)
        assert not await bot._journal_dispatcher.store.is_pending("$after-uncertified-room")
        notice.assert_not_awaited()

        await bot._on_sync_response(
            sync_response(
                joined_room_id="!other:localhost",
                next_batch="s_certified_other",
            ),
        )
        certified_other_event = megolm_event("$after-certified-other")
        await _admit_and_dispatch_decrypt(bot, room, certified_other_event)
        assert not await bot._journal_dispatcher.store.is_pending("$after-certified-other")
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
async def test_classic_unrecovered_gap_resets_transient_sync_state(
    tmp_path: Path,
) -> None:
    """Classic cannot advance past an in-memory gap it failed to recover."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_with_gap"
    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_with_gap"
    response.unrecovered_room_ids = frozenset({"!gap:localhost"})
    response.rooms = MagicMock(join={}, leave={})

    response.unrecovered_room_ids = frozenset({"!gap:localhost"})

    await bot._on_sync_response(response)

    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot.client.next_batch == ""
    assert bot._first_sync_done is True
    assert bot._classic_sync_rebuild_pending is True
    bot.client.reset_classic_sync_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_classic_incomplete_cache_rewinds_cursor(
    tmp_path: Path,
) -> None:
    """An incomplete cache write cannot advance the Classic checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_gap"
    bot._first_sync_done = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_gap"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={}, leave={})

    bot._sync_checkpoint_trust.record_dispatch_persist_failure()

    await bot._on_sync_response(response)

    assert bot.client.next_batch == ""


@pytest.mark.asyncio
async def test_tokenless_pre_certification_failure_resets_for_replay(
    tmp_path: Path,
) -> None:
    """Failed first response discards transient nio state before cold replay."""
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
            bot,
            "_run_pre_certification_sync_response_side_effects",
            new=AsyncMock(side_effect=RuntimeError("side effect failed")),
        ),
        pytest.raises(RuntimeError, match="side effect failed"),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch == ""
    bot.client.reset_classic_sync_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_cold_history_drop_emits_operator_telemetry(tmp_path: Path) -> None:
    """Rejected history identifies exact source and fence reason."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
    event = _text_event("$cold-history", "old", 1)
    admission = bot._journal_dispatcher._ingress._admit

    await admission(room, event, nio.TimelineEventProvenance.HISTORY)

    # Cold history is admitted as context, so it owes no semantic work at all
    # rather than being admitted and then refused. Being admitted settled is
    # the whole of what "context only" means durably; nothing records the
    # classification beside it.
    assert not await bot._journal_dispatcher.store.is_pending(event.event_id)
    assert await bot._journal_dispatcher.store.load_event(event.event_id) is not None


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
    store.replace_checkpoint(SyncCheckpoint("s_classic", store_generation=_STORE_GENERATION))
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
        checkpoint=SyncCheckpoint("s_classic", store_generation=_STORE_GENERATION),
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


def _mention_event(event_id: str, body: str, mentioned_user_id: str) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": body,
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [mentioned_user_id]},
            },
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
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


async def _is_projected(store: PrincipalStore, *, room_id: str, event_id: str) -> bool:
    """Return whether one message is readable in a room's projected conversation.

    Asked through the conversation read every production caller uses, rather
    than through a point lookup that exists for nobody else.
    """
    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=50)
    return any(message.logical_event_id == event_id for message in page.messages)


def _register_counted_source_callbacks(bot: AgentBot, client: nio.AsyncClient) -> MagicMock:
    with patch.object(
        client,
        "add_event_admission_callback",
        wraps=client.add_event_admission_callback,
    ) as add_admission:
        bot._journal_dispatcher.register(client)
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


def _sync_response(
    next_batch: str,
    *,
    joined_rooms: dict[str, object] | None = None,
) -> nio.SyncResponse:
    """Return one typed successful sync response with no recovery obligations."""
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = next_batch
    response.rooms = MagicMock(join=joined_rooms or {}, leave={})
    response.recovered_room_ids = frozenset()
    response.unrecovered_room_ids = frozenset()
    return cast("nio.SyncResponse", response)


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
    save_sync_token(tmp_path, "code", "s12345", store_generation=_STORE_GENERATION)

    token_path = _token_path(tmp_path)
    assert json.loads(token_path.read_text(encoding="utf-8")) == {
        "checkpoint": {
            "store_generation": _STORE_GENERATION,
            "token": "s12345",
        },
        "pending_join_decrypt_fences": [],
        "revision": 1,
        "version": "mindroom-sync-continuity-v3",
    }
    assert _load_sync_token_value(tmp_path, "code") == "s12345"
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == "s12345"  # noqa: S105
    assert checkpoint.store_generation == _STORE_GENERATION


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
    save_sync_token(tmp_path, "code", "s12345", store_generation=_STORE_GENERATION)

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
        # Certified by the journal now, which the fixture pins.
        store_generation=_STORE_GENERATION,
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

    assert client.loaded_sync_token == ""
    assert client.next_batch == "s_saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "persist_recovery"), [("classic", False), ("sliding", True)])
async def test_bot_start_gives_mindroom_sync_cursor_ownership(
    tmp_path: Path,
    mode: Literal["classic", "sliding"],
    persist_recovery: bool,
) -> None:
    """Every bot disables nio token storage while Sliding retains its recovery lane."""
    bot = _agent_bot(tmp_path)
    bot.config.matrix_sync = MatrixSyncConfig(mode=mode)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    login = AsyncMock(return_value=client)

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", login),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert login.await_args.kwargs["sync_storage"] == MatrixSyncStorage(
        store_tokens=False,
        persist_recovery=persist_recovery,
    )


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
        # Certified by the journal now, which the fixture pins.
        store_generation=_STORE_GENERATION,
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
    assert client.loaded_sync_token == ""
    assert client.next_batch == "s_saved"
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


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
    start_worker = MagicMock()
    bot._journal_dispatcher.start = start_worker
    release_turn_replay = MagicMock()
    bot._journal_dispatcher.release_turn_replay = release_turn_replay

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    # Startup drains everything that does not need the agent fleet; turn
    # replay stays gated until the coordinator releases it.
    start_worker.assert_called_once_with()
    release_turn_replay.assert_not_called()


@pytest.mark.asyncio
async def test_start_runs_pending_invite_recovery_after_callbacks_and_running(
    tmp_path: Path,
) -> None:
    """Recovery must begin only once the bot can actually serve what it finds."""
    bot = _agent_bot(tmp_path)
    bot.orchestrator = MagicMock()
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    observed: dict[str, object] = {}

    def observe_start() -> None:
        observed["running"] = bot.running
        observed["response_callbacks"] = client.add_response_callback.call_count

    bot._journal_dispatcher.start = observe_start

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    # Draining before the bot is running would run recovered work against
    # callbacks that are not registered yet.
    assert observed == {"running": True, "response_callbacks": 2}


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
    install_runtime_journal_support(bot)
    bot.orchestrator = MagicMock(running=False)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    start_worker = MagicMock()
    bot._journal_dispatcher.start = start_worker
    release_turn_replay = MagicMock()
    bot._journal_dispatcher.release_turn_replay = release_turn_replay

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    # Startup drains everything that does not need the agent fleet; turn
    # replay stays gated until the coordinator releases it.
    start_worker.assert_called_once_with()
    release_turn_replay.assert_not_called()


@pytest.mark.asyncio
async def test_authoritative_leave_fences_the_room_without_discarding_continuity(tmp_path: Path) -> None:
    """Leaving one room must not cost this principal its position in every other room.

    The room a principal leaves is fenced by its own membership epoch, which is
    what stops its rows being read again. Discarding the journal-certified global
    checkpoint on top of that would resync every room the principal is still in.
    """
    bot = _agent_bot(tmp_path)
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        store_generation=_STORE_GENERATION,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={"!left:localhost": MagicMock()})

    await bot._apply_own_room_membership_from_sync(response)

    saved = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert saved is not None
    assert saved.token == "s_before_leave"  # noqa: S105
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_before_leave")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_cleanup_failure",
    [
        RuntimeError("call cleanup interrupted"),
        asyncio.CancelledError("call cleanup interrupted"),
    ],
)
async def test_leave_fences_before_failing_call_reconciliation(
    tmp_path: Path,
    call_cleanup_failure: BaseException,
) -> None:
    """Call cleanup cannot suspend or fail before the departure is fenced."""
    bot = _agent_bot(tmp_path)
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={"!left:localhost": MagicMock()})
    operation_order: list[str] = []

    class OrderRecordingStore(FencedRoomRecorder):
        """Record that the fence ran before call cleanup."""

        async def fence_departure(self, room_id: str, *, source: DepartureSource) -> DepartureOutcome:
            """Note where this invalidation fell relative to call cleanup."""
            operation_order.append("fence")
            return await super().fence_departure(room_id, source=source)

    async def fail_call_cleanup(**_kwargs: object) -> None:
        operation_order.append("call")
        raise call_cleanup_failure

    bot._call_manager = MagicMock()
    bot._call_manager.on_sync_room_membership = AsyncMock(side_effect=fail_call_cleanup)

    bot._membership_fence.store = OrderRecordingStore()

    with pytest.raises(type(call_cleanup_failure), match="call cleanup interrupted"):
        await bot._apply_own_room_membership_from_sync(response)

    assert operation_order == ["fence", "call"]


@pytest.mark.asyncio
async def test_bot_start_rejects_checkpoint_from_reset_store_generation(tmp_path: Path) -> None:
    """A certified token cannot skip history after its backing cache was reset."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_stale",
        store_generation="stale-cache-generation",
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

    assert client.loaded_sync_token == ""
    assert client.next_batch == ""
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_bot_start_clears_checkpoint_when_store_generation_is_unavailable(tmp_path: Path) -> None:
    """An unavailable generation cannot prove a saved checkpoint."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_stale",
        store_generation="old-cache-generation",
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

    assert client.loaded_sync_token == ""
    assert client.next_batch == ""
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_legacy_v2_sync_token_path_is_not_parsed(tmp_path: Path) -> None:
    """Legacy token files are outside the unified continuity namespace."""
    token_path = _legacy_token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        '{"version":"mindroom-sync-token-v2","token":"s_old","store_generation":"old"}',
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

    assert client.loaded_sync_token == ""
    assert client.next_batch == ""
    assert token_path.exists()


@pytest.mark.asyncio
async def test_invalid_utf8_continuity_record_repairs_and_starts_cold(tmp_path: Path) -> None:
    """Malformed bytes must repair to a cold record instead of bricking startup."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = None

    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(b"\xff\xfe\xfd")

    assert await bot._sync_checkpoint_trust.prepare_startup() is None
    assert bot._sync_checkpoint_trust.state is SyncTrustState.COLD
    assert bot._sync_continuity_store.load() == SyncContinuityRecord(revision=1)


@pytest.mark.asyncio
async def test_unknown_pos_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Rejected first-sync saved tokens should be removed before nio retries."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected", store_generation=_STORE_GENERATION)
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch == ""
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_checkpoint_trust.state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_restored_first_sync_saves_later_checkpoint(tmp_path: Path) -> None:
    """After M_UNKNOWN_POS, later successful sync responses can save a fresh checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected", store_generation=_STORE_GENERATION)
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot._first_sync_done = True
    response = _sync_response("s_later")
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
        store_generation=_STORE_GENERATION,
    )
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch == ""
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_checkpoint_trust.state is SyncTrustState.UNCERTAIN


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
    response = _sync_response(
        "s_later_after_unknown_pos",
        joined_rooms={"!room:localhost": MagicMock(timeline=MagicMock(events=[], limited=False))},
    )
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
    response = _sync_response("s_latest")

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_latest"
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_latest"  # noqa: S105


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persist_fails", "expected_checkpoint"),
    [
        (False, "s_after_recovered"),
        (True, "s_before_recovered"),
    ],
    ids=["persisted", "persistence-failed"],
)
async def test_aggregate_admission_persistence_gates_recovered_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persist_fails: bool,
    expected_checkpoint: str,
) -> None:
    """Recovered certification advances only after aggregate admission persists its obligation."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_recovered"
    bot._first_sync_done = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_recovered")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_recovered",
        store_generation=_STORE_GENERATION,
    )
    room = nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id)
    event = _text_event("$recovered-source-failed" if persist_fails else "$recovered-source", "hello", 1)
    bot._journal_dispatcher.register(bot.client)
    aggregate_admission = bot.client.add_event_admission_callback.call_args.args[0]
    assert bot.client.add_event_admission_callback.call_count == 1
    response = _sync_response("s_after_recovered")
    response.recovered_room_ids = frozenset({room.room_id})

    if persist_fails:

        async def fail_persist(*_args: object, **_kwargs: object) -> None:
            message = "dispatch database unavailable"
            raise OSError(message)

        monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", fail_persist)

    with (
        patch.object(bot._journal_dispatcher, "_invoke", AsyncMock(return_value=None)) as run_persisted,
    ):
        if persist_fails:
            with pytest.raises(
                nio.CallbackNotAcceptedError,
                match="dispatch database unavailable",
            ) as exc_info:
                await aggregate_admission(room, event, nio.TimelineEventProvenance.LIVE)
            assert isinstance(exc_info.value.__cause__, OSError)
        else:
            await aggregate_admission(room, event, nio.TimelineEventProvenance.LIVE)
            assert await bot._journal_dispatcher.store.is_pending(event.event_id)
            await bot._journal_dispatcher.drain_once()
        await bot._on_sync_response(response)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    if persist_fails:
        run_persisted.assert_not_awaited()
    else:
        run_persisted.assert_awaited_once()
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot.client.next_batch == expected_checkpoint
    assert _load_sync_token_value(tmp_path, bot.agent_name) == expected_checkpoint


@pytest.mark.asyncio
async def test_tokenless_initial_response_commits_one_baseline(tmp_path: Path) -> None:
    """An initial response can become the sole durable baseline."""
    bot = _agent_bot(tmp_path)
    trust = bot._sync_checkpoint_trust
    assert await trust.prepare_startup() is None

    initial_baseline = await certify_response(
        trust,
        next_batch="s_initial",
        recovery=SyncRecoveryOutcome(),
    )
    assert initial_baseline.state is SyncTrustState.CERTIFIED
    assert initial_baseline.checkpoint_to_save == SyncCheckpoint("s_initial")
    assert initial_baseline.reset_client_token is False
    assert trust.retry_token() == "s_initial"
    assert load_sync_checkpoint(tmp_path, bot.agent_name) == SyncCheckpoint(
        "s_initial",
        store_generation=_STORE_GENERATION,
    )


@pytest.mark.asyncio
async def test_sync_response_side_effect_failure_preserves_raw_cache_checkpoint(tmp_path: Path) -> None:
    """A non-cache side effect failure must not poison independently durable raw continuity."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_side_effect_failure"
    bot.client.has_uncommitted_classic_sync_state = True
    response = _sync_response("s_after_side_effect_failure")
    bot._emit_agent_lifecycle_event = AsyncMock(side_effect=RuntimeError("bot ready failed"))  # type: ignore[method-assign]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(RuntimeError, match="bot ready failed"),
    ):
        await bot._on_sync_response(response)

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_after_side_effect_failure")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_side_effect_failure"
    bot.client.acknowledge_classic_sync.assert_called_once_with("s_after_side_effect_failure")


@pytest.mark.asyncio
async def test_membership_cancellation_resets_uncertified_classic_state(tmp_path: Path) -> None:
    """Membership cancellation discards the response before replaying it."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_membership"
    bot._first_sync_done = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_membership")
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
    bot.client.reset_classic_sync_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_classic_receive_loop_exit_rewinds_response_not_dispatched_to_callback(
    tmp_path: Path,
) -> None:
    """Loop cancellation after nio applies a response must restore certified continuity."""
    bot = _agent_bot(tmp_path)
    client = nio.AsyncClient(
        "https://example.org",
        bot.matrix_id.full_id,
        config=nio.AsyncClientConfig(
            encryption_enabled=False,
            backfill_limited_timelines=True,
        ),
    )
    client.restore_login(
        user_id=bot.matrix_id.full_id,
        device_id="TESTDEVICE",
        access_token="test-access-token",  # noqa: S106 - Test-only Matrix session.
    )
    bot.client = client
    bot._first_sync_done = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_response")
    client.next_batch = "s_before_response"
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s_after_response",
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "rooms": {"invite": {}, "leave": {}, "join": {}},
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        },
    )
    assert isinstance(response, nio.SyncResponse)
    response_applied = asyncio.Event()
    callback_started = asyncio.Event()
    hold_before_callback = asyncio.Event()

    async def receive_then_hold(*_args: object, **_kwargs: object) -> nio.SyncResponse:
        await client.receive_response(response)
        response_applied.set()
        await hold_before_callback.wait()
        return response

    async def observe_callback(_response: nio.SyncResponse) -> None:
        callback_started.set()

    client.sync = receive_then_hold  # type: ignore[method-assign]
    client.add_response_callback(observe_callback, nio.SyncResponse)
    sync_task = asyncio.create_task(bot.sync_forever())
    await response_applied.wait()
    assert client.next_batch == "s_after_response"

    sync_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sync_task

    assert not callback_started.is_set()
    assert client.next_batch == "s_before_response"


@pytest.mark.asyncio
async def test_classic_loop_exit_resets_rejected_transient_state(
    tmp_path: Path,
) -> None:
    """Admission failure returns the next receive loop to safe continuity."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_rejected"
    bot._first_sync_done = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_rejected")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_rejected",
        store_generation=_STORE_GENERATION,
    )

    bot._record_dispatch_persist_failure()
    assert bot.client.next_batch == "s_after_rejected"
    await bot._reconcile_classic_sync_cursor_after_loop_exit()
    assert bot.client.next_batch == "s_before_rejected"
    bot.client.reset_classic_sync_state.assert_awaited_once()

    bot.client.next_batch = "s_after_replay"
    response = _sync_response("s_after_replay")
    await bot._on_sync_response(response)

    assert bot.client.next_batch == "s_after_replay"
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_replay"


@pytest.mark.asyncio
async def test_failed_reset_drain_still_restores_committed_cursor(tmp_path: Path) -> None:
    """A retained callback error cannot strand the next Classic loop at an empty cursor."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_uncommitted"
    bot._first_sync_done = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_committed")
    bot.client.reset_classic_sync_state.side_effect = RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await bot._reset_classic_sync_state(force=True)

    assert bot.client.next_batch == "s_committed"
    assert bot._first_sync_done is True
    assert bot._classic_sync_rebuild_pending is True
    assert bot._room_member_join_hooks_armed is False


@pytest.mark.asyncio
async def test_rejected_equal_token_response_still_clears_nio_staging(tmp_path: Path) -> None:
    """Admission failure, not token inequality, decides whether transient state is safe."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_same"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_same")

    bot._record_dispatch_persist_failure()
    await bot._reconcile_classic_sync_cursor_after_loop_exit()

    bot.client.reset_classic_sync_state.assert_awaited_once_with()
    assert bot.client.next_batch == "s_same"


@pytest.mark.asyncio
async def test_running_classic_loop_exit_preserves_acknowledged_room_state(tmp_path: Path) -> None:
    """A clean transport restart must not erase acknowledged room state."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_same"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_same")
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    bot.client.rooms[room.room_id] = room
    bot.client.has_uncommitted_classic_sync_state = False
    bot._first_sync_done = True
    bot.running = True

    await bot._reconcile_classic_sync_cursor_after_loop_exit()

    bot.client.reset_classic_sync_state.assert_not_awaited()
    assert bot.client.next_batch == "s_same"
    assert bot.client.rooms[room.room_id] is room
    assert bot._first_sync_done is True


def test_classic_checkpoint_publication_skips_ack_for_clean_duplicate(tmp_path: Path) -> None:
    """A same-token no-op response has no newly staged nio state to acknowledge."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.has_uncommitted_classic_sync_state = False
    record = bot._sync_continuity_store.replace_checkpoint(
        SyncCheckpoint("s_same", store_generation=_STORE_GENERATION),
    )

    bot._publish_classic_sync_commit(bot.client, record, acknowledge=True)

    bot.client.acknowledge_classic_sync.assert_not_called()
    assert bot._room_lifecycle._applied_continuity_revision == record.revision


@pytest.mark.asyncio
async def test_gap_skipping_checkpoint_rewinds_instead_of_acknowledging(tmp_path: Path) -> None:
    """A checkpoint past an abandoned gap restarts nio rather than acknowledging it.

    nio may still hold recovery state for the room the checkpoint moved past and
    would refuse the acknowledgement, so the escape resets the client onto its
    newly certified position instead.
    """
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.has_uncommitted_classic_sync_state = True
    recovery = SyncRecoveryOutcome(unrecovered_room_ids=frozenset({"!wedged:localhost"}))
    for _ in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT):
        decision = bot._sync_checkpoint_trust.plan_response(
            next_batch="s_past_the_gap",
            recovery=recovery,
        )
    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reset_client_token is True

    applied = await bot._apply_sync_response_decision(decision, recovery=recovery)

    assert applied.state is SyncTrustState.CERTIFIED
    bot.client.acknowledge_classic_sync.assert_not_called()
    bot.client.reset_classic_sync_state.assert_awaited_once()
    assert bot.client.next_batch == "s_past_the_gap"


@pytest.mark.asyncio
async def test_a_never_recoverable_room_never_freezes_the_sync_watermark(tmp_path: Path) -> None:
    """A room nio can never rebuild must not pin the transport on one position.

    The escape certifies past the gap and then resets the client, and the reset
    installs whichever cursor cache trust currently calls safe. If that were the
    checkpoint the escape just moved past, every window would rewind onto the
    same position and the principal would re-sync it forever. So this drives
    several full stall windows through the real response path and pins the
    cursor and the durable checkpoint by value after each one.
    """
    wedged = frozenset({"!wedged:localhost"})
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._first_sync_done = True
    save_sync_token(tmp_path, bot.agent_name, "s_stuck", store_generation=_STORE_GENERATION)
    assert await bot._sync_checkpoint_trust.prepare_startup() == "s_stuck"

    windows = 3
    cursors: list[str] = []
    for attempt in range(_CLASSIC_SYNC_RECOVERY_STALL_LIMIT * windows):
        response = MagicMock(spec=nio.SyncResponse)
        response.next_batch = f"s_live_{attempt}"
        response.recovered_room_ids = frozenset()
        response.unrecovered_room_ids = wedged
        response.rooms = MagicMock(join={}, leave={})
        bot.client.next_batch = response.next_batch
        bot.client.has_uncommitted_classic_sync_state = True
        await bot._on_sync_response(response)
        cursors.append(bot.client.next_batch)

    # Each window rewinds onto the checkpoint it is measured from until the
    # escape certifies past the gap, and that escape is what the next window
    # rewinds to. A frozen watermark would repeat "s_stuck" to the end.
    limit = _CLASSIC_SYNC_RECOVERY_STALL_LIMIT
    escapes = [f"s_live_{window * limit + limit - 1}" for window in range(windows)]
    measured_from = ["s_stuck", *escapes[:-1]]
    assert cursors == [
        token
        for rewind, escape in zip(measured_from, escapes, strict=True)
        for token in [*[rewind] * (limit - 1), escape]
    ]
    assert _load_sync_token_value(tmp_path, bot.agent_name) == escapes[-1]


@pytest.mark.asyncio
async def test_running_classic_loop_exit_resets_equal_token_staging(tmp_path: Path) -> None:
    """An unacknowledged response is dirty even when its token equals the checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_same"
    bot.client.has_uncommitted_classic_sync_state = True
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_same")
    bot._first_sync_done = True
    bot.running = True

    await bot._reconcile_classic_sync_cursor_after_loop_exit()

    bot.client.reset_classic_sync_state.assert_awaited_once_with()
    assert bot.client.next_batch == "s_same"
    assert bot._first_sync_done is True
    assert bot._classic_sync_rebuild_pending is True


@pytest.mark.asyncio
async def test_cancelled_side_effects_rewind_live_cursor(tmp_path: Path) -> None:
    """Cancellation must replay the interrupted response from durable continuity."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_partial"
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_gap",
        store_generation=_STORE_GENERATION,
    )
    assert await bot._sync_checkpoint_trust.prepare_startup() == "s_before_gap"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_partial"
    response.recovered_room_ids = frozenset()
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(
        join={
            "!room:localhost": MagicMock(
                timeline=MagicMock(events=[], limited=True),
            ),
        },
    )

    with (
        patch.object(
            bot,
            "_run_pre_certification_sync_response_side_effects",
            AsyncMock(side_effect=asyncio.CancelledError("watchdog restart")),
        ),
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(asyncio.CancelledError, match="watchdog restart"),
    ):
        await bot._on_sync_response(response)

    assert bot.client.next_batch == "s_before_gap"
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_gap"
    # PENDING, not UNCERTAIN: the interrupted work says nothing about the saved
    # checkpoint, which is exactly where the cursor was just put back. Doubting
    # it was the old cache write's verdict on its own database.
    assert bot._sync_checkpoint_trust.state is SyncTrustState.PENDING


@pytest.mark.asyncio
async def test_gap_cache_cancellation_resets_before_loop_exit(tmp_path: Path) -> None:
    """A cancelled cache write discards its transient gap before replay."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_partial"
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_gap",
        store_generation=_STORE_GENERATION,
    )
    assert await bot._sync_checkpoint_trust.prepare_startup() == "s_before_gap"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_partial"
    response.recovered_room_ids = frozenset()
    response.unrecovered_room_ids = frozenset({"!room:localhost"})
    response.rooms = MagicMock(
        join={
            "!room:localhost": MagicMock(
                timeline=MagicMock(events=[], limited=True),
            ),
        },
    )

    with (
        patch.object(
            bot,
            "_run_pre_certification_sync_response_side_effects",
            AsyncMock(side_effect=asyncio.CancelledError("watchdog restart")),
        ),
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(asyncio.CancelledError, match="watchdog restart"),
    ):
        await bot._on_sync_response(response)

    await bot._reconcile_classic_sync_cursor_after_loop_exit()

    assert bot.client.next_batch == "s_before_gap"
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_gap"


@pytest.mark.asyncio
async def test_classic_startup_clears_legacy_recovery_on_the_client_event_loop(tmp_path: Path) -> None:
    """Legacy nio recovery is purged before the app-owned cursor is installed."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    event_loop_thread = threading.get_ident()
    clear_threads: list[int] = []

    def record_clear_thread() -> None:
        clear_threads.append(threading.get_ident())

    bot.client.clear_persisted_sync_recovery.side_effect = record_clear_thread
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_safe",
        store_generation=_STORE_GENERATION,
    )

    await bot._prepare_matrix_sync_continuity()
    bot.client.clear_persisted_sync_recovery.assert_called_once_with()
    assert clear_threads == [event_loop_thread]
    assert bot.client.next_batch == "s_safe"

    decision = await certify_response(
        bot._sync_checkpoint_trust,
        next_batch="s_after_recovery",
        recovery=SyncRecoveryOutcome(),
    )

    assert decision.state is SyncTrustState.CERTIFIED
    assert decision.reason is None


@pytest.mark.asyncio
async def test_sliding_startup_does_not_clear_its_recovery_lane(tmp_path: Path) -> None:
    """Sliding retains its protocol-specific recovery journal."""
    bot = _agent_bot(tmp_path)
    bot.config.matrix_sync = MatrixSyncConfig(mode="sliding")
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.loaded_sync_token = "s_nio_live"  # noqa: S105
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_safe",
        store_generation=_STORE_GENERATION,
    )

    await bot._prepare_matrix_sync_continuity()

    bot.client.clear_persisted_sync_recovery.assert_not_called()
    assert bot.client.next_batch == "s_safe"
    assert bot.client.loaded_sync_token == "s_nio_live"  # noqa: S105


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the latest cache-certified sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_shutdown")
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
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_shutdown")
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
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    with patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(return_value=False)):
        await bot.prepare_for_sync_shutdown()

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_post_drain_background_timeout_preserves_raw_checkpoint(tmp_path: Path) -> None:
    """Post-drain callback recovery must remain independent from raw cache continuity."""
    bot = _agent_bot(tmp_path)
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    wait_for_background_tasks = AsyncMock(side_effect=[True, False])

    with patch("mindroom.bot.wait_for_background_tasks", new=wait_for_background_tasks):
        await bot.prepare_for_sync_shutdown()

    assert wait_for_background_tasks.await_count == 2
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_shutdown")
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
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_previous"


@pytest.mark.asyncio
async def test_generic_callback_failure_does_not_poison_raw_checkpoint(tmp_path: Path) -> None:
    """Best-effort callback failure must not affect independently durable raw cache state."""
    bot = _agent_bot(tmp_path)
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_after_bad_callback")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    async def failing_callback() -> None:
        msg = "canonical key lookup failed"
        raise RuntimeError(msg)

    callback = _create_best_effort_task_wrapper(failing_callback, owner=bot._runtime_view)
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    with capture_logs() as logs:
        await bot.prepare_for_sync_shutdown()

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_after_bad_callback")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_bad_callback"
    assert not [entry for entry in logs if entry["event"] == "runtime_drain_incomplete_with_durable_dispatch_recovery"]


@pytest.mark.asyncio
async def test_callback_failure_preserves_saved_checkpoint_immediately(tmp_path: Path) -> None:
    """A failed best-effort callback must leave raw sync continuity unchanged."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_before_failure", store_generation=_STORE_GENERATION)
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_failure")

    async def failing_callback() -> None:
        msg = "callback failed"
        raise RuntimeError(msg)

    callback = _create_best_effort_task_wrapper(failing_callback, owner=bot._runtime_view)
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_before_failure")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_failure"


@pytest.mark.asyncio
async def test_durably_accepted_invite_failure_does_not_rewind_classic_cursor(
    tmp_path: Path,
) -> None:
    """Accepted invite work must retry independently of raw sync continuity."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_invite"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_invite")
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
    recovered_invite = AsyncMock()
    bot._room_lifecycle.on_invite = recovered_invite

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert bot.client.next_batch == "s_after_invite"
    # Invites are not journalled: the homeserver keeps offering an invite the
    # bot has not acted on, so it needs no durable row of its own.
    assert await bot._journal_dispatcher.store.pending() == ()
    recovered_invite.assert_awaited_once()
    recovered_event = recovered_invite.await_args.args[1]
    assert isinstance(recovered_event, nio.InviteMemberEvent)
    assert recovered_event.content == {"membership": "invite"}


@pytest.mark.asyncio
async def test_dispatch_persistence_failure_keeps_pre_recovery_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source write failure keeps the checkpoint that predates recovered callback delivery."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_failure"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_failure")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_failure",
        store_generation=_STORE_GENERATION,
    )

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", fail_persist)
    admission = bot._journal_dispatcher._ingress._admit

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
    assert bot.client.next_batch == "s_after_failure"
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_failure"


@pytest.mark.asyncio
async def test_tokenless_dispatch_persistence_failure_defers_cursor_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected tokenless work retries in NIO before cold replay begins."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_unpersisted"

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", fail_persist)
    admission = bot._journal_dispatcher._ingress._admit

    with pytest.raises(nio.CallbackNotAcceptedError, match="dispatch database unavailable"):
        await admission(
            nio.MatrixRoom("!room:localhost", bot.matrix_id.full_id),
            _text_event("$unpersisted", "hello", 1),
            nio.TimelineEventProvenance.LIVE,
        )

    assert bot.client.next_batch == "s_unpersisted"


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
        bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
        bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_failure")
    room_id = "!room:localhost"
    event = _text_event(f"$lost-{transport}", "hello", 1)
    response = _timeline_response(transport, room_id, event)

    create_attempts = 0
    admit = type(bot._journal_dispatcher.store).admit

    async def fail_first_create(store: object, *args: object, **kwargs: object) -> object:
        nonlocal create_attempts
        create_attempts += 1
        if create_attempts == 1:
            message = "dispatch database unavailable"
            error_type = OSError if transport == "classic" else sqlite3.OperationalError
            raise error_type(message)
        return await admit(cast("Any", store), *args, **kwargs)

    monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", fail_first_create)
    monkeypatch.setattr(bot._journal_dispatcher, "_invoke", AsyncMock(return_value=None))
    client.add_event_admission_callback(bot._journal_dispatcher._ingress._admit, nio.RoomMessageText)

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
    assert await bot._journal_dispatcher.store.is_pending(event.event_id)


@pytest.mark.asyncio
async def test_nio_gap_generation_is_rebuilt_after_dispatch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected gap callbacks replay from MindRoom's committed checkpoint."""
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
    client.next_batch = "s_before_failure"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_failure")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_failure",
        store_generation=_STORE_GENERATION,
    )
    room_id = "!room:localhost"
    event = _text_event("$gap-admission-retry", "hello", 1)
    response = cast(
        "nio.SyncResponse",
        _timeline_response("classic", room_id, event),
    )
    response.rooms.join[room_id].timeline.limited = True

    create_attempts = 0
    admit = type(bot._journal_dispatcher.store).admit

    async def fail_first_create(store: object, *args: object, **kwargs: object) -> object:
        nonlocal create_attempts
        create_attempts += 1
        if create_attempts == 1:
            msg = "dispatch database unavailable"
            raise OSError(msg)
        return await admit(cast("Any", store), *args, **kwargs)

    monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", fail_first_create)
    monkeypatch.setattr(bot._journal_dispatcher, "_invoke", AsyncMock(return_value=None))
    bot._journal_dispatcher.register(client)
    recovery_page = nio.RoomMessagesResponse(
        room_id=room_id,
        chunk=[],
        start="p0",
        end=None,
    )
    monkeypatch.setattr(
        client,
        "_recovery_room_messages",
        AsyncMock(return_value=recovery_page),
    )
    with pytest.raises(
        nio.CallbackNotAcceptedError,
        match="dispatch database unavailable",
    ):
        await client.receive_response(response)

    recovery = cast("Any", client)._recovery
    assert response.unrecovered_room_ids == frozenset({room_id})
    assert len(recovery.gaps[room_id]) == 1
    assert client.next_batch == "s_after_failure"
    await bot._reconcile_classic_sync_cursor_after_loop_exit()
    assert client.next_batch == "s_before_failure"
    assert not cast("Any", client)._recovery.gaps

    retry_response = cast(
        "nio.SyncResponse",
        _timeline_response("classic", room_id, event),
    )
    retry_response.rooms.join[room_id].timeline.limited = True
    await client.receive_response(retry_response)
    assert retry_response.recovered_room_ids == frozenset({room_id})
    assert not cast("Any", client)._recovery.gaps
    assert create_attempts == 2

    await bot._on_sync_response(retry_response)

    assert client.next_batch == "s_after_failure"
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_failure"
    await client.close()


@pytest.mark.asyncio
async def test_nio_limited_recovery_projects_history_before_dispatch(tmp_path: Path) -> None:
    """Continuity-recovered text and media must be durable before dispatch."""
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
    client.next_batch = "s_before_gap"
    room_id = "!room:localhost"
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
    was_projected_at_dispatch: list[bool] = []

    async def observe_projection_before_turn(
        room: nio.MatrixRoom,
        event: nio.Event,
    ) -> TurnDispatchOutcome:
        was_projected_at_dispatch.append(
            await _is_projected(
                bot._journal_dispatcher.store,
                room_id=room.room_id,
                event_id=event.event_id,
            ),
        )
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    turn_callback = AsyncMock(side_effect=observe_projection_before_turn)
    bot._journal_dispatcher.callbacks = replace(
        bot._journal_dispatcher.callbacks,
        on_message=turn_callback,
        on_media=turn_callback,
    )

    try:
        add_admission = _register_counted_source_callbacks(bot, client)
        response = _limited_empty_classic_response(room_id)
        await client.receive_response(response)
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

        add_admission.assert_called_once()
        assert response.recovered_room_ids == frozenset({room_id})
        assert response.unrecovered_room_ids == frozenset()
        assert response.rooms.join[room_id].timeline.events == []
        page = await bot._journal_dispatcher.store.read_conversation(
            room_id=room_id,
            thread_id=None,
            limit=10,
        )
        assert {message.logical_event_id for message in page.messages} == {
            history_text.event_id,
            history_image.event_id,
        }
        await bot._journal_dispatcher.drain_once()
        assert await bot._journal_dispatcher.store.pending() == ()
        # Both turns ran, and each one already had its own source event durable.
        assert was_projected_at_dispatch == [True, True]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_nio_recovered_gap_mention_reaches_the_real_response_runner(tmp_path: Path) -> None:
    """The restart-gap message nio 0.36 proves continuous must reach a real reply."""
    bot = _agent_bot(tmp_path, authorize_senders=True)
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
    mention = bot.agent_user.user_id
    gap_mention = _mention_event("$gap-mention", f"{mention}: ping", mention)
    client._recovery_room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[gap_mention],
            start="s_before_gap",
            end="p_gap_start",
        ),
    )
    generate_response = AsyncMock(return_value="$gap-reply")
    install_generate_response_mock(bot, generate_response)

    try:
        _register_counted_source_callbacks(bot, client)
        # The recovery client never logs in, so room classification cannot be served.
        with patch("mindroom.text_ingress_dispatch.is_dm_room", AsyncMock(return_value=False)):
            await client.receive_response(_limited_empty_classic_response(room_id))
            await bot._journal_dispatcher.drain_once()
            assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
            await drain_coalescing(bot)

        generate_response.assert_awaited_once()
        assert generate_response.await_args.kwargs["prompt"] == f"{mention}: ping"
        assert bot._turn_store.is_handled(gap_mention.event_id)
        await bot._journal_dispatcher.drain_once()
        assert await bot._journal_dispatcher.store.pending() == ()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_nio_bounded_exhaustion_answers_the_events_it_recovered(tmp_path: Path) -> None:
    """A backfill that runs dry inside a bounded gap has seen all of it.

    The gap is bounded at both ends -- the sync `prev_batch` above and the
    `since` below -- so a page with no end token means no further event is
    obtainable, not that the walk gave up. These are messages that arrived
    while this bot was a member and has not answered, and treating them as
    cold history is how a burst of user messages used to be silently ignored.

    The cold fence itself is pinned where it belongs, on provenance, by
    `test_cold_history_populates_context_without_work`.
    """
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
    client.next_batch = "s_before_gap"
    room_id = "!room:localhost"
    history_text = _text_event("$unproven-text", "old text", 1)
    history_image = _image_event("$unproven-image", "old image", 2)
    # No end token: the server has no more history to give inside this gap.
    client._recovery_room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[history_text, history_image],
            start="s_before_gap",
            end=None,
        ),
    )
    turn_callback = AsyncMock(return_value=TurnDispatchOutcome.INTENTIONALLY_IGNORED)
    bot._journal_dispatcher.callbacks = replace(
        bot._journal_dispatcher.callbacks,
        on_message=turn_callback,
        on_media=turn_callback,
    )

    try:
        _register_counted_source_callbacks(bot, client)
        response = _limited_empty_classic_response(room_id)
        await client.receive_response(response)
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

        page = await bot._journal_dispatcher.store.read_conversation(
            room_id=room_id,
            thread_id=None,
            limit=10,
        )
        assert {message.logical_event_id for message in page.messages} == {
            history_text.event_id,
            history_image.event_id,
        }
        await bot._journal_dispatcher.drain_once()
        assert await bot._journal_dispatcher.store.pending() == ()
        assert turn_callback.await_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_nio_retries_recovered_event_when_journal_admission_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed recovered-event journal write must leave nio work retryable."""
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
    client.next_batch = "s_before_gap"
    room_id = "!room:localhost"
    history_text = _text_event("$history-retry", "old text", 1)
    client._recovery_room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[history_text],
            start="s_before_gap",
            end="p_gap_start",
        ),
    )
    store = bot._journal_dispatcher.store
    store_type = type(store)
    original_admit = store_type.admit
    admit_attempts = 0

    async def fail_first_admission(self: object, *args: object, **kwargs: object) -> object:
        nonlocal admit_attempts
        admit_attempts += 1
        if admit_attempts == 1:
            msg = "journal database unavailable"
            raise OSError(msg)
        return await original_admit(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_type, "admit", fail_first_admission)
    turn_callback = AsyncMock(return_value=TurnDispatchOutcome.INTENTIONALLY_IGNORED)
    bot._journal_dispatcher.callbacks = replace(
        bot._journal_dispatcher.callbacks,
        on_message=turn_callback,
    )
    bot._journal_dispatcher.register(client)
    response = _limited_empty_classic_response(room_id)

    try:
        with pytest.raises(
            nio.CallbackNotAcceptedError,
            match="journal database unavailable",
        ) as exc_info:
            await client.receive_response(response)
        recovery = cast("Any", client)._recovery
        assert isinstance(exc_info.value.__cause__, OSError)
        assert history_text.event_id not in recovery.completed.get(room_id, {})
        assert not await _is_projected(store, room_id=room_id, event_id=history_text.event_id)

        await client.receive_response(response)

        assert admit_attempts == 2
        assert response.recovered_room_ids == frozenset({room_id})
        assert await _is_projected(store, room_id=room_id, event_id=history_text.event_id)
        await bot._journal_dispatcher.drain_once()
        assert await bot._journal_dispatcher.store.pending() == ()
        turn_callback.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_unreadable_journal_row_does_not_hide_the_events_behind_it(
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
    await bot._journal_dispatcher.admit_out_of_band(
        room,
        event,
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
        live=False,
    )
    journal_path = bot.storage_path / "tracking" / "event_journal.db"
    with closing(sqlite3.connect(journal_path)) as connection, connection:
        connection.execute(
            "UPDATE journal_events SET source_json = ? WHERE event_id = ?",
            ("{", event.event_id),
        )
    client.add_event_admission_callback(bot._journal_dispatcher._ingress._admit, nio.RoomMessageText)

    # Admission deduplicates against the existing row rather than reading it,
    # so a payload that cannot be replayed surfaces when the worker runs it.
    await client.receive_response(response)
    assert await bot._journal_dispatcher.store.is_pending(event.event_id)

    second = _text_event(f"$after-corrupt-{transport}", "hello again", 2)
    await bot._journal_dispatcher.admit_out_of_band(
        room,
        second,
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
        live=False,
    )

    with capture_logs() as logs:
        # One row to a page, so the unreadable row fills the page it sits in
        # and the event behind it lands on the next one. A page's limit counts
        # the rows it read, so the row that decoded nothing still consumed the
        # page -- and the empty result that comes back is explicitly not the
        # end of the backlog, which is the whole reason a caller can page past
        # it rather than stopping on a short page.
        blocked = await bot._journal_dispatcher.store.pending(limit=1)
        assert blocked == ()
        assert not blocked.reached_end
        pending = await bot._journal_dispatcher.store.pending(
            limit=1,
            after_receipt_order=blocked.resume_after,
        )

    # The unreadable row is skipped rather than settled — nothing ran, so
    # nothing may claim it did — and it does not hide the events behind it.
    assert [journal.event_id for journal in pending] == [second.event_id]
    assert any(entry["event"] == "journal_event_row_unreadable" for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_nio_accepts_late_non_acceptance_without_live_replay(
    tmp_path: Path,
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

    bot._journal_dispatcher.callbacks = replace(
        bot._journal_dispatcher.callbacks,
        on_message=reject_too_late,
    )
    client.add_event_admission_callback(bot._journal_dispatcher._ingress._admit, nio.RoomMessageText)

    async def run_admitted(_room: nio.MatrixRoom, _callback_event: nio.Event) -> None:
        await bot._journal_dispatcher.drain_once()

    client.add_event_callback(run_admitted, nio.RoomMessageText)

    # A callback cannot refuse an event that admission already accepted. The
    # worker absorbs the rejection and leaves the event pending, so the second
    # response neither replays it live nor loses it.
    await client.receive_response(response)
    await client.receive_response(response)

    assert callback_attempts == 1
    assert await bot._journal_dispatcher.store.is_pending(event.event_id)


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
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_failure")
    room_id = "!room:localhost"
    bot._room_lifecycle.apply_continuity_record(bot._sync_continuity_store.update_join_fences(add=(room_id,)))
    store_generation = _STORE_GENERATION
    assert store_generation is not None
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_failure",
        store_generation=store_generation,
    )

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", fail_persist)
    admission = bot._journal_dispatcher._ingress._admit
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
    assert bot.client.next_batch == "s_after_failure"

    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_failure"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={room_id: MagicMock()})
    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
    ):
        await bot._on_sync_response(response)

    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    assert bot.client.next_batch == "s_before_failure"
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_before_failure"  # noqa: S105

    restarted = _agent_bot(tmp_path)
    assert await restarted._sync_checkpoint_trust.prepare_startup() == "s_before_failure"


@pytest.mark.asyncio
async def test_cancelled_continuity_acceptance_publishes_runtime_fences_before_escape(
    tmp_path: Path,
) -> None:
    """Cancellation cannot split the durable record from its runtime fence view."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_committed"
    bot.client.has_uncommitted_classic_sync_state = True
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(room_id,)),
    )
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    decision = bot._sync_checkpoint_trust.plan_response(
        next_batch="s_committed",
        recovery=SyncRecoveryOutcome(),
    )
    write_started = threading.Event()
    release_write = threading.Event()
    accept_response = bot._sync_continuity_store.accept_classic_response

    def blocking_accept(
        checkpoint: SyncCheckpoint,
        *,
        joined_room_ids: Iterable[str],
    ) -> SyncContinuityRecord:
        write_started.set()
        assert release_write.wait(timeout=2)
        return accept_response(checkpoint, joined_room_ids=joined_room_ids)

    with patch.object(
        bot._sync_continuity_store,
        "accept_classic_response",
        side_effect=blocking_accept,
    ):
        apply_task = asyncio.create_task(
            bot._apply_sync_response_decision(
                decision,
                recovery=SyncRecoveryOutcome(),
                joined_room_ids=(room_id,),
            ),
        )
        assert await asyncio.to_thread(write_started.wait, 2)
        apply_task.cancel()
        await asyncio.sleep(0)
        release_write.set()

        with pytest.raises(asyncio.CancelledError):
            await apply_task

    record = bot._sync_continuity_store.load()
    assert record.pending_join_decrypt_fences == frozenset()
    assert bot._room_lifecycle._applied_continuity_revision == record.revision
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    bot.client.acknowledge_classic_sync.assert_called_once_with("s_committed")


@pytest.mark.asyncio
async def test_continuity_write_failure_rewinds_staging_on_loop_exit(
    tmp_path: Path,
) -> None:
    """Apply failure preserves the prior pair and replays from its checkpoint."""
    room_id = "!room:localhost"
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    store_generation = _STORE_GENERATION
    assert store_generation is not None
    old_checkpoint = SyncCheckpoint("s_before_failure", store_generation=store_generation)
    bot._sync_continuity_store.replace_checkpoint(old_checkpoint)
    bot._room_lifecycle.apply_continuity_record(bot._sync_continuity_store.update_join_fences(add=(room_id,)))
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_failure")
    bot.client.next_batch = "s_after_failure"
    bot._first_sync_done = True
    bot.running = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_failure"
    response.unrecovered_room_ids = frozenset()
    response.rooms = MagicMock(join={room_id: MagicMock()})

    with (
        capture_logs() as logs,
        patch(
            "mindroom.matrix.sync_continuity.write_json_file_durable",
            side_effect=OSError("continuity unavailable"),
        ),
        pytest.raises(OSError, match="continuity unavailable"),
    ):
        await bot._on_sync_response(response)

    await bot._reconcile_classic_sync_cursor_after_loop_exit()

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_before_failure")
    assert bot.client.next_batch == "s_before_failure"
    assert bot._sync_continuity_store.load() == SyncContinuityRecord(
        revision=2,
        checkpoint=old_checkpoint,
        pending_join_decrypt_fences=frozenset({room_id}),
    )
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    bot.client.reset_classic_sync_state.assert_awaited_once_with()
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
            bot._sync_continuity_store,
            "accept_classic_response",
            side_effect=record_write_thread,
        ),
    ):
        await bot._on_sync_response(response)

    assert write_thread is not None
    assert write_thread is not threading.main_thread()


@pytest.mark.asyncio
async def test_cancelled_admission_still_commits_and_holds_the_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot escape while its create worker may still commit unseen work."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.next_batch = "s_after_cancel"
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_before_cancel")
    create_started = threading.Event()
    release_create = threading.Event()
    original_admit = type(bot._journal_dispatcher.store).admit

    async def blocking_create(store: object, *args: object, **kwargs: object) -> object:
        create_started.set()
        assert await asyncio.to_thread(release_create.wait, 2)
        return await original_admit(cast("Any", store), *args, **kwargs)

    monkeypatch.setattr(type(bot._journal_dispatcher.store), "admit", blocking_create)
    run_persisted = AsyncMock(return_value=None)
    monkeypatch.setattr(bot._journal_dispatcher, "_invoke", run_persisted)
    admission = bot._journal_dispatcher._ingress._admit
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
    release_create.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation before the write means the event was never admitted, and
    # nio was never told it was. The cursor therefore has to stay put so the
    # next response redelivers it.
    assert await bot._journal_dispatcher.store.load_event(event.event_id) is None
    assert bot.client.next_batch == "s_after_cancel"
    run_persisted.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_shutdown_drain_remains_recoverable_across_repeated_shutdown(tmp_path: Path) -> None:
    """Repeated shutdown keeps raw continuity while durable callbacks own retry."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_previous", store_generation=_STORE_GENERATION)
    bot._sync_checkpoint_trust.state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint_trust.checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        side_effect=[
            CoalescingDrainResult(completed=False, cancelled_unready_count=1),
            CoalescingDrainResult(completed=True),
        ],
    )

    await bot.prepare_for_sync_shutdown()
    await bot.prepare_for_sync_shutdown()

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_shutdown")
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
        # Certified by the journal now, which the fixture pins.
        store_generation=_STORE_GENERATION,
    )
    bot._runtime_view.mark_runtime_started()
    bot.client.next_batch = await bot._sync_checkpoint_trust.prepare_startup()

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

    async def recovered_callback(_room: nio.MatrixRoom, recovered_event: nio.Event) -> TurnDispatchOutcome:
        assert recovered_event.event_id == event.event_id
        retried.set()
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    async def failing_dispatch(_batch: CoalescedBatch) -> None:
        msg = "coalesced dispatch failed"
        raise RuntimeError(msg)

    bot._journal_dispatcher.callbacks = replace(
        bot._journal_dispatcher.callbacks,
        on_message=recovered_callback,
    )
    bot._journal_dispatcher.room_for_id = lambda _room_id: room
    bot._coalescing_gate._dispatch_batch = failing_dispatch
    await bot._journal_dispatcher.admit_out_of_band(room, event, EventKind.MESSAGE, EventClass.ACTIONABLE, live=False)

    await bot._coalescing_gate.admit(
        CoalescingKey(room.room_id, None, event.sender),
        ready_result=ReadyPendingEvent(
            pending_event=PendingEvent(event=event, room=room, source_kind="message"),
        ),
        source_event_id=event.event_id,
        source_kind="message",
    )
    await bot._coalescing_gate.drain_all()
    await bot._journal_dispatcher.drain_once()
    await asyncio.wait_for(retried.wait(), timeout=1)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert not await bot._journal_dispatcher.store.is_pending(event.event_id)


@pytest.mark.parametrize(
    "source_kind",
    ["message", IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND],
)
def test_failed_coalesced_dispatch_retries_every_source_kind(
    tmp_path: Path,
    source_kind: str,
) -> None:
    """Gate failure returns each source to the journal, whatever its kind.

    The journal row already records the kind, so the retry does not have to
    reconstruct it — which is what the old durable key needed and got wrong
    when a source kind and callback kind disagreed.
    """
    bot = _agent_bot(tmp_path)
    event = _text_event("$retry-kind", "retry me", 1_000)
    bot._journal_dispatcher.retry_turn_source = MagicMock()

    bot._retry_failed_coalesced_dispatch(
        (
            PendingEvent(
                event=event,
                room=nio.MatrixRoom("!room:localhost", bot.agent_user.user_id),
                source_kind=source_kind,
            ),
        ),
    )

    bot._journal_dispatcher.retry_turn_source.assert_called_once_with(event.event_id)


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
    runner = bot._journal_dispatcher
    await runner.admit_out_of_band(room, event, EventKind.MESSAGE, EventClass.ACTIONABLE, live=False)

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
        # A successful empty result settles the source: there is nothing to run.
        assert not await runner.store.is_pending(event.event_id)
        assert not bot._coalescing_gate.has_pending_source_event(event.event_id)
        return

    # Failed readiness leaves the source pending, so the worker runs it again.
    assert await runner.store.is_pending(event.event_id)
    assert not bot._coalescing_gate.has_pending_source_event(event.event_id)


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
    # A response that parks on `release_response` cannot clean up until this
    # test lets it, so its drain window must expire; every other response
    # cleans up straight away and must be allowed to finish doing so.
    _install_bounded_response_drain(bot, cleanup_finishes=not remains_pending)
    await bot.prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)

    release_response.set()
    await asyncio.gather(*response_tasks, return_exceptions=True)
    assert all(task.cancelled() for task in response_tasks) == (not remains_pending)
    assert bot._response_runner.incomplete_inbox_responses_recoverable is response_recovery_complete
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
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
    _install_bounded_response_drain(bot)

    await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    assert response_task.done()
    assert not response_task.cancelled()
    assert response_task.exception() is None
    assert bot._response_runner.incomplete_inbox_responses_recoverable is False
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
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
        _install_bounded_response_drain(bot)
        await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    await asyncio.gather(response_task, return_exceptions=True)
    assert edit_message.await_count == 2
    assert response_task.cancelled()
    assert len(final_outcomes) == 1
    assert final_outcomes[0].mark_handled is True
    assert final_outcomes[0].final_visible_body == prior_visible_body
    assert not bot._interrupted_turn_rooms.contains(source_event_id)
    assert bot._response_runner.incomplete_inbox_responses_recoverable is False
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_orderly_shutdown_keeps_raw_checkpoint_for_handled_response_without_handoff(
    tmp_path: Path,
) -> None:
    """Durably handled state alone cannot preserve source continuity."""
    bot = _certified_shutdown_bot(tmp_path)
    source_event_id = "$orderly-source"
    response_started = asyncio.Event()

    async def handled_cancellation() -> None:
        response_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await bot._turn_store.record_turn(
                TurnRecord.create([source_event_id], response_event_id="$interrupted-response"),
            )

    response_task = bot._response_runner.track_inbox_response(
        handled_cancellation(),
        name="test_handled_orderly_cancellation",
        recovery_proof_ready=lambda: bot._interrupted_turn_rooms.contains(source_event_id),
    )
    await response_started.wait()
    _install_bounded_response_drain(bot)

    await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)

    assert response_task.done()
    assert not response_task.cancelled()
    assert bot._turn_store.is_handled(source_event_id)
    assert bot._response_runner.incomplete_inbox_responses_recoverable is False
    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


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

    assert bot._sync_checkpoint_trust.state is SyncTrustState.CERTIFIED
    assert bot._sync_checkpoint_trust.checkpoint == SyncCheckpoint("s_shutdown")
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_response_timeout_does_not_persist_uncertified_precallback_token(tmp_path: Path) -> None:
    """Response cancellation must not promote nio's token before callback certification."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_uncertified"
    bot._sync_checkpoint_trust.state = SyncTrustState.PENDING
    wrap_extracted_collaborators(bot, "_coalescing_gate", "_response_runner")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=False)

    await bot.prepare_for_sync_shutdown()

    assert bot._sync_checkpoint_trust.state is SyncTrustState.PENDING
    assert bot._sync_checkpoint_trust.checkpoint is None
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


class TestAPreviousRecordVersionSaysWhatItCosts:
    """A record from an earlier version is an upgrade, not damage, and says so."""

    @staticmethod
    def _write_record(tmp_path: Path, payload: dict[str, Any]) -> SyncContinuityStore:
        path = tmp_path / "sync_continuity" / "code.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return SyncContinuityStore(tmp_path, "code")

    def test_a_previous_version_record_names_the_messages_it_cannot_answer(self, tmp_path: Path) -> None:
        """Refusing a v2 record must say the downtime window goes unanswered.

        The refusal is correct -- that checkpoint certifies a store this build
        does not have. What must not happen is refusing silently, or in wording
        that reads like a corrupt file, because the operator's only remedy is
        knowing the window exists before they upgrade.
        """
        store = self._write_record(
            tmp_path,
            {
                "version": "mindroom-sync-continuity-v2",
                "revision": 3,
                "checkpoint": {"token": "s123", "cache_generation": "gen-1"},
                "pending_join_decrypt_fences": [],
            },
        )

        with capture_logs() as logs, pytest.raises(RuntimeError, match="unsupported version"):
            store.load()

        upgraded = [entry for entry in logs if entry["event"] == "matrix_sync_continuity_record_upgraded"]
        assert len(upgraded) == 1
        assert upgraded[0]["found_version"] == "mindroom-sync-continuity-v2"
        assert "never" in upgraded[0]["consequence"]
        assert "answered" in upgraded[0]["consequence"]

    def test_a_record_that_is_merely_damaged_does_not_claim_an_upgrade(self, tmp_path: Path) -> None:
        """An unrecognised version is not a previous one, and must not be described as one."""
        store = self._write_record(tmp_path, {"version": "not-a-mindroom-record", "revision": 0})

        with capture_logs() as logs, pytest.raises(RuntimeError, match="unsupported version"):
            store.load()

        assert [entry for entry in logs if entry["event"] == "matrix_sync_continuity_record_upgraded"] == []
