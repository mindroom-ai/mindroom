"""Tests for the generic Matrix API tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.matrix_api import MatrixApiTools, MatrixSearchResponse
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)


@pytest.fixture(autouse=True)
def _reset_matrix_api_rate_limit() -> None:
    MatrixApiTools._recent_write_units.clear()


def _make_context(
    *,
    room_id: str = "!room:localhost",
    conversation_cache: object | None = None,
) -> ToolRuntimeContext:
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    client.room_send = AsyncMock()
    client.room_get_state_event = AsyncMock()
    client.room_put_state = AsyncMock()
    client.room_redact = AsyncMock()
    client.room_get_event = AsyncMock()
    client._send = AsyncMock()
    resolved_conversation_cache = make_conversation_cache_mock() if conversation_cache is None else conversation_cache
    resolved_conversation_cache.get_event = AsyncMock(
        return_value=_event_response(
            event_id="$target:localhost",
            room_id=room_id,
            content={"body": "message", "msgtype": "m.text"},
        ),
    )
    resolved_conversation_cache.notify_outbound_message = Mock()
    resolved_conversation_cache.notify_outbound_redaction = Mock()
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=resolved_conversation_cache,
        event_cache=make_event_cache_mock(),
        room=None,
        reply_to_event_id="$reply:localhost",
        storage_path=runtime_root,
    )


def _state_response(
    *,
    content: dict[str, object],
    event_type: str = "com.example.state",
    state_key: str = "",
    room_id: str = "!room:localhost",
) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content=content,
        event_type=event_type,
        state_key=state_key,
        room_id=room_id,
    )


def _state_error(
    *,
    message: str = "missing",
    status_code: str = "M_NOT_FOUND",
    room_id: str = "!room:localhost",
) -> nio.RoomGetStateEventError:
    return nio.RoomGetStateEventError(
        message,
        status_code=status_code,
        room_id=room_id,
    )


def _event_response(
    *,
    event_id: str = "$evt:localhost",
    event_type: str = "m.room.message",
    room_id: str = "!room:localhost",
    sender: str = "@alice:localhost",
    origin_server_ts: int = 123,
    content: dict[str, object] | None = None,
) -> nio.RoomGetEventResponse:
    return nio.RoomGetEventResponse.from_dict(
        {
            "content": content or {"body": "hello", "msgtype": "m.text"},
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": origin_server_ts,
            "room_id": room_id,
            "type": event_type,
        },
    )


def _raw_event(
    *,
    event_id: str = "$evt:localhost",
    event_type: str = "m.room.message",
    room_id: str = "!room:localhost",
    sender: str = "@alice:localhost",
    origin_server_ts: int = 123,
    content: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "content": content or {"body": "hello", "msgtype": "m.text"},
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "room_id": room_id,
        "type": event_type,
    }


def _search_result(
    *,
    rank: float = 1.0,
    result: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"rank": rank, "result": result or _raw_event()}
    if context is not None:
        payload["context"] = context
    return payload


def test_matrix_api_tool_registered_and_instantiates() -> None:
    """Matrix API tool should be available from the metadata registry."""
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )

    assert "matrix_api" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("matrix_api", runtime_paths_for(config), worker_target=None),
        MatrixApiTools,
    )


def test_matrix_search_response_zero_results_without_results_key() -> None:
    """Matrix search should accept homeserver responses that omit results for zero matches."""
    response = MatrixSearchResponse.from_dict(
        {
            "search_categories": {
                "room_events": {
                    "count": 0,
                    "highlights": ["needle"],
                },
            },
        },
    )

    assert isinstance(response, MatrixSearchResponse)
    assert response.count == 0
    assert response.next_batch is None
    assert response.results == []


@pytest.mark.asyncio
async def test_matrix_api_requires_runtime_context() -> None:
    """Tool should fail clearly when no Matrix runtime context is available."""
    payload = json.loads(await MatrixApiTools().matrix_api(action="send_event"))

    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_api"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_api_send_event_happy_path() -> None:
    """send_event should call room_send and return the event id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.event",
                content={"body": "hello"},
            ),
        )

    assert payload == {
        "action": "send_event",
        "event_id": "$send:localhost",
        "event_type": "com.example.event",
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_send.assert_awaited_once_with(
        room_id=ctx.room_id,
        message_type="com.example.event",
        content={"body": "hello"},
    )


@pytest.mark.asyncio
async def test_matrix_api_send_event_records_threaded_room_message() -> None:
    """send_event should write successful threaded room messages through the conversation cache."""
    tool = MatrixApiTools()
    ctx = _make_context()
    content = {
        "msgtype": "m.notice",
        "body": "threaded",
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread:localhost",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$latest:localhost"},
        },
    }
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload == {
        "action": "send_event",
        "event_id": "$send:localhost",
        "event_type": "m.room.message",
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.conversation_cache.notify_outbound_message.assert_called_once_with(
        ctx.room_id,
        "$send:localhost",
        content,
    )


@pytest.mark.asyncio
async def test_matrix_api_send_event_room_message_preserves_raw_payload() -> None:
    """Low-level m.room.message sends should use raw room_send payloads without MindRoom rewrites."""
    tool = MatrixApiTools()
    ctx = _make_context()
    content = {
        "msgtype": "m.notice",
        "com.example.payload": "x" * 20000,
    }
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload["status"] == "ok"
    ctx.client.room_send.assert_awaited_once_with(
        room_id=ctx.room_id,
        message_type="m.room.message",
        content=content,
    )


@pytest.mark.asyncio
async def test_matrix_api_send_event_ignores_cache_failure_after_successful_send() -> None:
    """A successful send_event should delegate advisory bookkeeping through the cache facade."""
    tool = MatrixApiTools()
    ctx = _make_context()
    content = {
        "msgtype": "m.notice",
        "body": "threaded",
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread:localhost",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$latest:localhost"},
        },
    }
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$send:localhost"
    ctx.conversation_cache.notify_outbound_message.assert_called_once_with(
        ctx.room_id,
        "$send:localhost",
        content,
    )


