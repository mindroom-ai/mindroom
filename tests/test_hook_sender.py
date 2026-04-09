"""Tests for hook-driven Matrix message sending."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest

from mindroom import interactive
from mindroom.authorization import is_authorized_sender as real_is_authorized_sender
from mindroom.bot import (
    AgentBot,
    _DispatchPayload,
    _MessageContext,
    _PrecheckedEvent,
    _PreparedDispatch,
    _ResponseAction,
)
from mindroom.coalescing import PreparedTextEvent
from mindroom.commands.parsing import Command, CommandType
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_MESSAGE_RECEIVED,
    AfterResponseContext,
    AgentLifecycleContext,
    BeforeResponseContext,
    HookContext,
    HookMessageSender,
    HookRegistry,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ResponseDraft,
    ResponseResult,
    hook,
)
from mindroom.hooks.execution import emit
from mindroom.hooks.sender import HookMessageSender as SenderAlias
from mindroom.logging_config import get_logger
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.tool_system.skills import _SkillCommandDispatch, _SkillCommandSpec
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
            target=MessageTarget.resolve("!room:localhost", None, "$event"),
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


def _synthetic_envelope(*, agent_name: str = "code") -> MessageEnvelope:
    """Return a first-hop synthetic envelope from a message:received relay."""
    return MessageEnvelope(
        source_event_id="$hook-event",
        room_id="!room:localhost",
        target=MessageTarget.resolve(
            "!room:localhost",
            "$thread",
            "$hook-event",
        ),
        requester_id="@user:localhost",
        sender_id="@mindroom_router:localhost",
        body="synthetic",
        attachment_ids=(),
        mentioned_agents=(agent_name,),
        agent_name=agent_name,
        source_kind="hook_dispatch",
        hook_source="origin-plugin:message:received",
        message_received_depth=1,
    )


def _hook_bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    bot = AgentBot(
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    return bot


def _agent_bot(tmp_path: Path, *, agent_name: str = "code") -> AgentBot:
    config = _config(tmp_path)
    bot = AgentBot(
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
    bot.client = AsyncMock(spec=nio.AsyncClient)
    return bot


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
    sent_messages: list[tuple[str, str, str | None, str, dict[str, object] | None, bool]] = []

    async def sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        sent_messages.append((room_id, body, thread_id, source_hook, extra_content, trigger_dispatch))
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
            {
                "custom": 1,
                ORIGINAL_SENDER_KEY: "@user:localhost",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
            False,
        ),
        (
            "!room:localhost",
            "second",
            None,
            "hook-plugin:message:received",
            {
                ORIGINAL_SENDER_KEY: "@user:localhost",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
            False,
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("context_kind", ["enrich", "before", "after"])
async def test_downstream_hook_sends_advance_existing_message_received_depth(
    tmp_path: Path,
    context_kind: str,
) -> None:
    """Downstream hook contexts should advance an existing synthetic message:received chain."""
    sent_messages: list[dict[str, object] | None] = []

    async def sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, trigger_dispatch
        sent_messages.append(extra_content)
        return "$event"

    config = _config(tmp_path)
    base_kwargs = {
        "plugin_name": "downstream-plugin",
        "settings": {},
        "config": config,
        "runtime_paths": runtime_paths_for(config),
        "logger": get_logger("tests.hook_sender").bind(event_name="test"),
        "correlation_id": "corr-depth",
        "message_sender": sender,
    }
    envelope = _synthetic_envelope()
    if context_kind == "enrich":
        context = MessageEnrichContext(
            event_name="message:enrich",
            envelope=envelope,
            target_entity_name="code",
            target_member_names=None,
            **base_kwargs,
        )
    elif context_kind == "before":
        context = BeforeResponseContext(
            event_name="message:before_response",
            draft=ResponseDraft(
                response_text="hello",
                response_kind="ai",
                tool_trace=None,
                extra_content=None,
                envelope=envelope,
            ),
            **base_kwargs,
        )
    else:
        context = AfterResponseContext(
            event_name="message:after_response",
            result=ResponseResult(
                response_text="hello",
                response_event_id="$response",
                delivery_kind="sent",
                response_kind="ai",
                envelope=envelope,
            ),
            **base_kwargs,
        )

    event_id = await context.send_message("!room:localhost", "follow-up", trigger_dispatch=True)

    assert event_id == "$event"
    assert sent_messages == [
        {
            "com.mindroom.original_sender": "@user:localhost",
            HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("context_kind", ["enrich", "before", "after"])
async def test_non_message_hook_dispatch_starts_synthetic_chain_at_depth_one(
    tmp_path: Path,
    context_kind: str,
) -> None:
    """Non-message hook dispatch should mark the first synthetic hop with depth one."""
    sent_messages: list[dict[str, object] | None] = []

    async def sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, trigger_dispatch
        sent_messages.append(extra_content)
        return "$event"

    config = _config(tmp_path)
    base_kwargs = {
        "plugin_name": "downstream-plugin",
        "settings": {},
        "config": config,
        "runtime_paths": runtime_paths_for(config),
        "logger": get_logger("tests.hook_sender").bind(event_name="test"),
        "correlation_id": "corr-depth",
        "message_sender": sender,
    }
    envelope = _message_received_context(tmp_path).envelope
    if context_kind == "enrich":
        context = MessageEnrichContext(
            event_name="message:enrich",
            envelope=envelope,
            target_entity_name="code",
            target_member_names=None,
            **base_kwargs,
        )
    elif context_kind == "before":
        context = BeforeResponseContext(
            event_name="message:before_response",
            draft=ResponseDraft(
                response_text="hello",
                response_kind="ai",
                tool_trace=None,
                extra_content=None,
                envelope=envelope,
            ),
            **base_kwargs,
        )
    else:
        context = AfterResponseContext(
            event_name="message:after_response",
            result=ResponseResult(
                response_text="hello",
                response_event_id="$response",
                delivery_kind="sent",
                response_kind="ai",
                envelope=envelope,
            ),
            **base_kwargs,
        )

    event_id = await context.send_message("!room:localhost", "follow-up", trigger_dispatch=True)

    assert event_id == "$event"
    assert sent_messages == [
        {
            "com.mindroom.original_sender": "@user:localhost",
            HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
        },
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
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, extra_content, trigger_dispatch
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
    """Hook-originated messages should not immediately re-run the source plugin's message:received hooks."""
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
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
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
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        event_label="message",
    )

    assert dispatch is not None
    assert hook_calls == []
    assert dispatch.requester_user_id == "@mindroom_router:localhost"
    assert dispatch.envelope.source_kind == "hook"
    assert dispatch.envelope.hook_source == "hook-plugin:message:received"
    assert dispatch.envelope.message_received_depth == 1
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
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
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
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )

    bot._resolve_dispatch_action.assert_awaited_once()
    dispatch = bot._resolve_dispatch_action.await_args.args[2]
    assert dispatch.envelope.source_kind == "hook"
    assert dispatch.envelope.message_received_depth == 1
    assert dispatch.envelope.mentioned_agents == ("code",)
    assert hook_calls == []


