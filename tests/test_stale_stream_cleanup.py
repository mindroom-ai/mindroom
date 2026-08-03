"""Tests for stale streaming cleanup and restart auto-resume."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import nio
import pytest

from mindroom import runtime_generation_lease as runtime_generation_lease_module
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_KEY,
)
from mindroom.dispatch_source import AUTO_RESUME_MESSAGE, TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.entity_resolution import MissingManagedEntityAccountError, entity_identity_registry
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.cache import ThreadHistoryResult, thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.client_thread_history import OpaqueEncryptedThreadHistoryError
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.stale_stream_cleanup import (
    StaleStreamCleanupActor,
    recover_stale_streaming_messages,
)
from mindroom.matrix.stale_stream_cleanup import (
    _auto_resume_interrupted_threads as auto_resume_interrupted_threads,
)
from mindroom.matrix.stale_stream_cleanup import (
    _cleanup_stale_streaming_room as cleanup_stale_streaming_room,
)
from mindroom.matrix.stale_stream_cleanup import (
    _InterruptedThread as InterruptedThread,
)
from mindroom.matrix.stale_stream_cleanup import (
    _StaleRoomCleanupResult as StaleRoomCleanupResult,
)
from mindroom.matrix.stale_stream_cleanup import (
    _StaleStreamRecoveryResult as StaleStreamRecoveryResult,
)
from mindroom.matrix.state import MatrixState
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.runtime_generation_lease import runtime_generation_owner_stopped
from mindroom.streaming import build_cancelled_response_update, build_restart_interrupted_body
from mindroom.tool_system.events import _TOOL_TRACE_KEY
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_event,
    delivered_matrix_side_effect,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import TextIO

BOT_USER_ID = "@actual_test_agent:localhost"
OTHER_BOT_USER_ID = "@actual_other:localhost"
ROOM_ID = "!room:example.com"
RUNTIME_GENERATION = "runtime-generation"
NOW_MS = 1_000_000
STALE_AGE_MS = 70_000
OLD_STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_LOOKBACK_MS + 60_000
USER_ID = "@user:example.com"
OTHER_USER_ID = "@other-user:example.com"


def _make_config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
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
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "test_agent": "actual_test_agent", "other": "actual_other"},
    )
    return config


def _room_cleanup_result(
    cleaned_count: int,
    interrupted_threads: list[InterruptedThread],
    *,
    history_complete: bool = True,
) -> StaleRoomCleanupResult:
    return StaleRoomCleanupResult(
        cleaned_count=cleaned_count,
        interrupted_threads=interrupted_threads,
        history_complete=history_complete,
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


def _make_notice_event(
    *,
    event_id: str,
    body: str,
    timestamp_ms: int,
    sender: str = BOT_USER_ID,
    room_id: str = ROOM_ID,
    relates_to: dict[str, object] | None = None,
    extra_content: dict[str, object] | None = None,
    new_content: dict[str, object] | None = None,
) -> nio.RoomMessageNotice:
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.notice",
    }
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    if extra_content is not None:
        content.update(extra_content)
    if new_content is not None:
        content["m.new_content"] = new_content

    event = nio.RoomMessageNotice.from_dict(
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
    return cast("nio.RoomMessageNotice", event)


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


def _joined_room_cache(room_id: str = ROOM_ID, *, own_user_id: str = BOT_USER_ID) -> dict[str, nio.MatrixRoom]:
    room = nio.MatrixRoom(room_id, own_user_id)
    return {room_id: room}


def _make_client() -> AsyncMock:
    """Return one AsyncClient-shaped cleanup test client with the bot user ID."""
    return make_matrix_client_mock(user_id=BOT_USER_ID)


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
    terminal_interrupted_only: bool = False,
    runtime_generation: str = RUNTIME_GENERATION,
    unsettled_turn_source_event_ids: frozenset[str] = frozenset(),
) -> tuple[int, list[InterruptedThread]]:
    client.user_id = BOT_USER_ID
    assert joined_rooms == [ROOM_ID]
    with patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=now_ms / 1000):
        result = await cleanup_stale_streaming_room(
            client,
            room_id=ROOM_ID,
            actors={
                BOT_USER_ID: StaleStreamCleanupActor(
                    client,
                    None,
                    runtime_generation=runtime_generation,
                    unsettled_turn_source_event_ids=unsettled_turn_source_event_ids,
                ),
            },
            bot_user_ids={BOT_USER_ID} if bot_user_ids is None else bot_user_ids,
            config=config,
            runtime_paths=runtime_paths_for(config),
            terminal_interrupted_only=terminal_interrupted_only,
        )
    return result.cleaned_count, result.interrupted_threads


def _history_message(
    event_id: str,
    *,
    sender: str = BOT_USER_ID,
    timestamp: int = 0,
    content: dict[str, object] | None = None,
    body: str | None = None,
) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=event_id if body is None else body,
        event_id=event_id,
        timestamp=timestamp,
        content=content,
    )


def _authoritative_history(*messages: ResolvedVisibleMessage) -> ThreadHistoryResult:
    return thread_history_result(
        list(messages),
        is_full_history=True,
        diagnostics={"thread_read_source": "homeserver"},
    )


def _auto_resume_conversation_cache(interrupted: list[InterruptedThread]) -> AsyncMock:
    conversation_cache = AsyncMock()

    def history_for_thread(room_id: str, thread_id: str, **_: object) -> ThreadHistoryResult:
        return _authoritative_history(
            *[
                _history_message(item.target_event_id, timestamp=item.timestamp_ms)
                for item in interrupted
                if (item.room_id, item.thread_id) == (room_id, thread_id)
            ],
        )

    conversation_cache.refresh_startup_thread_history_from_source = AsyncMock(
        side_effect=history_for_thread,
    )
    conversation_cache.notify_outbound_message = Mock()
    return conversation_cache


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
    client = _make_client()
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
async def test_recent_edit_keeps_old_stream_within_cleanup_window(tmp_path: Path) -> None:
    """Cleanup should age an edited stream from its latest edit, not its original message."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    original = _make_message_event(
        event_id="$old-original",
        body="Initial answer",
        timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
    )
    recent_edit = _make_message_event(
        event_id="$recent-edit",
        body="* Still working",
        timestamp_ms=NOW_MS - STALE_AGE_MS,
        relates_to={"rel_type": "m.replace", "event_id": "$old-original"},
        new_content={"body": "Still working", "msgtype": "m.text", STREAM_STATUS_KEY: "streaming"},
    )
    client.room_messages.return_value = _room_messages_response(original, recent_edit)
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$cleanup-edit")),
    ) as mock_edit:
        cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == []
    assert mock_edit.await_args.args[2] == "$old-original"


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
    client = _make_client()
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
            conversation_cache=_auto_resume_conversation_cache(interrupted),
        )

    assert resumed_count == 2
    assert mock_send.await_count == 2
    first_content = mock_send.await_args_list[0].args[2]
    second_content = mock_send.await_args_list[1].args[2]
    assert first_content["body"] == f"@Test Agent {AUTO_RESUME_MESSAGE}"
    assert first_content["m.mentions"] == {
        "user_ids": [entity_ids(config, runtime_paths_for(config))["test_agent"].full_id],
    }
    assert first_content["m.relates_to"]["rel_type"] == "m.thread"
    assert first_content["m.relates_to"]["event_id"] == "$thread-one"
    assert first_content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$target-one"}
    assert first_content[ORIGINAL_SENDER_KEY] == USER_ID
    assert first_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert second_content["body"] == f"@Test Agent {AUTO_RESUME_MESSAGE}"
    assert second_content["m.relates_to"]["event_id"] == "$thread-two"
    assert second_content[ORIGINAL_SENDER_KEY] == USER_ID
    assert second_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    mock_sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_auto_resume_skips_interruption_without_resolved_requester(tmp_path: Path) -> None:
    """Auto-resume should fail closed when restart recovery cannot resolve requester identity."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(ROOM_ID, "$thread", "$target", "partial", "test_agent"),
    ]

    with patch("mindroom.matrix.stale_stream_cleanup.send_message_result", new=AsyncMock()) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=_auto_resume_conversation_cache(interrupted),
        )

    assert resumed_count == 0
    mock_send.assert_not_awaited()


@pytest.mark.parametrize(
    ("newer_sender", "newer_content", "expected_resumes"),
    [
        (USER_ID, None, 0),
        (
            OTHER_BOT_USER_ID,
            {
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                ORIGINAL_SENDER_KEY: USER_ID,
            },
            0,
        ),
        (OTHER_BOT_USER_ID, None, 1),
    ],
)
@pytest.mark.asyncio
async def test_auto_resume_classifies_later_activity_by_effective_sender_and_history_order(
    tmp_path: Path,
    newer_sender: str,
    newer_content: dict[str, object] | None,
    expected_resumes: int,
) -> None:
    """Later direct or relayed humans suppress resume; internal bot events do not."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            ROOM_ID,
            "$thread",
            "$target",
            "partial",
            "test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=100,
        ),
    ]
    conversation_cache = _auto_resume_conversation_cache(interrupted)
    conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
    conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
        _history_message("$target", timestamp=100),
        _history_message("$newer", sender=newer_sender, timestamp=100, content=newer_content),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert resumed_count == expected_resumes
    assert mock_send.await_count == expected_resumes


