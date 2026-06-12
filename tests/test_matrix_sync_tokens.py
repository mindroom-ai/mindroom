"""Tests for Matrix sync token persistence."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot, _create_task_wrapper
from mindroom.coalescing import CoalescingDrainResult, CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescedBatch, CoalescingKey, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.dispatch_handoff import PendingDispatchMetadata
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.matrix.sync_certification import SyncCertificationDecision, SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_token_record, save_sync_token
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.coalescing import LaneSlot, _GateEntry


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


def _token_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_tokens" / f"{agent_name}.token"


def _certification_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_tokens" / f"{agent_name}.token.certified"


def _load_sync_token_value(tmp_path: Path, agent_name: str) -> str | None:
    token_record = load_sync_token_record(tmp_path, agent_name)
    if token_record is None:
        return None
    return token_record.token


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


def test_load_sync_token_returns_none_for_whitespace_only_file(tmp_path: Path) -> None:
    """Whitespace-only token files should be treated as missing."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(" \n\t ", encoding="utf-8")

    assert _load_sync_token_value(tmp_path, "code") is None


def test_save_sync_token_round_trip(tmp_path: Path) -> None:
    """Saving and loading should round-trip the token value."""
    save_sync_token(tmp_path, "code", "s12345")

    token_path = _token_path(tmp_path)
    assert json.loads(token_path.read_text(encoding="utf-8")) == {
        "token": "s12345",
        "version": "mindroom-sync-token-v1",
    }
    assert not _certification_path(tmp_path).exists()
    assert _load_sync_token_value(tmp_path, "code") == "s12345"
    token_record = load_sync_token_record(tmp_path, "code")
    assert token_record is not None
    assert token_record.certified is True
    assert token_record.checkpoint == SyncCheckpoint("s12345")


def test_legacy_marker_file_does_not_certify_plaintext_token(tmp_path: Path) -> None:
    """Older marker-only tokens restore for sync continuity but are not certified checkpoints."""
    saved_batch = "s_marker_only"
    token_path = _token_path(tmp_path)
    certification_path = _certification_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(saved_batch, encoding="utf-8")
    certification_path.write_text("legacy-marker\n", encoding="utf-8")

    token_record = load_sync_token_record(tmp_path, "code")

    assert token_record is not None
    assert token_record.token == saved_batch
    assert token_record.certified is False


def test_clear_sync_token_removes_saved_token(tmp_path: Path) -> None:
    """Clearing should remove an existing persisted token."""
    save_sync_token(tmp_path, "code", "s12345")

    clear_sync_token(tmp_path, "code")

    assert _load_sync_token_value(tmp_path, "code") is None
    assert not _token_path(tmp_path).exists()
    assert not _certification_path(tmp_path).exists()


