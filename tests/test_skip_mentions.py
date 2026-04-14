"""Test that skip_mentions metadata prevents agents from responding to mentions."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.conversation_resolver import should_skip_mentions
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps, FinalDeliveryRequest, SendTextRequest
from mindroom.hooks import MessageEnvelope, ResponseDraft
from mindroom.logging_config import get_logger, setup_logging
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    make_event_cache_mock,
    runtime_paths_for,
    sync_bot_runtime_state,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_should_skip_mentions_with_metadata() -> None:
    """Test that should_skip_mentions detects the metadata."""
    # Event with skip_mentions metadata
    event_source = {
        "content": {
            "body": "✅ Scheduled task. @email_agent will be mentioned",
            "com.mindroom.skip_mentions": True,
        },
    }
    assert should_skip_mentions(event_source) is True


def test_should_skip_mentions_without_metadata() -> None:
    """Test that should_skip_mentions returns False when no metadata."""
    # Normal event without metadata
    event_source = {
        "content": {
            "body": "Regular message @email_agent",
        },
    }
    assert should_skip_mentions(event_source) is False


def test_should_skip_mentions_explicit_false() -> None:
    """Test that should_skip_mentions returns False when metadata is False."""
    event_source = {
        "content": {
            "body": "Message with explicit false @email_agent",
            "com.mindroom.skip_mentions": False,
        },
    }
    assert should_skip_mentions(event_source) is False


def _context_bot(tmp_path: Path, config: Config | None = None) -> AgentBot:
    """Build a real bot so context extraction exercises the resolver runtime path."""
    if config is None:
        config = bind_runtime_paths(
            Config(agents={"email_agent": AgentConfig(display_name="Email Agent")}),
            test_runtime_paths(tmp_path),
        )
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="email_agent",
            password=TEST_PASSWORD,
            display_name="Email Agent",
            user_id="@mindroom_email_agent:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.user_id = bot.agent_user.user_id
    bot.logger = MagicMock()
    bot._conversation_resolver.derive_conversation_context = AsyncMock(return_value=(False, None, []))
    sync_bot_runtime_state(bot)
    return bot


@pytest.mark.asyncio
async def test_send_response_with_skip_mentions(tmp_path: Path) -> None:
    """Test that _send_response adds metadata when skip_mentions is True."""
    config = bind_runtime_paths(
        Config(agents={"email_agent": AgentConfig(display_name="Email Agent")}),
        test_runtime_paths(tmp_path),
    )
    bot = _context_bot(tmp_path, config)

    # Mock the format_message_with_mentions to return a dict we can check
    mock_content = {"body": "test", "msgtype": "m.text"}

    # Create a test room and event
    room = nio.MatrixRoom(room_id="!test:server", own_user_id="@bot:server")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "!schedule in 5 minutes check email",
                "msgtype": "m.text",
            },
            "sender": "@user:server",
            "event_id": "$event123",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    # Patch the function to capture what was passed

    with patch("mindroom.delivery_gateway.format_message_with_mentions") as mock_create:
        mock_create.return_value = mock_content.copy()
        with patch("mindroom.delivery_gateway.send_message") as mock_send:
            mock_send.return_value = "$response123"

            # Call the actual _send_response method with skip_mentions=True
            await AgentBot._send_response(
                bot,
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                response_text="✅ Scheduled. Will notify @email_agent",
                thread_id=None,
                reply_to_event=event,
                skip_mentions=True,
            )

            # Check that send_message was called with content that has skip_mentions
            mock_send.assert_called_once()
            sent_content = mock_send.call_args[0][2]  # Third argument is content
            assert sent_content.get("com.mindroom.skip_mentions") is True


@pytest.mark.asyncio
async def test_extract_context_with_skip_mentions(tmp_path: Path) -> None:
    """Test that _extract_message_context ignores mentions when skip_mentions is set."""
    bot = _context_bot(tmp_path)

    # Create room
    room = nio.MatrixRoom(room_id="!test:server", own_user_id="@bot:server")

    # Event with skip_mentions metadata and a mention
    event_with_skip = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "✅ Scheduled task. @email_agent will handle it",
                "msgtype": "m.text",
                "com.mindroom.skip_mentions": True,
                "m.mentions": {
                    "user_ids": ["@mindroom_email_agent:localhost"],
                },
            },
            "sender": "@router:server",
            "event_id": "$event123",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    # Extract context - should not detect mentions
    context = await bot._conversation_resolver.extract_message_context_impl(
        room,
        event_with_skip,
        full_history=True,
    )

    # Verify mentions were ignored
    assert context.am_i_mentioned is False
    assert context.mentioned_agents == []

    # Now test without skip_mentions - should detect mentions
    event_without_skip = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Hey @email_agent can you help?",
                "msgtype": "m.text",
                "m.mentions": {
                    "user_ids": ["@mindroom_email_agent:localhost"],
                },
            },
            "sender": "@user:server",
            "event_id": "$event456",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    # Mock check_agent_mentioned to return that we're mentioned
    with patch("mindroom.conversation_resolver.check_agent_mentioned") as mock_check:
        mock_check.return_value = (["email_agent"], True, False)

        context = await bot._conversation_resolver.extract_message_context_impl(
            room,
            event_without_skip,
            full_history=True,
        )

        # Verify mentions were detected
        assert context.am_i_mentioned is True
        assert "email_agent" in context.mentioned_agents


@pytest.mark.asyncio
async def test_extract_context_without_skip_metadata_detects_tool_mentions(tmp_path: Path) -> None:
    """Tool-shaped events without skip metadata should still trigger mention detection."""
    config = bind_runtime_paths(
        Config(agents={"email_agent": AgentConfig(display_name="Email Agent")}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)

    bot = _context_bot(tmp_path, config)

    room = nio.MatrixRoom(room_id="!test:server", own_user_id="@bot:server")
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@mindroom_email_agent:localhost please continue",
                "msgtype": "m.text",
                "m.mentions": {
                    "user_ids": [bot.matrix_id.full_id],
                },
            },
            "sender": "@mindroom_general:localhost",
            "event_id": "$event789",
            "room_id": "!test:server",
            "origin_server_ts": 123456789,
        },
    )

    context = await bot._conversation_resolver.extract_message_context_impl(
        room,
        event,
        full_history=True,
    )

    assert context.am_i_mentioned is True
    assert [agent.full_id for agent in context.mentioned_agents] == [
        MatrixID.from_agent("email_agent", "localhost", runtime_paths).full_id,
    ]


def _gateway_with_mocks(tmp_path: Path) -> tuple[DeliveryGateway, AsyncMock, AsyncMock]:
    """Build a direct DeliveryGateway test harness."""
    config = bind_runtime_paths(
        Config(agents={"email_agent": AgentConfig(display_name="Email Agent")}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    before_hooks = AsyncMock()
    after_hooks = AsyncMock()
    response_hooks = MagicMock()
    response_hooks.apply_before_response = before_hooks
    response_hooks.emit_after_response = after_hooks
    conversation_cache = SimpleNamespace(
        get_latest_thread_event_id_if_needed=AsyncMock(return_value=None),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(
                client=AsyncMock(),
                config=config,
                enable_streaming=True,
                orchestrator=None,
                event_cache=make_event_cache_mock(),
            ),
            runtime_paths=runtime_paths,
            agent_name="email_agent",
            logger=MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=SimpleNamespace(
                build_message_target=MagicMock(),
                deps=SimpleNamespace(conversation_cache=conversation_cache),
            ),
            response_hooks=response_hooks,
        ),
    )
    return gateway, before_hooks, after_hooks


def _delivery_envelope() -> MessageEnvelope:
    """Build a minimal response envelope for delivery gateway tests."""
    return MessageEnvelope(
        source_event_id="$event123",
        room_id="!test:server",
        target=MessageTarget.resolve("!test:server", "$thread", "$event123"),
        requester_id="@user:server",
        sender_id="@user:server",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="email_agent",
        source_kind="message",
    )


@pytest.mark.asyncio
async def test_delivery_gateway_send_text_logs_target_thread_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct send logs should include the resolved target room/thread."""
    gateway, _, _ = _gateway_with_mocks(tmp_path)
    config = gateway.deps.runtime.config
    target = MessageTarget.resolve("!test:server", "$thread", "$event123")
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=runtime_paths_for(config))
    capsys.readouterr()
    gateway = DeliveryGateway(replace(gateway.deps, logger=get_logger("tests.delivery")))

    gateway.deps.resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(
        return_value="$latest",
    )
    with patch("mindroom.delivery_gateway.send_message", new=AsyncMock(return_value="$response")):
        event_id = await gateway.send_text(
            SendTextRequest(
                target=target,
                response_text="formatted response",
            ),
        )

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert event_id == "$response"
    assert payload["event"] == "Sent response"
    assert payload["room_id"] == "!test:server"
    assert payload["thread_id"] == "$thread"


