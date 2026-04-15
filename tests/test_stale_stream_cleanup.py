"""Tests for stale streaming cleanup and restart auto-resume."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME, STREAM_STATUS_KEY
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedThread,
    auto_resume_interrupted_threads,
    cleanup_stale_streaming_messages,
)
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.streaming import build_restart_interrupted_body
from mindroom.tool_system.events import _TOOL_TRACE_KEY
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_event,
    delivered_matrix_side_effect,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BOT_USER_ID = "@mindroom_test_agent:example.com"
OTHER_BOT_USER_ID = "@mindroom_other:example.com"
ROOM_ID = "!room:example.com"
NOW_MS = 1_000_000
STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_RECENCY_GUARD_MS + 60_000
OLD_STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_LOOKBACK_MS + 60_000
AUTO_RESUME_MESSAGE = (
    "[System: Previous response was interrupted by service restart. Please continue where you left off.]"
)
USER_ID = "@user:example.com"
OTHER_USER_ID = "@other-user:example.com"


def _make_config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={
                "test_agent": {
                    "display_name": "Test Agent",
                    "rooms": [ROOM_ID],
                },
                "other": {
                    "display_name": "Other Agent",
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
    extra_content: dict[str, object] | None = None,
    new_content: dict[str, object] | None = None,
) -> nio.RoomMessageText:
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
    }
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    if extra_content is not None:
        content.update(extra_content)
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


def _room_get_event_response(event: object) -> nio.RoomGetEventResponse:
    response = MagicMock()
    response.__class__ = nio.RoomGetEventResponse
    response.event = event
    return response


def _thread_reply_relation(thread_id: str, reply_to_event_id: str) -> dict[str, object]:
    return {
        "rel_type": "m.thread",
        "event_id": thread_id,
        "m.in_reply_to": {"event_id": reply_to_event_id},
    }


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


def _assert_preserved_edit_payload(content: dict[str, object], expected_keys: dict[str, object]) -> None:
    """Assert io.mindroom.* keys are present in both edit payload layers."""
    new_content = cast("dict[str, object]", content["m.new_content"])
    for key, value in expected_keys.items():
        assert content[key] == value
        assert new_content[key] == value


@pytest.mark.asyncio
async def test_relations_api_filters_reactions_and_unions_history_ids(tmp_path: Path) -> None:
    """Cleanup should redact valid relation hits plus any history-scanned stop reactions."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={STREAM_STATUS_KEY: "streaming"},
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
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
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
            body="Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={STREAM_STATUS_KEY: "streaming"},
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
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
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
        new_content={"body": "New answer", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
    )
    client.room_messages.return_value = _room_messages_response(original, edit)
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
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
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_scans_until_history_end_for_deep_stale_messages(tmp_path: Path) -> None:
    """Cleanup should keep paginating until history ends, not stop after an arbitrary page cap."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    stale_message = _make_message_event(
        event_id="$page12-stale",
        body="Deep history partial",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        extra_content={STREAM_STATUS_KEY: "streaming"},
    )
    history_pages = [
        _room_messages_response(
            _make_message_event(
                event_id=f"$page{page_number}-filler",
                body="Ignore me",
                timestamp_ms=NOW_MS - page_number,
                sender="@user:example.com",
            ),
            end=f"page-{page_number + 1}",
        )
        for page_number in range(1, 12)
    ]
    client.room_messages = AsyncMock(
        side_effect=[*history_pages, _room_messages_response(stale_message)],
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == []
    assert client.room_messages.await_count == 12
    assert mock_edit.await_args.args[2] == "$page12-stale"


@pytest.mark.asyncio
async def test_cleanup_skips_messages_older_than_restart_window(tmp_path: Path) -> None:
    """Cleanup should not edit or resume very old interrupted replies from previous outages."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    old_thread_message = _make_message_event(
        event_id="$ancient-stale",
        body="Ancient partial",
        timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
        relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        extra_content={STREAM_STATUS_KEY: "streaming"},
    )
    client.room_messages.return_value = _room_messages_response(old_thread_message)
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID], now_ms=NOW_MS)

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
            body="First partial",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$newer",
            body="Second partial",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
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
            agent_name="test_agent",
            original_sender_id=None,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$newer",
            partial_text="Second partial",
            agent_name="test_agent",
            original_sender_id=None,
        ),
    ]


