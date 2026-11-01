"""Tests for presence-based streaming functionality."""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.matrix.presence import is_user_online, should_use_streaming
from mindroom.matrix.users import AgentMatrixUser
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from tests.access_schema_support import with_current_room_member_access
from tests.authorization_helpers import (
    make_test_bot_for_entity,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_event,
    install_runtime_journal_support,
    make_matrix_client_mock,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _presence_bot(tmp_path: Path) -> AgentBot:
    """Return a streaming-enabled agent bot whose reply delivery runs through the real gateway."""
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        model="gpt-6-astra",
                        rooms=["test"],
                    ),
                },
                models={
                    "default": {"provider": "openai", "id": "test-model"},
                    "gpt-6-astra": {"provider": "openai", "id": "gpt-6-astra"},
                },
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
        user_id="@mindroom_test_agent:localhost",
        access_token="test_token",  # noqa: S106
    )
    bot = make_test_bot_for_entity("test_agent", agent_user, config, runtime_paths_for(config))
    assert isinstance(bot, AgentBot)
    bot.client = make_matrix_client_mock()
    bot.client.user_id = "@mindroom_test_agent:localhost"
    bot.client.room_send = AsyncMock()
    bot.client.room_put_state = AsyncMock()
    install_runtime_journal_support(bot)
    return bot


def _hello_request() -> ResponseRequest:
    """Return a user's message asking the agent to reply in its thread."""
    return ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=("$msg123",),
            logical_source_event_ids=("$msg123",),
        ),
        prompt="Hello bot",
        thread_history=[],
        user_id="@user:localhost",
        response_envelope=request_envelope(
            room_id="!test:localhost",
            reply_to_event_id="$msg123",
            thread_id="$thread123",
            prompt="Hello bot",
            user_id="@user:localhost",
            agent_name="test_agent",
        ),
    )


class TestPresenceDetection:
    """Test presence detection functionality."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("presence", "last_active_ago", "expected"),
        [
            # Active 1 second ago.
            pytest.param("online", 1000, True, id="online"),
            # Idle for 5 minutes, but the client is open.
            pytest.param("unavailable", 300000, True, id="unavailable"),
            # Last active 1 hour ago.
            pytest.param("offline", 3600000, False, id="offline"),
        ],
    )
    async def test_is_user_online_follows_the_reported_presence(
        self,
        presence: str,
        last_active_ago: int,
        expected: bool,
    ) -> None:
        """An online or unavailable user counts as online; an offline user does not."""
        mock_client = make_matrix_client_mock()
        mock_response = Mock(spec=nio.PresenceGetResponse)
        mock_response.presence = presence
        mock_response.last_active_ago = last_active_ago
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is expected
        mock_client.get_presence.assert_called_once_with("@user:example.com")

    @pytest.mark.asyncio
    async def test_is_user_online_prefers_cached_room_user_presence(self) -> None:
        """Cached room membership presence should avoid an extra `/presence` lookup."""
        mock_client = make_matrix_client_mock()
        mock_client.rooms = {
            "!room:example.com": Mock(
                users={
                    "@user:example.com": Mock(
                        presence="online",
                        last_active_ago=1000,
                    ),
                },
            ),
        }

        result = await is_user_online(mock_client, "@user:example.com", room_id="!room:example.com")

        assert result is True
        mock_client.get_presence.assert_not_called()

    @pytest.mark.asyncio
    async def test_is_user_online_falls_back_when_cached_room_user_presence_is_offline_default(self) -> None:
        """A cached room user with nio's offline default should still fall back to `/presence`."""
        mock_client = make_matrix_client_mock()
        mock_client.rooms = {
            "!room:example.com": Mock(
                users={
                    "@user:example.com": Mock(
                        presence="offline",
                        last_active_ago=None,
                    ),
                },
            ),
        }
        mock_response = Mock(spec=nio.PresenceGetResponse)
        mock_response.presence = "online"
        mock_response.last_active_ago = 1000
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com", room_id="!room:example.com")

        assert result is True
        mock_client.get_presence.assert_called_once_with("@user:example.com")

    @pytest.mark.asyncio
    async def test_is_user_online_on_error(self) -> None:
        """Test that is_user_online returns False on presence check error."""
        mock_client = make_matrix_client_mock()

        # Mock error response - create instance properly
        mock_error = nio.PresenceGetError.from_dict({"errcode": "M_FORBIDDEN", "error": "Forbidden"})
        mock_client.get_presence.return_value = mock_error

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_is_user_online_on_exception(self) -> None:
        """Test that is_user_online returns False when exception is raised."""
        mock_client = make_matrix_client_mock()

        # Mock exception
        mock_client.get_presence.side_effect = Exception("Network error")

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False


