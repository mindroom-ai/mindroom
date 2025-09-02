"""Tests for presence-based streaming functionality."""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot, create_bot_for_entity
from mindroom.config import Config
from mindroom.matrix.presence import is_user_online, should_use_streaming
from mindroom.matrix.users import AgentMatrixUser

if TYPE_CHECKING:
    from pathlib import Path


class TestPresenceDetection:
    """Test presence detection functionality."""

    @pytest.mark.asyncio
    async def test_is_user_online_when_online(self) -> None:
        """Test that is_user_online returns True when user is online."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        # Mock successful presence response - user is online
        mock_response = Mock(spec=nio.PresenceGetResponse)
        mock_response.presence = "online"
        mock_response.last_active_ago = 1000  # 1 second ago
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is True
        mock_client.get_presence.assert_called_once_with("@user:example.com")

    @pytest.mark.asyncio
    async def test_is_user_online_when_unavailable(self) -> None:
        """Test that is_user_online returns True when user is unavailable (idle but client open)."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        # Mock successful presence response - user is unavailable (idle)
        mock_response = Mock(spec=nio.PresenceGetResponse)
        mock_response.presence = "unavailable"
        mock_response.last_active_ago = 300000  # 5 minutes ago
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is True
        mock_client.get_presence.assert_called_once_with("@user:example.com")

    @pytest.mark.asyncio
    async def test_is_user_online_when_offline(self) -> None:
        """Test that is_user_online returns False when user is offline."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        # Mock successful presence response - user is offline
        mock_response = Mock(spec=nio.PresenceGetResponse)
        mock_response.presence = "offline"
        mock_response.last_active_ago = 3600000  # 1 hour ago
        mock_client.get_presence.return_value = mock_response

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False
        mock_client.get_presence.assert_called_once_with("@user:example.com")

    @pytest.mark.asyncio
    async def test_is_user_online_on_error(self) -> None:
        """Test that is_user_online returns False on presence check error."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        # Mock error response - create instance properly
        mock_error = nio.PresenceGetError.from_dict({"errcode": "M_FORBIDDEN", "error": "Forbidden"})
        mock_client.get_presence.return_value = mock_error

        result = await is_user_online(mock_client, "@user:example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_is_user_online_on_exception(self) -> None:
        """Test that is_user_online returns False when exception is raised."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

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

        with patch.dict("os.environ", {"MINDROOM_ENABLE_STREAMING": "true"}):
            result = await should_use_streaming(
                mock_client,
                "!room:example.com",
                requester_user_id="@user:example.com",
            )

        assert result is True
        mock_is_user_online.assert_called_once_with(mock_client, "@user:example.com")

    @pytest.mark.asyncio
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_should_use_streaming_when_user_offline(
        self,
        mock_is_user_online: AsyncMock,
    ) -> None:
        """Test that streaming is not used when user is offline."""
        mock_is_user_online.return_value = False

        mock_client = AsyncMock(spec=nio.AsyncClient)

        with patch.dict("os.environ", {"MINDROOM_ENABLE_STREAMING": "true"}):
            result = await should_use_streaming(
                mock_client,
                "!room:example.com",
                requester_user_id="@user:example.com",
            )

        assert result is False
        mock_is_user_online.assert_called_once_with(mock_client, "@user:example.com")

    @pytest.mark.asyncio
    @patch("mindroom.matrix.presence.ENABLE_STREAMING", False)
    async def test_should_use_streaming_when_globally_disabled(self) -> None:
        """Test that streaming is not used when globally disabled."""
        mock_client = AsyncMock(spec=nio.AsyncClient)

        result = await should_use_streaming(
            mock_client,
            "!room:example.com",
            requester_user_id="@user:example.com",
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

        with patch.dict("os.environ", {"MINDROOM_ENABLE_STREAMING": "true"}):
            result = await should_use_streaming(
                mock_client,
                "!room:example.com",
                requester_user_id=None,
            )

        assert result is True
        # Should not check presence when no requester
        mock_is_user_online.assert_not_called()


class TestBotIntegration:
    """Test bot integration with presence-based streaming."""

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.stream_agent_response")
    @patch("mindroom.bot.fetch_thread_history")
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_bot_uses_streaming_when_user_online(
        self,
        mock_is_user_online: AsyncMock,
        mock_fetch_history: AsyncMock,
        mock_stream_agent_response: AsyncMock,
        mock_ai_response: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Test that bot uses streaming when user is online."""
        # Setup mocks
        mock_is_user_online.return_value = True
        mock_fetch_history.return_value = []

        async def mock_streaming_response() -> AsyncIterator[str]:
            yield "Test"
            yield " streaming"
            yield " response"

        mock_stream_agent_response.return_value = mock_streaming_response()
        mock_ai_response.return_value = "Test response"

        # Create bot with streaming enabled
        from mindroom.config import AgentConfig  # noqa: PLC0415

        config = Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    model="gpt-4",
                    rooms=["#test:localhost"],
                ),
            },
        )

        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            display_name="Test Agent",
            password="test_password",  # noqa: S106
            user_id="@mindroom_test_agent:localhost",
            access_token="test_token",  # noqa: S106
        )

        with patch.dict("os.environ", {"MINDROOM_ENABLE_STREAMING": "true"}):
            bot = create_bot_for_entity("test_agent", agent_user, config, tmp_path)
            assert isinstance(bot, AgentBot)
            bot.client = AsyncMock(spec=nio.AsyncClient)
            bot.client.user_id = "@mindroom_test_agent:localhost"
            bot.client.room_send = AsyncMock()
            bot.client.room_put_state = AsyncMock()

            # Simulate a message from a user
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Hello bot",
                reply_to_event_id="$msg123",
                thread_id="$thread123",
                thread_history=[],
                user_id="@user:localhost",
            )

        # Should have used streaming since user is online
        mock_stream_agent_response.assert_called_once()
        mock_ai_response.assert_not_called()

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.stream_agent_response")
    @patch("mindroom.bot.fetch_thread_history")
    @patch("mindroom.matrix.presence.is_user_online")
    async def test_bot_uses_non_streaming_when_user_offline(
        self,
        mock_is_user_online: AsyncMock,
        mock_fetch_history: AsyncMock,
        mock_stream_agent_response: AsyncMock,
        mock_ai_response: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Test that bot uses non-streaming when user is offline."""
        # Setup mocks
        mock_is_user_online.return_value = False
        mock_fetch_history.return_value = []
        mock_ai_response.return_value = "Test response"

        # Create bot with streaming enabled
        from mindroom.config import AgentConfig  # noqa: PLC0415

        config = Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    model="gpt-4",
                    rooms=["#test:localhost"],
                ),
            },
        )

        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            display_name="Test Agent",
            password="test_password",  # noqa: S106
            user_id="@mindroom_test_agent:localhost",
            access_token="test_token",  # noqa: S106
        )

        with patch.dict("os.environ", {"MINDROOM_ENABLE_STREAMING": "true"}):
            bot = create_bot_for_entity("test_agent", agent_user, config, tmp_path)
            assert isinstance(bot, AgentBot)
            bot.client = AsyncMock(spec=nio.AsyncClient)
            bot.client.user_id = "@mindroom_test_agent:localhost"
            bot.client.room_send = AsyncMock()
            bot.client.room_put_state = AsyncMock()

            # Simulate a message from a user
            await bot._generate_response(
                room_id="!test:localhost",
                prompt="Hello bot",
                reply_to_event_id="$msg123",
                thread_id="$thread123",
                thread_history=[],
                user_id="@user:localhost",
            )

        # Should have used non-streaming since user is offline
        mock_ai_response.assert_called_once()
        mock_stream_agent_response.assert_not_called()