@pytest.mark.asyncio
async def test_prior_auto_resume_relay_does_not_suppress_sibling_resume(tmp_path: Path) -> None:
    """A synthetic resume relay should not masquerade as newer human work."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            ROOM_ID,
            "$thread",
            "$target",
            "partial",
            "test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    conversation_cache = _auto_resume_conversation_cache(interrupted)
    conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
    conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
        _history_message("$target"),
        _history_message(
            "$prior-resume",
            sender=OTHER_BOT_USER_ID,
            body=AUTO_RESUME_MESSAGE,
            content={
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                ORIGINAL_SENDER_KEY: USER_ID,
            },
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert resumed_count == 1
    mock_send.assert_awaited_once()


@pytest.mark.parametrize(
    "history_case",
    ["missing", "failed", "incomplete", "degraded", "opaque", "missing_target", "untrusted_sender"],
)
@pytest.mark.asyncio
async def test_auto_resume_fails_closed_without_authoritative_target_history(
    tmp_path: Path,
    history_case: str,
) -> None:
    """Unusable history or untrusted sender classification should suppress auto-resume."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            ROOM_ID,
            "$thread",
            "$target",
            "partial",
            "test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    conversation_cache = None if history_case == "missing" else _auto_resume_conversation_cache(interrupted)
    if conversation_cache is not None and history_case == "failed":
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = RuntimeError("history failed")
    elif conversation_cache is not None and history_case == "incomplete":
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
        conversation_cache.refresh_startup_thread_history_from_source.return_value = thread_history_result(
            [_history_message("$target")],
            is_full_history=False,
            diagnostics={"thread_read_source": "homeserver"},
        )
    elif conversation_cache is not None and history_case == "degraded":
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
        conversation_cache.refresh_startup_thread_history_from_source.return_value = thread_history_result(
            [_history_message("$target")],
            is_full_history=True,
            diagnostics={"thread_read_source": "homeserver", "thread_read_degraded": True},
        )
    elif conversation_cache is not None and history_case == "opaque":
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = OpaqueEncryptedThreadHistoryError(
            "opaque history",
        )
    elif conversation_cache is not None and history_case == "missing_target":
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
        conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
            _history_message("$other"),
        )
    elif conversation_cache is not None and history_case == "untrusted_sender":
        state = MatrixState.load(runtime_paths_for(config))
        state.accounts.pop(managed_account_key("other"))
        state.save(runtime_paths_for(config))
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
        conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
            _history_message("$target"),
            _history_message("$later", sender=OTHER_BOT_USER_ID),
        )

    with patch("mindroom.matrix.stale_stream_cleanup.send_message_result", new=AsyncMock()) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert resumed_count == 0
    mock_send.assert_not_awaited()
    if conversation_cache is not None:
        conversation_cache.refresh_startup_thread_history_from_source.assert_awaited_once_with(
            ROOM_ID,
            "$thread",
            caller_label="startup_auto_resume_freshness",
        )


@pytest.mark.asyncio
async def test_auto_resume_propagates_cancelled_source_refresh(tmp_path: Path) -> None:
    """Cancellation should abort recovery without sending an unchecked resume."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            ROOM_ID,
            "$thread",
            "$target",
            "partial",
            "test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    conversation_cache = _auto_resume_conversation_cache(interrupted)
    conversation_cache.refresh_startup_thread_history_from_source.side_effect = asyncio.CancelledError

    with (
        patch("mindroom.matrix.stale_stream_cleanup.send_message_result", new=AsyncMock()) as mock_send,
        pytest.raises(asyncio.CancelledError),
    ):
        await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    mock_send.assert_not_awaited()
    conversation_cache.refresh_startup_thread_history_from_source.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_resume_source_refresh_sees_newer_human_missing_from_startup_cache(tmp_path: Path) -> None:
    """Startup recovery should not resume when Matrix has newer human work absent from cache."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            ROOM_ID,
            "$thread",
            "$target",
            "partial",
            "test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    conversation_cache = _auto_resume_conversation_cache(interrupted)
    conversation_cache.refresh_startup_thread_history_from_source.side_effect = None
    conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
        _history_message("$target"),
        _history_message("$newer-human", sender=USER_ID),
    )

    with patch("mindroom.matrix.stale_stream_cleanup.send_message_result", new=AsyncMock()) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert resumed_count == 0
    conversation_cache.refresh_startup_thread_history_from_source.assert_awaited_once_with(
        ROOM_ID,
        "$thread",
        caller_label="startup_auto_resume_freshness",
    )
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_resume_checks_freshness_after_delay_before_each_delivery(tmp_path: Path) -> None:
    """Activity becoming visible during a rate-limit delay should suppress resume."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            ROOM_ID,
            f"$thread-{index}",
            f"$target-{index}",
            "partial",
            "test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=index,
        )
        for index in range(5)
    ]
    conversation_cache = _auto_resume_conversation_cache(interrupted)
    history_calls: dict[str, int] = {}
    activity_arrived_during_delay = asyncio.Event()
    delay_count = 0

    async def sleep_with_activity(_: float) -> None:
        nonlocal delay_count
        delay_count += 1
        if delay_count == 2:
            activity_arrived_during_delay.set()

    def history_for_call(_: str, thread_id: str, **__: object) -> ThreadHistoryResult:
        history_calls[thread_id] = history_calls.get(thread_id, 0) + 1
        index = thread_id.removeprefix("$thread-")
        messages = [_history_message(f"$target-{index}")]
        if index == "4" or (index == "2" and activity_arrived_during_delay.is_set()):
            messages.append(_history_message("$human-later", sender=USER_ID))
        return _authoritative_history(*messages)

    conversation_cache.refresh_startup_thread_history_from_source.side_effect = history_for_call

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ) as mock_send,
        patch(
            "mindroom.matrix.stale_stream_cleanup.asyncio.sleep",
            new=AsyncMock(side_effect=sleep_with_activity),
        ) as mock_sleep,
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )

    assert resumed_count == 3
    assert [call.args[2]["m.relates_to"]["event_id"] for call in mock_send.await_args_list] == [
        "$thread-0",
        "$thread-1",
        "$thread-3",
    ]
    assert history_calls == {
        "$thread-0": 1,
        "$thread-1": 1,
        "$thread-2": 1,
        "$thread-3": 1,
        "$thread-4": 1,
    }
    assert mock_sleep.await_args_list == [call(2.0), call(2.0), call(2.0)]


@pytest.mark.asyncio
async def test_auto_resume_target_mention_ignores_unprepared_unrelated_entity(tmp_path: Path) -> None:
    """Auto-resume should mention the target without resolving every configured entity."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    config.agents["stale"] = config.agents["other"].model_copy(update={"display_name": "Stale Agent"})
    state = MatrixState.load(runtime_paths)
    state.accounts.pop(managed_account_key("stale"), None)
    state.save(runtime_paths)
    with pytest.raises(MissingManagedEntityAccountError, match="stale"):
        entity_identity_registry(config, runtime_paths)
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
    ]

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(return_value=delivered_matrix_event("$resume1")),
    ) as mock_send:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=_auto_resume_conversation_cache(interrupted),
        )

    assert resumed_count == 1
    content = mock_send.await_args.args[2]
    assert content["body"] == f"@Test Agent {AUTO_RESUME_MESSAGE}"
    assert content["m.mentions"] == {"user_ids": [BOT_USER_ID]}


def test_ordered_auto_resume_candidates_returns_all_unique_threads_when_unlimited() -> None:
    """Candidate ordering should return every unique threaded interruption when uncapped."""
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

    selected = stale_stream_cleanup_module._ordered_auto_resume_candidates(interrupted)

    assert [thread.thread_id for thread in selected] == ["$thread-two", "$thread-one", "$thread-three"]
    assert [thread.target_event_id for thread in selected] == ["$target-two", "$newer-one", "$target-three"]