@pytest.mark.asyncio
async def test_matrix_api_send_event_plain_reply_to_threaded_target_records_thread_bookkeeping() -> None:
    """Plain replies to threaded targets should reuse the shared inherited-thread rule."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.side_effect = lambda room_id, event_id: (
        "$thread:localhost" if (room_id, event_id) == (ctx.room_id, "$thread-reply") else None
    )
    content = {
        "body": "bridged reply",
        "msgtype": "m.text",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply"}},
    }
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload["status"] == "ok"
    ctx.conversation_cache.notify_outbound_message.assert_called_once_with(
        ctx.room_id,
        "$send:localhost",
        content,
    )


@pytest.mark.asyncio
async def test_matrix_api_send_event_delegates_thread_classification_to_shared_helper() -> None:
    """send_event should call the shared thread-membership helper instead of inlining cache policy."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_api.resolve_event_thread_impact_for_client",
            new=AsyncMock(return_value=MutationThreadImpact.threaded("$thread:localhost")),
        ) as mock_resolve_thread_impact,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content={"body": "hello", "msgtype": "m.text"},
            ),
        )

    assert payload["status"] == "ok"
    mock_resolve_thread_impact.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_api_send_event_room_message_preserves_matrix_error_details() -> None:
    """Low-level m.room.message send errors should surface the actual homeserver failure."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendError(
        "forbidden",
        status_code="M_FORBIDDEN",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content={"body": "hello", "msgtype": "m.text"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["status_code"] == "M_FORBIDDEN"
    assert payload["response"] == "RoomSendError: M_FORBIDDEN forbidden"


@pytest.mark.asyncio
async def test_matrix_api_send_event_room_mode_edit_with_cache_does_not_notify_thread_bookkeeping() -> None:
    """Room-mode edits should not call threaded cache bookkeeping when the target is not in a thread."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = None
    ctx.conversation_cache.get_event.return_value = _event_response(
        event_id="$room-message",
        room_id=ctx.room_id,
        content={"body": "room message", "msgtype": "m.text"},
    )
    ctx.client.room_messages.return_value = nio.RoomMessagesResponse(
        room_id=ctx.room_id,
        chunk=[
            nio.RoomMessageText.from_dict(
                {
                    "content": {"body": "room message", "msgtype": "m.text"},
                    "event_id": "$room-message",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 123,
                    "room_id": ctx.room_id,
                    "type": "m.room.message",
                },
            ),
        ],
        start="",
        end=None,
    )
    content = {
        "body": "* updated",
        "msgtype": "m.text",
        "m.new_content": {"body": "updated", "msgtype": "m.text"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$room-message"},
    }
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload["status"] == "ok"
    ctx.conversation_cache.notify_outbound_message.assert_not_called()


@pytest.mark.asyncio
async def test_matrix_api_send_event_room_mode_edit_errors_when_thread_lookup_fails() -> None:
    """Room-mode edits should fail closed when thread classification cannot be resolved."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.side_effect = RuntimeError("db broken")
    content = {
        "body": "* updated",
        "msgtype": "m.text",
        "m.new_content": {"body": "updated", "msgtype": "m.text"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$room-message"},
    }
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload == {
        "action": "send_event",
        "event_type": "m.room.message",
        "message": "Failed to resolve threaded Matrix message send target.",
        "room_id": ctx.room_id,
        "status": "error",
        "tool": "matrix_api",
    }
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_send_event_errors_when_thread_classification_fails() -> None:
    """send_event should fail closed when threaded classification cannot be resolved."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = None
    ctx.conversation_cache.get_event.side_effect = RuntimeError("lookup boom")
    content = {
        "body": "bridged reply",
        "msgtype": "m.text",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply"}},
    }

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.message",
                content=content,
            ),
        )

    assert payload == {
        "action": "send_event",
        "event_type": "m.room.message",
        "message": "Failed to resolve threaded Matrix message send target.",
        "room_id": ctx.room_id,
        "status": "error",
        "tool": "matrix_api",
    }
    ctx.client.room_send.assert_not_awaited()
    ctx.conversation_cache.notify_outbound_message.assert_not_called()