@pytest.mark.asyncio
async def test_apply_message_enrichment_preserves_hook_chain_metadata(tmp_path: Path) -> None:
    """message:enrich setup should keep hook provenance and synthetic depth intact."""
    bot = _agent_bot(tmp_path)
    dispatch = _PreparedDispatch(
        requester_user_id="@user:localhost",
        context=_dispatch_context(bot),
        target=MessageTarget.resolve("!room:localhost", "$thread", "$hook-event"),
        correlation_id="corr-enrich",
        envelope=_synthetic_envelope(),
    )

    prepared = await bot._apply_message_enrichment(
        dispatch,
        _DispatchPayload(prompt="hello"),
        target_entity_name="code",
        target_member_names=None,
    )

    assert prepared.envelope.hook_source == "origin-plugin:message:received"
    assert prepared.envelope.message_received_depth == 1


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
    bot._resolve_dispatch_action = AsyncMock(return_value=None)

    await bot._dispatch_text_message(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
    )

    assert hook_calls == ["called"]
    bot._resolve_dispatch_action.assert_awaited_once()
    dispatch = bot._resolve_dispatch_action.await_args.args[2]
    assert dispatch.envelope.source_kind == "message"


@pytest.mark.asyncio
async def test_voice_prepared_text_does_not_trust_hook_metadata_from_user_content(tmp_path: Path) -> None:
    """Voice-normalized user events must not be able to inject internal hook provenance."""
    bot = _agent_bot(tmp_path)
    prepared_voice = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice-event",
        body="voice text",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "voice text",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "spoofed:message:received",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
            },
        },
        is_synthetic=True,
        source_kind_override="voice",
    )

    envelope = bot._build_message_envelope(
        room_id="!room:localhost",
        event=prepared_voice,
        requester_user_id="@user:localhost",
        context=_dispatch_context(bot),
    )

    assert envelope.source_kind == "voice"
    assert envelope.hook_source is None
    assert envelope.message_received_depth == 0