def test_deep_history_scan_limit_is_independent_of_resume_count(tmp_path: Path) -> None:
    """Uncapped resume delivery must not make each room scan more old pages."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True

    scan_policy = stale_stream_cleanup_module._cleanup_scan_policy(config)

    assert scan_policy.max_extra_old_pages == 10


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
            original_sender_id=USER_ID,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$threaded",
            target_event_id="$target",
            partial_text="Threaded",
            agent_name="test_agent",
            original_sender_id=USER_ID,
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
            conversation_cache=_auto_resume_conversation_cache(interrupted),
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
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$threaded",
            target_event_id="$target",
            partial_text="Threaded",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]
    conversation_cache = _auto_resume_conversation_cache(interrupted)

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
async def test_cleanup_does_not_auto_resume_a_durably_owned_turn(tmp_path: Path) -> None:
    """Exact callback replay owns recovery and must not race a second relay turn."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$source",
            body="Start here",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$message",
            body="Needs cleanup",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$source", "$source"),
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ):
        cleaned, interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            unsettled_turn_source_event_ids=frozenset({"$source"}),
        )

    assert cleaned == 1
    assert interrupted == []


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
    other_agent_user_id = entity_ids(config, runtime_paths_for(config))["other"].full_id
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
    other_agent_user_id = entity_ids(config, runtime_paths_for(config))["other"].full_id
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
                        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
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
    other_agent_user_id = entity_ids(config, runtime_paths_for(config))["other"].full_id
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
    client = _make_client()
    client.rooms = _joined_room_cache()
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
async def test_cleanup_repairs_threaded_pending_restart_note_without_auto_resume(tmp_path: Path) -> None:
    """Pending restart-note messages should keep repair-only behavior even when threaded."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    interrupted_body = build_restart_interrupted_body("Working ⋯")
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$message",
            body=interrupted_body,
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "pending"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert interrupted == []
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert cast("dict[str, object]", sent_content["m.new_content"])["body"] == interrupted_body


@pytest.mark.asyncio
async def test_cleanup_returns_restart_marked_terminal_thread_for_auto_resume(tmp_path: Path) -> None:
    """Terminal restart-interrupted messages should still seed auto-resume after graceful shutdown."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    restart_body = build_restart_interrupted_body("Partial answer")
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$message",
            body=restart_body,
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "error"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert len(interrupted) == 1
    assert interrupted[0].timestamp_ms == NOW_MS - STALE_AGE_MS
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$message",
            partial_text="Partial answer",
            agent_name="test_agent",
            original_sender_id=USER_ID,
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_human_activity", [False, True])
async def test_recent_mid_tool_shutdown_marker_resumes_only_without_newer_human_activity(
    tmp_path: Path,
    *,
    newer_human_activity: bool,
) -> None:
    """A clock-skewed mid-tool marker should be collected while human activity still gates its relay."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = _make_client()
    client.rooms = _joined_room_cache()
    interrupted_body, stream_status = build_cancelled_response_update(
        "Checking disk usage",
        cancel_source="sync_restart",
    )
    target_timestamp_ms = NOW_MS + 60_000
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Find the largest directory",
            sender=USER_ID,
            timestamp_ms=target_timestamp_ms - 1_000,
        ),
        _make_message_event(
            event_id="$mid-tool-response",
            body=interrupted_body,
            timestamp_ms=target_timestamp_ms,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: stream_status,
                _TOOL_TRACE_KEY: {
                    "version": 1,
                    "events": [{"type": "tool_call_started", "tool_name": "shell"}],
                },
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
    )

    assert cleaned == 0
    assert [candidate.target_event_id for candidate in interrupted] == ["$mid-tool-response"]
    history_messages = [
        _history_message("$mid-tool-response", timestamp=target_timestamp_ms),
    ]
    if newer_human_activity:
        history_messages.append(
            _history_message(
                "$newer-human-message",
                sender=USER_ID,
                timestamp=target_timestamp_ms + 1,
            ),
        )
    conversation_cache = AsyncMock()
    conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
        *history_messages,
    )
    conversation_cache.notify_outbound_message = Mock()

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$auto-resume")),
    ) as send_resume:
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
            delay=0,
        )

    assert resumed_count == (0 if newer_human_activity else 1)
    assert send_resume.await_count == resumed_count


@pytest.mark.asyncio
async def test_targeted_recovery_scans_only_handoff_rooms_without_a_clock_cutoff(tmp_path: Path) -> None:
    """Replacement recovery must exclude unrelated rooms and preserve the Matrix clock domain."""
    config = _make_config(tmp_path)
    client = make_matrix_client_mock(user_id=BOT_USER_ID)
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
    }

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
            new=AsyncMock(return_value=[ROOM_ID, "!unrelated:example.org"]),
        ),
        patch(
            "mindroom.matrix.stale_stream_cleanup._cleanup_stale_streaming_room",
            new=AsyncMock(return_value=_room_cleanup_result(0, [])),
        ) as cleanup_room,
    ):
        scanned_room_ids: set[str] = set()
        result = await recover_stale_streaming_messages(
            actors,
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths_for(config),
            scanned_room_ids=scanned_room_ids,
            target_room_ids={ROOM_ID},
        )

    assert result == StaleStreamRecoveryResult(room_count=1, cleaned_count=0, resumed_count=0)
    cleanup_room.assert_awaited_once()
    assert cleanup_room.await_args.kwargs["room_id"] == ROOM_ID
    assert cleanup_room.await_args.kwargs["terminal_interrupted_only"] is True
    assert scanned_room_ids == {ROOM_ID}


@pytest.mark.asyncio
async def test_targeted_recovery_does_not_clobber_live_replacement_stream(tmp_path: Path) -> None:
    """Replacement handoff recovery must ignore active output from the new bot generation."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$live-placeholder",
            body="Working ⋯",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "pending"},
        ),
    )

    cleaned, interrupted = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
        terminal_interrupted_only=True,
    )

    assert cleaned == 0
    assert interrupted == []
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_stamp_protects_stream_that_finalizes_after_snapshot(tmp_path: Path) -> None:
    """The generation stamp closes the finalize-after-snapshot TOCTOU.

    The scan snapshots a nonterminal state, then the live response finalizes
    before candidate processing. The generation stamp read from the same
    snapshot still proves current-generation ownership, so the stream is
    protected from cleanup.
    """
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    generation = "gen-current"
    skewed_ts = NOW_MS - (STALE_AGE_MS + 5_000)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$finalizing",
            body="Almost done ⋯",
            timestamp_ms=skewed_ts,
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: generation,
            },
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as edit_result:
        cleaned, interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            runtime_generation=generation,
        )

    assert cleaned == 0
    assert interrupted == []
    edit_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_generation_stamp_without_stopped_owner_proof_is_not_cleaned(tmp_path: Path) -> None:
    """A concurrent foreign runtime remains authoritative without death proof."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    skewed_ts = NOW_MS - (STALE_AGE_MS + 5_000)
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$leftover",
            body="Working ⋯",
            timestamp_ms=skewed_ts,
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: "gen-previous",
            },
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as edit_result:
        cleaned, _interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            runtime_generation="gen-current",
        )

    assert cleaned == 0
    edit_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_crashed_foreign_runtime_generation_is_cleaned_after_lease_release(tmp_path: Path) -> None:
    """A process-held generation lease protects live work and releases on SIGKILL."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    child_code = """
import signal
import sys
from pathlib import Path

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths

root = Path(sys.argv[1])
state = BotRuntimeState(
    client=None,
    config=Config(),
    runtime_paths=RuntimePaths(
        config_path=root / "config.yaml",
        config_dir=root,
        env_path=root / ".env",
        storage_root=root,
    ),
    enable_streaming=False,
    orchestrator=None,
    event_cache=None,
    event_cache_write_coordinator=None,
)
state.mark_runtime_started()
print(state.runtime_generation, flush=True)
signal.pause()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        str(runtime_paths.storage_root),
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    generation = (await process.stdout.readline()).decode().strip()
    assert generation

    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$crashed-leftover-1",
            body="Working on the first response ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: generation,
            },
        ),
        _make_message_event(
            event_id="$crashed-leftover-2",
            body="Working on the second response ⋯",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 4_000),
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: generation,
            },
        ),
    )
    lease_directory = runtime_paths.storage_root / "tracking" / "runtime_generation_leases"
    retired_at_ns = 1_000_000_000

    try:
        with patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ) as edit_result:
            cleaned_while_live, _interrupted = await _run_cleanup(
                client,
                config,
                joined_rooms=[ROOM_ID],
                runtime_generation="gen-current",
            )
            os.kill(process.pid, signal.SIGKILL)
            assert await asyncio.wait_for(process.wait(), timeout=10) == -signal.SIGKILL
            with patch("mindroom.runtime_generation_lease.time.time_ns", return_value=retired_at_ns):
                cleaned_after_crash, _interrupted = await _run_cleanup(
                    client,
                    config,
                    joined_rooms=[ROOM_ID],
                    runtime_generation="gen-current",
                )
            client.room_messages.return_value = _room_messages_response(
                _make_message_event(
                    event_id="$late-room-leftover",
                    body="Working in a later-discovered room ⋯",
                    timestamp_ms=NOW_MS - (STALE_AGE_MS + 3_000),
                    extra_content={
                        STREAM_STATUS_KEY: "streaming",
                        stale_stream_cleanup_module.STREAM_GENERATION_KEY: generation,
                    },
                ),
            )
            with patch(
                "mindroom.runtime_generation_lease.time.time_ns",
                return_value=retired_at_ns + 60 * 60 * 1_000_000_000,
            ):
                cleaned_by_later_recovery, _interrupted = await _run_cleanup(
                    client,
                    config,
                    joined_rooms=[ROOM_ID],
                    runtime_generation="gen-current",
                )
    finally:
        if process.returncode is None:
            os.kill(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), timeout=10)

    assert cleaned_while_live == 0
    assert cleaned_after_crash == 2
    assert cleaned_by_later_recovery == 1
    assert len(list(lease_directory.glob("*.lock"))) == 1
    assert edit_result.await_count == 3

    with patch(
        "mindroom.runtime_generation_lease.time.time_ns",
        return_value=retired_at_ns + 7 * 60 * 60 * 1_000_000_000,
    ):
        later_runtime = BotRuntimeState(
            client=None,
            config=config,
            runtime_paths=runtime_paths,
            enable_streaming=False,
            orchestrator=None,
            event_cache=None,
            event_cache_write_coordinator=None,
        )
        later_runtime.mark_runtime_started()
        later_generation = later_runtime.runtime_generation
        assert runtime_generation_owner_stopped(runtime_paths, generation)
        runtime_generation_lease_module.acknowledge_stopped_runtime_generation_proofs(
            runtime_paths,
            {generation},
        )
        assert not runtime_generation_owner_stopped(runtime_paths, generation)
        assert not runtime_generation_owner_stopped(runtime_paths, later_generation)
        assert len(list(lease_directory.glob("*.lock"))) == 1
        later_runtime.mark_runtime_stopped()
        assert runtime_generation_owner_stopped(runtime_paths, later_generation)
        runtime_generation_lease_module.acknowledge_stopped_runtime_generation_proofs(
            runtime_paths,
            {later_generation},
        )
        assert list(lease_directory.glob("*.lock")) == []


@pytest.mark.asyncio
async def test_orderly_stopped_generation_terminal_note_is_certified_for_auto_resume(tmp_path: Path) -> None:
    """An orderly stop retains proof until its terminal note is certified."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    prior_runtime.mark_runtime_stopped()
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$certified", room_id=ROOM_ID))

    cleaned, interrupted = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
        runtime_generation="gen-current",
    )

    assert cleaned == 1
    assert [item.target_event_id for item in interrupted] == ["$interrupted"]
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    certified_content = cast("dict[str, object]", sent_content["m.new_content"])
    assert stale_stream_cleanup_module.STREAM_GENERATION_KEY not in certified_content
    assert certified_content[STREAM_STATUS_KEY] == STREAM_STATUS_INTERRUPTED


@pytest.mark.asyncio
async def test_first_restart_after_seven_hours_certifies_orderly_terminal_note(tmp_path: Path) -> None:
    """An unconsumed orderly stopped-owner proof must survive long downtime."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    stopped_at_ns = 1_000_000_000
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    with patch("mindroom.runtime_generation_lease.time.time_ns", return_value=stopped_at_ns):
        prior_runtime.mark_runtime_stopped()

    current_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    with patch(
        "mindroom.runtime_generation_lease.time.time_ns",
        return_value=stopped_at_ns + 7 * 60 * 60 * 1_000_000_000,
    ):
        current_runtime.mark_runtime_started()

    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$certified", room_id=ROOM_ID))
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation=current_runtime.runtime_generation,
        ),
    }

    try:
        with patch(
            "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
            new=AsyncMock(return_value=[ROOM_ID]),
        ):
            result = await recover_stale_streaming_messages(
                actors,
                resume_client=None,
                resume_conversation_cache=None,
                config=config,
                runtime_paths=runtime_paths,
                scanned_room_ids=set(),
            )
    finally:
        current_runtime.mark_runtime_stopped()

    assert result.cleaned_count == 1
    assert not runtime_generation_owner_stopped(runtime_paths, prior_generation)
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert stale_stream_cleanup_module.STREAM_GENERATION_KEY not in cast(
        "dict[str, object]",
        sent_content["m.new_content"],
    )