@pytest.mark.asyncio
async def test_matrix_api_get_state_happy_path() -> None:
    """get_state should return the fetched content."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_state_event.return_value = _state_response(
        content={"enabled": True},
        event_type="com.example.state",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_state", event_type="com.example.state"))

    assert payload == {
        "action": "get_state",
        "content": {"enabled": True},
        "event_type": "com.example.state",
        "found": True,
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_get_state_event.assert_awaited_once_with(
        room_id=ctx.room_id,
        event_type="com.example.state",
        state_key="",
    )


@pytest.mark.asyncio
async def test_matrix_api_put_state_happy_path() -> None:
    """put_state should write state and return the resulting event id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state:localhost"},
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.state",
                content={"enabled": True},
            ),
        )

    assert payload == {
        "action": "put_state",
        "event_id": "$state:localhost",
        "event_type": "com.example.state",
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_put_state.assert_awaited_once_with(
        room_id=ctx.room_id,
        event_type="com.example.state",
        state_key="",
        content={"enabled": True},
    )


@pytest.mark.asyncio
async def test_matrix_api_redact_happy_path() -> None:
    """Threaded redactions should call room_redact and notify threaded cache bookkeeping."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = "$thread:localhost"
    ctx.conversation_cache.get_event.return_value = _event_response(
        event_id="$target:localhost",
        room_id=ctx.room_id,
        content={"body": "threaded message", "msgtype": "m.text"},
    )
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
            ),
        )

    assert payload == {
        "action": "redact",
        "reason": "cleanup",
        "redaction_event_id": "$redaction:localhost",
        "room_id": ctx.room_id,
        "status": "ok",
        "target_event_id": "$target:localhost",
        "tool": "matrix_api",
    }
    ctx.client.room_redact.assert_awaited_once_with(
        room_id=ctx.room_id,
        event_id="$target:localhost",
        reason="cleanup",
    )
    ctx.conversation_cache.notify_outbound_redaction.assert_called_once_with(ctx.room_id, "$target:localhost")


@pytest.mark.asyncio
async def test_matrix_api_redact_delegates_thread_classification_to_shared_helper() -> None:
    """Redact should call the shared thread-membership helper instead of inlining cache policy."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_api.resolve_redaction_thread_impact_for_client",
            new=AsyncMock(return_value=MutationThreadImpact.threaded("$thread:localhost")),
        ) as mock_resolve_thread_impact,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
            ),
        )

    assert payload["status"] == "ok"
    mock_resolve_thread_impact.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_api_redact_room_level_target_does_not_notify_thread_bookkeeping() -> None:
    """Room-level redactions should not call threaded cache bookkeeping when the target is not in a thread."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = None
    ctx.conversation_cache.get_event.return_value = _event_response(
        event_id="$target:localhost",
        room_id=ctx.room_id,
        content={"body": "room message", "msgtype": "m.text"},
    )
    ctx.client.room_messages.return_value = nio.RoomMessagesResponse(
        room_id=ctx.room_id,
        chunk=[
            nio.RoomMessageText.from_dict(
                {
                    "content": {"body": "room message", "msgtype": "m.text"},
                    "event_id": "$target:localhost",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 123,
                    "room_id": ctx.room_id,
                    "type": "m.room.message",
                },
            ),
        ],
        start="",
        end=None,
    )
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
            ),
        )

    assert payload["status"] == "ok"
    ctx.conversation_cache.notify_outbound_redaction.assert_not_called()


@pytest.mark.asyncio
async def test_matrix_api_redact_errors_when_thread_classification_fails() -> None:
    """Redact should fail closed when thread classification cannot be resolved."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = None
    ctx.conversation_cache.get_event.side_effect = RuntimeError("lookup boom")

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
            ),
        )

    assert payload == {
        "action": "redact",
        "message": "Failed to resolve redaction target thread mapping.",
        "room_id": ctx.room_id,
        "status": "error",
        "target_event_id": "$target:localhost",
        "tool": "matrix_api",
    }
    ctx.client.room_redact.assert_not_awaited()
    ctx.conversation_cache.notify_outbound_redaction.assert_not_called()


@pytest.mark.asyncio
async def test_matrix_api_redact_thread_lookup_uses_conversation_cache_facade() -> None:
    """Threaded redaction detection should prefer conversation_cache over direct event_cache access."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = "$thread:localhost"
    ctx.conversation_cache.get_event.return_value = _event_response(
        event_id="$target:localhost",
        room_id=ctx.room_id,
        content={"body": "threaded message", "msgtype": "m.text"},
    )
    ctx.event_cache.get_thread_id_for_event.side_effect = AssertionError("unexpected direct event_cache lookup")

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
                dry_run=True,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["dry_run"] is True
    ctx.conversation_cache.get_thread_id_for_event.assert_awaited_once_with(
        ctx.room_id,
        "$target:localhost",
    )


@pytest.mark.asyncio
async def test_matrix_api_redact_dry_run_reaction_target_stays_room_level() -> None:
    """Reaction redactions should not require thread bookkeeping just because the reaction targets a thread."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = None
    ctx.conversation_cache.get_event.return_value = _event_response(
        event_id="$reaction:localhost",
        event_type="m.reaction",
        room_id=ctx.room_id,
        content={
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$thread-reply:localhost",
                "key": "👍",
            },
        },
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$reaction:localhost",
                reason="cleanup",
                dry_run=True,
            ),
        )

    assert payload["status"] == "ok"
    ctx.client.room_redact.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_redact_transitive_plain_reply_target_records_thread_bookkeeping() -> None:
    """Transitive-threaded redactions should reuse the shared resolver instead of cache-only lookup rows."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.side_effect = lambda room_id, event_id: (
        "$thread:localhost" if (room_id, event_id) == (ctx.room_id, "$thread-reply") else None
    )
    ctx.conversation_cache.get_event.side_effect = lambda room_id, event_id: _event_response(
        event_id=event_id,
        room_id=room_id,
        sender="@bridge:localhost",
        origin_server_ts=2000 if event_id == "$plain-one" else 3000,
        content={
            "body": "plain one" if event_id == "$plain-one" else "plain two",
            "msgtype": "m.text",
            "m.relates_to": {
                "m.in_reply_to": {
                    "event_id": "$thread-reply" if event_id == "$plain-one" else "$plain-one",
                },
            },
        },
    )
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$plain-two",
                reason="cleanup",
            ),
        )

    assert payload["status"] == "ok"
    ctx.conversation_cache.get_event.assert_any_await(ctx.room_id, "$plain-two")
    ctx.conversation_cache.notify_outbound_redaction.assert_called_once_with(ctx.room_id, "$plain-two")


@pytest.mark.asyncio
async def test_matrix_api_redact_ignores_cache_failure_after_successful_redact() -> None:
    """A successful threaded redact should delegate advisory bookkeeping through the cache facade."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_thread_id_for_event.return_value = "$thread:localhost"
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["redaction_event_id"] == "$redaction:localhost"
    ctx.conversation_cache.notify_outbound_redaction.assert_called_once_with(
        ctx.room_id,
        "$target:localhost",
    )