@pytest.mark.asyncio
async def test_dispatch_text_message_runs_message_received_before_command_parsing(tmp_path: Path) -> None:
    """Router command handling must still allow message:received hooks to suppress first."""
    bot = _agent_bot(tmp_path, agent_name="router")
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hooked-command",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "!help",
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(ctx: MessageReceivedContext) -> None:
        hook_calls.append("called")
        ctx.suppress = True

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._extract_message_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._handle_command = AsyncMock()
    bot.response_tracker.mark_responded = MagicMock()

    await bot._dispatch_text_message(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
    )

    assert hook_calls == ["called"]
    bot._handle_command.assert_not_awaited()
    bot.response_tracker.mark_responded.assert_called_once_with(event.event_id)


@pytest.mark.asyncio
async def test_prepare_dispatch_marks_all_source_events_when_hooks_suppress_batch(tmp_path: Path) -> None:
    """Hook suppression should mark every source event in a coalesced batch as handled."""
    bot = _agent_bot(tmp_path, agent_name="router")
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$m2",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "hello",
            },
        },
    )

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(ctx: MessageReceivedContext) -> None:
        ctx.suppress = True

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._extract_message_context = AsyncMock(return_value=_dispatch_context(bot))
    bot.response_tracker.mark_responded = MagicMock()

    dispatch = await bot._prepare_dispatch(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@user:localhost"),
        event_label="message",
        source_event_ids=["$m1", "$m2"],
    )

    assert dispatch is None
    assert bot.response_tracker.mark_responded.call_args_list == [call("$m1"), call("$m2")]


@pytest.mark.asyncio
async def test_resolve_text_dispatch_event_preserves_voice_source_kind_for_synthetic_events(tmp_path: Path) -> None:
    """Synthetic voice events should keep their source kind through preparation for hooks."""
    bot = _agent_bot(tmp_path)
    synthetic_voice = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice-event",
        body="voice text",
        source={"content": {"body": "voice text", "com.mindroom.source_kind": "voice"}},
        is_synthetic=True,
        source_kind_override="voice",
    )

    prepared = await bot._resolve_text_dispatch_event(synthetic_voice)
    envelope = bot._build_message_envelope(
        room_id="!room:localhost",
        event=prepared,
        requester_user_id="@user:localhost",
        context=_dispatch_context(bot),
    )

    assert prepared.source_kind_override == "voice"
    assert envelope.source_kind == "voice"