@pytest.mark.asyncio
async def test_first_restart_repairs_old_stamped_stream_before_acknowledging_proof(tmp_path: Path) -> None:
    """A complete scan must not skip an old stamped stream and consume its sole authority proof."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    prior_runtime.mark_runtime_stopped()
    current_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    current_runtime.mark_runtime_started()
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$old-stamped-stream",
            body="Old partial response ⋯",
            timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
            },
        ),
    )
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation=current_runtime.runtime_generation,
        ),
    }

    try:
        with (
            patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
            patch(
                "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
                new=AsyncMock(return_value=[ROOM_ID]),
            ),
            patch(
                "mindroom.matrix.stale_stream_cleanup.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$certified")),
            ) as edit_result,
        ):
            result = await recover_stale_streaming_messages(
                actors,
                resume_client=None,
                resume_conversation_cache=None,
                config=config,
                runtime_paths=runtime_paths,
                scanned_room_ids=set(),
            )
    finally:
        current_runtime.mark_runtime_stopped()

    assert result.cleaned_count == 1
    edit_result.assert_awaited_once()
    assert not runtime_generation_owner_stopped(runtime_paths, prior_generation)


@pytest.mark.asyncio
async def test_stopped_proof_scan_reaches_target_beyond_old_page_cap_and_discharges(tmp_path: Path) -> None:
    """Stopped-proof coverage must advance past the normal cap and eventually discharge."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    prior_runtime.mark_runtime_stopped()
    current_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    current_runtime.mark_runtime_started()
    client = _make_client()
    client.rooms = _joined_room_cache()
    old_filler_pages = [
        _room_messages_response(
            _make_message_event(
                event_id=f"$old-filler-{page_number}",
                body="Old unrelated chatter",
                sender=USER_ID,
                timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - page_number,
            ),
            end=f"old-page-{page_number}",
        )
        for page_number in range(1, 13)
    ]
    client.room_messages = AsyncMock(
        side_effect=[
            *old_filler_pages,
            _room_messages_response(
                _make_message_event(
                    event_id="$deep-stamped-stream",
                    body="Deep old partial response ⋯",
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 20_000,
                    extra_content={
                        STREAM_STATUS_KEY: "streaming",
                        stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
                    },
                ),
            ),
        ],
    )
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation=current_runtime.runtime_generation,
        ),
    }

    try:
        with (
            patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
            patch(
                "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
                new=AsyncMock(return_value=[ROOM_ID]),
            ),
            patch(
                "mindroom.matrix.stale_stream_cleanup.edit_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$certified")),
            ),
        ):
            result = await recover_stale_streaming_messages(
                actors,
                resume_client=None,
                resume_conversation_cache=None,
                config=config,
                runtime_paths=runtime_paths,
                scanned_room_ids=set(),
            )
    finally:
        current_runtime.mark_runtime_stopped()

    assert client.room_messages.await_count == 13
    assert result.cleaned_count == 1
    assert not runtime_generation_owner_stopped(runtime_paths, prior_generation)


@pytest.mark.asyncio
async def test_failed_terminal_certification_requeues_room_until_success(tmp_path: Path) -> None:
    """A capped long-room scan retains proof until transient certification succeeds."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    prior_runtime.mark_runtime_stopped()
    unreferenced_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    unreferenced_runtime.mark_runtime_started()
    unreferenced_generation = unreferenced_runtime.runtime_generation
    unreferenced_runtime.mark_runtime_stopped()
    current_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    current_runtime.mark_runtime_started()

    client = _make_client()
    client.rooms = _joined_room_cache()
    history_pages = [
        *[
            _room_messages_response(
                _make_message_event(
                    event_id=f"$old-filler-{page_number}",
                    body="Old unrelated chatter",
                    sender=USER_ID,
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - page_number,
                ),
                end=f"old-page-{page_number}",
            )
            for page_number in range(1, 10)
        ],
        _room_messages_response(
            _make_message_event(
                event_id="$thread-root",
                body="Question",
                sender=USER_ID,
                timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 20_000,
            ),
            _make_message_event(
                event_id="$interrupted",
                body="Partial answer\n\n**[Response interrupted]**",
                timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 5_000,
                relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
                extra_content={
                    STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                    stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
                },
            ),
            end="old-page-10",
        ),
    ]
    client.room_messages = AsyncMock(
        side_effect=[
            *history_pages,
            _room_messages_response(),
            *history_pages,
            _room_messages_response(),
        ],
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation=current_runtime.runtime_generation,
        ),
    }
    scanned_room_ids: set[str] = set()

    try:
        with (
            patch(
                "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
                new=AsyncMock(return_value=[ROOM_ID]),
            ),
            patch(
                "mindroom.matrix.stale_stream_cleanup.edit_message_result",
                new=AsyncMock(
                    side_effect=[
                        None,
                        delivered_matrix_event("$certified"),
                    ],
                ),
            ) as edit_result,
        ):
            first_result = await recover_stale_streaming_messages(
                actors,
                resume_client=None,
                resume_conversation_cache=None,
                config=config,
                runtime_paths=runtime_paths,
                scanned_room_ids=scanned_room_ids,
            )
            assert runtime_generation_owner_stopped(runtime_paths, prior_generation)
            assert runtime_generation_owner_stopped(runtime_paths, unreferenced_generation)
            second_result = await recover_stale_streaming_messages(
                actors,
                resume_client=None,
                resume_conversation_cache=None,
                config=config,
                runtime_paths=runtime_paths,
                scanned_room_ids=scanned_room_ids,
            )
    finally:
        current_runtime.mark_runtime_stopped()

    assert first_result.cleaned_count == 0
    assert second_result.cleaned_count == 1
    assert scanned_room_ids == {ROOM_ID}
    assert edit_result.await_count == 2
    assert client.room_messages.await_count == 22
    assert not runtime_generation_owner_stopped(runtime_paths, prior_generation)
    assert not runtime_generation_owner_stopped(runtime_paths, unreferenced_generation)


def test_runtime_generation_acquire_retries_replaced_open_inode(tmp_path: Path) -> None:
    """Acquisition must not return a lease locked on an unlinked inode."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    generation = "generation-racing-with-pruner"
    lease_path = runtime_generation_lease_module._generation_lease_path(runtime_paths, generation)
    real_flock = runtime_generation_lease_module.fcntl.flock
    path_replaced = False

    def replace_path_before_lock(file_descriptor: int, operation: int) -> None:
        nonlocal path_replaced
        if not path_replaced and operation & runtime_generation_lease_module.fcntl.LOCK_EX:
            path_replaced = True
            lease_path.unlink()
            lease_path.touch()
        real_flock(file_descriptor, operation)

    with patch.object(
        runtime_generation_lease_module.fcntl,
        "flock",
        side_effect=replace_path_before_lock,
    ):
        lease = runtime_generation_lease_module.acquire_runtime_generation_lease(
            runtime_paths,
            generation,
        )

    try:
        assert path_replaced
        lock_file = lease._lock_file
        assert lock_file is not None
        assert runtime_generation_lease_module._path_references_lock_file(
            lease_path,
            lock_file,
        )
    finally:
        lease.release()


def test_partial_retirement_write_preserves_prior_generation_proof(tmp_path: Path) -> None:
    """A failed retirement append must leave the prior complete lease record readable."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    generation = "generation-with-partial-retirement"
    real_open = runtime_generation_lease_module._open_generation_lease_file
    opened_lock_files: list[tuple[TextIO, MagicMock]] = []

    def open_wrapped_lock_file(path: Path) -> MagicMock:
        raw_lock_file = real_open(path)
        wrapped_lock_file = MagicMock(wraps=raw_lock_file)
        opened_lock_files.append((raw_lock_file, wrapped_lock_file))
        return wrapped_lock_file

    with patch.object(
        runtime_generation_lease_module,
        "_open_generation_lease_file",
        side_effect=open_wrapped_lock_file,
    ):
        lease = runtime_generation_lease_module.acquire_runtime_generation_lease(
            runtime_paths,
            generation,
        )
    assert len(opened_lock_files) == 1
    raw_lock_file, wrapped_lock_file = opened_lock_files[0]

    def partial_write(body: str) -> int:
        raw_lock_file.write(body[:5])
        raw_lock_file.flush()
        os.fsync(raw_lock_file.fileno())
        msg = "simulated crash during retirement write"
        raise OSError(msg)

    wrapped_lock_file.write.side_effect = partial_write
    with pytest.raises(OSError, match="simulated crash"):
        lease.release()

    assert runtime_generation_owner_stopped(runtime_paths, generation)


@pytest.mark.asyncio
async def test_runtime_generation_acquire_retries_cross_process_pruner_race(tmp_path: Path) -> None:
    """A cross-process prune between open and flock must force path reacquisition."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    generation = "cross-process-pruner-race"
    child_code = """
import sys
from pathlib import Path

from mindroom import runtime_generation_lease as lease_module
from mindroom.constants import RuntimePaths

root = Path(sys.argv[1])
runtime_paths = RuntimePaths(
    config_path=root / "config.yaml",
    config_dir=root,
    env_path=root / ".env",
    storage_root=root,
)
generation = sys.argv[2]
original_open = lease_module._open_generation_lease_file
first_open = True

def coordinated_open(lease_path):
    global first_open
    lock_file = original_open(lease_path)
    if first_open:
        first_open = False
        print("opened", flush=True)
        sys.stdin.readline()
    return lock_file

lease_module._open_generation_lease_file = coordinated_open
lease = lease_module.acquire_runtime_generation_lease(runtime_paths, generation)
print(
    int(lease_module._path_references_lock_file(lease._lease_path, lease._lock_file)),
    flush=True,
)
sys.stdin.readline()
lease.release()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        str(runtime_paths.storage_root),
        generation,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    try:
        assert (await asyncio.wait_for(process.stdout.readline(), timeout=10)).decode().strip() == "opened"
        lease_path = runtime_generation_lease_module._generation_lease_path(runtime_paths, generation)
        runtime_generation_lease_module._retire_unlocked_leases(lease_path.parent, now_ns=1_000_000_000)
        process.stdin.write(b"\n")
        await process.stdin.drain()
        assert (await asyncio.wait_for(process.stdout.readline(), timeout=10)).decode().strip() == "1"
        assert not runtime_generation_owner_stopped(runtime_paths, generation)
        process.stdin.write(b"\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.wait(), timeout=10) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)


@pytest.mark.asyncio
async def test_concurrent_stopped_owner_cleaners_use_one_logical_matrix_resume(tmp_path: Path) -> None:
    """Concurrent cleaners must converge through stable Matrix transaction IDs."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    prior_runtime.mark_runtime_stopped()

    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
            },
        ),
    )
    client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter())
    event_ids_by_transaction: dict[str, str] = {}

    async def idempotent_room_send(**kwargs: object) -> nio.RoomSendResponse:
        transaction_id = cast("str", kwargs["tx_id"])
        event_id = event_ids_by_transaction.setdefault(
            transaction_id,
            f"$event-{len(event_ids_by_transaction) + 1}",
        )
        return nio.RoomSendResponse(event_id=event_id, room_id=ROOM_ID)

    client.room_send = AsyncMock(side_effect=idempotent_room_send)
    cleanup_results = await asyncio.gather(
        _run_cleanup(client, config, joined_rooms=[ROOM_ID], runtime_generation="gen-current-a"),
        _run_cleanup(client, config, joined_rooms=[ROOM_ID], runtime_generation="gen-current-b"),
    )
    interrupted_by_cleaner = [interrupted for _cleaned, interrupted in cleanup_results]

    resume_counts = await asyncio.gather(
        *(
            auto_resume_interrupted_threads(
                client,
                interrupted,
                config=config,
                runtime_paths=runtime_paths,
                conversation_cache=_auto_resume_conversation_cache(interrupted),
                delay=0,
            )
            for interrupted in interrupted_by_cleaner
        ),
    )

    assert [cleaned for cleaned, _interrupted in cleanup_results] == [1, 1]
    assert resume_counts == [1, 1]
    certification_transaction_ids = [
        transaction_id for transaction_id in event_ids_by_transaction if transaction_id.startswith("mindroom-certify-")
    ]
    resume_transaction_ids = [
        transaction_id
        for transaction_id in event_ids_by_transaction
        if transaction_id.startswith("mindroom-auto-resume-")
    ]
    assert len(certification_transaction_ids) == 1
    assert len(resume_transaction_ids) == 1
    assert client.room_send.await_count == 4


