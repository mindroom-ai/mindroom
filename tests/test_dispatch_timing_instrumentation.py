"""Tests for opt-in tool-dispatch timing instrumentation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest
from agno.models.response import ToolExecution
from agno.run.agent import ToolCallStartedEvent

from mindroom import ai as ai_module
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.matrix.cache.thread_writes import ThreadOutboundWritePolicy
from mindroom.matrix.client_delivery import send_message_result
from mindroom.media_inputs import MediaInputs
from mindroom.streaming_delivery import _queue_delivery_request
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _record_timing_event(
    events: list[tuple[str, dict[str, object]]],
    event_name: str,
    **event_data: object,
) -> None:
    events.append((event_name, event_data))


@pytest.mark.asyncio
async def test_stream_processing_marks_tool_call_started() -> None:
    """AI stream processing should mark the handoff from model stream to tool dispatch."""
    timing_events: list[tuple[str, dict[str, object]]] = []

    async def stream() -> AsyncIterator[object]:
        yield ToolCallStartedEvent(
            tool=ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "date +%s.%N"},
            ),
        )

    with patch(
        "mindroom.ai.emit_timing_event",
        side_effect=lambda *args, **kwargs: _record_timing_event(timing_events, *args, **kwargs),
        create=True,
    ):
        chunks = [
            chunk
            async for chunk in ai_module._process_stream_events(
                stream(),
                state=ai_module._StreamingAttemptState(),
                show_tool_calls=True,
                agent_name="code",
                media_inputs=MediaInputs(),
                retried_without_inline_media=False,
                timing_scope="test",
            )
        ]

    assert len(chunks) == 1
    assert timing_events == [
        (
            "Dispatch tool-call timing",
            {
                "phase": "agno_tool_call_started",
                "agent_name": "code",
                "tool_name": "run_shell_command",
                "tool_call_id": None,
                "show_tool_calls": True,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_queue_delivery_request_marks_enqueued_delivery() -> None:
    """Streaming delivery requests should expose when tool-trace Matrix delivery is queued."""
    timing_events: list[tuple[str, dict[str, object]]] = []
    delivery_queue: asyncio.Queue[object | None] = asyncio.Queue()

    with patch(
        "mindroom.streaming_delivery.emit_timing_event",
        side_effect=lambda *args, **kwargs: _record_timing_event(timing_events, *args, **kwargs),
        create=True,
    ):
        capture_completion = _queue_delivery_request(
            delivery_queue,
            progress_hint=True,
            boundary_refresh=True,
            wait_for_capture=True,
        )

    assert capture_completion is not None
    capture_completion.set_result(None)
    request = delivery_queue.get_nowait()
    assert request is not None
    assert timing_events == [
        (
            "Dispatch tool delivery timing",
            {
                "phase": "queued",
                "queue_size": 1,
                "progress_hint": True,
                "force_refresh": False,
                "boundary_refresh": True,
                "phase_boundary_flush": False,
                "allow_empty_progress": False,
                "wait_for_capture": True,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_send_message_result_marks_prepare_and_send_phases() -> None:
    """Matrix sends should mark large-message prep and room-send boundaries."""
    timing_events: list[tuple[str, dict[str, object]]] = []
    client = AsyncMock(spec=nio.AsyncClient)
    room = Mock()
    room.encrypted = False
    client.rooms = {"!room:localhost": room}
    client.room_send.return_value = nio.RoomSendResponse("$evt:localhost", "!room:localhost")
    prepared_content = {"body": "hello", "msgtype": "m.text"}

    with (
        patch("mindroom.matrix.client_delivery.prepare_large_message", new=AsyncMock(return_value=prepared_content)),
        patch(
            "mindroom.matrix.client_delivery.emit_timing_event",
            side_effect=lambda *args, **kwargs: _record_timing_event(timing_events, *args, **kwargs),
            create=True,
        ),
    ):
        result = await send_message_result(
            client,
            "!room:localhost",
            {"body": "hello", "msgtype": "m.text"},
            config=Config(),
        )

    assert result is not None
    assert result.event_id == "$evt:localhost"
    assert timing_events == [
        (
            "Matrix send timing",
            {"phase": "prepare_start", "room_id": "!room:localhost", "message_type": "m.room.message"},
        ),
        (
            "Matrix send timing",
            {"phase": "prepare_finish", "room_id": "!room:localhost", "message_type": "m.room.message"},
        ),
        (
            "Matrix send timing",
            {
                "phase": "send_start",
                "room_id": "!room:localhost",
                "message_type": "m.room.message",
                "cache_bypass": False,
            },
        ),
        (
            "Matrix send timing",
            {
                "phase": "send_finish",
                "room_id": "!room:localhost",
                "message_type": "m.room.message",
                "cache_bypass": False,
                "outcome": "sent",
                "event_id": "$evt:localhost",
            },
        ),
    ]


def test_notify_outbound_message_marks_cache_schedule() -> None:
    """Outbound Matrix cache notifications should mark the cache-barrier scheduling point."""
    timing_events: list[tuple[str, dict[str, object]]] = []

    class CacheOps:
        def __init__(self) -> None:
            self.logger = Mock()
            self.thread_updates: list[tuple[str, str, dict[str, object]]] = []

        def cache_runtime_available(self) -> bool:
            return True

        def queue_thread_cache_update(
            self,
            room_id: str,
            thread_id: str,
            update_coro_factory: object,
            **kwargs: object,
        ) -> object:
            del update_coro_factory
            self.thread_updates.append((room_id, thread_id, dict(kwargs)))
            return object()

        def queue_room_cache_update(
            self,
            room_id: str,
            update_coro_factory: object,
            **kwargs: object,
        ) -> object:
            del update_coro_factory
            msg = f"Unexpected room cache update for {room_id}: {kwargs}"
            raise AssertionError(msg)

    cache_ops = CacheOps()
    policy = ThreadOutboundWritePolicy(
        resolver=Mock(),
        cache_ops=cache_ops,
        require_client=lambda: SimpleNamespace(user_id="@mindroom_code:localhost"),
    )

    with patch(
        "mindroom.matrix.cache.thread_writes.emit_timing_event",
        side_effect=lambda *args, **kwargs: _record_timing_event(timing_events, *args, **kwargs),
    ):
        policy.notify_outbound_message(
            "!room:localhost",
            "$tool_use",
            {
                "body": "tool started",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread",
                    "is_falling_back": True,
                },
            },
        )

    assert cache_ops.thread_updates
    assert timing_events == [
        (
            "Event cache outbound schedule timing",
            {
                "operation": "matrix_cache_notify_outbound_event",
                "barrier_kind": "thread",
                "room_id": "!room:localhost",
                "thread_id": "$thread",
                "event_id": "$tool_use",
                "event_type": "m.room.message",
                "is_edit": False,
                "is_reaction": False,
                "has_coalesce_key": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_tool_hook_bridge_marks_hook_and_tool_entry() -> None:
    """Agno tool-hook bridging should mark before-hook timing and actual tool entry."""
    timing_events: list[tuple[str, dict[str, object]]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        assert ctx.tool_name == "echo"

    registry = HookRegistry.from_plugins(
        [
            SimpleNamespace(
                name="tool-policy",
                discovered_hooks=(before,),
                entry_config=PluginEntryConfig(path="./plugins/tool-policy", settings={}),
                plugin_order=0,
            ),
        ],
    )
    bridge = build_tool_hook_bridge(registry, agent_name="code")

    async def echo(text: str) -> str:
        return text.upper()

    with patch(
        "mindroom.tool_system.tool_hooks.emit_timing_event",
        side_effect=lambda *args, **kwargs: _record_timing_event(timing_events, *args, **kwargs),
        create=True,
    ):
        result = await bridge("echo", echo, {"text": "hello"})

    assert result == "HELLO"
    assert [
        (event_name, event_data["phase"])
        for event_name, event_data in timing_events
        if event_name == "Tool hook dispatch timing"
    ] == [
        ("Tool hook dispatch timing", "bridge_entry"),
        ("Tool hook dispatch timing", "before_hooks_start"),
        ("Tool hook dispatch timing", "before_hooks_finish"),
        ("Tool hook dispatch timing", "tool_entry"),
    ]
    before_finish = timing_events[2][1]
    assert before_finish["tool_name"] == "echo"
    assert before_finish["agent_name"] == "code"
    assert before_finish["declined"] is False
    assert isinstance(before_finish["duration_ms"], float)
