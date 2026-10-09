"""Tests for stale streaming cleanup after restarts."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from nio.api import RelationshipType

from mindroom.config.main import Config
from mindroom.constants import (
    ROUTER_AGENT_NAME,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_KEY,
)
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.stale_stream_cleanup import (
    _cleanup_stale_streaming_room as cleanup_stale_streaming_room,
)
from mindroom.matrix.stale_stream_cleanup import (
    _StaleStreamRecoveryResult as StaleStreamRecoveryResult,
)
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.streaming import build_restart_interrupted_body
from mindroom.tool_system.events import _TOOL_TRACE_KEY
from tests.access_schema_support import with_current_room_member_access
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_matrix_client_mock,
    runtime_paths_for,
    serve_media_from_download,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BOT_USER_ID = "@actual_test_agent:localhost"
OTHER_BOT_USER_ID = "@actual_other:localhost"
ROOM_ID = "!room:example.com"


def test_approval_waiting_message_is_not_a_stale_stream_candidate() -> None:
    """A restart must leave a valid persisted approval placeholder untouched."""
    state = stale_stream_cleanup_module._MessageState(
        latest_body="Waiting for approval: `run_shell_command`",
        stream_status=STREAM_STATUS_APPROVAL_PENDING,
    )

    assert stale_stream_cleanup_module._is_cleanup_candidate(state) is False


NOW_MS = 1_000_000
STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_RECENCY_GUARD_MS + 60_000
OLD_STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_LOOKBACK_MS + 60_000
USER_ID = "@user:example.com"


def _make_config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        with_current_room_member_access(
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
                authorization={},
                mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
            ),
        ),
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "test_agent": "actual_test_agent", "other": "actual_other"},
    )
    return config


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


@asynccontextmanager
async def _permitted_recovery_scope(*_args: str) -> AsyncIterator[bool]:
    """Keep scanner transport unit tests independent of source ownership policy."""
    yield True


async def _run_cleanup(  # noqa: C901 - Adapts static originals, edits and reactions to exact Matrix reads.
    client: AsyncMock,
    config: Config,
    *,
    joined_rooms: list[str],
    bot_user_ids: set[str] | None = None,
    now_ms: int = NOW_MS,
    startup_cutoff_ms: int | None = None,
    target_event_ids: tuple[str, ...] | None = None,
) -> int:
    """Exercise exact cleanup using the existing static Matrix event fixtures."""
    client.user_id = BOT_USER_ID
    assert joined_rooms == [ROOM_ID]
    response = client.room_messages.return_value
    if client.room_messages.side_effect is not None:
        pages = list(client.room_messages.side_effect)
        events = [event for page in pages for event in page.chunk]
    else:
        events = list(response.chunk)
    by_id = {event.event_id: event for event in events}
    original_get_event = client.room_get_event.side_effect
    fallback_response = client.room_get_event.return_value

    async def get_event(room_id: str, event_id: str) -> nio.RoomGetEventResponse:
        if event_id in by_id and (
            by_id[event_id].sender == BOT_USER_ID or not EventInfo.from_event(by_id[event_id].source).is_edit
        ):
            return _room_get_event_response(by_id[event_id])
        if callable(original_get_event):
            result = original_get_event(room_id, event_id)
            return await result if inspect.isawaitable(result) else result
        if original_get_event is not None:
            return next(original_get_event)
        return fallback_response

    original_relations = client.room_get_event_relations

    async def relations(
        room_id: str,
        event_id: str,
        relation_type: object,
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[nio.Event]:
        if relation_type == RelationshipType.replacement:
            for event in events:
                relation = event.source.get("content", {}).get("m.relates_to", {})
                if relation.get("rel_type") == "m.replace" and relation.get("event_id") == event_id:
                    yield event
        else:
            for event in events:
                if (
                    isinstance(event, nio.ReactionEvent)
                    and EventInfo.from_event(event.source).reaction_target_event_id == event_id
                ):
                    yield event
            async for event in original_relations(room_id, event_id, relation_type, *args, **kwargs):
                yield event

    client.room_get_event.side_effect = get_event
    client.room_get_event_relations = MagicMock(side_effect=relations)
    targets = tuple(
        event.event_id
        for event in events
        if isinstance(event, nio.RoomMessageText | nio.RoomMessageNotice)
        and event.sender == BOT_USER_ID
        and not EventInfo.from_event(event.source).is_edit
    )
    with patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=now_ms / 1000):
        return await cleanup_stale_streaming_room(
            client,
            response_recovery_scope=_permitted_recovery_scope,
            room_id=ROOM_ID,
            actors={BOT_USER_ID: client},
            target_event_ids=targets if target_event_ids is None else target_event_ids,
            bot_user_ids={BOT_USER_ID} if bot_user_ids is None else bot_user_ids,
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=startup_cutoff_ms,
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
        cleaned = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            bot_user_ids={BOT_USER_ID},
        )

    assert cleaned == 1
    assert {call.kwargs["event_id"] for call in client.room_redact.await_args_list} == {
        "$history-stop",
        "$relations-stop",
    }


@pytest.mark.asyncio
async def test_relations_api_error_does_not_invent_reaction_targets(tmp_path: Path) -> None:
    """A failed exact relation lookup must not invent reaction IDs."""
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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    client.room_redact.assert_not_awaited()


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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_skips_messages_older_than_restart_window(tmp_path: Path) -> None:
    """Cleanup should not edit very old interrupted replies from previous outages."""
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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID], now_ms=NOW_MS)

    assert cleaned == 0
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_skips_streaming_messages_at_or_after_startup_cutoff(tmp_path: Path) -> None:
    """Post-sync cleanup must ignore messages that could have been created by this process."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    startup_cutoff_ms = NOW_MS - 120_000
    before_cutoff_message = _make_message_event(
        event_id="$before-cutoff",
        body="Previous process partial",
        timestamp_ms=startup_cutoff_ms - 1,
        extra_content={STREAM_STATUS_KEY: "streaming"},
    )
    at_cutoff_message = _make_message_event(
        event_id="$at-cutoff",
        body="Current process partial",
        timestamp_ms=startup_cutoff_ms,
        extra_content={STREAM_STATUS_KEY: "streaming"},
    )
    after_cutoff_message = _make_message_event(
        event_id="$after-cutoff",
        body="Current process newer partial",
        timestamp_ms=startup_cutoff_ms + 1,
        extra_content={STREAM_STATUS_KEY: "streaming"},
    )
    client.room_messages.return_value = _room_messages_response(
        before_cutoff_message,
        at_cutoff_message,
        after_cutoff_message,
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    with patch(
        "mindroom.matrix.stale_stream_cleanup.edit_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
    ) as mock_edit:
        cleaned = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            startup_cutoff_ms=startup_cutoff_ms,
        )

    assert cleaned == 1
    assert mock_edit.await_count == 1
    assert mock_edit.await_args.args[2] == "$before-cutoff"


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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    mock_edit.assert_not_awaited()


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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert sent_content[STREAM_STATUS_KEY] == "error"
    assert sent_content["io.mindroom.ai_run"] == {"version": 1, "run_id": "run-123"}
    assert cast("dict[str, object]", sent_content["m.new_content"])["body"] == interrupted_body
    client.room_redact.assert_awaited_once()
    assert client.room_redact.await_args.kwargs["event_id"] == "$history-stop"