@pytest.mark.asyncio
async def test_cleanup_returns_interrupted_thread_for_transitive_plain_reply(tmp_path: Path) -> None:
    """Cleanup should keep interrupted-thread metadata for plain replies inside a transitive thread chain."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$thread-reply",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        ),
        _make_message_event(
            event_id="$plain-reply",
            body="Working ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"m.in_reply_to": {"event_id": "$thread-reply"}},
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    assert interrupted[0].thread_id == "$thread-root"
    assert interrupted[0].target_event_id == "$plain-reply"
    assert interrupted[0].agent_name == "test_agent"


@pytest.mark.asyncio
async def test_auto_resume_sends_correctly_threaded_messages(tmp_path: Path) -> None:
    """Auto-resume should send the requested system message into each interrupted thread."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$target-one",
            partial_text="One",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-two",
            target_event_id="$target-two",
            partial_text="Two",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message_result",
            new=AsyncMock(
                side_effect=[
                    delivered_matrix_event("$resume1"),
                    delivered_matrix_event("$resume2"),
                ],
            ),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert resumed_count == 2
    assert mock_send.await_count == 2
    first_content = mock_send.await_args_list[0].args[2]
    second_content = mock_send.await_args_list[1].args[2]
    assert first_content["body"] == f"@Test Agent {AUTO_RESUME_MESSAGE}"
    assert first_content["m.mentions"] == {
        "user_ids": [config.get_ids(runtime_paths_for(config))["test_agent"].full_id],
    }
    assert first_content["m.relates_to"]["rel_type"] == "m.thread"
    assert first_content["m.relates_to"]["event_id"] == "$thread-one"
    assert first_content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$target-one"}
    assert first_content[ORIGINAL_SENDER_KEY] == USER_ID
    assert second_content["body"] == f"@Test Agent {AUTO_RESUME_MESSAGE}"
    assert second_content["m.relates_to"]["event_id"] == "$thread-two"
    assert second_content[ORIGINAL_SENDER_KEY] == USER_ID
    mock_sleep.assert_awaited_once_with(2.0)


def test_select_threads_to_resume_returns_all_unique_threads_when_unlimited() -> None:
    """Selector should return every unique threaded interruption when uncapped."""
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$older-one",
            partial_text="Older one",
            agent_name="test_agent",
            timestamp_ms=100,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-two",
            target_event_id="$target-two",
            partial_text="Two",
            agent_name="test_agent",
            timestamp_ms=200,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$newer-one",
            partial_text="Newer one",
            agent_name="test_agent",
            timestamp_ms=300,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-three",
            target_event_id="$target-three",
            partial_text="Three",
            agent_name="test_agent",
            timestamp_ms=400,
        ),
    ]

    selected = stale_stream_cleanup_module._select_threads_to_resume(
        interrupted,
        max_resumes=None,
    )

    assert [thread.thread_id for thread in selected] == ["$thread-two", "$thread-one", "$thread-three"]
    assert [thread.target_event_id for thread in selected] == ["$target-two", "$newer-one", "$target-three"]


@pytest.mark.asyncio
async def test_auto_resume_skips_thread_id_none(tmp_path: Path) -> None:
    """Auto-resume should skip interrupted records that do not have a thread ID."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id=None,
            target_event_id="$non-threaded",
            partial_text="Unthreaded",
            agent_name="test_agent",
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$threaded",
            target_event_id="$target",
            partial_text="Threaded",
            agent_name="test_agent",
        ),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert resumed_count == 1
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[1] == ROOM_ID
    assert mock_send.await_args.args[2]["m.relates_to"]["event_id"] == "$threaded"


@pytest.mark.asyncio
async def test_auto_resume_records_outbound_message_when_send_succeeds(tmp_path: Path) -> None:
    """Auto-resume should write successful threaded sends through the conversation cache."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    conversation_cache = AsyncMock()
    conversation_cache.notify_outbound_message = Mock()
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$threaded",
            target_event_id="$target",
            partial_text="Threaded",
            agent_name="test_agent",
        ),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert resumed_count == 1
    conversation_cache.notify_outbound_message.assert_called_once()
    record_args = conversation_cache.notify_outbound_message.call_args.args
    assert record_args[:2] == (ROOM_ID, "$resume")
    assert record_args[2]["m.relates_to"]["event_id"] == "$threaded"


