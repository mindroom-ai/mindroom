"""Tests for stale streaming cleanup and restart auto-resume."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedThread,
    auto_resume_interrupted_threads,
    cleanup_stale_streaming_messages,
)
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BOT_USER_ID = "@mindroom_test_agent:example.com"
OTHER_BOT_USER_ID = "@mindroom_other:example.com"
ROOM_ID = "!room:example.com"
NOW_MS = 1_000_000
STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_RECENCY_GUARD_MS + 60_000
AUTO_RESUME_MESSAGE = (
    "[System: Previous response was interrupted by service restart. Please continue where you left off.]"
)


def _make_config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={
                "test_agent": {
                    "display_name": "Test Agent",
                    "rooms": [ROOM_ID],
                },
            },
            authorization={"default_room_access": True, "agent_reply_permissions": {}},
            mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
        ),
        test_runtime_paths(tmp_path),
    )


def _make_message_event(
    *,
    event_id: str,
    body: str,
    timestamp_ms: int,
    sender: str = BOT_USER_ID,
    room_id: str = ROOM_ID,
    relates_to: dict[str, object] | None = None,
    new_content: dict[str, object] | None = None,
) -> nio.RoomMessageText:
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
    }
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    if new_content is not None:
        content["m.new_content"] = new_content

    event = nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "type": "m.room.message",
            "room_id": room_id,
        },
    )
    event.source = event.__dict__["source"]
    return cast("nio.RoomMessageText", event)


def _make_reaction_event(
    *,
    event_id: str,
    target_event_id: str,
    key: str,
    timestamp_ms: int,
    sender: str = BOT_USER_ID,
    room_id: str = ROOM_ID,
) -> nio.ReactionEvent:
    event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target_event_id,
                    "key": key,
                },
            },
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "type": "m.reaction",
            "room_id": room_id,
        },
    )
    event.source = event.__dict__["source"]
    return event


def _room_messages_response(*events: object, end: str | None = None) -> nio.RoomMessagesResponse:
    response = MagicMock()
    response.__class__ = nio.RoomMessagesResponse
    response.chunk = list(events)
    response.end = end
    return response


async def _aiter(*events: object) -> AsyncIterator[object]:
    for event in events:
        yield event


async def _raising_aiter(exc: Exception) -> AsyncIterator[None]:
    if False:
        yield None
    raise exc


async def _run_cleanup(
    client: AsyncMock,
    config: Config,
    *,
    joined_rooms: list[str],
    bot_user_ids: set[str] | None = None,
    now_ms: int = NOW_MS,
) -> tuple[int, list[InterruptedThread]]:
    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
            new=AsyncMock(return_value=joined_rooms),
        ),
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=now_ms / 1000),
    ):
        return await cleanup_stale_streaming_messages(
            client,
            bot_user_id=BOT_USER_ID,
            bot_user_ids={BOT_USER_ID} if bot_user_ids is None else bot_user_ids,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )


@pytest.mark.asyncio
async def test_relations_api_filters_reactions_and_unions_history_ids(tmp_path: Path) -> None:
    """Cleanup should redact valid relation hits plus any history-scanned stop reactions."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
        ),
        _make_reaction_event(
            event_id="$history-stop",
            target_event_id="$message",
            key="🛑",
            timestamp_ms=NOW_MS - 1_200,
        ),
    )
    client.room_get_event_relations = MagicMock(
        return_value=_aiter(
            _make_reaction_event(
                event_id="$relations-stop",
                target_event_id="$message",
                key="🛑",
                timestamp_ms=NOW_MS - 1_000,
            ),
            _make_reaction_event(
                event_id="$wrong-key",
                target_event_id="$message",
                key="👍",
                timestamp_ms=NOW_MS - 900,
            ),
            _make_reaction_event(
                event_id="$wrong-sender",
                target_event_id="$message",
                key="🛑",
                timestamp_ms=NOW_MS - 800,
                sender=OTHER_BOT_USER_ID,
            ),
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
    ):
        cleaned, interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            bot_user_ids={BOT_USER_ID},
        )

    assert cleaned == 1
    assert interrupted == []
    assert {call.kwargs["event_id"] for call in client.room_redact.await_args_list} == {
        "$history-stop",
        "$relations-stop",
    }


@pytest.mark.asyncio
async def test_relations_api_error_falls_back_to_history_scan_ids(tmp_path: Path) -> None:
    """Cleanup should still redact history-scanned IDs when relations lookup fails."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
        ),
        _make_reaction_event(
            event_id="$history-stop",
            target_event_id="$message",
            key="🛑",
            timestamp_ms=NOW_MS - 1_000,
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_raising_aiter(AttributeError("next_batch")))

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
    ):
        cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    client.room_redact.assert_awaited_once()
    assert client.room_redact.await_args.kwargs["event_id"] == "$history-stop"


@pytest.mark.asyncio
async def test_relations_lookup_uses_original_event_id_not_latest_edit(tmp_path: Path) -> None:
    """Relations lookup must target the original message event, not the latest edit event."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    original = _make_message_event(
        event_id="$original",
        body="Initial answer",
        timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
    )
    edit = _make_message_event(
        event_id="$latest-edit",
        body="* New answer",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
        new_content={"body": "New answer ⋯", "msgtype": "m.text"},
    )
    client.room_messages.return_value = _room_messages_response(original, edit)
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$cleanup-edit"),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == []
    assert client.room_get_event_relations.call_args.args[1] == "$original"
    assert mock_edit.await_args.args[2] == "$original"