@pytest.mark.asyncio
async def test_delivery_gateway_deliver_final_uses_send_text_for_new_messages(tmp_path: Path) -> None:
    """Final delivery should route fresh sends through the gateway's native send helper."""
    gateway, before_hooks, after_hooks = _gateway_with_mocks(tmp_path)
    before_hooks.return_value = ResponseDraft(
        response_text="raw response",
        response_kind="ai",
        tool_trace=None,
        extra_content=None,
        envelope=_delivery_envelope(),
    )

    parsed = MagicMock()
    parsed.formatted_text = "formatted response"
    parsed.option_map = None
    parsed.options_list = None

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$response")) as mock_send_text,
        patch("mindroom.delivery_gateway.interactive.parse_and_format_interactive", return_value=parsed),
    ):
        result = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=_delivery_envelope().target,
                existing_event_id=None,
                response_text="raw response",
                response_kind="ai",
                response_envelope=_delivery_envelope(),
                correlation_id="corr-1",
                tool_trace=None,
                extra_content=None,
            ),
        )

    mock_send_text.assert_awaited_once()
    after_hooks.assert_awaited_once()
    assert result.event_id == "$response"
    assert result.delivery_kind == "sent"


@pytest.mark.asyncio
async def test_delivery_gateway_deliver_final_uses_edit_text_for_existing_messages(tmp_path: Path) -> None:
    """Final delivery should route edits through the gateway's native edit helper."""
    gateway, before_hooks, after_hooks = _gateway_with_mocks(tmp_path)
    before_hooks.return_value = ResponseDraft(
        response_text="raw response",
        response_kind="ai",
        tool_trace=None,
        extra_content=None,
        envelope=_delivery_envelope(),
    )

    parsed = MagicMock()
    parsed.formatted_text = "formatted response"
    parsed.option_map = None
    parsed.options_list = None

    with (
        patch.object(DeliveryGateway, "edit_text", new=AsyncMock(return_value=True)) as mock_edit_text,
        patch("mindroom.delivery_gateway.interactive.parse_and_format_interactive", return_value=parsed),
    ):
        result = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=_delivery_envelope().target,
                existing_event_id="$existing",
                response_text="raw response",
                response_kind="ai",
                response_envelope=_delivery_envelope(),
                correlation_id="corr-2",
                tool_trace=None,
                extra_content=None,
            ),
        )

    mock_edit_text.assert_awaited_once()
    after_hooks.assert_awaited_once()
    assert result.event_id == "$existing"
    assert result.delivery_kind == "edited"