@pytest.mark.asyncio
async def test_edit_stale_message_records_outbound_edit_when_successful(tmp_path: Path) -> None:
    """Restart cleanup edits should write through the outbound edit event."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    conversation_cache = AsyncMock()
    conversation_cache.notify_outbound_message = Mock()

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.format_message_with_mentions",
            return_value={"body": "cleanup", "msgtype": "m.text"},
        ),
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
        ),
    ):
        edited = await stale_stream_cleanup_module._edit_stale_message(
            client,
            room_id=ROOM_ID,
            target_event_id="$target",
            new_text="cleanup",
            preserved_content=None,
            thread_id="$thread-root",
            latest_thread_event_id="$reply-latest",
            sender_domain="example.com",
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert edited is True
    conversation_cache.notify_outbound_message.assert_called_once()
    record_args = conversation_cache.notify_outbound_message.call_args.args
    assert record_args[:2] == (ROOM_ID, "$cleanup-edit")
    assert record_args[2]["m.relates_to"]["rel_type"] == "m.replace"
    assert record_args[2]["m.relates_to"]["event_id"] == "$target"


@pytest.mark.asyncio
async def test_cleanup_skips_recent_in_progress_message_on_startup(tmp_path: Path) -> None:
    """Startup cleanup should skip fresh in-progress messages to avoid cross-instance clobbering."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - 2_000,
        ),
        _make_message_event(
            event_id="$message",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - 1_000,
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ) as mock_edit,
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_returns_thread_requester_for_auto_resume(tmp_path: Path) -> None:
    """Cleanup should carry the exact replied-to requester into the auto-resume record."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$message",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$message",
            partial_text="Needs cleanup",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]


@pytest.mark.asyncio
async def test_cleanup_uses_exact_replied_to_requester_not_latest_thread_speaker(tmp_path: Path) -> None:
    """Cleanup should recover requester from the interrupted reply target, not later thread speakers."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$other-user-message",
            body="Later thread message",
            sender=OTHER_USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock()

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$original",
            partial_text="Needs cleanup",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    client.room_get_event.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_uses_latest_thread_event_for_threaded_edit_fallback(tmp_path: Path) -> None:
    """Cleanup edits should target the latest event in the thread for MSC3440 fallback semantics."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$later-user-message",
            body="Later thread message",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS - 1_000),
            relates_to=_thread_reply_relation("$thread-root", "$original"),
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.format_message_with_mentions",
            return_value={"body": "cleanup", "msgtype": "m.text"},
        ) as mock_format,
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    assert mock_format.call_args.kwargs["latest_thread_event_id"] == "$later-user-message"


@pytest.mark.asyncio
async def test_cleanup_uses_same_timestamp_descendant_for_threaded_edit_fallback(tmp_path: Path) -> None:
    """Cleanup should treat a same-timestamp descendant as later than its parent."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    same_timestamp = NOW_MS - (STALE_AGE_MS - 1_000)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$zzz_parent",
            body="Parent thread message",
            sender=USER_ID,
            timestamp_ms=same_timestamp,
            relates_to=_thread_reply_relation("$thread-root", "$original"),
        ),
        _make_message_event(
            event_id="$aaa_child",
            body="Child plain reply",
            sender=USER_ID,
            timestamp_ms=same_timestamp,
            relates_to={"m.in_reply_to": {"event_id": "$zzz_parent"}},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.format_message_with_mentions",
            return_value={"body": "cleanup", "msgtype": "m.text"},
        ) as mock_format,
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    assert mock_format.call_args.kwargs["latest_thread_event_id"] == "$aaa_child"