@pytest.mark.asyncio
async def test_distinct_interrupted_episodes_on_same_target_use_distinct_transactions(tmp_path: Path) -> None:
    """A later interrupted episode on one target must not reuse the prior Matrix transactions."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    runtime_paths = runtime_paths_for(config)
    first_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    first_runtime.mark_runtime_started()
    first_generation = first_runtime.runtime_generation
    first_runtime.mark_runtime_stopped()
    second_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    second_runtime.mark_runtime_started()
    second_generation = second_runtime.runtime_generation
    second_runtime.mark_runtime_stopped()

    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter())
    client.room_send = AsyncMock(
        side_effect=[
            nio.RoomSendResponse(event_id="$certified-one", room_id=ROOM_ID),
            nio.RoomSendResponse(event_id="$resume-one", room_id=ROOM_ID),
            nio.RoomSendResponse(event_id="$certified-two", room_id=ROOM_ID),
            nio.RoomSendResponse(event_id="$resume-two", room_id=ROOM_ID),
        ],
    )
    root_event = _make_message_event(
        event_id="$thread-root",
        body="Question",
        sender=USER_ID,
        timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
    )
    first_interrupted_event = _make_message_event(
        event_id="$interrupted",
        body="First partial answer\n\n**[Response interrupted]**",
        timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
        relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
        extra_content={
            STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
            stale_stream_cleanup_module.STREAM_GENERATION_KEY: first_generation,
        },
    )
    client.room_messages.return_value = _room_messages_response(root_event, first_interrupted_event)
    _first_cleaned, first_interrupted = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
        runtime_generation="gen-current",
    )
    await auto_resume_interrupted_threads(
        client,
        first_interrupted,
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=_auto_resume_conversation_cache(first_interrupted),
        delay=0,
    )

    second_body = "Second partial answer\n\n**[Response interrupted]**"
    client.room_messages.return_value = _room_messages_response(
        root_event,
        first_interrupted_event,
        _make_message_event(
            event_id="$episode-two-edit",
            body=f"* {second_body}",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 1_000),
            relates_to={"rel_type": "m.replace", "event_id": "$interrupted"},
            new_content={
                "body": second_body,
                "msgtype": "m.text",
                STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: second_generation,
            },
        ),
    )
    _second_cleaned, second_interrupted = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
        runtime_generation="gen-current",
    )
    await auto_resume_interrupted_threads(
        client,
        second_interrupted,
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=_auto_resume_conversation_cache(second_interrupted),
        delay=0,
    )

    transaction_ids = [call.kwargs["tx_id"] for call in client.room_send.await_args_list]
    assert transaction_ids[0] != transaction_ids[2]
    assert transaction_ids[1] != transaction_ids[3]


@pytest.mark.asyncio
async def test_certified_terminal_note_retries_after_generation_proof_acknowledgement(tmp_path: Path) -> None:
    """A failed initial resume remains retryable after local proof acknowledgement."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    runtime_paths = runtime_paths_for(config)
    prior_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    retired_at_ns = 1_000_000_000
    prior_runtime.mark_runtime_started()
    prior_generation = prior_runtime.runtime_generation
    with patch("mindroom.runtime_generation_lease.time.time_ns", return_value=retired_at_ns):
        prior_runtime.mark_runtime_stopped()

    client = _make_client()
    client.rooms = _joined_room_cache()
    original_events = (
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: prior_generation,
            },
        ),
    )
    client.room_messages.return_value = _room_messages_response(*original_events)
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$certified", room_id=ROOM_ID))

    with patch(
        "mindroom.runtime_generation_lease.time.time_ns",
        return_value=retired_at_ns + 60 * 60 * 1_000_000_000,
    ):
        cleaned, interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            runtime_generation="gen-current",
        )

    assert cleaned == 1
    assert [item.target_event_id for item in interrupted] == ["$interrupted"]
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    certified_content = cast("dict[str, object]", sent_content["m.new_content"])
    assert stale_stream_cleanup_module.STREAM_GENERATION_KEY not in certified_content

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(return_value=None),
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=_auto_resume_conversation_cache(interrupted),
            delay=0,
        )
    assert resumed_count == 0

    assert runtime_generation_owner_stopped(runtime_paths, prior_generation)
    runtime_generation_lease_module.acknowledge_stopped_runtime_generation_proofs(
        runtime_paths,
        {prior_generation},
    )
    assert not runtime_generation_owner_stopped(runtime_paths, prior_generation)

    client.room_messages.return_value = _room_messages_response(
        *original_events,
        _make_message_event(
            event_id="$certified",
            body=str(certified_content["body"]),
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$interrupted"},
            new_content=certified_content,
        ),
    )
    client.room_send.reset_mock()

    cleaned_after_delay, retried = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
        now_ms=NOW_MS + 7 * 60 * 60 * 1_000,
        runtime_generation="gen-later",
    )

    assert cleaned_after_delay == 0
    assert [item.target_event_id for item in retried] == ["$interrupted"]
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_unstamped_legacy_stream_is_repaired(tmp_path: Path) -> None:
    """Fresh legacy streams without a generation stamp remain repairable after restart."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$legacy-leftover",
            body="Working ⋯",
            timestamp_ms=NOW_MS - 1_000,
            extra_content={STREAM_STATUS_KEY: "streaming"},
        ),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as edit_result:
        cleaned, interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            runtime_generation="gen-current",
        )

    assert cleaned == 1
    assert interrupted == []
    edit_result.assert_awaited_once()


def test_runtime_generation_rotates_on_same_object_restart(tmp_path: Path) -> None:
    """mark_runtime_started rotates the generation so prior-run streams stay repairable.

    The orchestrator reuses bot objects across stop()/start(), so without
    rotation an interrupted stream from the previous run would carry the
    current generation and be falsely protected from cleanup forever.
    """
    config = _make_config(tmp_path)
    state = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths_for(config),
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    state.mark_runtime_started()
    first_generation = state.runtime_generation
    state.mark_runtime_stopped()
    state.mark_runtime_started()

    assert state.runtime_generation != first_generation
    state.mark_runtime_stopped()


def test_orderly_runtime_cycle_proofs_are_discarded_after_complete_scan_ack(tmp_path: Path) -> None:
    """Complete scan acknowledgement prevents unreferenced proof accumulation."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    lease_directory = runtime_paths.storage_root / "tracking" / "runtime_generation_leases"
    state = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    for _ in range(5):
        state.mark_runtime_started()
        generation = state.runtime_generation

        assert not runtime_generation_owner_stopped(runtime_paths, generation)

        state.mark_runtime_stopped()
        assert runtime_generation_owner_stopped(runtime_paths, generation)
        runtime_generation_lease_module.acknowledge_stopped_runtime_generation_proofs(
            runtime_paths,
            {generation},
        )
        assert list(lease_directory.glob("*.lock")) == []

    assert list(lease_directory.glob("*.lock")) == []


def test_scan_ack_does_not_discard_generation_stopped_after_snapshot(tmp_path: Path) -> None:
    """A runtime that stops during a scan must retain proof for the next recovery."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    state = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    state.mark_runtime_started()
    generation = state.runtime_generation

    stopped_at_scan_start = runtime_generation_lease_module.stopped_runtime_generation_proofs(runtime_paths)
    state.mark_runtime_stopped()
    runtime_generation_lease_module.acknowledge_stopped_runtime_generation_proofs(
        runtime_paths,
        stopped_at_scan_start,
    )

    assert stopped_at_scan_start == set()
    assert runtime_generation_owner_stopped(runtime_paths, generation)


@pytest.mark.asyncio
async def test_second_wave_does_not_ack_new_proof_using_first_wave_room_coverage(tmp_path: Path) -> None:
    """A proof first observed in the delta wave requires fresh room coverage."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    stopped_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    stopped_runtime.mark_runtime_started()
    stopped_generation = stopped_runtime.runtime_generation
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response()
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation="current-generation",
        ),
    }
    scanned_room_ids: set[str] = set()

    with patch(
        "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
        new=AsyncMock(return_value=[ROOM_ID]),
    ):
        first_result = await recover_stale_streaming_messages(
            actors,
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            scanned_room_ids=scanned_room_ids,
        )
        stopped_runtime.mark_runtime_stopped()
        second_result = await recover_stale_streaming_messages(
            actors,
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            scanned_room_ids=scanned_room_ids,
        )

    assert first_result.room_count == 1
    assert second_result.room_count == 0
    assert runtime_generation_owner_stopped(runtime_paths, stopped_generation)


