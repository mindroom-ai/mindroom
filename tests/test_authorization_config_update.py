"""Test that authorization updates when config is reloaded."""

from __future__ import annotations

from unittest.mock import patch

from mindroom.config import Config
from mindroom.thread_utils import is_authorized_sender


def test_authorization_check_uses_updated_config() -> None:
    """Test that is_authorized_sender uses the updated config.

    This demonstrates that when the config.authorization is updated,
    the authorization checks will use the new configuration.
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
        authorization={
            "global_users": ["@alice:example.com"],
            "room_permissions": {},
            "default_room_access": False,
        },
    )

    # Mock the domain property
    with patch.object(Config, "domain", property(lambda _: "example.com")):
        # Alice should be authorized
        assert is_authorized_sender("@alice:example.com", config, "!test:server")

        # Bob should not be authorized
        assert not is_authorized_sender("@bob:example.com", config, "!test:server")

        # Now update the config to add Bob
        config.authorization.global_users = ["@alice:example.com", "@bob:example.com"]

        # Both should now be authorized
        assert is_authorized_sender("@alice:example.com", config, "!test:server")
        assert is_authorized_sender("@bob:example.com", config, "!test:server")

        # mindroom_user should always be authorized
        assert is_authorized_sender("@mindroom_user:example.com", config, "!test:server")