@pytest.mark.asyncio
async def test_cleanup_uses_bot_promoted_plain_reply_as_latest_thread_event(tmp_path: Path) -> None:
    """Cleanup should prefer the threaded scanned copy when the stale bot message is itself a promoted plain reply."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$zzz_parent",
            body="Explicit parent",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
        ),
        _make_message_event(
            event_id="$aaa_child",
            body="Interrupted bot reply",
            sender=BOT_USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS - 1_000),
            relates_to={"m.in_reply_to": {"event_id": "$zzz_parent"}},
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.format_message_with_mentions",
            return_value={"body": "cleanup", "msgtype": "m.text"},
        ) as mock_format,
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    assert interrupted[0].target_event_id == "$aaa_child"
    assert mock_format.call_args.kwargs["latest_thread_event_id"] == "$aaa_child"


@pytest.mark.asyncio
async def test_cleanup_uses_scanned_history_when_edited_bot_message_lacks_visible_reply_target(tmp_path: Path) -> None:
    """Edited bot messages should recover requester from scanned history before any API fetch."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(side_effect=RuntimeError("boom"))

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$original",
            partial_text="Needs cleanup",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    client.room_get_event.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_follows_agent_reply_chain_outside_scanned_history(tmp_path: Path) -> None:
    """Cleanup should fetch the exact reply chain until it reaches the original human requester."""
    config = _make_config(tmp_path)
    other_agent_user_id = config.get_ids(runtime_paths_for(config))["other"].full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$agent-a"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(
        side_effect=[
            _room_get_event_response(
                _make_message_event(
                    event_id="$agent-a",
                    body="Handing off",
                    sender=other_agent_user_id,
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
                    relates_to=_thread_reply_relation("$thread-root", "$user-root"),
                ),
            ),
            _room_get_event_response(
                _make_message_event(
                    event_id="$user-root",
                    body="Start here",
                    sender=USER_ID,
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 30_000),
                ),
            ),
        ],
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$original",
            partial_text="Needs cleanup",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    assert [call.args[1] for call in client.room_get_event.await_args_list] == [
        "$agent-a",
        "$user-root",
    ]


@pytest.mark.asyncio
async def test_cleanup_uses_visible_content_for_fetched_edit_events(tmp_path: Path) -> None:
    """Requester resolution should use canonical visible content for fetched edit events."""
    config = _make_config(tmp_path)
    other_agent_user_id = config.get_ids(runtime_paths_for(config))["other"].full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$agent-a-edit"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(
        return_value=_room_get_event_response(
            _make_message_event(
                event_id="$agent-a-edit",
                body="* Preview handoff",
                sender=other_agent_user_id,
                timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
                relates_to={"rel_type": "m.replace", "event_id": "$agent-a-original"},
                new_content={
                    "body": "Preview handoff",
                    "msgtype": "m.file",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/agent-a-edit-sidecar",
                },
            ),
        ),
    )
    client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "body": "* Handoff",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "Handoff",
                        "msgtype": "m.text",
                        ORIGINAL_SENDER_KEY: USER_ID,
                        "m.relates_to": _thread_reply_relation("$thread-root", "$user-root"),
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$agent-a-original"},
                },
            ).encode("utf-8"),
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$original",
            partial_text="Needs cleanup",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    client.download.assert_awaited()