@pytest.mark.asyncio
async def test_matrix_api_get_event_happy_path() -> None:
    """get_event should return the raw Matrix event even when conversation cache is available."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_event.return_value = _event_response(
        room_id=ctx.room_id,
        content={"body": "edited view", "msgtype": "m.text"},
        origin_server_ts=999,
    )
    ctx.client.room_get_event.return_value = _event_response(
        room_id=ctx.room_id,
        content={"body": "raw body", "msgtype": "m.text"},
        origin_server_ts=123,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_event", event_id="$evt:localhost"))

    assert payload == {
        "action": "get_event",
        "event": {
            "content": {"body": "raw body", "msgtype": "m.text"},
            "event_id": "$evt:localhost",
            "origin_server_ts": 123,
            "room_id": ctx.room_id,
            "sender": "@alice:localhost",
            "type": "m.room.message",
        },
        "event_id": "$evt:localhost",
        "event_type": "m.room.message",
        "found": True,
        "origin_server_ts": 123,
        "room_id": ctx.room_id,
        "sender": "@alice:localhost",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.conversation_cache.get_event.assert_not_awaited()
    ctx.client.room_get_event.assert_awaited_once_with(ctx.room_id, "$evt:localhost")


@pytest.mark.asyncio
async def test_matrix_api_search_happy_path() -> None:
    """Search should call the Matrix search endpoint and normalize results."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.return_value = MatrixSearchResponse(
        count=1,
        next_batch=None,
        results=[
            _search_result(
                rank=12.5,
                result=_raw_event(
                    room_id=ctx.room_id,
                    content={"body": "Needle in a haystack", "msgtype": "m.text"},
                ),
            ),
        ],
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="search", search_term="needle"))

    assert payload == {
        "action": "search",
        "count": 1,
        "next_batch": None,
        "results": [
            {
                "rank": 12.5,
                "event_id": "$evt:localhost",
                "room_id": ctx.room_id,
                "sender": "@alice:localhost",
                "origin_server_ts": 123,
                "type": "m.room.message",
                "snippet": "Needle in a haystack",
            },
        ],
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client._send.assert_awaited_once()
    response_class, method, path, data = ctx.client._send.await_args.args
    assert response_class is MatrixSearchResponse
    assert method == "POST"
    assert path == "/_matrix/client/v3/search"
    assert json.loads(data) == {
        "search_categories": {
            "room_events": {
                "filter": {"rooms": [ctx.room_id], "limit": 10},
                "order_by": "rank",
                "search_term": "needle",
            },
        },
    }


@pytest.mark.asyncio
async def test_matrix_api_search_omits_keys_when_not_supplied() -> None:
    """Search should let the homeserver default keys apply when caller omits keys."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.return_value = MatrixSearchResponse(count=0, next_batch=None, results=[])

    with tool_runtime_context(ctx):
        await tool.matrix_api(action="search", search_term="needle")

    request_body = json.loads(ctx.client._send.await_args.args[3])
    assert "keys" not in request_body["search_categories"]["room_events"]


@pytest.mark.asyncio
async def test_matrix_api_search_explicit_keys_pass_through() -> None:
    """Search should pass through validated explicit keys when caller narrows them."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.return_value = MatrixSearchResponse(count=0, next_batch=None, results=[])

    with tool_runtime_context(ctx):
        await tool.matrix_api(
            action="search",
            search_term="meeting",
            keys=["content.name"],
        )

    request_body = json.loads(ctx.client._send.await_args.args[3])
    assert request_body["search_categories"]["room_events"]["keys"] == ["content.name"]


@pytest.mark.asyncio
async def test_matrix_api_search_snippet_falls_back_to_topic() -> None:
    """Search should use topic text for snippet when body is absent."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.return_value = MatrixSearchResponse(
        count=1,
        next_batch=None,
        results=[
            _search_result(
                result=_raw_event(
                    event_id="$topic:localhost",
                    event_type="m.room.topic",
                    room_id=ctx.room_id,
                    content={"topic": "Team Meeting coordination room"},
                ),
            ),
        ],
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="search",
                search_term="meeting",
                keys=["content.topic"],
            ),
        )

    assert payload["results"] == [
        {
            "rank": 1.0,
            "event_id": "$topic:localhost",
            "room_id": ctx.room_id,
            "sender": "@alice:localhost",
            "origin_server_ts": 123,
            "type": "m.room.topic",
            "snippet": "Team Meeting coordination room",
        },
    ]


@pytest.mark.asyncio
async def test_matrix_api_search_pagination_round_trips_next_batch() -> None:
    """Search should forward pagination tokens to the homeserver and return the next token."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.side_effect = [
        MatrixSearchResponse(
            count=3,
            next_batch="batch-1",
            results=[
                _search_result(
                    result=_raw_event(
                        event_id="$first:localhost",
                        room_id=ctx.room_id,
                        content={"body": "First page", "msgtype": "m.text"},
                    ),
                ),
            ],
        ),
        MatrixSearchResponse(
            count=3,
            next_batch=None,
            results=[
                _search_result(
                    result=_raw_event(
                        event_id="$second:localhost",
                        room_id=ctx.room_id,
                        content={"body": "Second page", "msgtype": "m.text"},
                    ),
                ),
            ],
        ),
    ]

    with tool_runtime_context(ctx):
        first_payload = json.loads(
            await tool.matrix_api(
                action="search",
                search_term="needle",
                limit=1,
            ),
        )
        second_payload = json.loads(
            await tool.matrix_api(
                action="search",
                search_term="needle",
                limit=1,
                next_batch=first_payload["next_batch"],
            ),
        )

    assert first_payload["count"] == 3
    assert first_payload["next_batch"] == "batch-1"
    assert first_payload["results"][0]["event_id"] == "$first:localhost"

    first_request_body = json.loads(ctx.client._send.await_args_list[0].args[3])
    assert first_request_body["search_categories"]["room_events"]["filter"]["limit"] == 1
    assert "next_batch" not in first_request_body["search_categories"]["room_events"]

    _, _, second_path, second_data = ctx.client._send.await_args_list[1].args
    assert second_path == nio.Api._build_path(["search"], {"next_batch": "batch-1"})
    assert "next_batch" not in json.loads(second_data)["search_categories"]["room_events"]
    assert second_payload["count"] == 3
    assert second_payload["next_batch"] is None
    assert second_payload["results"] == [
        {
            "rank": 1.0,
            "event_id": "$second:localhost",
            "room_id": ctx.room_id,
            "sender": "@alice:localhost",
            "origin_server_ts": 123,
            "type": "m.room.message",
            "snippet": "Second page",
        },
    ]


