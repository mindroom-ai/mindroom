"""Tests for hook-driven Matrix message sending."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, _MessageContext
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_MESSAGE_RECEIVED,
    AgentLifecycleContext,
    HookContext,
    HookMessageSender,
    HookRegistry,
    MessageEnvelope,
    MessageReceivedContext,
    hook,
)
from mindroom.hooks.execution import emit
from mindroom.hooks.sender import HookMessageSender as SenderAlias
from mindroom.logging_config import get_logger
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )


def test_hooks_package_reexports_hook_message_sender() -> None:
    """The public hooks package should keep exporting HookMessageSender."""
    assert HookMessageSender is SenderAlias


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


def _message_received_context(tmp_path: Path, *, plugin_name: str = "") -> MessageReceivedContext:
    config = _config(tmp_path)
    return MessageReceivedContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name=plugin_name,
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hook_sender").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-hook-send",
        envelope=MessageEnvelope(
            source_event_id="$event",
            room_id="!room:localhost",
            thread_id=None,
            resolved_thread_id="$event",
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="code",
            source_kind="message",
        ),
    )


def _message_received_context_with_sender(
    tmp_path: Path,
    sender: HookMessageSender | None,
    *,
    plugin_name: str = "",
) -> MessageReceivedContext:
    context = _message_received_context(tmp_path, plugin_name=plugin_name)
    context.message_sender = sender
    return context


def _hook_bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    return AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="router",
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )


def _agent_bot(tmp_path: Path, *, agent_name: str = "code") -> AgentBot:
    config = _config(tmp_path)
    return AgentBot(
        agent_user=AgentMatrixUser(
            agent_name=agent_name,
            password=TEST_PASSWORD,
            display_name=agent_name.title(),
            user_id=f"@mindroom_{agent_name}:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )


def _dispatch_context(bot: AgentBot) -> _MessageContext:
    """Return a typed message context for dispatch-path tests."""
    return _MessageContext(
        am_i_mentioned=True,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[bot.matrix_id],
        has_non_agent_mentions=False,
    )


@pytest.mark.asyncio
async def test_hook_context_send_message_without_bound_sender_returns_none(tmp_path: Path) -> None:
    """HookContext.send_message should fail closed when no sender is bound to the context."""
    config = _config(tmp_path)
    logger = MagicMock()
    context = HookContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="test-plugin",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=logger,
        correlation_id="corr-missing-sender",
    )

    result = await context.send_message("!room:localhost", "hello")

    assert result is None
    logger.warning.assert_called_once_with("send_message called but no sender registered")


@pytest.mark.asyncio
async def test_hook_context_send_message_supports_multiple_hook_sends(tmp_path: Path) -> None:
    """Multiple hooks should be able to send sequential messages through the bound sender."""
    sent_messages: list[tuple[str, str, str | None, str, dict[str, object] | None]] = []

    async def sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
    ) -> str | None:
        sent_messages.append((room_id, body, thread_id, source_hook, extra_content))
        return f"$event{len(sent_messages)}"

    @hook(EVENT_MESSAGE_RECEIVED, priority=10)
    async def first(ctx: MessageReceivedContext) -> None:
        event_id = await ctx.send_message(
            "!room:localhost",
            "first",
            thread_id="$thread",
            extra_content={"custom": 1},
        )
        assert event_id == "$event1"

    @hook(EVENT_MESSAGE_RECEIVED, priority=20)
    async def second(ctx: MessageReceivedContext) -> None:
        event_id = await ctx.send_message("!room:localhost", "second")
        assert event_id == "$event2"

    registry = HookRegistry.from_plugins([_plugin("hook-plugin", [first, second])])

    await emit(registry, EVENT_MESSAGE_RECEIVED, _message_received_context_with_sender(tmp_path, sender))

    assert sent_messages == [
        (
            "!room:localhost",
            "first",
            "$thread",
            "hook-plugin:message:received",
            {"custom": 1, ORIGINAL_SENDER_KEY: "@user:localhost"},
        ),
        (
            "!room:localhost",
            "second",
            None,
            "hook-plugin:message:received",
            {ORIGINAL_SENDER_KEY: "@user:localhost"},
        ),
    ]


@pytest.mark.asyncio
async def test_hook_send_message_failure_does_not_crash_later_hooks(tmp_path: Path) -> None:
    """Sender failures should be isolated by normal hook execution error handling."""

    async def failing_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, extra_content
        msg = "boom"
        raise RuntimeError(msg)

    @hook(EVENT_MESSAGE_RECEIVED, priority=10)
    async def first(ctx: MessageReceivedContext) -> None:
        await ctx.send_message("!room:localhost", "first")

    @hook(EVENT_MESSAGE_RECEIVED, priority=20)
    async def second(ctx: MessageReceivedContext) -> None:
        ctx.suppress = True

    registry = HookRegistry.from_plugins([_plugin("hook-plugin", [first, second])])
    context = _message_received_context_with_sender(tmp_path, failing_sender)

    await emit(registry, EVENT_MESSAGE_RECEIVED, context)

    assert context.suppress is True


@pytest.mark.asyncio
async def test_agent_bot_hook_send_message_tags_source_and_threads(tmp_path: Path) -> None:
    """Hook sends should include hook metadata and thread relations."""
    bot = _hook_bot(tmp_path)
    bot.client = AsyncMock()

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
        captured_content.update(content)
        return "$hook-event"

    with (
        patch("mindroom.hooks.sender.get_latest_thread_event_id_if_needed", new=AsyncMock(return_value="$latest")),
        patch("mindroom.hooks.sender.send_message", side_effect=mock_send),
    ):
        event_id = await bot._hook_send_message(
            "!room:localhost",
            "hello",
            "$thread",
            "plugin:event",
            {"custom": "value"},
        )

    assert event_id == "$hook-event"
    assert captured_content["com.mindroom.source_kind"] == "hook"
    assert captured_content["com.mindroom.hook_source"] == "plugin:event"
    assert captured_content["custom"] == "value"
    assert isinstance(captured_content["m.relates_to"], dict)
    assert captured_content["m.relates_to"]["rel_type"] == "m.thread"
    assert captured_content["m.relates_to"]["event_id"] == "$thread"


@pytest.mark.asyncio
async def test_hook_send_message_preserves_original_sender_for_downstream_dispatch(tmp_path: Path) -> None:
    """Hook sends should preserve the requester identity for downstream permission checks."""
    bot = _hook_bot(tmp_path)
    bot.client = AsyncMock()

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
        captured_content.update(content)
        return "$hook-event"

    with (
        patch("mindroom.hooks.sender.get_latest_thread_event_id_if_needed", new=AsyncMock(return_value=None)),
        patch("mindroom.hooks.sender.send_message", side_effect=mock_send),
    ):
        event_id = await bot._hook_send_message(
            "!room:localhost",
            "hello",
            None,
            "plugin:event",
            {ORIGINAL_SENDER_KEY: "@user:localhost"},
        )

    assert event_id == "$hook-event"
    assert captured_content[ORIGINAL_SENDER_KEY] == "@user:localhost"


@pytest.mark.asyncio
async def test_prepare_dispatch_skips_hook_reemission_but_keeps_hook_dispatch(tmp_path: Path) -> None:
    """Hook-originated messages should bypass message:received hooks but still prepare normal dispatch."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-originated",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "automation",
                "com.mindroom.source_kind": "hook",
                "com.mindroom.hook_source": "hook-plugin:message:received",
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("called")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._extract_message_context = AsyncMock(return_value=_dispatch_context(bot))
    bot.response_tracker.mark_responded = MagicMock()

    dispatch = await bot._prepare_dispatch(
        room,
        event,
        requester_user_id="@mindroom_router:localhost",
        event_label="message",
    )

    assert dispatch is not None
    assert hook_calls == []
    assert dispatch.requester_user_id == "@mindroom_router:localhost"
    assert dispatch.envelope.source_kind == "hook"
    assert dispatch.envelope.mentioned_agents == ("code",)
    bot.response_tracker.mark_responded.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_text_message_continues_for_hook_originated_mentions(tmp_path: Path) -> None:
    """Hook-originated messages should continue into normal agent dispatch resolution."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-originated",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "@mindroom_code:localhost automation",
                "com.mindroom.source_kind": "hook",
                "com.mindroom.hook_source": "hook-plugin:message:received",
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("called")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._extract_message_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._resolve_dispatch_action = AsyncMock(return_value=None)

    await bot._dispatch_text_message(
        room,
        event,
        requester_user_id="@mindroom_router:localhost",
    )

    bot._resolve_dispatch_action.assert_awaited_once()
    dispatch = bot._resolve_dispatch_action.await_args.args[2]
    assert dispatch.envelope.source_kind == "hook"
    assert dispatch.envelope.mentioned_agents == ("code",)
    assert hook_calls == []


@pytest.mark.asyncio
async def test_user_message_cannot_spoof_hook_origin_to_bypass_message_received_hooks(tmp_path: Path) -> None:
    """User-authored events must not bypass message:received via hook metadata spoofing."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$spoofed-hook-origin",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "pretend automation",
                "com.mindroom.source_kind": "hook",
                "com.mindroom.hook_source": "hook-plugin:message:received",
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("called")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._extract_message_context = AsyncMock(return_value=_dispatch_context(bot))
    bot.response_tracker.mark_responded = MagicMock()

    dispatch = await bot._prepare_dispatch(
        room,
        event,
        requester_user_id="@user:localhost",
        event_label="message",
    )

    assert dispatch is not None
    assert hook_calls == ["called"]
    assert dispatch.envelope.source_kind == "message"
    bot.response_tracker.mark_responded.assert_not_called()


@pytest.mark.asyncio
async def test_agent_lifecycle_hooks_can_send_without_global_registration(tmp_path: Path) -> None:
    """Agent lifecycle hooks should receive a bound sender directly on the context."""
    bot = _hook_bot(tmp_path)
    bot.client = AsyncMock()
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": bot}
    bot.orchestrator = orchestrator

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
        captured_content.update(content)
        return "$hook-event"

    @hook(EVENT_AGENT_STARTED)
    async def started(ctx: AgentLifecycleContext) -> None:
        await ctx.send_message("!room:localhost", "router started")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [started])])

    with (
        patch("mindroom.hooks.sender.get_latest_thread_event_id_if_needed", new=AsyncMock(return_value=None)),
        patch("mindroom.hooks.sender.send_message", side_effect=mock_send),
    ):
        await bot._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

    assert captured_content["com.mindroom.source_kind"] == "hook"
    assert captured_content["com.mindroom.hook_source"] == "hook-plugin:agent:started"