@pytest.mark.asyncio
async def test_cleanup_skips_completed_stream_status_even_with_trailing_marker(tmp_path: Path) -> None:
    """Cleanup must trust persisted stream status over a stale visible marker."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    original = _make_message_event(
        event_id="$original",
        body="Partial answer ⋯",
        timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
    )
    completed_edit = _make_message_event(
        event_id="$completed-edit",
        body="* Finished answer ⋯",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
        new_content={
            "body": "Finished answer ⋯",
            "msgtype": "m.text",
            "io.mindroom.stream_status": "completed",
        },
    )
    client.room_messages.return_value = _room_messages_response(original, completed_edit)

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$cleanup-edit"),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_returns_interrupted_thread_per_cleaned_threaded_message(tmp_path: Path) -> None:
    """Cleanup should return one interrupted-thread record per cleaned threaded message."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$older",
            body="First partial ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        ),
        _make_message_event(
            event_id="$newer",
            body="Second partial ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(side_effect=["$edit1", "$edit2"]),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 2
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$older",
            partial_text="First partial",
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$newer",
            partial_text="Second partial",
        ),
    ]


@pytest.mark.asyncio
async def test_auto_resume_sends_correctly_threaded_messages(tmp_path: Path) -> None:
    """Auto-resume should send the requested system message into each interrupted thread."""
    _ = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$target-one",
            partial_text="One",
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-two",
            target_event_id="$target-two",
            partial_text="Two",
        ),
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message",
            new=AsyncMock(side_effect=["$resume1", "$resume2"]),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        resumed_count = await auto_resume_interrupted_threads(client, interrupted)

    assert resumed_count == 2
    assert mock_send.await_count == 2
    first_content = mock_send.await_args_list[0].args[2]
    second_content = mock_send.await_args_list[1].args[2]
    assert first_content["body"] == AUTO_RESUME_MESSAGE
    assert first_content["m.relates_to"]["rel_type"] == "m.thread"
    assert first_content["m.relates_to"]["event_id"] == "$thread-one"
    assert first_content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$target-one"}
    assert second_content["body"] == AUTO_RESUME_MESSAGE
    assert second_content["m.relates_to"]["event_id"] == "$thread-two"
    mock_sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_auto_resume_honors_max_resumes_cap(tmp_path: Path) -> None:
    """Auto-resume should stop after the configured resume cap."""
    _ = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id=f"$thread-{index}",
            target_event_id=f"$target-{index}",
            partial_text=f"Part {index}",
        )
        for index in range(12)
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message",
            new=AsyncMock(return_value="$resume"),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        resumed_count = await auto_resume_interrupted_threads(client, interrupted, max_resumes=10)

    assert resumed_count == 10
    assert mock_send.await_count == 10
    assert mock_sleep.await_count == 9


@pytest.mark.asyncio
async def test_auto_resume_skips_thread_id_none(tmp_path: Path) -> None:
    """Auto-resume should skip interrupted records that do not have a thread ID."""
    _ = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id=None,
            target_event_id="$non-threaded",
            partial_text="Unthreaded",
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$threaded",
            target_event_id="$target",
            partial_text="Threaded",
        ),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message",
        new=AsyncMock(return_value="$resume"),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(client, interrupted)

    assert resumed_count == 1
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[1] == ROOM_ID
    assert mock_send.await_args.args[2]["m.relates_to"]["event_id"] == "$threaded"


@pytest.mark.asyncio
async def test_orchestrator_runs_cleanup_and_resume_before_sync_loops(tmp_path: Path) -> None:
    """Startup should clean stale streams and queue resumes before sync loops begin."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config

    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.try_start = AsyncMock(return_value=True)
    router_bot.running = True
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.agent_user = MagicMock(user_id="@mindroom_router:example.com")
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}

    call_order: list[str] = []

    async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
        call_order.append("wait")

    async def _setup_rooms(_: list[object]) -> None:
        call_order.append("setup")

    async def _cleanup(_: list[object], __: Config) -> list[InterruptedThread]:
        call_order.append("cleanup")
        return [
            InterruptedThread(
                room_id=ROOM_ID,
                thread_id="$thread-root",
                target_event_id="$target",
                partial_text="Half finished",
            ),
        ]

    async def _resume(_: list[InterruptedThread], __: Config) -> None:
        call_order.append("resume")

    async def _knowledge(*_args: object, **_kwargs: object) -> None:
        call_order.append("knowledge")

    with (
        patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
        patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
        patch.object(orchestrator, "_cleanup_stale_streams_after_restart", side_effect=_cleanup),
        patch.object(orchestrator, "_auto_resume_after_restart", side_effect=_resume),
        patch.object(orchestrator, "_schedule_knowledge_refresh", side_effect=_knowledge),
        patch.object(orchestrator, "_sync_memory_auto_flush_worker", new=AsyncMock()),
        patch("mindroom.orchestrator.sync_forever_with_restart", new=AsyncMock()),
    ):
        await orchestrator.start()

    assert call_order == ["wait", "setup", "cleanup", "resume", "knowledge"]


def test_bot_module_does_not_import_stale_stream_cleanup() -> None:
    """bot.py must not import cleanup_stale_streaming_messages (ISSUE-024b).

    The per-bot cleanup was racing with the orchestrator-level cleanup:
    bot.start() cleaned stale messages first and discarded interrupted threads,
    so the orchestrator cleanup found nothing left and auto-resume never ran.
    Only the orchestrator should call cleanup to preserve interrupted_threads.
    """
    bot_source = Path(importlib.import_module("mindroom.bot").__file__).read_text()
    assert "cleanup_stale_streaming_messages" not in bot_source, (
        "bot.py must not import or call cleanup_stale_streaming_messages; "
        "the orchestrator handles this to preserve interrupted_threads for auto-resume"
    )