def test_clear_sync_token_is_idempotent(tmp_path: Path) -> None:
    """Clearing a missing token should be a no-op."""
    clear_sync_token(tmp_path, "code")

    assert _load_sync_token_value(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_bot_start_restores_saved_sync_token(tmp_path: Path) -> None:
    """Startup should hydrate the nio client from the previously saved token."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_saved")

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
async def test_legacy_plaintext_sync_token_restores_without_cache_trust(tmp_path: Path) -> None:
    """Origin/main plaintext tokens are sync continuity only, not cache-trust roots."""
    bot = _agent_bot(tmp_path)
    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("s_legacy", encoding="utf-8")

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

    assert client.next_batch == "s_legacy"
    assert bot._sync_trust_state is SyncTrustState.COLD

    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_legacy"
    response.rooms = MagicMock(join={})

    await bot._on_sync_response(response)

    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.token == "s_after_legacy"  # noqa: S105
    assert token_record.certified is True
    assert token_record.checkpoint == SyncCheckpoint("s_after_legacy")


def test_restore_saved_sync_token_ignores_invalid_utf8(tmp_path: Path) -> None:
    """Malformed token bytes should fall back to a cold sync instead of crashing startup."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = None

    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(b"\xff\xfe\xfd")

    bot._restore_saved_sync_token()

    assert bot.client.next_batch is None


@pytest.mark.asyncio
async def test_unknown_pos_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Rejected first-sync saved tokens should be removed before nio retries."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_restored_first_sync_saves_later_checkpoint(tmp_path: Path) -> None:
    """After M_UNKNOWN_POS, later successful sync responses can save a fresh checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later"
    response.rooms = MagicMock(join={})
    await bot._on_sync_response(response)

    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.token == "s_later"  # noqa: S105
    assert token_record.checkpoint == SyncCheckpoint("s_later")


@pytest.mark.asyncio
async def test_unknown_pos_after_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Post-start M_UNKNOWN_POS must not leave a poisoned sync token in place."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_after_start"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected_after_start")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN


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
    response.rooms = MagicMock(join={"!room:localhost": MagicMock(timeline=MagicMock(events=[], limited=False))})
    await bot._on_sync_response(response)

    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.token == "s_later_after_unknown_pos"  # noqa: S105
    assert token_record.checkpoint == SyncCheckpoint("s_later_after_unknown_pos")


@pytest.mark.asyncio
async def test_on_sync_response_persists_latest_sync_token(tmp_path: Path) -> None:
    """Successful sync responses should update the saved next_batch token."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_latest"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_latest"
    response.rooms = MagicMock(join={})

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_latest"
    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.checkpoint == SyncCheckpoint("s_latest")


@pytest.mark.asyncio
async def test_sync_response_side_effect_failure_clears_certified_checkpoint(tmp_path: Path) -> None:
    """A post-certification sync side effect failure must poison the saved token."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_side_effect_failure"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_side_effect_failure"
    response.rooms = MagicMock(join={})
    bot._emit_agent_lifecycle_event = AsyncMock(side_effect=RuntimeError("bot ready failed"))  # type: ignore[method-assign]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(RuntimeError, match="bot ready failed"),
    ):
        await bot._on_sync_response(response)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the latest cache-certified sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"
    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.checkpoint == SyncCheckpoint("s_shutdown")


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_cancelled_ingress(tmp_path: Path) -> None:
    """Incomplete bounded drains must not save certified shutdown checkpoints."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=CoalescingDrainResult(completed=False, cancelled_unready_count=1),
    )

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_unsettled_callbacks(tmp_path: Path) -> None:
    """Shutdown must not checkpoint if callback tasks timed out before the gate drain."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    with patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(return_value=False)):
        await bot.prepare_for_sync_shutdown()

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_post_drain_background_work(tmp_path: Path) -> None:
    """Shutdown must prove owner background work is settled after the gate drain too."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    wait_for_background_tasks = AsyncMock(side_effect=[True, False])

    with patch("mindroom.bot.wait_for_background_tasks", new=wait_for_background_tasks):
        await bot.prepare_for_sync_shutdown()

    assert wait_for_background_tasks.await_count == 2
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_callback_failure_prevents_certified_shutdown_checkpoint(tmp_path: Path) -> None:
    """A Matrix callback exception must make the certified sync token unsafe."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_after_bad_callback")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    async def failing_callback() -> None:
        msg = "canonical key lookup failed"
        raise RuntimeError(msg)

    callback = _create_task_wrapper(failing_callback, owner=bot._runtime_view)
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    await bot.prepare_for_sync_shutdown()

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_callback_failure_clears_saved_checkpoint_immediately(tmp_path: Path) -> None:
    """A failed Matrix callback must clear already-persisted sync continuity."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_before_failure")
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_failure")

    async def failing_callback() -> None:
        msg = "callback failed"
        raise RuntimeError(msg)

    callback = _create_task_wrapper(
        failing_callback,
        owner=bot._runtime_view,
        on_error=bot._mark_callback_failed,
    )
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


def test_callback_failure_blocks_later_certified_checkpoint(tmp_path: Path) -> None:
    """No later sync response may restore certification after a callback failure."""
    bot = _agent_bot(tmp_path)
    bot._mark_callback_failed()

    bot._apply_sync_certification_decision(
        SyncCertificationDecision(
            state=SyncTrustState.CERTIFIED,
            checkpoint_to_save=SyncCheckpoint("s_after_failure"),
        ),
    )

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_room_member_callback_failure_prevents_certified_checkpoint(tmp_path: Path) -> None:
    """Room-member callback exceptions must use the same sync-failure accounting."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_before_member_failure")
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_member_failure")
    bot._on_room_member = AsyncMock(side_effect=RuntimeError("member callback failed"))  # type: ignore[method-assign]
    wrapper = bot._create_room_member_task_wrapper()
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)

    await wrapper(room, _room_member_event())
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_incomplete_shutdown_drain_poison_persists_across_repeated_shutdown(tmp_path: Path) -> None:
    """A later no-op shutdown call must not save a checkpoint after unsafe drain work."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_previous")
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        side_effect=[
            CoalescingDrainResult(completed=False, cancelled_unready_count=1),
            CoalescingDrainResult(completed=True),
        ],
    )

    await bot.prepare_for_sync_shutdown()
    await bot.prepare_for_sync_shutdown()

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_skips_precallback_uncertified_token(tmp_path: Path) -> None:
    """Shutdown must not flush a nio-advanced token before sync-response certification starts."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    save_sync_token(tmp_path, bot.agent_name, "s_before_precallback")
    bot._runtime_view.mark_runtime_started()
    bot._restore_saved_sync_token()

    bot.client.next_batch = "s_after_precallback"

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_precallback"


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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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
        upload_grace_seconds=lambda: 0.0,
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


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_undrained_inbox_responses(tmp_path: Path) -> None:
    """A stuck detached inbox response must block the certified shutdown checkpoint."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=False)

    await bot.prepare_for_sync_shutdown()

    bot._response_runner.drain_inbox_responses.assert_awaited_once_with(cancel_after_seconds=5.0)
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