@pytest.mark.asyncio
async def test_dispatch_text_message_hydrates_sidecar_body_for_hooks_and_prompt(tmp_path: Path) -> None:
    """Inbound dispatch should use the canonical sidecar body everywhere downstream."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "@mindroom_code:localhost what is 99+1?",
                    "m.mentions": {"user_ids": ["@mindroom_code:localhost"]},
                },
            ).encode("utf-8"),
        ),
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot._extract_message_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._resolve_dispatch_action = AsyncMock(return_value=_ResponseAction(kind="individual"))
    bot._build_dispatch_payload_with_attachments = AsyncMock(return_value=_DispatchPayload(prompt="unused"))
    bot._execute_dispatch_action = AsyncMock()
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-message",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "@mindroom_code:localhost [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/inbound-sidecar",
            },
        },
    )
    captured_bodies: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(ctx: MessageReceivedContext) -> None:
        captured_bodies.append(ctx.envelope.body)

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])

    assert isinstance(event, nio.RoomMessageFile)
    await bot._on_media_message(room, event)

    assert captured_bodies == ["@mindroom_code:localhost what is 99+1?"]
    assert (
        bot._resolve_dispatch_action.await_args.kwargs["message_for_decision"]
        == "@mindroom_code:localhost what is 99+1?"
    )
    payload_builder = bot._execute_dispatch_action.await_args.args[4]
    await payload_builder(_dispatch_context(bot))
    assert (
        bot._build_dispatch_payload_with_attachments.await_args.kwargs["prompt"]
        == "@mindroom_code:localhost what is 99+1?"
    )


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


@pytest.mark.asyncio
async def test_trigger_dispatch_sets_hook_dispatch_source_kind(tmp_path: Path) -> None:
    """trigger_dispatch=True should set source_kind to hook_dispatch instead of hook."""
    bot = _hook_bot(tmp_path)
    bot.client = AsyncMock()

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object]) -> str:
        captured_content.update(content)
        return "$hook-event"

    @hook(EVENT_AGENT_STARTED)
    async def started(ctx: AgentLifecycleContext) -> None:
        await ctx.send_message("!room:localhost", "dispatch me", trigger_dispatch=True)

    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": bot}
    bot.orchestrator = orchestrator
    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [started])])

    with (
        patch("mindroom.hooks.sender.get_latest_thread_event_id_if_needed", new=AsyncMock(return_value=None)),
        patch("mindroom.hooks.sender.send_message", side_effect=mock_send),
    ):
        await bot._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

    assert captured_content["com.mindroom.source_kind"] == "hook_dispatch"
    expected_requester = bot.config.get_mindroom_user_id(bot.runtime_paths)
    if expected_requester is None:
        assert ORIGINAL_SENDER_KEY not in captured_content
    else:
        assert captured_content[ORIGINAL_SENDER_KEY] == expected_requester


@pytest.mark.asyncio
async def test_prepare_dispatch_allows_hook_dispatch_without_mention(tmp_path: Path) -> None:
    """hook_dispatch messages from agents should bypass the agent-not-mentioned filter."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-dispatch-msg",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "restart notification",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "restart-notify:bot:ready",
            },
        },
    )

    # No mentions — am_i_mentioned is False
    no_mention_context = _MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    bot._extract_message_context = AsyncMock(return_value=no_mention_context)

    dispatch = await bot._prepare_dispatch(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        event_label="message",
    )

    # Should NOT be filtered despite sender being an agent and no mention
    assert dispatch is not None
    assert dispatch.envelope.source_kind == "hook_dispatch"