@pytest.mark.asyncio
async def test_cleanup_repairs_threaded_pending_restart_note(tmp_path: Path) -> None:
    """Threaded pending restart-note messages should get the same metadata-only repair."""
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    sent_content = cast("dict[str, object]", client.room_send.await_args.kwargs["content"])
    assert cast("dict[str, object]", sent_content["m.new_content"])["body"] == interrupted_body


@pytest.mark.asyncio
async def test_cleanup_leaves_restart_marked_terminal_message_unedited(tmp_path: Path) -> None:
    """A terminal restart-interrupted message from graceful shutdown needs no cleanup edit."""
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_status", ["error", STREAM_STATUS_INTERRUPTED])
async def test_cleanup_leaves_terminal_interrupted_and_cancelled_messages_unedited(
    tmp_path: Path,
    stream_status: str,
) -> None:
    """Generic terminal interrupted messages from shutdown and user cancels need no cleanup edit."""
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_skips_completed_message_ending_with_generic_interrupted_note(tmp_path: Path) -> None:
    """Completed responses that happen to mention the generic note need no restart cleanup."""
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0


@pytest.mark.asyncio
async def test_cleanup_uses_canonical_stream_body_instead_of_transient_warmup_suffix(tmp_path: Path) -> None:
    """Restart cleanup should finish from canonical stream text, not the transient worker warmup suffix."""
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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

    cleaned2 = await _run_cleanup(client2, config, joined_rooms=[ROOM_ID])

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
    serve_media_from_download(client)
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 1
    client.download.assert_not_awaited()
    assert mock_edit.await_args.args[2] == "$bot-message"


