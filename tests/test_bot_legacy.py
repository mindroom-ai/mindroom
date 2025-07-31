"""Tests for legacy Bot class compatibility."""

from unittest.mock import AsyncMock, patch

import pytest

from mindroom.bot import Bot
from mindroom.matrix_agent_manager import AgentMatrixUser


@pytest.mark.asyncio
@patch("mindroom.bot.ensure_all_agent_users")
@patch("mindroom.bot.login_agent_user")
async def test_legacy_bot_deprecation_warning(
    mock_login: AsyncMock,
    mock_ensure_users: AsyncMock,
) -> None:
    """Test that legacy Bot class shows deprecation warning."""
    mock_ensure_users.return_value = {}

    # The Bot class should create an orchestrator internally
    bot = Bot()

    # Verify it has an orchestrator
    assert hasattr(bot, "orchestrator")
    assert bot.orchestrator is not None


@pytest.mark.asyncio
@patch("mindroom.bot.ensure_all_agent_users")
@patch("mindroom.bot.login_agent_user")
async def test_legacy_bot_starts_orchestrator(
    mock_login: AsyncMock,
    mock_ensure_users: AsyncMock,
) -> None:
    """Test that legacy Bot class starts the multi-agent orchestrator."""
    # Mock agent users
    mock_agent_users = {
        "general": AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="GeneralAgent",
            password="gen_pass",
        ),
    }
    mock_ensure_users.return_value = mock_agent_users

    # Mock client
    mock_client = AsyncMock()
    mock_client.sync_forever = AsyncMock(side_effect=KeyboardInterrupt)
    mock_login.return_value = mock_client

    bot = Bot()

    # Start should use the orchestrator
    with pytest.raises(KeyboardInterrupt):
        await bot.start()

    # Verify orchestrator was initialized
    assert bot.orchestrator is not None
    assert len(bot.orchestrator.agent_bots) == 1
    mock_login.assert_called_once()