@pytest.mark.asyncio
async def test_matrix_api_search_event_context_preserves_profile_info() -> None:
    """Search should preserve profile_info when include_profile is requested."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.return_value = MatrixSearchResponse(
        count=1,
        next_batch=None,
        results=[
            _search_result(
                context={
                    "events_before": [],
                    "events_after": [],
                    "profile_info": {
                        "@alice:localhost": {
                            "displayname": "Alice",
                            "avatar_url": "mxc://localhost/alice",
                        },
                    },
                },
            ),
        ],
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="search",
                search_term="needle",
                event_context={"include_profile": True},
            ),
        )

    assert payload["results"][0]["context"] == {
        "events_before": [],
        "events_after": [],
        "profile_info": {
            "@alice:localhost": {
                "displayname": "Alice",
                "avatar_url": "mxc://localhost/alice",
            },
        },
    }


@pytest.mark.asyncio
async def test_matrix_api_search_room_scoping_uses_target_room_id() -> None:
    """Search should scope the Matrix request body to the explicitly requested room."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client._send.return_value = MatrixSearchResponse(
        count=1,
        next_batch=None,
        results=[_search_result(result=_raw_event(room_id="!other:localhost"))],
    )

    with (
        patch("mindroom.custom_tools.matrix_api.room_access_allowed", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="search",
                room_id="!other:localhost",
                search_term="needle",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["room_id"] == "!other:localhost"
    request_body = json.loads(ctx.client._send.await_args.args[3])
    assert request_body["search_categories"]["room_events"]["filter"]["rooms"] == ["!other:localhost"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    [
        ({"action": "search", "search_term": "needle", "order_by": "score"}, "order_by must be one of"),
        (
            {"action": "search", "search_term": "needle", "keys": ["content.body", "content.url"]},
            "keys entries must be one of",
        ),
        (
            {"action": "search", "search_term": "needle", "filter": {"rooms": ["!room:localhost"], "limit": 5}},
            "filter.limit is not supported; use the top-level limit parameter.",
        ),
        ({"action": "search", "search_term": "needle", "limit": -1}, "limit must be an integer between"),
        ({"action": "search", "search_term": "needle", "limit": 0}, "limit must be an integer between"),
        ({"action": "search", "search_term": "needle", "limit": 51}, "limit must be an integer between"),
        ({"action": "search"}, "search_term is required"),
    ],
)
async def test_matrix_api_search_validation_errors(kwargs: dict[str, object], expected_message: str) -> None:
    """Search should return structured errors for invalid search-specific parameters."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(**kwargs))

    assert payload["status"] == "error"
    assert payload["action"] == "search"
    assert payload["room_id"] == ctx.room_id
    assert expected_message in payload["message"]
    ctx.client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_search_filter_and_event_context_pass_through() -> None:
    """Search should preserve caller-supplied filter and event_context payloads."""
    tool = MatrixApiTools()
    ctx = _make_context()
    raw_filter = {
        "rooms": [ctx.room_id],
        "not_types": ["m.reaction"],
        "senders": ["@alice:localhost"],
    }
    raw_event_context = {
        "before_limit": 1,
        "after_limit": 1,
        "include_profile": True,
    }
    ctx.client._send.return_value = MatrixSearchResponse(
        count=1,
        next_batch=None,
        results=[
            _search_result(
                context={
                    "events_before": [
                        _raw_event(
                            event_id="$before:localhost",
                            room_id=ctx.room_id,
                            content={"body": "Before", "msgtype": "m.text"},
                        ),
                    ],
                    "events_after": [
                        _raw_event(
                            event_id="$after:localhost",
                            room_id=ctx.room_id,
                            content={"body": "After", "msgtype": "m.text"},
                        ),
                    ],
                    "start": "start-token",
                    "end": "end-token",
                    "profile_info": {
                        "@alice:localhost": {
                            "displayname": "Alice",
                            "avatar_url": "mxc://localhost/alice",
                        },
                    },
                },
            ),
        ],
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="search",
                search_term="needle",
                filter=raw_filter,
                event_context=raw_event_context,
            ),
        )

    request_body = json.loads(ctx.client._send.await_args.args[3])
    assert request_body["search_categories"]["room_events"]["filter"] == {
        **raw_filter,
        "limit": 10,
    }
    assert request_body["search_categories"]["room_events"]["event_context"] == raw_event_context
    assert payload["results"][0]["context"] == {
        "events_before": [
            {
                "event_id": "$before:localhost",
                "room_id": ctx.room_id,
                "sender": "@alice:localhost",
                "origin_server_ts": 123,
                "type": "m.room.message",
                "snippet": "Before",
            },
        ],
        "events_after": [
            {
                "event_id": "$after:localhost",
                "room_id": ctx.room_id,
                "sender": "@alice:localhost",
                "origin_server_ts": 123,
                "type": "m.room.message",
                "snippet": "After",
            },
        ],
        "start": "start-token",
        "end": "end-token",
        "profile_info": {
            "@alice:localhost": {
                "displayname": "Alice",
                "avatar_url": "mxc://localhost/alice",
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "search", "search_term": "needle", "dry_run": True},
        {"action": "search", "search_term": "needle", "allow_dangerous": True},
    ],
)
async def test_matrix_api_search_rejects_write_only_flags(kwargs: dict[str, object]) -> None:
    """Search should reject write-only flags that do not apply to read-only actions."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(**kwargs))

    assert payload == {
        "action": "search",
        "message": "dry_run/allow_dangerous not applicable to read-only search action",
        "room_id": ctx.room_id,
        "status": "error",
        "tool": "matrix_api",
    }
    ctx.client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_send_event_dry_run() -> None:
    """send_event dry runs should not call Matrix."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.event",
                content={"body": "preview"},
                dry_run=True,
            ),
        )

    assert payload == {
        "action": "send_event",
        "dry_run": True,
        "event_type": "com.example.event",
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
        "would_send": {
            "content": {"body": "preview"},
            "event_type": "com.example.event",
        },
    }
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_put_state_dry_run() -> None:
    """put_state dry runs should not call Matrix."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.state",
                content={"enabled": True},
                dry_run=True,
            ),
        )

    assert payload == {
        "action": "put_state",
        "dangerous": False,
        "dry_run": True,
        "event_type": "com.example.state",
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
        "would_put": {
            "content": {"enabled": True},
            "event_type": "com.example.state",
            "state_key": "",
        },
    }
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_put_state_blocks_room_create() -> None:
    """m.room.create should be hard-blocked before any Matrix write."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="m.room.create",
                content={"creator": "@user:localhost"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "put_state"
    assert "blocked" in payload["message"]
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["m.room.power_levels", "m.room.guest_access"])
async def test_matrix_api_put_state_requires_allow_dangerous(event_type: str) -> None:
    """Dangerous state writes should require explicit opt-in."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type=event_type,
                content={"users": {"@user:localhost": 100}},
            ),
        )

    assert payload["status"] == "error"
    assert payload["event_type"] == event_type
    assert payload["dangerous"] is True
    assert "allow_dangerous" in payload["message"]
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_put_state_allow_dangerous_succeeds() -> None:
    """Dangerous state writes should succeed when explicitly allowed."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$power:localhost"},
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="m.room.power_levels",
                content={"users": {"@user:localhost": 100}},
                allow_dangerous=True,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$power:localhost"
    ctx.client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_api_redact_dry_run() -> None:
    """Redact dry runs should not call Matrix."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="preview",
                dry_run=True,
            ),
        )

    assert payload == {
        "action": "redact",
        "dry_run": True,
        "reason": "preview",
        "room_id": ctx.room_id,
        "status": "ok",
        "target_event_id": "$target:localhost",
        "tool": "matrix_api",
        "would_redact": {
            "event_id": "$target:localhost",
            "reason": "preview",
        },
    }
    ctx.client.room_redact.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_get_state_maps_not_found_to_found_false() -> None:
    """M_NOT_FOUND state reads should return found:false instead of an error."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_state_event.return_value = _state_error(room_id=ctx.room_id)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_state", event_type="com.example.state"))

    assert payload == {
        "action": "get_state",
        "event_type": "com.example.state",
        "found": False,
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
    }


@pytest.mark.asyncio
async def test_matrix_api_get_state_returns_normalized_non_not_found_error() -> None:
    """Non-M_NOT_FOUND state read errors should return normalized error details."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_state_event.return_value = _state_error(
        message="forbidden",
        status_code="M_FORBIDDEN",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_state", event_type="com.example.state"))

    assert payload == {
        "action": "get_state",
        "event_type": "com.example.state",
        "message": "Failed to fetch Matrix state event.",
        "response": "RoomGetStateEventError: M_FORBIDDEN forbidden",
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "error",
        "status_code": "M_FORBIDDEN",
        "tool": "matrix_api",
    }