@pytest.mark.asyncio
async def test_zero_joined_rooms_retains_proof_without_busy_retry(tmp_path: Path) -> None:
    """An empty room set keeps stopped proof for the later delta without immediate retry debt."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    stopped_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    stopped_runtime.mark_runtime_started()
    stopped_generation = stopped_runtime.runtime_generation
    stopped_runtime.mark_runtime_stopped()
    client = _make_client()
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation="current-generation",
        ),
    }

    with patch(
        "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
        new=AsyncMock(return_value=[]),
    ):
        result = await recover_stale_streaming_messages(
            actors,
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            scanned_room_ids=set(),
        )

    assert result == StaleStreamRecoveryResult(room_count=0, cleaned_count=0, resumed_count=0)
    assert runtime_generation_owner_stopped(runtime_paths, stopped_generation)


@pytest.mark.asyncio
async def test_repeated_proof_pagination_token_stops_incomplete_and_retains_debt(tmp_path: Path) -> None:
    """A non-progressing Matrix cursor must stop without acknowledging stopped proof."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    stopped_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    stopped_runtime.mark_runtime_started()
    stopped_generation = stopped_runtime.runtime_generation
    stopped_runtime.mark_runtime_stopped()
    client = _make_client()
    looping_page = _room_messages_response(
        _make_message_event(
            event_id="$old-filler",
            body="Old unrelated chatter",
            sender=USER_ID,
            timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
        ),
        end="loop-token",
    )
    history_call_count = 0

    async def repeating_history(*_args: object, **_kwargs: object) -> nio.RoomMessagesResponse:
        nonlocal history_call_count
        history_call_count += 1
        if history_call_count > 2:
            msg = "cleanup requested the repeated pagination token again"
            raise AssertionError(msg)
        return looping_page

    client.room_messages = AsyncMock(side_effect=repeating_history)
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            None,
            runtime_generation="current-generation",
        ),
    }
    scanned_room_ids: set[str] = set()

    with patch(
        "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
        new=AsyncMock(return_value=[ROOM_ID]),
    ):
        result = await recover_stale_streaming_messages(
            actors,
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            scanned_room_ids=scanned_room_ids,
        )

    assert history_call_count == 2
    assert client.room_messages.await_args_list[0].kwargs["start"] is None
    assert client.room_messages.await_args_list[1].kwargs["start"] == "loop-token"
    assert result.retry_required is True
    assert scanned_room_ids == set()
    assert runtime_generation_owner_stopped(runtime_paths, stopped_generation)


def test_long_lived_active_generation_lease_remains_protected(tmp_path: Path) -> None:
    """A later runtime start must not disturb a lease held by a live owner."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    first_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    second_runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    started_at_ns = 1_000_000_000

    with patch("mindroom.runtime_generation_lease.time.time_ns", return_value=started_at_ns):
        first_runtime.mark_runtime_started()
    first_generation = first_runtime.runtime_generation

    with patch(
        "mindroom.runtime_generation_lease.time.time_ns",
        return_value=started_at_ns + 7 * 60 * 60 * 1_000_000_000,
    ):
        second_runtime.mark_runtime_started()

    assert not runtime_generation_owner_stopped(runtime_paths, first_generation)

    second_runtime.mark_runtime_stopped()
    first_runtime.mark_runtime_stopped()


@pytest.mark.asyncio
async def test_failed_targeted_room_scan_remains_unscanned_for_retry(tmp_path: Path) -> None:
    """A transient room-history failure must leave the claimed handoff retryable."""
    config = _make_config(tmp_path)
    client = make_matrix_client_mock(user_id=BOT_USER_ID)
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
    }

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.get_joined_rooms",
            new=AsyncMock(return_value=[ROOM_ID]),
        ),
        patch(
            "mindroom.matrix.stale_stream_cleanup._cleanup_stale_streaming_room",
            new=AsyncMock(side_effect=RuntimeError("temporary history failure")),
        ),
    ):
        scanned_room_ids: set[str] = set()
        result = await recover_stale_streaming_messages(
            actors,
            resume_client=client,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths_for(config),
            scanned_room_ids=scanned_room_ids,
            target_room_ids={ROOM_ID},
        )

    assert result == StaleStreamRecoveryResult(
        room_count=1,
        cleaned_count=0,
        resumed_count=0,
        retry_required=True,
    )
    assert scanned_room_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_status", ["error", STREAM_STATUS_INTERRUPTED])
async def test_cleanup_returns_generic_interrupted_thread_from_graceful_restart(
    tmp_path: Path,
    stream_status: str,
) -> None:
    """Generic terminal interrupted messages from shutdown should be resumable but user cancels should not."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: stream_status},
        ),
        _make_message_event(
            event_id="$cancelled",
            body="User-stopped answer\n\n**[Response cancelled by user]**",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 1),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "cancelled"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert [thread.target_event_id for thread in interrupted] == ["$interrupted"]
    assert interrupted[0].partial_text == "Partial answer"


@pytest.mark.asyncio
async def test_cleanup_returns_old_terminal_interrupted_thread_for_auto_resume(tmp_path: Path) -> None:
    """Old terminal interrupted replies should still resume; only in-progress stale streams age out."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 20_000,
        ),
        _make_message_event(
            event_id="$old-interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert len(interrupted) == 1
    assert interrupted[0].timestamp_ms == NOW_MS - OLD_STALE_AGE_MS
    assert interrupted == [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$old-interrupted",
            partial_text="Partial answer",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
        ),
    ]


@pytest.mark.asyncio
async def test_cleanup_skips_same_generation_terminal_interruption(tmp_path: Path) -> None:
    """In-memory sync-restart retry owns a terminal note from the current generation."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$current-interrupted",
            body="Partial answer\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={
                STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED,
                stale_stream_cleanup_module.STREAM_GENERATION_KEY: "gen-current",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(
        client,
        config,
        joined_rooms=[ROOM_ID],
        runtime_generation="gen-current",
    )

    assert cleaned == 0
    assert interrupted == []


@pytest.mark.asyncio
async def test_cleanup_scans_past_lookback_page_for_old_terminal_interruption(tmp_path: Path) -> None:
    """A busy room may push old terminal interrupted notes behind a lookback-crossing page."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages = AsyncMock(
        side_effect=[
            _room_messages_response(
                _make_message_event(
                    event_id="$old-filler",
                    body="Later unrelated chatter",
                    sender=USER_ID,
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
                ),
                end="older-page",
            ),
            _room_messages_response(
                _make_message_event(
                    event_id="$thread-root",
                    body="Question",
                    sender=USER_ID,
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 20_000,
                ),
                _make_message_event(
                    event_id="$old-interrupted",
                    body="Partial answer\n\n**[Response interrupted]**",
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 10_000,
                    relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED},
                ),
            ),
        ],
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert client.room_messages.await_count == 2
    assert [thread.target_event_id for thread in interrupted] == ["$old-interrupted"]


@pytest.mark.asyncio
async def test_cleanup_stops_at_lookback_page_when_auto_resume_disabled(tmp_path: Path) -> None:
    """Opted-out startup cleanup must not full-scan busy rooms when no resume relay will be queued."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = False
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages = AsyncMock(
        side_effect=[
            _room_messages_response(
                _make_message_event(
                    event_id="$old-filler",
                    body="Later unrelated chatter",
                    sender=USER_ID,
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
                ),
                end="older-page",
            ),
            _room_messages_response(
                _make_message_event(
                    event_id="$old-interrupted",
                    body="Partial answer\n\n**[Response interrupted]**",
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 10_000,
                    relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED},
                ),
            ),
        ],
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    assert client.room_messages.await_count == 1


@pytest.mark.asyncio
async def test_cleanup_caps_old_terminal_interruption_scan_when_auto_resume_enabled(tmp_path: Path) -> None:
    """Auto-resume opt-in may scan past the outage window, but never the whole room history."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = _make_client()
    client.rooms = _joined_room_cache()
    old_pages = [
        _room_messages_response(
            _make_message_event(
                event_id=f"$old-filler-{page_number}",
                body="Later unrelated chatter",
                sender=USER_ID,
                timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - page_number,
            ),
            end=f"old-page-{page_number}",
        )
        for page_number in range(1, 13)
    ]
    client.room_messages = AsyncMock(
        side_effect=[
            *old_pages,
            _room_messages_response(
                _make_message_event(
                    event_id="$too-deep-interrupted",
                    body="Partial answer\n\n**[Response interrupted]**",
                    timestamp_ms=NOW_MS - OLD_STALE_AGE_MS - 20_000,
                    relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_INTERRUPTED},
                ),
            ),
        ],
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []
    assert client.room_messages.await_count == 10


@pytest.mark.asyncio
async def test_cleanup_skips_completed_message_ending_with_generic_interrupted_note(tmp_path: Path) -> None:
    """Completed responses that happen to mention the generic note are not restart-resumable."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$completed",
            body="Literal text\n\n**[Response interrupted]**",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "completed"},
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []


@pytest.mark.asyncio
async def test_cleanup_skips_restart_interrupted_thread_after_auto_resume_was_queued(tmp_path: Path) -> None:
    """A later startup should not queue another resume for the same interrupted target."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    restart_body = build_restart_interrupted_body("Partial answer")
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$thread-root",
            body="Question",
            sender=USER_ID,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 20_000),
        ),
        _make_message_event(
            event_id="$message",
            body=restart_body,
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 5_000),
            relates_to=_thread_reply_relation("$thread-root", "$thread-root"),
            extra_content={STREAM_STATUS_KEY: "error"},
        ),
        _make_message_event(
            event_id="$resume",
            body=f"@Test Agent {AUTO_RESUME_MESSAGE}",
            sender=entity_ids(config, runtime_paths_for(config))[ROUTER_AGENT_NAME].full_id,
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$message"),
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert interrupted == []


@pytest.mark.asyncio
async def test_cleanup_uses_canonical_stream_body_instead_of_transient_warmup_suffix(tmp_path: Path) -> None:
    """Restart cleanup should resume from canonical stream text, not the transient worker warmup suffix."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="hello\n\n⏳ Preparing isolated worker...",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread", "$user"),
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                "io.mindroom.visible_body": "hello",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    assert interrupted[0].partial_text == "hello"
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert cast("dict[str, object]", sent_content["m.new_content"])["body"] == build_restart_interrupted_body("hello")