@pytest.mark.asyncio
async def test_cleanup_fetches_exact_scanned_edit_ancestor_for_requester_resolution(tmp_path: Path) -> None:
    """Scanned edit ancestors should still fetch the exact event when the raw wrapper hides the reply edge."""
    config = _make_config(tmp_path)
    other_agent_user_id = config.get_ids(runtime_paths_for(config))["other"].full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$agent-a-edit"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$agent-a-edit",
            body="* Preview handoff",
            sender=other_agent_user_id,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
            relates_to={"rel_type": "m.replace", "event_id": "$agent-a-original"},
            new_content={
                "body": "Preview handoff",
                "msgtype": "m.text",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(
        side_effect=[
            _room_get_event_response(
                _make_message_event(
                    event_id="$agent-a-edit",
                    body="* Handoff",
                    sender=other_agent_user_id,
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
                    relates_to={"rel_type": "m.replace", "event_id": "$agent-a-original"},
                    new_content={
                        "body": "Handoff",
                        "msgtype": "m.text",
                        "m.relates_to": _thread_reply_relation("$thread-root", "$user-root"),
                    },
                ),
            ),
            _room_get_event_response(
                _make_message_event(
                    event_id="$user-root",
                    body="Start here",
                    sender=USER_ID,
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 30_000),
                ),
            ),
        ],
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$original",
            partial_text="Needs cleanup",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    assert [call.args[1] for call in client.room_get_event.await_args_list] == [
        "$agent-a-edit",
        "$user-root",
    ]


@pytest.mark.asyncio
async def test_cleanup_preserves_stream_status_and_tool_trace_metadata(tmp_path: Path) -> None:
    """Cleanup edits should preserve structured metadata needed by clients and continuation."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$original",
            body="Working ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Working",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={
                "body": "Working ⋯",
                "msgtype": "m.text",
                STREAM_STATUS_KEY: "streaming",
                _TOOL_TRACE_KEY: {"version": 1, "events": [{"type": "tool_started", "tool_name": "shell"}]},
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
    ) as mock_edit:
        cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    edit_content = mock_edit.await_args.args[3]
    assert edit_content[STREAM_STATUS_KEY] == "error"
    assert edit_content[_TOOL_TRACE_KEY] == {
        "version": 1,
        "events": [{"type": "tool_started", "tool_name": "shell"}],
    }


@pytest.mark.asyncio
async def test_cleanup_repairs_pending_stream_status_on_restart_note_messages(tmp_path: Path) -> None:
    """Restart-note messages should still get a metadata-only repair when status is pending."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    interrupted_body = build_restart_interrupted_body("Working ⋯")
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body=interrupted_body,
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={
                STREAM_STATUS_KEY: "pending",
                "io.mindroom.ai_run": {"version": 1, "run_id": "run-123"},
            },
        ),
        _make_reaction_event(
            event_id="$history-stop",
            target_event_id="$message",
            key="🛑",
            timestamp_ms=NOW_MS - 1_000,
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == []
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert sent_content[STREAM_STATUS_KEY] == "error"
    assert sent_content["io.mindroom.ai_run"] == {"version": 1, "run_id": "run-123"}
    assert cast("dict[str, object]", sent_content["m.new_content"])["body"] == interrupted_body
    client.room_redact.assert_awaited_once()
    assert client.room_redact.await_args.kwargs["event_id"] == "$history-stop"


@pytest.mark.asyncio
async def test_cleanup_preserves_tool_trace_and_ai_run_metadata(tmp_path: Path) -> None:
    """Cleanup edits should preserve Cinny-facing run metadata in both edit payload layers."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Partial answer",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
                "io.mindroom.ai_run": {"version": 1, "run_id": "run-123"},
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(
        sent_content,
        {
            "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            "io.mindroom.ai_run": {"version": 1, "run_id": "run-123"},
        },
    )


@pytest.mark.asyncio
async def test_cleanup_preserves_multiple_mindroom_metadata_keys(tmp_path: Path) -> None:
    """Cleanup edits should preserve every io.mindroom.* key, not just one special case."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    input_keys = {
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.compaction": {"version": 1, "compacted": False},
        "io.mindroom.thread_summary": {"version": 1, "summary": "Draft summary"},
    }
    expected_keys = {**input_keys, "io.mindroom.stream_status": "error"}
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="More streaming output ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content=input_keys,
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(sent_content, expected_keys)


