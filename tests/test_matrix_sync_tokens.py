"""Tests for Matrix sync token persistence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.edit_regenerator import EditHandlingResult
from mindroom.handled_turns import HandledTurnRecord, HandledTurnState
from mindroom.matrix.sync_tokens import load_sync_token, save_sync_token
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


def _message_event(
    *,
    room_id: str = "!room:localhost",
    event_id: str = "$event:localhost",
    body: str = "hello",
) -> nio.RoomMessageText:
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": body,
                "msgtype": "m.text",
            },
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
        },
    )
    event.source = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@user:localhost",
        "room_id": room_id,
        "content": {
            "body": body,
            "msgtype": "m.text",
        },
    }
    return cast("nio.RoomMessageText", event)


def _edit_event(
    *,
    room_id: str = "!room:localhost",
    event_id: str = "$edit:localhost",
    original_event_id: str = "$original:localhost",
    body: str = "* hello edited",
    new_body: str = "hello edited",
) -> nio.RoomMessageText:
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": body,
                "msgtype": "m.text",
                "m.new_content": {
                    "body": new_body,
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": original_event_id,
                    "rel_type": "m.replace",
                },
            },
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1001,
            "type": "m.room.message",
        },
    )
    event.source = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@user:localhost",
        "room_id": room_id,
        "content": {
            "body": body,
            "msgtype": "m.text",
            "m.new_content": {
                "body": new_body,
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": original_event_id,
                "rel_type": "m.replace",
            },
        },
    }
    return cast("nio.RoomMessageText", event)


def test_load_sync_token_returns_none_when_missing(tmp_path: Path) -> None:
    """First-run agents should have no saved sync token."""
    assert load_sync_token(tmp_path, "code") is None


def test_load_sync_token_returns_none_for_whitespace_only_file(tmp_path: Path) -> None:
    """Whitespace-only token files should be treated as missing."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(" \n\t ", encoding="utf-8")

    assert load_sync_token(tmp_path, "code") is None


def test_save_sync_token_round_trip(tmp_path: Path) -> None:
    """Saving and loading should round-trip the token value."""
    save_sync_token(tmp_path, "code", "s12345")

    token_path = _token_path(tmp_path)
    assert token_path.read_text(encoding="utf-8") == "s12345"
    assert load_sync_token(tmp_path, "code") == "s12345"


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
async def test_bot_start_tracks_only_message_and_media_callbacks_for_sync_checkpointing(
    tmp_path: Path,
) -> None:
    """Startup catch-up checkpointing should only track offline text/media ingress callbacks.

    This locks the intended product boundary for the restart fix: source
    messages and media received while the bot was offline or still starting are
    durably caught up, while reactions remain a separate live-only flow.
    """
    bot = _agent_bot(tmp_path)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    tracked_callbacks: dict[str, set[bool]] = {}

    def capture_wrapper(
        callback: object,
        *,
        owner: object | None = None,
        register_sync_event_task: object | None = None,
    ) -> object:
        del owner
        callback_name = callback.__name__
        tracked_callbacks.setdefault(callback_name, set()).add(register_sync_event_task is not None)

        async def noop_wrapper(*_args: object, **_kwargs: object) -> None:
            return None

        return noop_wrapper

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
        patch("mindroom.bot._create_task_wrapper", side_effect=capture_wrapper),
    ):
        await bot.start()

    assert tracked_callbacks["_on_sync_message"] == {True}
    assert tracked_callbacks["_on_sync_media_message"] == {True}
    assert tracked_callbacks["_on_invite"] == {False}
    assert tracked_callbacks["_on_redaction"] == {False}
    assert tracked_callbacks["_on_reaction"] == {False}


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


def test_agent_bot_does_not_own_sync_token_persistence(tmp_path: Path) -> None:
    """Sync token persistence should live behind a dedicated helper, not AgentBot."""
    bot = _agent_bot(tmp_path)

    assert "_persist_sync_token" not in AgentBot.__dict__
    assert "_persist_sync_token" not in vars(bot)


@pytest.mark.asyncio
async def test_on_sync_response_persists_latest_sync_token(tmp_path: Path) -> None:
    """Successful sync responses should update the saved next_batch token."""
    bot = _agent_bot(tmp_path)
    bot._first_sync_done = True
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_latest"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_latest"
    response.rooms = MagicMock(join={})

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)
    flush_task = bot._sync_checkpoint.flush_task
    if flush_task is not None:
        await asyncio.gather(flush_task, return_exceptions=True)

    assert load_sync_token(tmp_path, bot.agent_name) == "s_latest"


