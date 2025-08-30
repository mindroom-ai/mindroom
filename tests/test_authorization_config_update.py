"""Test that authorization updates when config is reloaded."""

from __future__ import annotations

from unittest.mock import patch

from mindroom.config import Config
from mindroom.thread_utils import is_authorized_sender


def test_authorization_check_uses_updated_config() -> None:
    """Test that is_authorized_sender uses the updated config.

    This demonstrates that when the config.authorized_users list is updated,
    the authorization checks will use the new list.
    """
    # Create config with alice authorized
    config = Config(
        agents={
            "test_agent": {
                "display_name": "Test Agent",
                "role": "Test role",
                "rooms": ["test_room"],
            },
        },
        authorized_users=["@alice:example.com"],
    )

    # Mock the domain property
    with patch.object(Config, "domain", property(lambda _: "example.com")):
        # Alice should be authorized
        assert is_authorized_sender("@alice:example.com", config)

        # Bob should not be authorized
        assert not is_authorized_sender("@bob:example.com", config)

        # Now update the config to add Bob
        config.authorized_users = ["@alice:example.com", "@bob:example.com"]

        # Both should now be authorized
        assert is_authorized_sender("@alice:example.com", config)
        assert is_authorized_sender("@bob:example.com", config)

        # mindroom_user should always be authorized
        assert is_authorized_sender("@mindroom_user:example.com", config)


def test_config_update_mechanism() -> None:
    """Document how the config update mechanism works.

    When update_config() is called in MultiAgentOrchestrator:
    1. Line 1471: self.config = new_config
    2. Lines 1474-1476: For all existing bots not being restarted:
       bot.config = new_config

    This ensures all AgentBot instances get the updated authorized_users list
    when the config.yaml file is modified and saved.
    """
    # This test documents the behavior rather than testing it
    # The actual implementation is in src/mindroom/bot.py:1439-1476