@pytest.mark.asyncio
async def test_cleanup_prefers_latest_mindroom_metadata_from_edit_chain(tmp_path: Path) -> None:
    """Cleanup should use the canonical io.mindroom.* keys from the newest edit's m.new_content."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    original = _make_message_event(
        event_id="$original",
        body="Initial partial ⋯",
        timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
        extra_content={
            "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "search"}]},
            "io.mindroom.ai_run": {"version": 1, "run_id": "run-old"},
        },
    )
    input_latest_keys = {
        "io.mindroom.tool_trace": {"version": 2, "events": [{"tool": "shell"}]},
        "io.mindroom.ai_run": {"version": 1, "run_id": "run-new"},
        "io.mindroom.stream_status": "streaming",
    }
    expected_latest_keys = {**input_latest_keys, "io.mindroom.stream_status": "error"}
    edit = _make_message_event(
        event_id="$edit-1",
        body="* Updated partial",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
        new_content={"body": "Updated partial ⋯", "msgtype": "m.text", **input_latest_keys},
    )
    client.room_messages.return_value = _room_messages_response(original, edit)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(sent_content, expected_latest_keys)
    assert sent_content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$original"}


@pytest.mark.asyncio
async def test_cleanup_sets_terminal_stream_status(tmp_path: Path) -> None:
    """Cleanup must override io.mindroom.stream_status to error, even when it is missing."""
    config = _make_config(tmp_path)

    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$msg-streaming",
            body="Still typing ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={
                "io.mindroom.stream_status": "streaming",
                "io.mindroom.tool_trace": {"version": 1},
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$c1", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert sent["io.mindroom.stream_status"] == "error"
    assert sent["io.mindroom.tool_trace"] == {"version": 1}
    new_content = cast("dict[str, object]", sent["m.new_content"])
    assert new_content["io.mindroom.stream_status"] == "error"
    assert new_content["io.mindroom.tool_trace"] == {"version": 1}

    client2 = AsyncMock(spec=nio.AsyncClient)
    client2.rooms = {}
    client2.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$msg-pending",
            body="Still typing",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={STREAM_STATUS_KEY: "pending", "io.mindroom.tool_trace": {"version": 2}},
        ),
    )
    client2.room_get_event_relations = MagicMock(return_value=_aiter())
    client2.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$c2", room_id=ROOM_ID))

    cleaned2, _ = await _run_cleanup(client2, config, joined_rooms=[ROOM_ID])

    assert cleaned2 == 1
    sent2 = cast("dict[str, object]", client2.room_send.await_args.kwargs["content"])
    assert sent2["io.mindroom.stream_status"] == "error"
    assert sent2["io.mindroom.tool_trace"] == {"version": 2}
    new_content2 = cast("dict[str, object]", sent2["m.new_content"])
    assert new_content2["io.mindroom.stream_status"] == "error"
    assert new_content2["io.mindroom.tool_trace"] == {"version": 2}


@pytest.mark.asyncio
async def test_cleanup_preserves_tool_trace_from_v2_sidecar(tmp_path: Path) -> None:
    """Cleanup should hydrate a v2 sidecar and preserve metadata that only exists there."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}

    sidecar_tool_trace = {"version": 1, "events": [{"tool": "web_search"}]}
    sidecar_payload = {
        "msgtype": "m.text",
        "body": "A very long response with tool traces",
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.tool_trace": sidecar_tool_trace,
        "io.mindroom.ai_run": {"version": 1, "run_id": "run-sidecar"},
    }

    preview_event = _make_message_event(
        event_id="$message",
        body="Preview of long text",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        extra_content={
            STREAM_STATUS_KEY: "streaming",
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            "url": "mxc://example.com/sidecar123",
        },
    )
    client.room_messages.return_value = _room_messages_response(preview_event)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(sidecar_payload).encode("utf-8"),
        ),
    )
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(
        sent_content,
        {
            "io.mindroom.tool_trace": sidecar_tool_trace,
            "io.mindroom.ai_run": {"version": 1, "run_id": "run-sidecar"},
            "io.mindroom.stream_status": "error",
        },
    )


@pytest.mark.asyncio
async def test_cleanup_does_not_hydrate_sidecars_for_unrelated_user_messages(tmp_path: Path) -> None:
    """Cleanup should resolve visible message state only for the current bot's messages."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}

    user_sidecar_event = _make_message_event(
        event_id="$user-preview",
        body="User preview [Message continues in attached file]",
        timestamp_ms=NOW_MS - STALE_AGE_MS - 10,
        sender="@user:example.com",
        extra_content={
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            "url": "mxc://example.com/user-sidecar",
        },
    )
    stale_bot_message = _make_message_event(
        event_id="$bot-message",
        body="Bot partial",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        extra_content={STREAM_STATUS_KEY: "streaming"},
    )
    client.room_messages.return_value = _room_messages_response(user_sidecar_event, stale_bot_message)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.download = AsyncMock()

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == []
    client.download.assert_not_awaited()
    assert mock_edit.await_args.args[2] == "$bot-message"


@pytest.mark.asyncio
async def test_cleanup_sidecar_hydration_failure_falls_back_gracefully(tmp_path: Path) -> None:
    """Cleanup should still work when sidecar hydration fails."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}

    preview_event = _make_message_event(
        event_id="$message",
        body="Preview text",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        extra_content={
            STREAM_STATUS_KEY: "streaming",
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            "io.mindroom.ai_run": {"version": 1, "run_id": "run-preview"},
            "url": "mxc://example.com/broken",
        },
    )
    client.room_messages.return_value = _room_messages_response(preview_event)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.download = AsyncMock(return_value=MagicMock(spec=nio.DownloadError))
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(
        sent_content,
        {
            "io.mindroom.ai_run": {"version": 1, "run_id": "run-preview"},
            "io.mindroom.stream_status": "error",
        },
    )
    new_content = cast("dict[str, object]", sent_content["m.new_content"])
    assert "io.mindroom.long_text" not in sent_content
    assert "io.mindroom.long_text" not in new_content
    assert "url" not in sent_content
    assert "url" not in new_content
    assert "io.mindroom.tool_trace" not in new_content


