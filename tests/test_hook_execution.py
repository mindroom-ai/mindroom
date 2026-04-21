"""Tests for hook execution helpers and runtime integration."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    BeforeResponseContext,
    CustomEventContext,
    HookRegistry,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ResponseDraft,
    build_hook_matrix_admin,
    hook,
)
from mindroom.hooks.execution import emit, emit_collect, emit_transform
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    emit_custom_event,
    get_plugin_state_root,
    tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["!room:localhost"]),
            },
        ),
        runtime_paths,
    )


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(path=f"./plugins/{name}"),
            "plugin_order": 0,
        },
    )()


def _envelope(*, agent_name: str = "code", body: str = "hello") -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$event",
        room_id="!room:localhost",
        target=MessageTarget.resolve("!room:localhost", None, "$event"),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
        source_kind="message",
    )


def _message_received_context(tmp_path: Path) -> MessageReceivedContext:
    config = _config(tmp_path)
    return MessageReceivedContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hooks").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-received",
        envelope=_envelope(),
    )


def _message_enrich_context(tmp_path: Path) -> MessageEnrichContext:
    config = _config(tmp_path)
    return MessageEnrichContext(
        event_name=EVENT_MESSAGE_ENRICH,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hooks").bind(event_name=EVENT_MESSAGE_ENRICH),
        correlation_id="corr-enrich",
        envelope=_envelope(body="hello"),
        target_entity_name="code",
        target_member_names=None,
    )


def _before_response_context(tmp_path: Path) -> BeforeResponseContext:
    config = _config(tmp_path)
    return BeforeResponseContext(
        event_name=EVENT_MESSAGE_BEFORE_RESPONSE,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hooks").bind(event_name=EVENT_MESSAGE_BEFORE_RESPONSE),
        correlation_id="corr-before",
        draft=ResponseDraft(
            response_text="start",
            response_kind="ai",
            tool_trace=None,
            extra_content=None,
            envelope=_envelope(),
        ),
    )


def _custom_event_context(tmp_path: Path) -> CustomEventContext:
    config = _config(tmp_path)
    return CustomEventContext(
        event_name="todo:item_added",
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hooks").bind(event_name="todo:item_added"),
        correlation_id="corr-custom",
        payload={"depth": 0},
        source_plugin="todo",
        room_id="!room:localhost",
        thread_id=None,
        sender_id="@user:localhost",
    )


@pytest.mark.asyncio
async def test_emit_observer_continues_after_failure_and_propagates_suppression(tmp_path: Path) -> None:
    """Observer failures should not stop later hooks or lose suppression changes."""
    seen: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED, priority=10)
    async def failing_hook(ctx: MessageReceivedContext) -> None:
        del ctx
        seen.append("failing")
        raise RuntimeError

    @hook(EVENT_MESSAGE_RECEIVED, priority=20)
    async def suppressing_hook(ctx: MessageReceivedContext) -> None:
        seen.append("suppressing")
        ctx.suppress = True

    registry = HookRegistry.from_plugins([_plugin("observer-plugin", [failing_hook, suppressing_hook])])
    context = _message_received_context(tmp_path)

    await emit(registry, EVENT_MESSAGE_RECEIVED, context)

    assert seen == ["failing", "suppressing"]
    assert context.suppress is True


@pytest.mark.asyncio
async def test_emit_collect_merges_in_hook_order_and_isolates_per_hook_state(tmp_path: Path) -> None:
    """Collectors should run concurrently but merge results in registry order."""

    @hook(EVENT_MESSAGE_ENRICH, priority=10)
    async def slow_first(ctx: MessageEnrichContext) -> None:
        await asyncio.sleep(0.02)
        ctx.add_metadata("first", "slow")

    @hook(EVENT_MESSAGE_ENRICH, priority=20)
    async def fast_second(ctx: MessageEnrichContext) -> None:
        ctx.add_metadata("second", "fast")

    registry = HookRegistry.from_plugins([_plugin("collector-plugin", [slow_first, fast_second])])
    context = _message_enrich_context(tmp_path)

    items = await emit_collect(registry, EVENT_MESSAGE_ENRICH, context)

    assert [item.key for item in items] == ["first", "second"]
    assert context._items == []


@pytest.mark.asyncio
async def test_emit_transform_keeps_previous_draft_when_one_hook_fails(tmp_path: Path) -> None:
    """Transformer failures should not discard changes from earlier hooks."""

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE, priority=10)
    async def append_one(ctx: BeforeResponseContext) -> None:
        ctx.draft.response_text += " one"

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE, priority=20)
    async def fail_midway(ctx: BeforeResponseContext) -> None:
        del ctx
        raise RuntimeError

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE, priority=30)
    async def append_three(ctx: BeforeResponseContext) -> ResponseDraft:
        return replace(ctx.draft, response_text=f"{ctx.draft.response_text} three")

    registry = HookRegistry.from_plugins([_plugin("transform-plugin", [append_one, fail_midway, append_three])])

    result = await emit_transform(registry, EVENT_MESSAGE_BEFORE_RESPONSE, _before_response_context(tmp_path))

    assert result.response_text == "start one three"


@pytest.mark.asyncio
async def test_emit_recursion_guard_drops_nested_custom_events_after_depth_three(tmp_path: Path) -> None:
    """Nested custom events should stop once the recursion depth guard is hit."""
    seen_depths: list[int] = []

    @hook("todo:item_added")
    async def recursive_hook(ctx: CustomEventContext) -> None:
        depth = int(ctx.payload["depth"])
        seen_depths.append(depth)
        if depth < 5:
            await emit(
                registry,
                "todo:item_added",
                replace(ctx, payload={"depth": depth + 1}),
            )

    registry = HookRegistry.from_plugins([_plugin("todo-plugin", [recursive_hook])])

    await emit(registry, "todo:item_added", _custom_event_context(tmp_path))

    assert seen_depths == [0, 1, 2]


@pytest.mark.asyncio
async def test_emit_custom_event_uses_runtime_context_and_plugin_state_root(tmp_path: Path) -> None:
    """Tool-side custom events should flow through the hook registry and shared storage root."""
    seen: list[tuple[str, str, Path, dict[str, object] | None, bool, str | None]] = []

    @hook("todo:item_added")
    async def audit_hook(ctx: CustomEventContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$thread",
            {"tags": {"wip": True}},
        )
        assert ctx.matrix_admin is not None
        alias_room_id = await ctx.matrix_admin.resolve_alias("#todo-room:localhost")
        seen.append(
            (ctx.payload["item_id"], ctx.source_plugin, ctx.state_root, query_result, put_result, alias_room_id),
        )

    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    registry = HookRegistry.from_plugins([_plugin("todo-plugin", [audit_hook])])
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.room_get_state_event.return_value = SimpleNamespace(content={"name": "Lobby"})
    client.room_put_state.return_value = object()
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#todo-room:localhost",
        room_id="!todo-room:localhost",
        servers=["localhost"],
    )
    tool_context = ToolRuntimeContext(
        agent_name="code",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id="$event",
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        hook_registry=registry,
        correlation_id="corr-tool",
        matrix_admin=build_hook_matrix_admin(client, runtime_paths),
    )

    with tool_runtime_context(tool_context):
        await emit_custom_event("todo", "todo:item_added", {"item_id": "123"})

    expected_root = get_plugin_state_root("todo-plugin", runtime_paths=runtime_paths)
    assert seen == [("123", "todo", expected_root, {"name": "Lobby"}, True, "!todo-room:localhost")]
    assert expected_root.is_dir()
    client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"wip": True}},
        state_key="$thread",
    )
    client.room_resolve_alias.assert_awaited_once_with("#todo-room:localhost")
