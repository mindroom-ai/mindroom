"""Tests for user authorization mechanism."""

from __future__ import annotations

import pytest

from mindroom.config import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.thread_utils import is_authorized_sender


@pytest.fixture
def mock_config_no_restrictions() -> Config:
    """Config with no authorized users (defaults to only mindroom_user)."""
    return Config(
        agents={
            "assistant": {
                "display_name": "Assistant",
                "role": "Test assistant",
                "rooms": ["test_room"],
            },
        },
        teams={
            "test_team": {
                "display_name": "Test Team",
                "role": "Test team",
                "agents": ["assistant"],
                "rooms": ["test_room"],
            },
        },
        # No authorization field means default empty authorization
    )


@pytest.fixture
def mock_config_with_restrictions() -> Config:
    """Config with authorization restrictions."""
    return Config(
        agents={
            "assistant": {
                "display_name": "Assistant",
                "role": "Test assistant",
                "rooms": ["test_room"],
            },
            "analyst": {
                "display_name": "Analyst",
                "role": "Test analyst",
                "rooms": ["test_room"],
            },
        },
        teams={
            "test_team": {
                "display_name": "Test Team",
                "role": "Test team",
                "agents": ["assistant"],
                "rooms": ["test_room"],
            },
        },
        authorization={
            "global_users": ["@alice:example.com", "@bob:example.com"],
            "room_permissions": {},
            "default_room_access": False,
        },
    )


