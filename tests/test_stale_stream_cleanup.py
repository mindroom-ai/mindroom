"""Tests for stale streaming cleanup and restart auto-resume."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_KEY,
)
from mindroom.dispatch_source import AUTO_RESUME_MESSAGE, TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.matrix import stale_stream_cleanup as stale_stream_cleanup_module
from mindroom.matrix.cache import ThreadHistoryResult, thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.stale_stream_cleanup import (
    InterruptedTargetFreshness,
    InterruptedThread,
    StaleStreamCleanupActor,
    cleanup_stale_streaming_room,
    interrupted_target_freshness,
)
from mindroom.streaming import build_restart_interrupted_body
from mindroom.tool_system.events import _TOOL_TRACE_KEY
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BOT_USER_ID = "@actual_test_agent:localhost"
OTHER_BOT_USER_ID = "@actual_other:localhost"
ROOM_ID = "!room:example.com"
NOW_MS = 1_000_000
STALE_AGE_MS = stale_stream_cleanup_module._STALE_STREAM_RECENCY_GUARD_MS + 60_000
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
    startup_cutoff_ms: int | None = None,
    terminal_interrupted_only: bool = False,
) -> tuple[int, list[InterruptedThread]]:
    client.user_id = BOT_USER_ID
    assert joined_rooms == [ROOM_ID]
    with patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=now_ms / 1000):
        result = await cleanup_stale_streaming_room(
            StaleStreamCleanupActor(client, None),
            owner_user_id=BOT_USER_ID,
            room_id=ROOM_ID,
            bot_user_ids={BOT_USER_ID} if bot_user_ids is None else bot_user_ids,
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=startup_cutoff_ms,
            terminal_interrupted_only=terminal_interrupted_only,
        )
    return result.cleaned_count, list(result.interrupted_threads)


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
@pytest.mark.parametrize(
    ("sender", "content", "expected"),
    [
        (USER_ID, None, InterruptedTargetFreshness.NEWER_HUMAN),
        (OTHER_BOT_USER_ID, None, InterruptedTargetFreshness.CURRENT),
        (
            OTHER_BOT_USER_ID,
            {
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                ORIGINAL_SENDER_KEY: USER_ID,
            },
            InterruptedTargetFreshness.NEWER_HUMAN,
        ),
    ],
)
async def test_interrupted_target_freshness_classifies_effective_later_sender(
    tmp_path: Path,
    sender: str,
    content: dict[str, object] | None,
    expected: InterruptedTargetFreshness,
) -> None:
    """Later effective human work supersedes a restart target; internal work does not."""
    config = _make_config(tmp_path)
    interrupted = InterruptedThread(
        ROOM_ID,
        "$thread",
        "$target",
        "partial",
        "test_agent",
        owner_user_id=BOT_USER_ID,
        original_sender_id=USER_ID,
    )
    conversation_cache = AsyncMock()
    conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
        _history_message("$target"),
        _history_message("$later", sender=sender, content=content),
    )

    freshness = await interrupted_target_freshness(
        interrupted,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=conversation_cache,
    )

    assert freshness is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_case", "expected"),
    [
        ("failed", InterruptedTargetFreshness.RETRY),
        ("incomplete", InterruptedTargetFreshness.RETRY),
        ("missing_target", InterruptedTargetFreshness.UNRECOVERABLE),
    ],
)
async def test_interrupted_target_freshness_classifies_unusable_history(
    tmp_path: Path,
    history_case: str,
    expected: InterruptedTargetFreshness,
) -> None:
    """Retry unavailable proof, but settle an authoritative missing target."""
    config = _make_config(tmp_path)
    interrupted = InterruptedThread(
        ROOM_ID,
        "$thread",
        "$target",
        "partial",
        "test_agent",
        owner_user_id=BOT_USER_ID,
        original_sender_id=USER_ID,
    )
    conversation_cache = AsyncMock()
    if history_case == "failed":
        conversation_cache.refresh_startup_thread_history_from_source.side_effect = RuntimeError("history failed")
    elif history_case == "incomplete":
        conversation_cache.refresh_startup_thread_history_from_source.return_value = thread_history_result(
            [_history_message("$target")],
            is_full_history=False,
            diagnostics={"thread_read_source": "homeserver"},
        )
    else:
        conversation_cache.refresh_startup_thread_history_from_source.return_value = _authoritative_history(
            _history_message("$other"),
        )

    freshness = await interrupted_target_freshness(
        interrupted,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=conversation_cache,
    )

    assert freshness is expected


@pytest.mark.asyncio
async def test_interrupted_target_freshness_propagates_cancellation(tmp_path: Path) -> None:
    """Coordinator cancellation must abort an in-flight authoritative history read."""
    config = _make_config(tmp_path)
    interrupted = InterruptedThread(
        ROOM_ID,
        "$thread",
        "$target",
        "partial",
        "test_agent",
        owner_user_id=BOT_USER_ID,
        original_sender_id=USER_ID,
    )
    conversation_cache = AsyncMock()
    conversation_cache.refresh_startup_thread_history_from_source.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await interrupted_target_freshness(
            interrupted,
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
        )


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
        cleaned, interrupted = await _run_cleanup(
            client,
            config,
            joined_rooms=[ROOM_ID],
            startup_cutoff_ms=startup_cutoff_ms,
        )

    assert cleaned == 1
    assert interrupted == []
    assert mock_edit.await_count == 1
    assert mock_edit.await_args.args[2] == "$before-cutoff"


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
            owner_user_id=BOT_USER_ID,
            original_sender_id=None,
        ),
        InterruptedThread(
            room_id=ROOM_ID,
            thread_id="$thread-root",
            target_event_id="$newer",
            partial_text="Second partial",
            agent_name="test_agent",
            owner_user_id=BOT_USER_ID,
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


def test_deep_history_scan_limit_is_independent_of_resume_count(tmp_path: Path) -> None:
    """Uncapped resume delivery must not make each room scan more old pages."""
    config = _make_config(tmp_path)
    config.defaults.auto_resume_after_restart = True

    scan_policy = stale_stream_cleanup_module._cleanup_scan_policy(config, startup_cutoff_ms=NOW_MS)

    assert scan_policy.max_extra_old_pages == 10


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
            owner_user_id=BOT_USER_ID,
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
            owner_user_id=BOT_USER_ID,
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
            owner_user_id=BOT_USER_ID,
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
            owner_user_id=BOT_USER_ID,
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
            owner_user_id=BOT_USER_ID,
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
            owner_user_id=BOT_USER_ID,
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
            owner_user_id=BOT_USER_ID,
            original_sender_id=USER_ID,
        ),
    ]


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
            owner_user_id=BOT_USER_ID,
            original_sender_id=USER_ID,
            timestamp_ms=NOW_MS - OLD_STALE_AGE_MS,
        ),
    ]


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
async def test_prior_auto_resume_relay_does_not_suppress_sibling_target(
    tmp_path: Path,
) -> None:
    """A relay for one target must not suppress another interruption in the thread."""
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
            event_id="$sibling-resume",
            body=f"@Test Agent {AUTO_RESUME_MESSAGE}",
            sender=entity_ids(config, runtime_paths_for(config))[ROUTER_AGENT_NAME].full_id,
            timestamp_ms=NOW_MS - STALE_AGE_MS,
            relates_to=_thread_reply_relation("$thread-root", "$sibling-target"),
        ),
    )
    client.room_get_event_relations = MagicMock(return_value=_aiter())

    cleaned, interrupted = await _run_cleanup(client, config, joined_rooms=[ROOM_ID])

    assert cleaned == 0
    assert [target.target_event_id for target in interrupted] == ["$message"]


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
async def test_owner_room_cleanup_ignores_messages_from_other_bots(tmp_path: Path) -> None:
    """One owner history scan must repair only that exact owner's messages."""
    config = _make_config(tmp_path)
    first_client = make_matrix_client_mock(user_id=BOT_USER_ID)
    actor = StaleStreamCleanupActor(first_client, MagicMock())
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
            new=AsyncMock(
                return_value=stale_stream_cleanup_module._CandidateCleanupResult(
                    edited=True,
                ),
            ),
        ) as cleanup_candidate,
    ):
        result = await cleanup_stale_streaming_room(
            actor,
            owner_user_id=BOT_USER_ID,
            room_id=ROOM_ID,
            bot_user_ids={BOT_USER_ID, OTHER_BOT_USER_ID},
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=NOW_MS,
        )

    assert result.cleaned_count == 1
    assert result.interrupted_threads == ()
    cleanup_candidate.assert_awaited_once()
    assert cleanup_candidate.await_args.args[0] is first_client