@pytest.mark.asyncio
async def test_cleanup_preserves_canonical_visible_body_after_mention_rewrite(tmp_path: Path) -> None:
    """Cleanup should store mention-rewritten canonical body in visible_body metadata."""
    config = _make_config(tmp_path)
    client = _make_client()
    client.rooms = _joined_room_cache()
    client.room_messages.return_value = _room_messages_response(
        _make_message_event(
            event_id="$message",
            body="Ping @mindroom_helper:localhost\n\n⏳ Preparing isolated worker...",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread", "$user"),
            extra_content={
                STREAM_STATUS_KEY: "streaming",
                "io.mindroom.visible_body": "Ping @helper",
            },
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$cleanup", room_id=ROOM_ID))

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    assert len(interrupted) == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    new_content = cast("dict[str, object]", sent_content["m.new_content"])
    assert sent_content["io.mindroom.visible_body"] == new_content["body"]
    assert new_content["io.mindroom.visible_body"] == new_content["body"]


@pytest.mark.asyncio
async def test_cleanup_preserves_tool_trace_and_ai_run_metadata(tmp_path: Path) -> None:
    """Cleanup edits should preserve Cinny-facing run metadata in both edit payload layers."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = _joined_room_cache()
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
    client.rooms = _joined_room_cache()
    input_keys = {
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.compaction": {"version": 3, "compacted": False},
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
    client.rooms = _joined_room_cache()
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
    client.rooms = _joined_room_cache()
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
    client2.rooms = _joined_room_cache()
    client2.room_messages.return_value = _room_messages_response(
        _make_notice_event(
            event_id="$msg-pending",
            body="Still typing",
            timestamp_ms=NOW_MS - (STALE_AGE_MS + 1_000),
            extra_content={STREAM_STATUS_KEY: "pending", "io.mindroom.tool_trace": {"version": 2}},
        ),
        _make_notice_event(
            event_id="$msg-streaming-edit",
            body="* Still typing an answer",
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to={"rel_type": "m.replace", "event_id": "$msg-pending"},
            new_content={
                "body": "Still typing an answer",
                "msgtype": "m.notice",
                STREAM_STATUS_KEY: "streaming",
                "io.mindroom.tool_trace": {"version": 2},
            },
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
    client.rooms = _joined_room_cache()

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
    client.rooms = _joined_room_cache()

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
    client.rooms = _joined_room_cache()

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
    client.rooms = _joined_room_cache()

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
            original_sender_id=USER_ID,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$newer",
            partial_text="Newer",
            agent_name="test_agent",
            original_sender_id=USER_ID,
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
            conversation_cache=_auto_resume_conversation_cache(interrupted),
        )

    assert resumed_count == 1
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[2]["m.relates_to"]["m.in_reply_to"] == {"event_id": "$newer"}


@pytest.mark.asyncio
async def test_auto_resume_sends_all_unique_threads_after_replacing_older_targets(tmp_path: Path) -> None:
    """Auto-resume should send every unique thread and keep its newest interruption."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$older-one",
            partial_text="Older one",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=100,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-two",
            target_event_id="$thread-two-target",
            partial_text="Two",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=200,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-three",
            target_event_id="$thread-three-target",
            partial_text="Three",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=300,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-one",
            target_event_id="$newer-one",
            partial_text="Newer one",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=400,
        ),
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()),
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=_auto_resume_conversation_cache(interrupted),
        )

    assert resumed_count == 3
    assert [call.args[2]["m.relates_to"]["m.in_reply_to"] for call in mock_send.await_args_list] == [
        {"event_id": "$thread-two-target"},
        {"event_id": "$thread-three-target"},
        {"event_id": "$newer-one"},
    ]