@pytest.mark.asyncio
async def test_prepare_dispatch_reruns_message_received_for_hook_dispatch_from_non_message_hooks(
    tmp_path: Path,
) -> None:
    """hook_dispatch from non-message hooks should still run message:received hooks."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-dispatch-msg",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "restart notification",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "restart-notify:bot:ready",
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("called")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._extract_message_context = AsyncMock(
        return_value=_MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
    )

    dispatch = await bot._prepare_dispatch(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        event_label="message",
    )

    assert dispatch is not None
    assert hook_calls == ["called"]
    assert dispatch.envelope.source_kind == "hook_dispatch"


@pytest.mark.asyncio
async def test_hook_dispatch_from_message_received_reenters_once_and_skips_origin_plugin(
    tmp_path: Path,
) -> None:
    """First-hop hook_dispatch should re-enter message:received once and skip only the origin plugin."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-dispatch-msg",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "restart notification",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:received",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def origin(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("origin")

    @hook(EVENT_MESSAGE_RECEIVED)
    async def other(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("other")

    bot.hook_registry = HookRegistry.from_plugins(
        [_plugin("origin-plugin", [origin]), _plugin("other-plugin", [other])],
    )
    bot._extract_message_context = AsyncMock(
        return_value=_MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
    )

    dispatch = await bot._prepare_dispatch(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        event_label="message",
    )

    assert dispatch is not None
    assert hook_calls == ["other"]
    assert dispatch.envelope.source_kind == "hook_dispatch"
    assert dispatch.envelope.hook_source == "origin-plugin:message:received"
    assert dispatch.envelope.message_received_depth == 1


@pytest.mark.asyncio
async def test_hook_dispatch_from_message_received_stops_reentry_after_first_synthetic_hop(
    tmp_path: Path,
) -> None:
    """Deeper synthetic hops should not keep re-entering message:received across plugins."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-dispatch-msg",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "restart notification",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "other-plugin:message:received",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def origin(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("origin")

    @hook(EVENT_MESSAGE_RECEIVED)
    async def other(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("other")

    bot.hook_registry = HookRegistry.from_plugins(
        [_plugin("origin-plugin", [origin]), _plugin("other-plugin", [other])],
    )
    bot._extract_message_context = AsyncMock(
        return_value=_MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
    )

    dispatch = await bot._prepare_dispatch(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        event_label="message",
    )

    assert dispatch is not None
    assert hook_calls == []
    assert dispatch.envelope.source_kind == "hook_dispatch"
    assert dispatch.envelope.message_received_depth == 2


@pytest.mark.asyncio
async def test_deep_hook_dispatch_stops_before_command_or_response_dispatch(tmp_path: Path) -> None:
    """Deeper synthetic hook relays should stop before command parsing or AI dispatch."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$deep-hook-dispatch",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "follow-up automation",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:before_response",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
            },
        },
    )
    bot._resolve_text_dispatch_event = AsyncMock(return_value=event)
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._resolve_dispatch_action = AsyncMock()

    await bot._dispatch_text_message(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )

    bot._resolve_dispatch_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_deep_hook_dispatch_does_not_consume_interactive_answer_on_message_path(tmp_path: Path) -> None:
    """Deep synthetic relays should stop before interactive answers are consumed."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$deep-hook-interactive",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "1",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:before_response",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
            },
        },
    )
    interactive._active_questions.clear()
    interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
        room_id=room.room_id,
        thread_id=None,
        options={"1": "first"},
        creator_agent=bot.agent_name,
    )
    bot._precheck_dispatch_event = MagicMock(
        return_value=_PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )
    bot._resolve_text_dispatch_event = AsyncMock(return_value=event)
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._dispatch_text_message = AsyncMock()

    try:
        await bot._on_message(room, event)
    finally:
        assert "$question123" in interactive._active_questions
        interactive._active_questions.clear()

    bot._dispatch_text_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_hop_hook_dispatch_does_not_consume_interactive_answer_on_message_path(tmp_path: Path) -> None:
    """First-hop synthetic hook traffic should not answer interactive prompts."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$first-hop-hook-interactive",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "1",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:bot:ready",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
    )
    interactive._active_questions.clear()
    interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
        room_id=room.room_id,
        thread_id=None,
        options={"1": "first"},
        creator_agent=bot.agent_name,
    )
    bot._precheck_dispatch_event = MagicMock(
        return_value=_PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )
    bot._resolve_text_dispatch_event = AsyncMock(return_value=event)
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._dispatch_text_message = AsyncMock()

    try:
        await bot._on_message(room, event)
    finally:
        assert "$question123" in interactive._active_questions
        interactive._active_questions.clear()

    bot._dispatch_text_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_hop_plain_hook_from_non_message_hook_still_dispatches(tmp_path: Path) -> None:
    """First-hop plain hook messages from non-message hooks should still reach normal dispatch."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$plain-hook-first-hop",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "@mindroom_code:localhost restart notification",
                "com.mindroom.source_kind": "hook",
                "com.mindroom.hook_source": "restart-notify:bot:ready",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
    )
    hook_calls: list[str] = []

    @hook(EVENT_MESSAGE_RECEIVED)
    async def received(_ctx: MessageReceivedContext) -> None:
        hook_calls.append("called")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("hook-plugin", [received])])
    bot._resolve_text_dispatch_event = AsyncMock(return_value=event)
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._resolve_dispatch_action = AsyncMock(return_value=None)

    await bot._dispatch_text_message(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )

    bot._resolve_dispatch_action.assert_awaited_once()
    assert hook_calls == ["called"]


@pytest.mark.asyncio
async def test_first_hop_hook_dispatch_sidecar_preview_skips_interactive_answer_but_dispatches(
    tmp_path: Path,
) -> None:
    """First-hop sidecar previews should skip interactive consumption and still dispatch."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    sidecar_event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-hook-dispatch",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "1 [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/inbound-sidecar",
            },
        },
    )
    prepared_text_event = PreparedTextEvent(
        sender="@mindroom_router:localhost",
        event_id="$sidecar-hook-dispatch",
        body="1",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "1",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:bot:ready",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
        is_synthetic=True,
    )
    bot._prepare_file_sidecar_text_event = AsyncMock(return_value=prepared_text_event)
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._dispatch_text_message = AsyncMock()
    interactive._active_questions.clear()
    interactive._active_questions["$question123"] = interactive._InteractiveQuestion(
        room_id=room.room_id,
        thread_id=None,
        options={"1": "first"},
        creator_agent=bot.agent_name,
    )

    try:
        with patch.object(interactive, "handle_text_response", new=AsyncMock()) as mock_handle_text_response:
            assert isinstance(sidecar_event, nio.RoomMessageFile)
            handled = await bot._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(
                    event=sidecar_event,
                    requester_user_id="@mindroom_router:localhost",
                ),
            )

        assert handled is True
        assert "$question123" in interactive._active_questions
        mock_handle_text_response.assert_not_awaited()
        bot._dispatch_text_message.assert_awaited_once()
    finally:
        interactive._active_questions.clear()


