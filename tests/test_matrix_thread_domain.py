"""Direct tests for Matrix thread resolution and bookkeeping helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import resolve_canonical_tool_thread_target
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import (
    MutationThreadImpact,
    event_requires_thread_bookkeeping,
    redaction_requires_thread_bookkeeping,
    resolve_event_thread_impact_for_client,
    resolve_redaction_thread_impact_for_client,
)
from mindroom.matrix.thread_membership import (
    ThreadResolution,
    map_backed_thread_membership_access,
    resolve_event_thread_membership,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths


def _message_event_info(content: dict[str, object]) -> EventInfo:
    return EventInfo.from_event(
        {
            "type": "m.room.message",
            "content": content,
        },
    )


def _tool_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
) -> ToolRuntimeContext:
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        reply_to_event_id=None,
        storage_path=runtime_root,
    )


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_promotes_plain_reply_transitively() -> None:
    """Plain replies should inherit thread membership from a threaded ancestor."""
    event_infos = {
        "$plain-two:localhost": _message_event_info(
            {
                "body": "plain two",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-one:localhost"}},
            },
        ),
        "$plain-one:localhost": _message_event_info(
            {
                "body": "plain one",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
            },
        ),
        "$thread-reply:localhost": _message_event_info(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root:localhost",
                },
            },
        ),
    }

    resolution = await resolve_event_thread_membership(
        "!room:localhost",
        event_infos["$plain-two:localhost"],
        access=map_backed_thread_membership_access(
            event_infos=event_infos,
            resolved_thread_ids={},
        ),
    )

    assert resolution == ThreadResolution.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_proves_current_root_when_allowed() -> None:
    """A root event should normalize to itself only when descendants prove it is threaded."""
    event_infos = {
        "$thread-root:localhost": _message_event_info(
            {
                "body": "root",
                "msgtype": "m.text",
            },
        ),
        "$thread-reply:localhost": _message_event_info(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root:localhost",
                },
            },
        ),
    }

    resolution = await resolve_event_thread_membership(
        "!room:localhost",
        event_infos["$thread-root:localhost"],
        event_id="$thread-root:localhost",
        allow_current_root=True,
        access=map_backed_thread_membership_access(
            event_infos=event_infos,
            resolved_thread_ids={},
        ),
    )

    assert resolution == ThreadResolution.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_event_requires_thread_bookkeeping_uses_shared_thread_resolution() -> None:
    """Outbound plain replies to threaded events should be classified as thread-scoped."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_thread_id_for_event.side_effect = (
        lambda room_id, event_id: "$thread-root:localhost"
        if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost")
        else None
    )

    requires_thread_bookkeeping = await event_requires_thread_bookkeeping(
        client,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
        conversation_cache=conversation_cache,
    )

    assert requires_thread_bookkeeping is True


@pytest.mark.asyncio
async def test_resolve_event_thread_impact_for_client_returns_threaded_impact() -> None:
    """Client-side message classification should expose the canonical threaded impact, not only a bool."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_thread_id_for_event.side_effect = (
        lambda room_id, event_id: "$thread-root:localhost"
        if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost")
        else None
    )

    impact = await resolve_event_thread_impact_for_client(
        client,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
        conversation_cache=conversation_cache,
    )

    assert impact == MutationThreadImpact.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_redaction_requires_thread_bookkeeping_ignores_reactions() -> None:
    """Reaction redactions should stay room-level even when the reaction targets a thread."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_event.return_value = nio.RoomGetEventResponse.from_dict(
        {
            "event_id": "$reaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$thread-reply:localhost",
                    "key": "👍",
                },
            },
        },
    )

    requires_thread_bookkeeping = await redaction_requires_thread_bookkeeping(
        client,
        "!room:localhost",
        event_id="$reaction:localhost",
        conversation_cache=conversation_cache,
    )

    assert requires_thread_bookkeeping is False


@pytest.mark.asyncio
async def test_resolve_redaction_thread_impact_for_client_returns_room_level_for_reactions() -> None:
    """Client-side redaction classification should expose room-level reaction handling directly."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_event.return_value = nio.RoomGetEventResponse.from_dict(
        {
            "event_id": "$reaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$thread-reply:localhost",
                    "key": "👍",
                },
            },
        },
    )

    impact = await resolve_redaction_thread_impact_for_client(
        client,
        "!room:localhost",
        event_id="$reaction:localhost",
        conversation_cache=conversation_cache,
    )

    assert impact == MutationThreadImpact.room_level()


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_uses_context_thread() -> None:
    """Tool-facing normalization should share one helper that applies context fallback before canonicalization."""
    context = _tool_context(thread_id="$ctx-thread:localhost")
    normalize_thread_id = AsyncMock(return_value="$thread-root:localhost")

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        normalize_thread_id=normalize_thread_id,
    )

    assert target.requested_thread_id == "$ctx-thread:localhost"
    assert target.canonical_thread_id == "$thread-root:localhost"
    assert target.error is None
    normalize_thread_id.assert_awaited_once_with(context.room_id, "$ctx-thread:localhost")


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_requires_thread_context() -> None:
    """Tool-facing normalization should return the shared missing-thread error when no target is available."""
    context = _tool_context(thread_id=None)
    normalize_thread_id = AsyncMock(return_value="$thread-root:localhost")

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        normalize_thread_id=normalize_thread_id,
    )

    assert target.requested_thread_id is None
    assert target.canonical_thread_id is None
    assert target.error == "thread_id is required when no active thread context is available for the target room."
    normalize_thread_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_fail_closed_preserves_requested_thread_id() -> None:
    """Fail-closed tool normalization should keep the resolved request target for error reporting."""
    context = _tool_context(thread_id="$ctx-thread:localhost")
    normalize_thread_id = AsyncMock(side_effect=TimeoutError("timed out"))

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        normalize_thread_id=normalize_thread_id,
        fail_closed_on_normalization_error=True,
    )

    assert target.requested_thread_id == "$ctx-thread:localhost"
    assert target.canonical_thread_id is None
    assert target.error == "Failed to resolve a canonical thread root for the target event."
    normalize_thread_id.assert_awaited_once_with(context.room_id, "$ctx-thread:localhost")