@pytest.mark.asyncio
async def test_cleanup_preserves_sidecar_tool_trace_from_edit_chain(tmp_path: Path) -> None:
    """For edit-based sidecars, tool_trace should come from the latest edit sidecar."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}

    sidecar_tool_trace = {"version": 1, "events": [{"tool": "shell"}, {"tool": "file"}]}
    sidecar_inner = {
        "msgtype": "m.text",
        "body": "Full response text with streaming indicator",
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.tool_trace": sidecar_tool_trace,
    }
    sidecar_payload = {
        "msgtype": "m.text",
        "body": "* Full response text with streaming indicator",
        "m.new_content": sidecar_inner,
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
    }

    original = _make_message_event(
        event_id="$original",
        body="Initial short text",
        timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
    )
    edit = _make_message_event(
        event_id="$latest-edit",
        body="* Preview of long edit",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
        new_content={
            "body": "Preview of long edit",
            "msgtype": "m.file",
            "url": "mxc://example.com/edit-sidecar",
            STREAM_STATUS_KEY: "streaming",
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
        },
    )
    client.room_messages.return_value = _room_messages_response(original, edit)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(sidecar_payload).encode("utf-8"),
        ),
    )
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(
        sent_content,
        {
            "io.mindroom.tool_trace": sidecar_tool_trace,
            "io.mindroom.stream_status": "error",
        },
    )


@pytest.mark.asyncio
async def test_auto_resume_dedupes_same_agent_and_thread_using_newest_target(tmp_path: Path) -> None:
    """Auto-resume should emit one relay per agent/thread pair, targeting the newest interruption."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$older",
            partial_text="Older",
            agent_name="test_agent",
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$newer",
            partial_text="Newer",
            agent_name="test_agent",
        ),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert resumed_count == 1
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[2]["m.relates_to"]["m.in_reply_to"] == {"event_id": "$newer"}


@pytest.mark.asyncio
async def test_auto_resume_honors_cap_after_replacing_older_duplicate_targets(tmp_path: Path) -> None:
    """Auto-resume should keep the newest unique interrupted threads under the cap."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$older-one",
            partial_text="Older one",
            agent_name="test_agent",
            timestamp_ms=100,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-two",
            target_event_id="$thread-two-target",
            partial_text="Two",
            agent_name="test_agent",
            timestamp_ms=200,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-three",
            target_event_id="$thread-three-target",
            partial_text="Three",
            agent_name="test_agent",
            timestamp_ms=300,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$newer-one",
            partial_text="Newer one",
            agent_name="test_agent",
            timestamp_ms=400,
        ),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            max_resumes=2,
        )

    assert resumed_count == 2
    assert mock_send.await_count == 2
    first_content = mock_send.await_args_list[0].args[2]
    second_content = mock_send.await_args_list[1].args[2]
    assert first_content["m.relates_to"]["event_id"] == "$thread-three"
    assert first_content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$thread-three-target"}
    assert second_content["m.relates_to"]["event_id"] == "$thread-one"
    assert second_content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$newer-one"}


@pytest.mark.asyncio
async def test_auto_resume_cap_uses_timestamps_not_room_iteration_order(tmp_path: Path) -> None:
    """Auto-resume should prefer genuinely newer interruptions even if older rooms were appended later."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id="!room-a:example.com",
            thread_id="$thread-new",
            target_event_id="$target-new",
            partial_text="New",
            agent_name="test_agent",
            timestamp_ms=500,
        ),
        InterruptedThread(
            room_id="!room-b:example.com",
            thread_id="$thread-old",
            target_event_id="$target-old",
            partial_text="Old",
            agent_name="test_agent",
            timestamp_ms=100,
        ),
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            max_resumes=1,
        )

    assert resumed_count == 1
    mock_send.assert_awaited_once()
    sent_content = mock_send.await_args.args[2]
    assert sent_content["m.relates_to"]["event_id"] == "$thread-new"
    assert sent_content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$target-new"}


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
                agent_name="test_agent",
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