@pytest.mark.asyncio
async def test_room_cleanup_continues_after_failed_edit_and_requests_retry(
    tmp_path: Path,
) -> None:
    """One failed edit must not starve later candidates, but must keep the room retryable."""
    config = _make_config(tmp_path)
    client = make_matrix_client_mock(user_id=BOT_USER_ID)
    actor = StaleStreamCleanupActor(client, MagicMock())
    scanned_state = stale_stream_cleanup_module._ScannedRoomMessageStates(
        message_states={
            "$failed": stale_stream_cleanup_module._MessageState(
                latest_body="First partial",
                latest_timestamp=NOW_MS - STALE_AGE_MS,
                latest_event_id="$failed",
                stream_status="streaming",
                bot_user_id=BOT_USER_ID,
            ),
            "$cleaned": stale_stream_cleanup_module._MessageState(
                latest_body="Second partial",
                latest_timestamp=NOW_MS - STALE_AGE_MS + 1,
                latest_event_id="$cleaned",
                stream_status="streaming",
                bot_user_id=BOT_USER_ID,
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
            "mindroom.matrix.stale_stream_cleanup._edit_stale_message",
            new=AsyncMock(side_effect=[False, True]),
        ) as edit_stale_message,
        patch(
            "mindroom.matrix.stale_stream_cleanup._redact_stop_reactions",
            new=AsyncMock(),
        ),
    ):
        result = await cleanup_stale_streaming_room(
            actor,
            owner_user_id=BOT_USER_ID,
            room_id=ROOM_ID,
            bot_user_ids={BOT_USER_ID},
            config=config,
            runtime_paths=runtime_paths_for(config),
            startup_cutoff_ms=NOW_MS,
        )

    assert result.cleaned_count == 1
    assert result.retry_required is True
    assert edit_stale_message.await_count == 2


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
async def test_requester_resolution_exception_requests_retry_after_cleanup(tmp_path: Path) -> None:
    """A transient requester read must retain recovery after cleanup."""
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

    with (
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ),
    ):
        result = await cleanup_stale_streaming_room(
            StaleStreamCleanupActor(client, None),
            owner_user_id=BOT_USER_ID,
            room_id=ROOM_ID,
            bot_user_ids={BOT_USER_ID},
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert result.cleaned_count == 1
    assert len(result.interrupted_threads) == 1
    assert result.interrupted_threads[0].original_sender_id is None
    assert result.retry_required is True


@pytest.mark.parametrize(
    ("lookup_response", "expected_retry"),
    [
        pytest.param(
            nio.RoomGetEventError("Forbidden", status_code="M_FORBIDDEN"),
            True,
            id="matrix-error",
        ),
        pytest.param(object(), True, id="malformed"),
        pytest.param(
            nio.RoomGetEventError("Missing", status_code="M_NOT_FOUND"),
            False,
            id="not-found",
        ),
    ],
)
@pytest.mark.asyncio
async def test_requester_resolution_response_classifies_retry_after_cleanup(
    tmp_path: Path,
    lookup_response: object,
    expected_retry: bool,
) -> None:
    """Requester lookup responses must distinguish definitive absence from retry."""
    config = _make_config(tmp_path)
    client = AsyncMock(spec=nio.AsyncClient)
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
    client.room_get_event = AsyncMock(return_value=lookup_response)

    with (
        patch("mindroom.matrix.stale_stream_cleanup.time.time", return_value=NOW_MS / 1000),
        patch(
            "mindroom.matrix.stale_stream_cleanup.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit")),
        ),
    ):
        result = await cleanup_stale_streaming_room(
            StaleStreamCleanupActor(client, None),
            owner_user_id=BOT_USER_ID,
            room_id=ROOM_ID,
            bot_user_ids={BOT_USER_ID},
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    assert result.cleaned_count == 1
    assert len(result.interrupted_threads) == 1
    assert result.interrupted_threads[0].original_sender_id is None
    assert result.retry_required is expected_retry


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


def test_bot_module_does_not_own_restart_recovery() -> None:
    """bot.py must not own restart recovery (ISSUE-024b).

    Per-bot cleanup raced with orchestrator-level recovery:
    bot.start() cleaned stale messages first and discarded interrupted threads,
    so orchestrator recovery found nothing left and auto-resume never ran.
    Only the orchestrator should start the shared recovery path.
    """
    bot_source = Path(importlib.import_module("mindroom.bot").__file__).read_text()
    recovery_symbols = (
        "cleanup_stale_streaming_room",
        "interrupted_target_freshness",
        "build_auto_resume_content",
    )
    for symbol in recovery_symbols:
        assert symbol not in bot_source, f"bot.py must not import or call {symbol}; the orchestrator owns recovery"


@pytest.mark.asyncio
async def test_room_messages_error_requests_typed_room_retry(tmp_path: Path) -> None:
    """A rejected room history read must retain the semantic recovery job."""
    config = _make_config(tmp_path)
    client = make_matrix_client_mock(user_id=BOT_USER_ID)
    client.room_messages.return_value = nio.RoomMessagesError.from_dict(
        {"errcode": "M_FORBIDDEN", "error": "History unavailable"},
        ROOM_ID,
    )

    result = await cleanup_stale_streaming_room(
        StaleStreamCleanupActor(client, MagicMock()),
        owner_user_id=BOT_USER_ID,
        room_id=ROOM_ID,
        bot_user_ids={BOT_USER_ID},
        config=config,
        runtime_paths=runtime_paths_for(config),
        startup_cutoff_ms=NOW_MS,
    )

    assert result.cleaned_count == 0
    assert result.interrupted_threads == ()
    assert result.retry_required is True