@pytest.mark.asyncio
async def test_matrix_api_get_event_maps_not_found_to_found_false() -> None:
    """M_NOT_FOUND event reads should return found:false instead of an error."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_event.return_value = nio.RoomGetEventError(
        "missing",
        status_code="M_NOT_FOUND",
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_event", event_id="$missing:localhost"))

    assert payload == {
        "action": "get_event",
        "event_id": "$missing:localhost",
        "found": False,
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.conversation_cache.get_event.assert_not_awaited()
    ctx.client.room_get_event.assert_awaited_once_with(ctx.room_id, "$missing:localhost")


@pytest.mark.asyncio
async def test_matrix_api_send_event_blocks_redaction_type() -> None:
    """send_event should reject redaction events so they use the dedicated redact path."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.redaction",
                content={"redacts": "$target:localhost"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send_event"
    assert payload["event_type"] == "m.room.redaction"
    assert "redact" in payload["message"]
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_send_event_blocks_dangerous_state_types() -> None:
    """send_event should reject dangerous state event types instead of bypassing put_state guards."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.encryption",
                content={"algorithm": "m.megolm.v1.aes-sha2"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send_event"
    assert payload["dangerous"] is True
    assert "put_state" in payload["message"]
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_rate_limit_uses_weighted_budget() -> None:
    """Real writes should consume the shared 8-unit budget with action weights."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state:localhost"},
        room_id=ctx.room_id,
    )
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        first = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.one",
                content={"value": 1},
            ),
        )
        second = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.two",
                content={"value": 2},
            ),
        )
        third = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.three",
                content={"value": 3},
            ),
        )
        fourth = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.four",
                content={"value": 4},
            ),
        )
        fifth = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.extra",
                content={"value": 5},
            ),
        )

    assert [payload["status"] for payload in (first, second, third, fourth)] == ["ok", "ok", "ok", "ok"]
    assert fifth["status"] == "error"
    assert "Rate limit exceeded" in fifth["message"]
    assert ctx.client.room_put_state.await_count == 4
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("room_id", "expected_message"),
    [
        (123, "non-empty Matrix room ID string"),
        (False, "non-empty Matrix room ID string"),
        ("   ", "non-empty Matrix room ID string"),
        ("#lobby:localhost", "!room:server form"),
        ("lobby", "!room:server form"),
    ],
)
async def test_matrix_api_rejects_invalid_room_id(room_id: object, expected_message: str) -> None:
    """Explicit room_id values must be canonical Matrix room IDs."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                room_id=room_id,
                event_type="com.example.event",
                content={"body": "x"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send_event"
    assert expected_message in payload["message"]
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        (
            {
                "action": "send_event",
                "event_type": "com.example.event",
                "content": {"body": "preview"},
                "dry_run": "false",
            },
            "dry_run",
        ),
        (
            {
                "action": "put_state",
                "event_type": "m.room.power_levels",
                "content": {"users": {"@user:localhost": 100}},
                "allow_dangerous": "false",
            },
            "allow_dangerous",
        ),
    ],
)
async def test_matrix_api_rejects_non_bool_flags(kwargs: dict[str, object], field_name: str) -> None:
    """Boolean flags must reject stringified truthy values instead of using Python truthiness."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(**kwargs))

    assert payload["status"] == "error"
    assert payload["action"] == kwargs["action"]
    assert field_name in payload["message"]
    ctx.client.room_send.assert_not_awaited()
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("send_event", {"event_type": "com.example.event", "content": {"body": "x"}}),
        ("get_state", {"event_type": "com.example.state"}),
        ("put_state", {"event_type": "com.example.state", "content": {"enabled": True}}),
        ("redact", {"event_id": "$evt:localhost"}),
        ("get_event", {"event_id": "$evt:localhost"}),
    ],
)
async def test_matrix_api_cross_room_access_is_denied(action: str, kwargs: dict[str, object]) -> None:
    """Every action should enforce room access checks before touching another room."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action=action, room_id="!other:localhost", **kwargs))

    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert "Not authorized" in payload["message"]
    ctx.client.room_send.assert_not_awaited()
    ctx.client.room_get_state_event.assert_not_awaited()
    ctx.client.room_put_state.assert_not_awaited()
    ctx.client.room_redact.assert_not_awaited()
    ctx.client.room_get_event.assert_not_awaited()
    ctx.conversation_cache.get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kwargs", "client_attr", "response"),
    [
        (
            "send_event",
            {"event_type": "com.example.event", "content": {"body": "x"}},
            "room_send",
            nio.RoomSendResponse(event_id="$send:localhost", room_id="!other:localhost"),
        ),
        (
            "get_state",
            {"event_type": "com.example.state"},
            "room_get_state_event",
            _state_response(
                content={"enabled": True},
                event_type="com.example.state",
                room_id="!other:localhost",
            ),
        ),
        (
            "put_state",
            {"event_type": "com.example.state", "content": {"enabled": True}},
            "room_put_state",
            nio.RoomPutStateResponse.from_dict(
                {"event_id": "$state:localhost"},
                room_id="!other:localhost",
            ),
        ),
        (
            "redact",
            {"event_id": "$evt:localhost"},
            "room_redact",
            nio.RoomRedactResponse(event_id="$redaction:localhost", room_id="!other:localhost"),
        ),
    ],
)
async def test_matrix_api_cross_room_access_allowed_uses_target_room_id(
    action: str,
    kwargs: dict[str, object],
    client_attr: str,
    response: object,
) -> None:
    """Authorized cross-room actions should dispatch using the requested room id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    getattr(ctx.client, client_attr).return_value = response

    with (
        patch("mindroom.custom_tools.matrix_api.room_access_allowed", return_value=True),
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_api(action=action, room_id="!other:localhost", **kwargs))

    assert payload["status"] == "ok"
    assert payload["room_id"] == "!other:localhost"
    if action == "send_event":
        ctx.client.room_send.assert_awaited_once_with(
            room_id="!other:localhost",
            message_type="com.example.event",
            content={"body": "x"},
        )
    elif action == "get_state":
        ctx.client.room_get_state_event.assert_awaited_once_with(
            room_id="!other:localhost",
            event_type="com.example.state",
            state_key="",
        )
    elif action == "put_state":
        ctx.client.room_put_state.assert_awaited_once_with(
            room_id="!other:localhost",
            event_type="com.example.state",
            state_key="",
            content={"enabled": True},
        )
    elif action == "redact":
        ctx.client.room_redact.assert_awaited_once_with(
            room_id="!other:localhost",
            event_id="$evt:localhost",
            reason=None,
        )