@pytest.mark.asyncio
async def test_auto_resume_sends_threads_from_every_room(tmp_path: Path) -> None:
    """Auto-resume should not omit a room based on timestamp or iteration order."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    interrupted = [
        InterruptedThread(
            room_id="!room-a:example.com",
            thread_id="$thread-new",
            target_event_id="$target-new",
            partial_text="New",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=500,
        ),
        InterruptedThread(
            room_id="!room-b:example.com",
            thread_id="$thread-old",
            target_event_id="$target-old",
            partial_text="Old",
            agent_name="test_agent",
            original_sender_id=USER_ID,
            timestamp_ms=100,
        ),
    ]

    with (
        patch(
            "mindroom.matrix.stale_stream_cleanup.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$resume")),
        ) as mock_send,
        patch("mindroom.matrix.stale_stream_cleanup.asyncio.sleep", new=AsyncMock()),
    ):
        resumed_count = await auto_resume_interrupted_threads(
            client,
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=_auto_resume_conversation_cache(interrupted),
        )

    assert resumed_count == 2
    assert [call.args[2]["m.relates_to"]["event_id"] for call in mock_send.await_args_list] == [
        "$thread-old",
        "$thread-new",
    ]


@pytest.mark.asyncio
async def test_recovery_scans_unique_rooms_and_resumes_before_slow_rooms_finish(tmp_path: Path) -> None:
    """One shared room scan should serve every bot and stream completed-room resumes."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    router_user_id = "@actual_router:localhost"
    router_client = make_matrix_client_mock(user_id=router_user_id)
    agent_client = make_matrix_client_mock(user_id=BOT_USER_ID)
    actors = {
        router_user_id: StaleStreamCleanupActor(
            router_client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
        BOT_USER_ID: StaleStreamCleanupActor(
            agent_client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
    }
    slow_room_started = asyncio.Event()
    release_slow_room = asyncio.Event()
    resume_sent = asyncio.Event()
    scanned_rooms: dict[str, tuple[object, set[str]]] = {}
    scanned_room_ids: set[str] = set()
    include_new_room = False

    async def joined_rooms(client: object) -> list[str]:
        if client is router_client:
            joined_room_ids = ["!shared:example.com", "!fast:example.com"]
            if include_new_room:
                joined_room_ids.append("!new:example.com")
            return joined_room_ids
        assert client is agent_client
        return ["!shared:example.com", "!slow:example.com"]

    async def cleanup_room(scan_client: object, **kwargs: object) -> StaleRoomCleanupResult:
        room_id = cast("str", kwargs["room_id"])
        room_actors = cast("dict[str, StaleStreamCleanupActor]", kwargs["actors"])
        scanned_rooms[room_id] = (scan_client, set(room_actors))
        if room_id == "!slow:example.com":
            slow_room_started.set()
            await release_slow_room.wait()
            return _room_cleanup_result(0, [])
        if room_id == "!fast:example.com":
            return _room_cleanup_result(
                1,
                [
                    InterruptedThread(
                        room_id=room_id,
                        thread_id="$thread",
                        target_event_id="$target",
                        partial_text="Partial",
                        agent_name="test_agent",
                    ),
                ],
            )
        return _room_cleanup_result(0, [])

    async def auto_resume(*_args: object, **_kwargs: object) -> int:
        resume_sent.set()
        return 1

    with (
        patch("mindroom.matrix.stale_stream_cleanup.get_joined_rooms", side_effect=joined_rooms),
        patch("mindroom.matrix.stale_stream_cleanup._cleanup_stale_streaming_room", side_effect=cleanup_room),
        patch("mindroom.matrix.stale_stream_cleanup._auto_resume_interrupted_threads", side_effect=auto_resume),
    ):
        recovery_task = asyncio.create_task(
            recover_stale_streaming_messages(
                actors,
                resume_client=router_client,
                resume_conversation_cache=actors[router_user_id].conversation_cache,
                config=config,
                runtime_paths=runtime_paths_for(config),
                scanned_room_ids=scanned_room_ids,
            ),
        )
        await asyncio.wait_for(slow_room_started.wait(), timeout=1.0)
        await asyncio.wait_for(resume_sent.wait(), timeout=1.0)
        assert not recovery_task.done()
        release_slow_room.set()
        result = await asyncio.wait_for(recovery_task, timeout=1.0)
        include_new_room = True
        delta_result = await recover_stale_streaming_messages(
            actors,
            resume_client=router_client,
            resume_conversation_cache=actors[router_user_id].conversation_cache,
            config=config,
            runtime_paths=runtime_paths_for(config),
            scanned_room_ids=scanned_room_ids,
        )

    assert result == StaleStreamRecoveryResult(room_count=3, cleaned_count=1, resumed_count=1)
    assert delta_result == StaleStreamRecoveryResult(room_count=1, cleaned_count=0, resumed_count=0)
    assert set(scanned_rooms) == {
        "!shared:example.com",
        "!fast:example.com",
        "!slow:example.com",
        "!new:example.com",
    }
    assert scanned_rooms["!shared:example.com"] == (router_client, {router_user_id, BOT_USER_ID})
    assert scanned_rooms["!fast:example.com"] == (router_client, {router_user_id})
    assert scanned_rooms["!slow:example.com"] == (agent_client, {BOT_USER_ID})
    assert scanned_rooms["!new:example.com"] == (router_client, {router_user_id})
    assert scanned_room_ids == set(scanned_rooms)


@pytest.mark.asyncio
async def test_recovery_resumes_all_51_rooms_even_when_newest_room_finishes_last(tmp_path: Path) -> None:
    """Completion order must not impose a hidden total resume cap."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    router_client = make_matrix_client_mock(user_id="@actual_router:localhost")
    actors = {
        "@actual_router:localhost": StaleStreamCleanupActor(
            router_client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
    }
    room_ids = [f"!room-{index}:example.com" for index in range(51)]
    slow_room_id = room_ids[-1]
    slow_room_started = asyncio.Event()
    release_slow_room = asyncio.Event()
    fast_rooms_resumed = asyncio.Event()
    resumed_room_ids: list[str] = []
    scanned_room_ids: set[str] = set()

    async def cleanup_room(_: object, **kwargs: object) -> StaleRoomCleanupResult:
        room_id = cast("str", kwargs["room_id"])
        room_index = room_ids.index(room_id)
        if room_id == slow_room_id:
            slow_room_started.set()
            await release_slow_room.wait()
        return _room_cleanup_result(
            0,
            [
                InterruptedThread(
                    room_id=room_id,
                    thread_id=f"$thread-{room_index}",
                    target_event_id=f"$target-{room_index}",
                    partial_text="Partial",
                    agent_name="test_agent",
                    timestamp_ms=room_index,
                ),
            ],
        )

    async def auto_resume(_: object, interrupted: list[InterruptedThread], **__: object) -> int:
        resumed_room_ids.extend(item.room_id for item in interrupted)
        if len(resumed_room_ids) == 50:
            fast_rooms_resumed.set()
        return len(interrupted)

    with (
        patch("mindroom.matrix.stale_stream_cleanup.get_joined_rooms", new=AsyncMock(return_value=room_ids)),
        patch("mindroom.matrix.stale_stream_cleanup._cleanup_stale_streaming_room", side_effect=cleanup_room),
        patch("mindroom.matrix.stale_stream_cleanup._auto_resume_interrupted_threads", side_effect=auto_resume),
    ):
        recovery_task = asyncio.create_task(
            recover_stale_streaming_messages(
                actors,
                resume_client=router_client,
                resume_conversation_cache=actors["@actual_router:localhost"].conversation_cache,
                config=config,
                runtime_paths=runtime_paths_for(config),
                scanned_room_ids=scanned_room_ids,
                room_concurrency=51,
            ),
        )
        await asyncio.wait_for(slow_room_started.wait(), timeout=1.0)
        await asyncio.wait_for(fast_rooms_resumed.wait(), timeout=1.0)
        assert not recovery_task.done()
        release_slow_room.set()
        result = await asyncio.wait_for(recovery_task, timeout=1.0)

    assert result == StaleStreamRecoveryResult(room_count=51, cleaned_count=0, resumed_count=51)
    assert set(resumed_room_ids) == set(room_ids)
    assert resumed_room_ids[-1] == slow_room_id
    assert scanned_room_ids == set(room_ids)


@pytest.mark.asyncio
async def test_recovery_without_resume_client_still_cleans_rooms(tmp_path: Path) -> None:
    """Router loss should disable only resume delivery, not Matrix cleanup."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    client = make_matrix_client_mock(user_id=BOT_USER_ID)
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
    }
    interrupted = InterruptedThread(
        room_id=ROOM_ID,
        thread_id="$thread",
        target_event_id="$target",
        partial_text="Partial",
        agent_name="test_agent",
    )

    with (
        patch("mindroom.matrix.stale_stream_cleanup.get_joined_rooms", new=AsyncMock(return_value=[ROOM_ID])),
        patch(
            "mindroom.matrix.stale_stream_cleanup._cleanup_stale_streaming_room",
            new=AsyncMock(return_value=_room_cleanup_result(1, [interrupted])),
        ),
        patch(
            "mindroom.matrix.stale_stream_cleanup._auto_resume_interrupted_threads",
            new=AsyncMock(),
        ) as auto_resume,
    ):
        scanned_room_ids: set[str] = set()
        result = await recover_stale_streaming_messages(
            actors,
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths_for(config),
            scanned_room_ids=scanned_room_ids,
        )

    assert result == StaleStreamRecoveryResult(room_count=1, cleaned_count=1, resumed_count=0)
    assert scanned_room_ids == {ROOM_ID}
    auto_resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_room_cleanup_routes_edits_through_each_message_owner(tmp_path: Path) -> None:
    """A shared history scan must use each bot's own client for Matrix edits."""
    config = _make_config(tmp_path)
    first_client = make_matrix_client_mock(user_id=BOT_USER_ID)
    second_client = make_matrix_client_mock(user_id=OTHER_BOT_USER_ID)
    actors = {
        BOT_USER_ID: StaleStreamCleanupActor(
            first_client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
        OTHER_BOT_USER_ID: StaleStreamCleanupActor(
            second_client,
            MagicMock(),
            runtime_generation=RUNTIME_GENERATION,
        ),
    }
    scanned_state = stale_stream_cleanup_module._ScannedRoomMessageStates(
        message_states={
            "$first": stale_stream_cleanup_module._MessageState(
                latest_body="First partial",
                latest_timestamp=NOW_MS - STALE_AGE_MS,
                latest_event_id="$first",
                stream_status="streaming",
                bot_user_id=BOT_USER_ID,
            ),
            "$second": stale_stream_cleanup_module._MessageState(
                latest_body="Second partial",
                latest_timestamp=NOW_MS - STALE_AGE_MS + 1,
                latest_event_id="$second",
                stream_status="streaming",
                bot_user_id=OTHER_BOT_USER_ID,
            ),
        },
        auto_resume_target_event_ids=set(),
    )

    with (
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
        patch(
            "mindroom.matrix.stale_stream_cleanup._scan_room_message_states",
            new=AsyncMock(return_value=scanned_state),
        ),
        patch(
            "mindroom.matrix.stale_stream_cleanup._cleanup_candidate_message",
            new=AsyncMock(return_value=(True, None)),
        ) as cleanup_candidate,
    ):
        result = await cleanup_stale_streaming_room(
            first_client,
            room_id=ROOM_ID,
            actors=actors,
            bot_user_ids=set(actors),
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert result.cleaned_count == 2
    assert result.interrupted_threads == []
    assert [call.args[0] for call in cleanup_candidate.await_args_list] == [first_client, second_client]


@pytest.mark.asyncio
async def test_orchestrator_recovery_uses_router_for_resume_and_all_started_bots(tmp_path: Path) -> None:
    """Recovery should scan every started bot but post relays through the router."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config

    router_client = AsyncMock(spec=nio.AsyncClient)
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.agent_user = MagicMock(user_id="@mindroom_router:example.com")
    router_bot._conversation_cache = MagicMock()
    router_bot.runtime_generation = "router-generation"
    router_bot.unsettled_turn_dispatch_source_event_ids = AsyncMock(return_value=frozenset({"$router-source"}))
    agent_client = AsyncMock(spec=nio.AsyncClient)
    agent_bot = MagicMock()
    agent_bot.agent_name = "test_agent"
    agent_bot.client = agent_client
    agent_bot.agent_user = MagicMock(user_id=BOT_USER_ID)
    agent_bot._conversation_cache = MagicMock()
    agent_bot.runtime_generation = "agent-generation"
    agent_bot.unsettled_turn_dispatch_source_event_ids = AsyncMock(return_value=frozenset({"$agent-source"}))
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot, "test_agent": agent_bot}

    with patch(
        "mindroom.orchestrator.recover_stale_streaming_messages",
        new=AsyncMock(return_value=StaleStreamRecoveryResult(room_count=2, cleaned_count=1, resumed_count=1)),
    ) as mock_recover:
        scanned_room_ids: set[str] = set()
        await orchestrator._recover_stale_streams_after_restart(
            [router_bot, agent_bot],
            config,
            scanned_room_ids,
        )

    mock_recover.assert_awaited_once()
    actors = mock_recover.await_args.args[0]
    assert set(actors) == {"@mindroom_router:example.com", BOT_USER_ID}
    assert actors[BOT_USER_ID].client is agent_client
    assert actors["@mindroom_router:example.com"].runtime_generation == "router-generation"
    assert actors[BOT_USER_ID].runtime_generation == "agent-generation"
    assert actors["@mindroom_router:example.com"].unsettled_turn_source_event_ids == frozenset({"$router-source"})
    assert actors[BOT_USER_ID].unsettled_turn_source_event_ids == frozenset({"$agent-source"})
    assert mock_recover.await_args.kwargs["resume_client"] is router_client
    assert mock_recover.await_args.kwargs["resume_conversation_cache"] is router_bot._conversation_cache
    assert mock_recover.await_args.kwargs["config"] == config
    assert mock_recover.await_args.kwargs["runtime_paths"] == runtime_paths_for(config)
    assert mock_recover.await_args.kwargs["scanned_room_ids"] is scanned_room_ids


@pytest.mark.asyncio
async def test_orchestrator_recovery_still_cleans_when_router_is_unavailable(tmp_path: Path) -> None:
    """Missing resume delivery must not suppress cleanup through started bot clients."""
    config = _make_config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config

    agent_client = AsyncMock(spec=nio.AsyncClient)
    agent_bot = MagicMock()
    agent_bot.agent_name = "test_agent"
    agent_bot.client = agent_client
    agent_bot.agent_user = MagicMock(user_id=BOT_USER_ID)
    agent_bot._conversation_cache = MagicMock()
    agent_bot.unsettled_turn_dispatch_source_event_ids = AsyncMock(return_value=frozenset())
    orchestrator.agent_bots = {"test_agent": agent_bot}

    with patch(
        "mindroom.orchestrator.recover_stale_streaming_messages",
        new=AsyncMock(return_value=StaleStreamRecoveryResult(room_count=1, cleaned_count=1, resumed_count=0)),
    ) as mock_recover:
        await orchestrator._recover_stale_streams_after_restart([agent_bot], config, set())

    mock_recover.assert_awaited_once()
    assert mock_recover.await_args.kwargs["resume_client"] is None
    assert mock_recover.await_args.kwargs["resume_conversation_cache"] is None
    actors = mock_recover.await_args.args[0]
    assert actors[BOT_USER_ID].client is agent_client


@pytest.mark.asyncio
async def test_orchestrator_surfaces_incomplete_recovery_for_startup_retry(tmp_path: Path) -> None:
    """A failed cleanup room must reach the startup maintenance retry owner."""
    config = _make_config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config
    agent_bot = MagicMock()
    agent_bot.client = AsyncMock(spec=nio.AsyncClient)
    agent_bot.agent_user = MagicMock(user_id=BOT_USER_ID)
    agent_bot._conversation_cache = MagicMock()
    agent_bot.runtime_generation = "agent-generation"
    agent_bot.unsettled_turn_dispatch_source_event_ids = AsyncMock(return_value=frozenset())
    recovery_result = StaleStreamRecoveryResult(room_count=1, cleaned_count=0, resumed_count=0)
    object.__setattr__(recovery_result, "retry_required", True)

    with (
        patch(
            "mindroom.orchestrator.recover_stale_streaming_messages",
            new=AsyncMock(return_value=recovery_result),
        ),
        pytest.raises(RuntimeError, match="incomplete"),
    ):
        await orchestrator._recover_stale_streams_after_restart([agent_bot], config, set())


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
            original_sender_id=USER_ID,
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
            conversation_cache=_auto_resume_conversation_cache(interrupted),
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
    other_agent_user_id = entity_ids(config, runtime_paths_for(config))["other"].full_id
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
    """bot.py must not own restart recovery (ISSUE-024b).

    Per-bot cleanup raced with orchestrator-level recovery:
    bot.start() cleaned stale messages first and discarded interrupted threads,
    so orchestrator recovery found nothing left and auto-resume never ran.
    Only the orchestrator should start the shared recovery path.
    """
    bot_source = Path(importlib.import_module("mindroom.bot").__file__).read_text()
    assert "recover_stale_streaming_messages" not in bot_source, (
        "bot.py must not import or call recover_stale_streaming_messages; the orchestrator owns restart recovery"
    )