@pytest.mark.asyncio
async def test_orchestrator_auto_resume_uses_router_client(tmp_path: Path) -> None:
    """Auto-resume should post visible relays from the router, not the internal user."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config

    router_client = AsyncMock(spec=nio.AsyncClient)
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.agent_user = MagicMock(user_id="@mindroom_router:example.com")
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}

    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$target",
            partial_text="Half finished",
            agent_name="test_agent",
        ),
    ]

    with patch(
        "mindroom.orchestrator.auto_resume_interrupted_threads",
        new=AsyncMock(return_value=1),
    ) as mock_auto_resume:
        await orchestrator._auto_resume_after_restart(interrupted, config)

    mock_auto_resume.assert_awaited_once()
    assert mock_auto_resume.await_args.args[0] is router_client
    assert mock_auto_resume.await_args.args[1] == interrupted
    assert mock_auto_resume.await_args.kwargs["config"] == config
    assert mock_auto_resume.await_args.kwargs["runtime_paths"] == runtime_paths_for(config)


@pytest.mark.asyncio
async def test_restart_marked_message_still_redacts_stale_stop_reactions(tmp_path: Path) -> None:
    """Stop reactions on restart-noted messages should still be redacted during cleanup."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    restart_body = stale_stream_cleanup_module.build_restart_interrupted_body("Partial answer ⋯")
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body=restart_body,
            timestamp_ms=NOW_MS - STALE_AGE_MS,
        ),
        _make_reaction_event(
            event_id="$stop-reaction",
            target_event_id="$message",
            key="🛑",
            timestamp_ms=NOW_MS - STALE_AGE_MS + 100,
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    mock_edit.assert_not_awaited()
    client.room_redact.assert_awaited_once()
    assert client.room_redact.await_args.kwargs["event_id"] == "$stop-reaction"


@pytest.mark.asyncio
async def test_auto_resume_continues_after_send_exception(tmp_path: Path) -> None:
    """A send_message exception on one thread should not abort the remaining resumes."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id=f"$thread-{index}",
            target_event_id=f"$target-{index}",
            partial_text=f"Part {index}",
            agent_name="test_agent",
        )
        for index in range(3)
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message_result",
            new=AsyncMock(
                side_effect=[
                    delivered_matrix_event("$resume0"),
                    RuntimeError("deleted room"),
                    delivered_matrix_event("$resume2"),
                ],
            ),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()),
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert resumed_count == 2
    assert mock_send.await_count == 3


@pytest.mark.asyncio
async def test_requester_resolution_exception_degrades_gracefully(tmp_path: Path) -> None:
    """A room_get_event exception during requester resolution should not skip room cleanup."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    # Bot message replies to $external-user-msg which is NOT in scanned history,
    # forcing a room_get_event fetch that will raise.
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$external-user-msg"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(side_effect=RuntimeError("network timeout"))

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    assert interrupted[0].original_sender_id is None


@pytest.mark.asyncio
async def test_requester_resolution_respects_max_depth(tmp_path: Path) -> None:
    """Requester resolution should stop after max_depth to prevent unbounded API calls."""
    config = _make_config(tmp_path)
    other_agent_user_id = config.get_ids(runtime_paths_for(config))["other"].full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$original",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$agent-hop-0"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    # Build a chain of 15 agent hops — deeper than _MAX_REQUESTER_RESOLUTION_DEPTH (10)
    def _make_hop_response(hop_index: int) -> nio.RoomGetEventResponse:
        next_hop = f"$agent-hop-{hop_index + 1}" if hop_index < 14 else "$user-root"
        return _room_get_event_response(
            _make_message_event(
                event_id=f"$agent-hop-{hop_index}",
                body=f"Relay {hop_index}",
                sender=other_agent_user_id,
                timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000 + hop_index * 1000),
                relates_to=_thread_reply_relation("$thread-root", next_hop),
            ),
        )

    client.room_get_event = AsyncMock(
        side_effect=[
            *[_make_hop_response(i) for i in range(15)],
        ],
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    # Should have stopped before reaching $user-root due to depth limit
    assert len(interrupted) == 1
    assert interrupted[0].original_sender_id is None
    # Verify we didn't make 15+ API calls — depth limit should cap it
    assert client.room_get_event.await_count <= 13


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