class TestStreamingDecision:
    """Test streaming decision logic."""

    @pytest.mark.asyncio
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_should_use_streaming_when_user_online(
        self,
        mock_is_user_online: AsyncMock,
    ) -> None:
        """Test that streaming is used when user is online."""
        mock_is_user_online.return_value = True

        mock_client = AsyncMock(spec=nio.AsyncClient)

        result = await should_use_streaming(
            mock_client,
            "!room:example.com",
            requester_user_id="@user:example.com",
            enable_streaming=True,
        )

        assert result is True
        mock_is_user_online.assert_called_once_with(
            mock_client,
            "@user:example.com",
            room_id="!room:example.com",
        )

    @pytest.mark.asyncio
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_should_use_streaming_when_user_offline(
        self,
        mock_is_user_online: AsyncMock,
    ) -> None:
        """Test that streaming is not used when user is offline."""
        mock_is_user_online.return_value = False

        mock_client = AsyncMock(spec=nio.AsyncClient)

        result = await should_use_streaming(
            mock_client,
            "!room:example.com",
            requester_user_id="@user:example.com",
            enable_streaming=True,
        )

        assert result is False
        mock_is_user_online.assert_called_once_with(
            mock_client,
            "@user:example.com",
            room_id="!room:example.com",
        )

    @pytest.mark.asyncio
    async def test_should_use_streaming_when_globally_disabled(self) -> None:
        """Test that streaming is not used when globally disabled."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        result = await should_use_streaming(
            mock_client,
            "!room:example.com",
            requester_user_id="@user:example.com",
            enable_streaming=False,
        )

        assert result is False
        # Should not even check presence when globally disabled
        mock_client.get_presence.assert_not_called()

    @pytest.mark.asyncio
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_should_use_streaming_no_requester(
        self,
        mock_is_user_online: AsyncMock,
    ) -> None:
        """Test that streaming defaults to True when no requester specified."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        result = await should_use_streaming(
            mock_client,
            "!room:example.com",
            requester_user_id=None,
            enable_streaming=True,
        )

        assert result is True
        # Should not check presence when no requester
        mock_is_user_online.assert_not_called()


class TestBotIntegration:
    """Test bot integration with presence-based streaming."""

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_bot_uses_streaming_when_user_online(
        self,
        mock_is_user_online: AsyncMock,
        mock_stream_agent_response: Mock,
        tmp_path: Path,
    ) -> None:
        """Test that bot streams its reply when user is online."""
        # Setup mocks
        mock_is_user_online.return_value = True

        async def mock_streaming_response() -> AsyncIterator[str]:
            yield "Test"
            yield " streaming"
            yield " response"

        mock_stream_agent_response.return_value = mock_streaming_response()
        bot = _presence_bot(tmp_path)

        async def mock_edit_message_result(
            _client: object,
            _room_id: str,
            _event_id: str,
            content: dict,
            _display_text: str,
        ) -> object:
            return delivered_matrix_event("$edit", content)

        # Simulate a message from a user
        with (
            patch("mindroom.streaming.edit_message_result", side_effect=mock_edit_message_result),
            patch.object(
                DeliveryGateway,
                "deliver_stream",
                autospec=True,
                side_effect=DeliveryGateway.deliver_stream,
            ) as deliver_stream,
            patch.object(
                DeliveryGateway,
                "deliver_final",
                autospec=True,
                side_effect=DeliveryGateway.deliver_final,
            ) as deliver_final,
        ):
            await bot._response_runner.generate_response(_hello_request())

        # Should have streamed the reply since user is online
        mock_stream_agent_response.assert_called_once()
        deliver_stream.assert_awaited_once()
        deliver_final.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_bot_uses_non_streaming_when_user_offline(
        self,
        mock_is_user_online: AsyncMock,
        mock_stream_agent_response: Mock,
        tmp_path: Path,
    ) -> None:
        """Test that bot delivers its reply whole when user is offline."""
        # Setup mocks
        mock_is_user_online.return_value = False

        async def mock_streaming_response() -> AsyncIterator[str]:
            yield "Test response"

        mock_stream_agent_response.return_value = mock_streaming_response()
        bot = _presence_bot(tmp_path)

        # Simulate a message from a user
        with (
            patch.object(
                DeliveryGateway,
                "deliver_stream",
                autospec=True,
                side_effect=DeliveryGateway.deliver_stream,
            ) as deliver_stream,
            patch.object(
                DeliveryGateway,
                "deliver_final",
                autospec=True,
                side_effect=DeliveryGateway.deliver_final,
            ) as deliver_final,
        ):
            await bot._response_runner.generate_response(_hello_request())

        # Should have delivered the collected reply whole since user is offline
        mock_stream_agent_response.assert_called_once()
        deliver_final.assert_awaited_once()
        assert deliver_final.await_args.args[1].response_text == "Test response"
        deliver_stream.assert_not_awaited()