@pytest.mark.asyncio
async def test_on_sync_response_defers_persisting_latest_sync_token_until_pending_event_tasks_finish(
    tmp_path: Path,
) -> None:
    """The sync checkpoint should wait until pending event tasks settle."""
    bot = _agent_bot(tmp_path)
    bot._first_sync_done = True
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after"
    gate = asyncio.Event()

    async def slow_event_task() -> None:
        await gate.wait()

    task = asyncio.create_task(slow_event_task())
    bot._sync_checkpoint.register_event_task(task)
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after"
    response.rooms = MagicMock(join={})

    try:
        with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
            await bot._on_sync_response(response)
            assert load_sync_token(tmp_path, bot.agent_name) is None
    finally:
        gate.set()
        await task
        flush_task = bot._sync_checkpoint.flush_task
        if flush_task is not None:
            await asyncio.gather(flush_task, return_exceptions=True)

    assert load_sync_token(tmp_path, bot.agent_name) == "s_after"


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the current client sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._coalescing_gate.drain_all = AsyncMock()

    await bot.prepare_for_sync_shutdown()

    assert load_sync_token(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_waits_for_pending_event_tasks_before_flushing_token(tmp_path: Path) -> None:
    """Shutdown should not checkpoint past unfinished event work."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._coalescing_gate.drain_all = AsyncMock()
    gate = asyncio.Event()

    async def slow_event_task() -> None:
        await gate.wait()

    task = asyncio.create_task(slow_event_task())
    bot._sync_checkpoint.register_event_task(task)
    shutdown_task = asyncio.create_task(bot.prepare_for_sync_shutdown())

    try:
        await asyncio.sleep(0)
        assert load_sync_token(tmp_path, bot.agent_name) is None
    finally:
        gate.set()
        await task
        await shutdown_task

    assert load_sync_token(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_sync_checkpoint_flush_does_not_wait_for_background_message_processing_after_ingress_claim(
    tmp_path: Path,
) -> None:
    """Sync checkpointing should unblock once the inbound turn is durably claimed."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _message_event()
    gate = asyncio.Event()

    async def slow_background_message(_room: nio.MatrixRoom, _event: nio.RoomMessageText) -> None:
        await gate.wait()

    try:
        with (
            patch.object(bot._turn_controller, "prepare_sync_text_event", AsyncMock(return_value=True)),
            patch.object(bot, "_on_message", side_effect=slow_background_message),
        ):
            callback_task = asyncio.create_task(bot._on_sync_message(room, event))
            bot._sync_checkpoint.register_event_task(callback_task)
            bot._sync_checkpoint.note_sync_token("s_claimed")
            await callback_task
            flush_task = bot._sync_checkpoint.flush_task
            assert flush_task is not None
            await asyncio.gather(flush_task, return_exceptions=True)

        assert load_sync_token(tmp_path, bot.agent_name) == "s_claimed"
    finally:
        gate.set()
        await wait_for_background_tasks(owner=bot._runtime_view)


@pytest.mark.asyncio
async def test_pending_inbound_event_stays_replayable_while_dispatch_is_still_in_progress(tmp_path: Path) -> None:
    """Checkpointed inbound events must stay replayable until they reach a durable terminal outcome."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _message_event(event_id="$in-flight:localhost")
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_prepare_dispatch(*_args: object, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    assert bot._turn_store.claim_pending_inbound(room_id=room.room_id, event_source=event.source) is True

    dispatch_task: asyncio.Task[None] | None = None
    try:
        with patch.object(
            bot._turn_controller,
            "_prepare_dispatch",
            side_effect=blocked_prepare_dispatch,
        ):
            dispatch_task = asyncio.create_task(
                bot._turn_controller._dispatch_text_message(
                    room,
                    event,
                    requester_user_id="@user:localhost",
                ),
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            assert [replay.event_id for replay in bot._turn_store.pending_inbound_replays()] == ["$in-flight:localhost"]
    finally:
        release.set()
        if dispatch_task is not None:
            await dispatch_task


@pytest.mark.asyncio
async def test_replay_pending_inbound_turns_schedules_saved_text_message_processing(tmp_path: Path) -> None:
    """Startup replay should requeue pending inbound text turns from durable claims."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    event = _message_event(event_id="$replay:localhost")
    bot._turn_store.claim_pending_inbound(room_id="!room:localhost", event_source=event.source)
    gate = asyncio.Event()

    async def slow_background_message(_room: nio.MatrixRoom, _event: nio.RoomMessageText) -> None:
        await gate.wait()

    try:
        with patch.object(bot, "_on_message", side_effect=slow_background_message) as mock_on_message:
            replayed_source_event_ids = await bot.replay_pending_inbound_turns()
            await asyncio.sleep(0)

        assert replayed_source_event_ids == {"$replay:localhost"}
        mock_on_message.assert_awaited_once()
    finally:
        gate.set()
        await wait_for_background_tasks(owner=bot._runtime_view)


@pytest.mark.asyncio
async def test_replay_pending_inbound_turns_skips_events_with_interrupted_visible_reply_anchor(
    tmp_path: Path,
) -> None:
    """Pending inbox replay should defer to stale-stream recovery once a visible reply exists."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    event = _message_event(event_id="$replay-skip:localhost")
    bot._turn_store.claim_pending_inbound(room_id="!room:localhost", event_source=event.source)
    bot._turn_store.record_turn_record(
        HandledTurnRecord(
            anchor_event_id=event.event_id,
            source_event_ids=(event.event_id,),
            response_event_id="$response-anchor:localhost",
            completed=False,
        ),
    )

    with patch.object(bot, "_on_message", AsyncMock()) as mock_on_message:
        replayed_source_event_ids = await bot.replay_pending_inbound_turns(
            interrupted_target_event_ids={"$response-anchor:localhost"},
        )
        await asyncio.sleep(0)

    assert replayed_source_event_ids == set()
    mock_on_message.assert_not_awaited()
    assert bot._turn_store.pending_inbound_replays() == []


@pytest.mark.asyncio
async def test_startup_replay_does_not_reschedule_same_message_when_sync_redelivers_it(tmp_path: Path) -> None:
    """A claimed startup replay should suppress later sync redelivery of the same event."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _message_event(event_id="$dup:localhost")
    bot._turn_store.claim_pending_inbound(room_id="!room:localhost", event_source=event.source)
    gate = asyncio.Event()

    async def slow_background_message(_room: nio.MatrixRoom, _event: nio.RoomMessageText) -> None:
        await gate.wait()

    try:
        with (
            patch.object(bot, "_on_message", side_effect=slow_background_message) as mock_on_message,
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
            patch.object(
                bot._turn_controller,
                "_should_skip_router_before_shared_ingress_work",
                AsyncMock(return_value=False),
            ),
        ):
            replayed_source_event_ids = await bot.replay_pending_inbound_turns()
            await asyncio.sleep(0)
            assert replayed_source_event_ids == {"$dup:localhost"}

            await bot._on_sync_message(room, event)
            await asyncio.sleep(0)

        assert mock_on_message.await_count == 1
    finally:
        gate.set()
        await wait_for_background_tasks(owner=bot._runtime_view)


@pytest.mark.asyncio
async def test_prepare_sync_text_event_records_edit_for_replay(tmp_path: Path) -> None:
    """Edit events delivered during startup should be durably replayable before processing starts."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _edit_event()

    with patch("mindroom.turn_controller.is_authorized_sender", return_value=True):
        should_process = await bot._turn_controller.prepare_sync_text_event(room, event)

    assert should_process is True
    pending_replays = bot._turn_store.pending_inbound_replays()
    assert [replay.event_source["event_id"] for replay in pending_replays] == ["$edit:localhost"]


@pytest.mark.asyncio
async def test_edit_with_no_durable_regeneration_outcome_stays_replayable(tmp_path: Path) -> None:
    """Edits must not drop their replay record when regeneration produced no durable outcome."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    event = _edit_event(event_id="$edit-pending:localhost")
    assert bot._turn_store.claim_pending_inbound(room_id=room.room_id, event_source=event.source) is True

    with (
        patch.object(
            bot._edit_regenerator,
            "handle_message_edit",
            new=AsyncMock(return_value=EditHandlingResult.retry_later()),
        ),
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        await bot._on_message(room, event)

    assert [replay.event_id for replay in bot._turn_store.pending_inbound_replays()] == ["$edit-pending:localhost"]


@pytest.mark.asyncio
async def test_startup_replayed_edit_retries_until_regeneration_reaches_terminal_outcome(tmp_path: Path) -> None:
    """Startup replay should re-attempt edits that were replayed before their durable anchor existed."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    event = _edit_event(event_id="$edit-retry:localhost")
    assert bot._turn_store.claim_pending_inbound(room_id="!room:localhost", event_source=event.source) is True

    with (
        patch.object(
            bot._edit_regenerator,
            "handle_message_edit",
            new=AsyncMock(
                side_effect=[
                    EditHandlingResult.retry_later(),
                    EditHandlingResult.terminal(
                        HandledTurnState.from_source_event_id(
                            "$edit-retry:localhost",
                            response_event_id="$regen:localhost",
                        ),
                    ),
                ],
            ),
        ) as mock_handle_message_edit,
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
    ):
        replayed_source_event_ids = await bot.replay_pending_inbound_turns()
        assert replayed_source_event_ids == {"$edit-retry:localhost"}
        await asyncio.sleep(0.75)
        await wait_for_background_tasks(owner=bot._runtime_view)

    assert mock_handle_message_edit.await_count == 2
    assert bot._turn_store.pending_inbound_replays() == []