@pytest.mark.asyncio
async def test_cleanup_sidecar_hydration_failure_retains_retryable_response(tmp_path: Path) -> None:
    """An incomplete preview must never replace the full interrupted response."""
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

    with pytest.raises(RuntimeError, match="Cannot resolve owned recovery response"):
        await _run_cleanup(client, config, joined_rooms=[ROOM_ID])
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_preserves_sidecar_tool_trace_from_edit_chain(tmp_path: Path) -> None:
    """For edit-based sidecars, tool_trace should come from the latest edit sidecar."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
    serve_media_from_download(client)
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

    cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

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
async def test_shared_room_cleanup_routes_edits_through_each_message_owner(tmp_path: Path) -> None:
    """A shared history scan must use each bot's own client for Matrix edits."""
    config = _make_config(tmp_path)
    first_client = make_matrix_client_mock(user_id=BOT_USER_ID)
    second_client = make_matrix_client_mock(user_id=OTHER_BOT_USER_ID)
    actors = {
        BOT_USER_ID: first_client,
        OTHER_BOT_USER_ID: second_client,
    }
    scanned_state = {
        "$first": stale_stream_cleanup_module._MessageState(
            latest_body="First partial",
            latest_timestamp=NOW_MS - STALE_AGE_MS,
            stream_status="streaming",
            bot_user_id=BOT_USER_ID,
        ),
        "$second": stale_stream_cleanup_module._MessageState(
            latest_body="Second partial",
            latest_timestamp=NOW_MS - STALE_AGE_MS + 1,
            stream_status="streaming",
            bot_user_id=OTHER_BOT_USER_ID,
        ),
    }

    with (
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
        patch(
            "mindroom.matrix.stale_stream_cleanup._load_recovery_message_states",
            new=AsyncMock(return_value=scanned_state),
        ),
        patch(
            "mindroom.matrix.stale_stream_cleanup._cleanup_candidate_message",
            new=AsyncMock(return_value=True),
        ) as cleanup_candidate,
    ):
        cleaned_count = await cleanup_stale_streaming_room(
            first_client,
            response_recovery_scope=_permitted_recovery_scope,
            room_id=ROOM_ID,
            actors=actors,
            target_event_ids=("$first", "$second"),
            bot_user_ids=set(actors),
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=NOW_MS,
        )

    assert cleaned_count == 2
    assert [call.args[0] for call in cleanup_candidate.await_args_list] == [first_client, second_client]


