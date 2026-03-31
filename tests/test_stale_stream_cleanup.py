"""Tests for stale streaming cleanup and restart auto-resume."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

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
from mindroom.tool_system.events import _TOOL_TRACE_KEY
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
) -> tuple[int, list[InterruptedThread]]:
    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
            new=AsyncMock(return_value=joined_rooms),
        ),
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
async def test_cleanup_keeps_latest_interrupted_thread_per_agent_and_thread(tmp_path: Path) -> None:
    """Cleanup should keep only the newest interrupted record for each agent in a thread."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Please continue",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
        ),
        _make_message_event(
            event_id="$older",
            body="First partial ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
        ),
        _make_message_event(
            event_id="$newer",
            body="Second partial ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
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
            target_event_id="$newer",
            partial_text="Second partial",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]


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
            "mindroom.matrix.stale_stream_cleanup.send_message",
            new=AsyncMock(side_effect=["$resume1", "$resume2"]),
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


@pytest.mark.asyncio
async def test_auto_resume_honors_max_resumes_cap(tmp_path: Path) -> None:
    """Auto-resume should stop after the configured resume cap."""
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
        for index in range(12)
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message",
            new=AsyncMock(return_value="$resume"),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            max_resumes=10,
        )

    assert resumed_count == 10
    assert mock_send.await_count == 10
    assert mock_sleep.await_count == 9


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
        "mindroom.matrix.stale_stream_cleanup.send_message",
        new=AsyncMock(return_value="$resume"),
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
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - 1_000,
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message",
            new=AsyncMock(return_value="$edit"),
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
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
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
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
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
            new_content={"body": "Needs cleanup ⋯", "msgtype": "m.text"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(
        return_value=_room_get_event_response(
            _make_message_event(
                event_id="$original",
                body="Needs cleanup ⋯",
                timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
                relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            ),
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
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
    client.room_get_event.assert_awaited_once_with(ROOM_ID, "$original")


@pytest.mark.asyncio
async def test_cleanup_follows_agent_reply_chain_outside_scanned_history(tmp_path: Path) -> None:
    """Cleanup should fetch the exact reply chain until it reaches the original human requester."""
    config = _make_config(tmp_path)
    other_agent_user_id = config.get_ids(runtime_paths_for(config))["other"].full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$original",
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$agent-a"),
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup ⋯", "msgtype": "m.text"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(
        side_effect=[
            _room_get_event_response(
                _make_message_event(
                    event_id="$original",
                    body="Needs cleanup ⋯",
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
                    relates_to=_thread_reply_relation("$thread-root", "$agent-a"),
                ),
            ),
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
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
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
        "$original",
        "$agent-a",
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
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$cleanup-edit"),
    ) as mock_edit:
        cleaned, _ = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    edit_content = mock_edit.await_args.args[3]
    assert edit_content[STREAM_STATUS_KEY] == "streaming"
    assert edit_content[_TOOL_TRACE_KEY] == {
        "version": 1,
        "events": [{"type": "tool_started", "tool_name": "shell"}],
    }


@pytest.mark.asyncio
async def test_cleanup_skips_restart_marked_streaming_message(tmp_path: Path) -> None:
    """Cleanup should not re-edit a message that already carries the restart interruption note."""
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
                "body": stale_stream_cleanup_module.build_restart_interrupted_body("Working ⋯"),
                "msgtype": "m.text",
                STREAM_STATUS_KEY: "streaming",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$cleanup-edit"),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_preserves_tool_trace_and_ai_run_metadata(tmp_path: Path) -> None:
    """Cleanup edits should preserve Cinny-facing run metadata in both edit payload layers."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Partial answer ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content={
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
                "io.mindroom.ai_run": {"version": 1, "run_id": "run-123"},
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned_count, interrupted_threads = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned_count == 1
    assert interrupted_threads == []
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
    expected_keys = {
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.compaction": {"version": 1, "compacted": False},
        "io.mindroom.thread_summary": {"version": 1, "summary": "Draft summary"},
    }
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="More streaming output ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            extra_content=expected_keys,
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned_count, interrupted_threads = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned_count == 1
    assert interrupted_threads == []
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
    latest_keys = {
        "io.mindroom.tool_trace": {"version": 2, "events": [{"tool": "shell"}]},
        "io.mindroom.ai_run": {"version": 1, "run_id": "run-new"},
        "io.mindroom.stream_status": "streaming",
    }
    edit = _make_message_event(
        event_id="$edit-1",
        body="* Updated partial",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        relates_to={"rel_type": "m.replace", "event_id": "$original"},
        new_content={"body": "Updated partial ⋯", "msgtype": "m.text", **latest_keys},
    )
    client.room_messages.return_value = _room_messages_response(original, edit)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned_count, interrupted_threads = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned_count == 1
    assert interrupted_threads == []
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    _assert_preserved_edit_payload(sent_content, latest_keys)
    assert sent_content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$original"}


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
        "mindroom.matrix.stale_stream_cleanup.send_message",
        new=AsyncMock(return_value="$resume"),
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
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
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
            "mindroom.matrix.stale_stream_cleanup.send_message",
            new=AsyncMock(side_effect=["$resume0", RuntimeError("deleted room"), "$resume2"]),
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
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$external-user-msg"),
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_get_event = AsyncMock(side_effect=RuntimeError("network timeout"))

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
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
            body="Needs cleanup ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
            relates_to=_thread_reply_relation("$thread-root", "$agent-hop-0"),
        ),
        _make_message_event(
            event_id="$latest-edit",
            body="* Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$original"},
            new_content={"body": "Needs cleanup ⋯", "msgtype": "m.text"},
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
            _room_get_event_response(
                _make_message_event(
                    event_id="$original",
                    body="Needs cleanup ⋯",
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 10_000),
                    relates_to=_thread_reply_relation("$thread-root", "$agent-hop-0"),
                ),
            ),
            *[_make_hop_response(i) for i in range(15)],
        ],
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message",
        new=AsyncMock(return_value="$edit"),
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