@pytest.mark.asyncio
async def test_matrix_api_cross_room_get_event_uses_target_room_id() -> None:
    """Authorized cross-room get_event should fetch the raw Matrix event for that room."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_event.return_value = _event_response(room_id="!other:localhost")

    with (
        patch("mindroom.custom_tools.matrix_api.room_access_allowed", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="get_event",
                room_id="!other:localhost",
                event_id="$evt:localhost",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["room_id"] == "!other:localhost"
    ctx.conversation_cache.get_event.assert_not_awaited()
    ctx.client.room_get_event.assert_awaited_once_with("!other:localhost", "$evt:localhost")


@pytest.mark.asyncio
async def test_matrix_api_rejects_invalid_action() -> None:
    """Unsupported actions should return a clear error listing valid options."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="delete_room"))

    assert payload["status"] == "error"
    assert payload["action"] == "delete_room"
    assert "send_event" in payload["message"]
    assert "get_event" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_event", "put_state"])
async def test_matrix_api_rejects_non_dict_content(action: str) -> None:
    """Write actions should require dict content payloads."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action=action,
                event_type="com.example.event",
                content="hello",
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "dict" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_event", "get_state", "put_state"])
async def test_matrix_api_rejects_empty_event_type(action: str) -> None:
    """Actions that require event_type should reject blank values."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action=action,
                event_type="   ",
                content={"body": "x"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "event_type" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["redact", "get_event"])