@pytest.mark.asyncio
async def test_deep_hook_dispatch_sidecar_preview_stops_before_interactive_or_dispatch(tmp_path: Path) -> None:
    """Deep sidecar previews should stop before interactive handling or text dispatch."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    sidecar_event = nio.Event.parse_event(
        {
            "event_id": "$sidecar-deep-hook-dispatch",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.file",
                "body": "follow-up [Message continues in attached file]",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/inbound-sidecar",
            },
        },
    )
    prepared_text_event = PreparedTextEvent(
        sender="@mindroom_router:localhost",
        event_id="$sidecar-deep-hook-dispatch",
        body="follow-up",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "follow-up",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:before_response",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
            },
        },
        is_synthetic=True,
    )
    bot._prepare_file_sidecar_text_event = AsyncMock(return_value=prepared_text_event)
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._dispatch_text_message = AsyncMock()

    with patch.object(interactive, "handle_text_response", new=AsyncMock()) as mock_handle_text_response:
        assert isinstance(sidecar_event, nio.RoomMessageFile)
        handled = await bot._dispatch_file_sidecar_text_preview(
            room,
            _PrecheckedEvent(
                event=sidecar_event,
                requester_user_id="@mindroom_router:localhost",
            ),
        )

    assert handled is True
    mock_handle_text_response.assert_not_awaited()
    bot._dispatch_text_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_hop_prepared_text_hook_dispatch_still_reaches_dispatch(tmp_path: Path) -> None:
    """Prepared synthetic text should keep first-hop hook dispatch behavior."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = PreparedTextEvent(
        sender="@mindroom_router:localhost",
        event_id="$prepared-hook-dispatch",
        body="@mindroom_code:localhost follow up",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "@mindroom_code:localhost follow up",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:bot:ready",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
        is_synthetic=True,
    )
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._resolve_dispatch_action = AsyncMock(return_value=None)

    await bot._dispatch_text_message(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )

    bot._resolve_dispatch_action.assert_awaited_once()
    dispatch = bot._resolve_dispatch_action.await_args.args[2]
    assert dispatch.envelope.source_kind == "hook_dispatch"
    assert dispatch.envelope.message_received_depth == 1