@pytest.mark.asyncio
async def test_orchestrator_runs_two_recovery_waves_around_room_setup(tmp_path: Path) -> None:
    """Startup should recover current rooms and then rooms joined during setup."""
    config = _make_config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config

    call_order: list[str] = []
    router_bot = MagicMock(
        pending_response_owner_count=0,
        pending_response_phase_counts={},
        deferred_stop_phase=None,
        deferred_stop_required=False,
    )
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.try_start = AsyncMock(return_value=True)
    router_bot.stop = AsyncMock()
    router_bot._quiesce_matrix_ingestion = AsyncMock()
    router_bot.recover_pending_turn_journal_events = AsyncMock(
        side_effect=lambda: call_order.append("turn_dispatch"),
    )
    router_bot.running = True
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.agent_user = MagicMock(user_id="@mindroom_router:example.com")
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}

    recovery_finished = asyncio.Event()

    async def _wait_for_homeserver(*_args: object, **_kwargs: object) -> None:
        call_order.append("wait")

    async def _setup_rooms(_: list[object]) -> None:
        call_order.append("setup")

    async def _recover(_: list[object], __: Config, startup_cutoff_ms: int) -> None:
        assert startup_cutoff_ms > 0
        call_order.append("recover")
        if call_order.count("recover") == 2:
            recovery_finished.set()

    ready = asyncio.Event()

    def _mark_ready() -> None:
        ready.set()

    def _start_sync_task(entity_name: str, __: object) -> None:
        call_order.append("sync")
        if entity_name == ROUTER_AGENT_NAME:
            orchestrator._router_reply_memberships_live_sync_ready.set()

    with (
        patch("mindroom.orchestrator.wait_for_matrix_homeserver", side_effect=_wait_for_homeserver),
        patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=_setup_rooms),
        patch.object(orchestrator, "_recover_stale_streams_after_restart", side_effect=_recover),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_start_sync_task", side_effect=_start_sync_task),
        patch("mindroom.orchestrator.check_embedder_health", new=AsyncMock()),
        patch("mindroom.orchestrator.set_runtime_ready", side_effect=_mark_ready),
    ):
        runtime_task = asyncio.create_task(orchestrator.start())
        try:
            # Ordering is under test, not cold-import or machine scheduling latency.
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            await asyncio.wait_for(recovery_finished.wait(), timeout=5.0)
            await orchestrator.stop()
            await asyncio.wait_for(runtime_task, timeout=1.0)
        finally:
            if not runtime_task.done():
                runtime_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runtime_task

    router_bot.recover_pending_turn_journal_events.assert_not_awaited()
    assert call_order == ["wait", "sync", "recover", "setup", "recover"]


@pytest.mark.asyncio
async def test_orchestrator_recovery_uses_all_started_bots(tmp_path: Path) -> None:
    """Recovery should repair through every started bot's own client, the router included."""
    config = _make_config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config

    router_client = AsyncMock(spec=nio.AsyncClient)
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.agent_user = MagicMock(user_id="@mindroom_router:example.com")
    agent_client = AsyncMock(spec=nio.AsyncClient)
    agent_bot = MagicMock()
    agent_bot.agent_name = "test_agent"
    agent_bot.client = agent_client
    agent_bot.agent_user = MagicMock(user_id=BOT_USER_ID)
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot, "test_agent": agent_bot}

    with patch(
        "mindroom.orchestrator.recover_stale_streaming_messages",
        new=AsyncMock(return_value=StaleStreamRecoveryResult(room_count=2, cleaned_count=1)),
    ) as mock_recover:
        await orchestrator._recover_stale_streams_after_restart([router_bot, agent_bot], config, NOW_MS)

    mock_recover.assert_awaited_once()
    actors = mock_recover.await_args.args[0]
    assert set(actors) == {"@mindroom_router:example.com", BOT_USER_ID}
    assert actors[BOT_USER_ID] is agent_client
    assert actors["@mindroom_router:example.com"] is router_client
    assert mock_recover.await_args.kwargs["config"] == config
    assert mock_recover.await_args.kwargs["runtime_paths"] == runtime_paths_for(config)
    assert mock_recover.await_args.kwargs["startup_cutoff_ms"] == NOW_MS


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
        cleaned = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    mock_edit.assert_not_awaited()
    client.room_redact.assert_awaited_once()
    assert client.room_redact.await_args.kwargs["event_id"] == "$stop-reaction"


def test_bot_module_does_not_import_stale_stream_cleanup() -> None:
    """bot.py must not own restart recovery (ISSUE-024b).

    Per-bot cleanup raced with orchestrator-level recovery.
    Only the orchestrator should start the shared recovery path.
    """
    bot_source = Path(importlib.import_module("mindroom.bot").__file__).read_text()
    assert "recover_stale_streaming_messages" not in bot_source, (
        "bot.py must not import or call recover_stale_streaming_messages; the orchestrator owns restart recovery"
    )