async def test_matrix_api_rejects_empty_event_id(action: str) -> None:
    """Actions that require event_id should reject blank values."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action=action, event_id="   "))

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "event_id" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["get_state", "put_state"])
async def test_matrix_api_rejects_non_string_state_key(action: str) -> None:
    """State actions should require string state keys."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action=action,
                event_type="com.example.state",
                state_key=123,
                content={"enabled": True},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "state_key" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_api_preserves_retry_after_ms_in_error_output() -> None:
    """Normalized Matrix errors should keep retry-after details for rate-limited calls."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendError(
        "rate limited",
        status_code="M_LIMIT_EXCEEDED",
        retry_after_ms=5000,
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.event",
                content={"body": "hello"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["status_code"] == "M_LIMIT_EXCEEDED"
    assert payload["response"] == "RoomSendError: M_LIMIT_EXCEEDED rate limited - retry after 5000ms"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kwargs", "expected_summary"),
    [
        (
            "send_event",
            {
                "event_type": "com.example.event",
                "content": {"safe": "value", "secret": "dont-log-me"},
            },
            {"content_bytes", "content_keys"},
        ),
        (
            "put_state",
            {
                "event_type": "com.example.state",
                "content": {"enabled": True, "secret": "dont-log-me"},
            },
            {"content_bytes", "content_keys"},
        ),
        (
            "redact",
            {
                "event_id": "$target:localhost",
                "reason": "cleanup",
            },
            set(),
        ),
    ],
)
async def test_matrix_api_audit_logs_real_writes(
    action: str,
    kwargs: dict[str, object],
    expected_summary: set[str],
) -> None:
    """Every real write should emit one summarized warning-level audit record."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendResponse(event_id="$send:localhost", room_id=ctx.room_id)
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state:localhost"},
        room_id=ctx.room_id,
    )
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning") as mock_warning,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_api(action=action, **kwargs))

    assert payload["status"] == "ok"
    mock_warning.assert_called_once()
    assert mock_warning.call_args.args[0] == "matrix_api_write_audit"
    audit_payload = mock_warning.call_args.kwargs
    assert audit_payload["action"] == action
    assert audit_payload["agent"] == ctx.agent_name
    assert audit_payload["user_id"] == ctx.requester_id
    assert audit_payload["room_id"] == ctx.room_id
    assert audit_payload["status"] == "ok"
    assert set(audit_payload).issuperset({"action", "agent", "user_id", "room_id", "status"})
    assert set(audit_payload).issuperset(expected_summary)
    assert "dont-log-me" not in repr(audit_payload)