@pytest.mark.asyncio
async def test_deep_prepared_text_hook_dispatch_stops_before_dispatch(tmp_path: Path) -> None:
    """Prepared synthetic text should stop at the same deep-relay boundary as raw text."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = PreparedTextEvent(
        sender="@mindroom_router:localhost",
        event_id="$prepared-deep-hook-dispatch",
        body="follow-up automation",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "follow-up automation",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:before_response",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
            },
        },
        is_synthetic=True,
    )
    bot._extract_dispatch_context = AsyncMock(return_value=_dispatch_context(bot))
    bot._resolve_dispatch_action = AsyncMock()

    await bot._dispatch_text_message(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
    )

    bot._resolve_dispatch_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_dispatch_skill_command_preserves_source_envelope_in_runtime(tmp_path: Path) -> None:
    """Skill-command responses should inherit hook provenance and synthetic depth."""
    bot = _hook_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-skill",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "!skill demo summarize",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:received",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
    )
    bot._resolve_text_dispatch_event = AsyncMock(return_value=event)
    bot._extract_dispatch_context = AsyncMock(
        return_value=_MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
    )
    bot._send_skill_command_response = AsyncMock(return_value="$skill-reply")

    async def fake_handle_command(
        *,
        context: object,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        command: object,
        requester_user_id: str,
    ) -> None:
        del command
        await context.send_skill_command_response(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            thread_id=None,
            thread_history=[],
            prompt="Use demo skill",
            agent_name="code",
            user_id=requester_user_id,
            reply_to_event=event,
        )

    with patch("mindroom.bot.handle_command", new=fake_handle_command):
        await bot._dispatch_text_message(
            room,
            _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        )

    source_envelope = bot._send_skill_command_response.await_args.kwargs["source_envelope"]
    assert source_envelope.hook_source == "origin-plugin:message:received"
    assert source_envelope.message_received_depth == 1


@pytest.mark.asyncio
async def test_hook_dispatch_skill_tool_command_builds_full_tool_runtime_context(tmp_path: Path) -> None:
    """Hook-dispatched !skill tool runs should inherit the full tool runtime context."""
    bot = _hook_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$hook-skill-tool",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "!skill demo summarize",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "origin-plugin:message:received",
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
        },
    )
    command = Command(
        type=CommandType.SKILL,
        args={"skill_name": "demo", "args_text": "summarize"},
        raw_text="!skill demo summarize",
    )
    bot.client.rooms = {}
    bot._send_response = AsyncMock(return_value="$response")
    captured_runtime_context = None

    async def fake_run_skill_command_tool(**kwargs: object) -> str:
        nonlocal captured_runtime_context
        captured_runtime_context = kwargs["runtime_context"]
        return "tool-result"

    spec = _SkillCommandSpec(
        name="demo",
        description="demo",
        source_path=tmp_path / "demo" / "SKILL.md",
        user_invocable=True,
        disable_model_invocation=False,
        dispatch=_SkillCommandDispatch(tool_name="shell.demo"),
    )

    with (
        patch("mindroom.commands.handler._resolve_skill_command_agent", return_value=("code", None)),
        patch("mindroom.commands.handler.resolve_skill_command_spec", return_value=spec),
        patch("mindroom.bot._run_skill_command_tool", new=AsyncMock(side_effect=fake_run_skill_command_tool)),
    ):
        await bot._handle_command(
            room,
            _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
            command,
            source_envelope=_synthetic_envelope(agent_name="router"),
        )

    assert captured_runtime_context is not None
    assert captured_runtime_context.message_received_depth == 1
    assert captured_runtime_context.hook_message_sender is not None
    assert captured_runtime_context.room_state_querier is not None
    assert captured_runtime_context.room_state_putter is not None


@pytest.mark.asyncio
async def test_prepare_dispatch_still_filters_plain_hook_without_mention(tmp_path: Path) -> None:
    """Plain hook messages from agents without mentions should still be filtered."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$plain-hook-msg",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "plain hook message",
                "com.mindroom.source_kind": "hook",
                "com.mindroom.hook_source": "some-plugin:message:received",
            },
        },
    )

    no_mention_context = _MessageContext(
        am_i_mentioned=False,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    bot._extract_message_context = AsyncMock(return_value=no_mention_context)

    dispatch = await bot._prepare_dispatch(
        room,
        _PrecheckedEvent(event=event, requester_user_id="@mindroom_router:localhost"),
        event_label="message",
    )

    # Plain hook messages without mention should still be filtered
    assert dispatch is None


@pytest.mark.asyncio
async def test_router_precheck_allows_self_authored_hook_dispatch_without_requester(tmp_path: Path) -> None:
    """Router-authored hook_dispatch without preserved requester should survive ingress precheck."""
    bot = _hook_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$router-hook-dispatch",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "restart notification",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "hook-plugin:agent:started",
            },
        },
    )
    bot.hook_registry = HookRegistry.empty()
    bot._extract_message_context = AsyncMock(
        return_value=_MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
    )

    prechecked = bot._precheck_dispatch_event(room, event)

    assert prechecked is not None
    assert prechecked.requester_user_id == "@mindroom_router:localhost"

    dispatch = await bot._prepare_dispatch(room, prechecked, event_label="message")

    assert dispatch is not None
    assert dispatch.requester_user_id == "@mindroom_router:localhost"
    assert dispatch.envelope.source_kind == "hook_dispatch"


@pytest.mark.asyncio
async def test_precheck_rejects_hook_dispatch_with_unauthorized_original_sender(tmp_path: Path) -> None:
    """hook_dispatch should enforce room authorization against the preserved requester."""
    bot = _hook_bot(tmp_path)
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    room.canonical_alias = None
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$unauthorized-hook-dispatch",
            "sender": "@mindroom_router:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "restart notification",
                "com.mindroom.source_kind": "hook_dispatch",
                "com.mindroom.hook_source": "hook-plugin:agent:started",
                ORIGINAL_SENDER_KEY: "@unauthorized:localhost",
            },
        },
    )

    with patch("mindroom.bot.is_authorized_sender", side_effect=real_is_authorized_sender):
        prechecked = bot._precheck_dispatch_event(room, event)

    assert prechecked is None
    bot.response_tracker.mark_responded.assert_called_once_with(event.event_id)