def test_no_restrictions_only_allows_mindroom_user(
    mock_config_no_restrictions: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that empty authorized_users list only allows mindroom_user and agents."""
    # Mock the domain property
    monkeypatch.setattr(mock_config_no_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Random users should NOT be allowed
    assert not is_authorized_sender("@random_user:example.com", mock_config_no_restrictions, "!test:server")
    assert not is_authorized_sender("@another_user:different.com", mock_config_no_restrictions, "!test:server")

    # Agents should still be allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_no_restrictions, "!test:server")

    # mindroom_user should always be allowed
    assert is_authorized_sender("@mindroom_user:example.com", mock_config_no_restrictions, "!test:server")


def test_authorized_users_allowed(mock_config_with_restrictions: Config) -> None:
    """Test that users in the authorized_users list are allowed."""
    assert is_authorized_sender("@alice:example.com", mock_config_with_restrictions, "!test:server")
    assert is_authorized_sender("@bob:example.com", mock_config_with_restrictions, "!test:server")


def test_unauthorized_users_blocked(mock_config_with_restrictions: Config) -> None:
    """Test that users NOT in the authorized_users list are blocked."""
    assert not is_authorized_sender("@charlie:example.com", mock_config_with_restrictions, "!test:server")
    assert not is_authorized_sender("@random_user:example.com", mock_config_with_restrictions, "!test:server")


def test_agents_always_allowed(mock_config_with_restrictions: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that configured agents are always allowed regardless of authorized_users."""
    # Mock the domain property
    monkeypatch.setattr(mock_config_with_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Configured agents should be allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_with_restrictions, "!test:server")
    assert is_authorized_sender("@mindroom_analyst:example.com", mock_config_with_restrictions, "!test:server")

    # Non-configured agent should be blocked
    assert not is_authorized_sender("@mindroom_unknown:example.com", mock_config_with_restrictions, "!test:server")


def test_teams_always_allowed(mock_config_with_restrictions: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that configured teams are always allowed regardless of authorized_users."""
    monkeypatch.setattr(mock_config_with_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Configured team should be allowed
    assert is_authorized_sender("@mindroom_test_team:example.com", mock_config_with_restrictions, "!test:server")

    # Non-configured team should be blocked
    assert not is_authorized_sender("@mindroom_unknown_team:example.com", mock_config_with_restrictions, "!test:server")


def test_router_always_allowed(mock_config_with_restrictions: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that the router agent is always allowed."""
    monkeypatch.setattr(mock_config_with_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Router should always be allowed
    assert is_authorized_sender(
        f"@mindroom_{ROUTER_AGENT_NAME}:example.com",
        mock_config_with_restrictions,
        "!test:server",
    )


def test_mindroom_user_always_allowed(mock_config_with_restrictions: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that mindroom_user on the current domain is always allowed."""
    # Mock the domain property
    monkeypatch.setattr(mock_config_with_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # mindroom_user should always be allowed, even with restrictions
    assert is_authorized_sender("@mindroom_user:example.com", mock_config_with_restrictions, "!test:server")

    # mindroom_user from a different domain should NOT be allowed
    assert not is_authorized_sender("@mindroom_user:different.com", mock_config_with_restrictions, "!test:server")


def test_mixed_authorization_scenarios(mock_config_with_restrictions: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test various mixed authorization scenarios."""
    monkeypatch.setattr(mock_config_with_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Authorized users - allowed
    assert is_authorized_sender("@alice:example.com", mock_config_with_restrictions, "!test:server")

    # Unauthorized users - blocked
    assert not is_authorized_sender("@eve:example.com", mock_config_with_restrictions, "!test:server")

    # Agents - allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_with_restrictions, "!test:server")

    # Teams - allowed
    assert is_authorized_sender("@mindroom_test_team:example.com", mock_config_with_restrictions, "!test:server")

    # Router - allowed
    assert is_authorized_sender(
        f"@mindroom_{ROUTER_AGENT_NAME}:example.com",
        mock_config_with_restrictions,
        "!test:server",
    )

    # Unknown agent - blocked
    assert not is_authorized_sender("@mindroom_fake_agent:example.com", mock_config_with_restrictions, "!test:server")


@pytest.fixture
def mock_config_with_room_permissions() -> Config:
    """Config with room-specific permissions."""
    return Config(
        agents={
            "assistant": {
                "display_name": "Assistant",
                "role": "Test assistant",
                "rooms": ["test_room"],
            },
        },
        authorization={
            "global_users": ["@alice:example.com"],  # Alice has global access
            "room_permissions": {
                "!room1:example.com": ["@bob:example.com", "@charlie:example.com"],
                "!room2:example.com": ["@charlie:example.com"],
            },
            "default_room_access": False,
        },
    )


def test_room_specific_permissions(mock_config_with_room_permissions: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test room-specific permission system."""
    monkeypatch.setattr(mock_config_with_room_permissions.__class__, "domain", property(lambda _: "example.com"))

    # Alice has global access - allowed everywhere
    assert is_authorized_sender("@alice:example.com", mock_config_with_room_permissions, "!room1:example.com")
    assert is_authorized_sender("@alice:example.com", mock_config_with_room_permissions, "!room2:example.com")
    assert is_authorized_sender("@alice:example.com", mock_config_with_room_permissions, "!room3:example.com")

    # Bob only has access to room1
    assert is_authorized_sender("@bob:example.com", mock_config_with_room_permissions, "!room1:example.com")
    assert not is_authorized_sender("@bob:example.com", mock_config_with_room_permissions, "!room2:example.com")
    assert not is_authorized_sender("@bob:example.com", mock_config_with_room_permissions, "!room3:example.com")

    # Charlie has access to room1 and room2
    assert is_authorized_sender("@charlie:example.com", mock_config_with_room_permissions, "!room1:example.com")
    assert is_authorized_sender("@charlie:example.com", mock_config_with_room_permissions, "!room2:example.com")
    assert not is_authorized_sender("@charlie:example.com", mock_config_with_room_permissions, "!room3:example.com")

    # Dave has no access anywhere
    assert not is_authorized_sender("@dave:example.com", mock_config_with_room_permissions, "!room1:example.com")
    assert not is_authorized_sender("@dave:example.com", mock_config_with_room_permissions, "!room2:example.com")
    assert not is_authorized_sender("@dave:example.com", mock_config_with_room_permissions, "!room3:example.com")


def test_default_room_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test default_room_access setting."""
    config_allow_default = Config(
        agents={
            "assistant": {
                "display_name": "Assistant",
                "role": "Test assistant",
                "rooms": ["test_room"],
            },
        },
        authorization={
            "global_users": ["@alice:example.com"],
            "room_permissions": {
                "!room1:example.com": ["@bob:example.com"],
            },
            "default_room_access": True,  # Allow by default
        },
    )

    monkeypatch.setattr(config_allow_default.__class__, "domain", property(lambda _: "example.com"))

    # Alice has global access
    assert is_authorized_sender("@alice:example.com", config_allow_default, "!room1:example.com")
    assert is_authorized_sender("@alice:example.com", config_allow_default, "!room2:example.com")

    # Bob has explicit access to room1
    assert is_authorized_sender("@bob:example.com", config_allow_default, "!room1:example.com")

    # For room2 (not in room_permissions), Bob gets default access (True)
    assert is_authorized_sender("@bob:example.com", config_allow_default, "!room2:example.com")

    # Charlie has no explicit permissions but gets default access
    assert not is_authorized_sender(
        "@charlie:example.com",
        config_allow_default,
        "!room1:example.com",
    )  # Explicit empty list
    assert is_authorized_sender("@charlie:example.com", config_allow_default, "!room2:example.com")  # Default access
