"""Tests for user authorization mechanism."""

from __future__ import annotations

import pytest

from mindroom.config import AuthorizationConfig, Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.thread_utils import is_authorized_sender


@pytest.fixture
def mock_config_no_restrictions() -> Config:
    """Config with no authorized users (defaults to only internal system user)."""
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


def test_no_restrictions_only_allows_internal_user(
    mock_config_no_restrictions: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that empty authorized_users list only allows internal system user and agents."""
    # Mock the domain property
    monkeypatch.setattr(mock_config_no_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Random users should NOT be allowed
    assert not is_authorized_sender("@random_user:example.com", mock_config_no_restrictions, "!test:server")
    assert not is_authorized_sender("@another_user:different.com", mock_config_no_restrictions, "!test:server")

    # Agents should still be allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_no_restrictions, "!test:server")

    # Internal system user should always be allowed
    assert is_authorized_sender(
        mock_config_no_restrictions.get_mindroom_user_id(),
        mock_config_no_restrictions,
        "!test:server",
    )


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


def test_internal_system_user_always_allowed(
    mock_config_with_restrictions: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that configured internal user on the current domain is always allowed."""
    # Mock the domain property
    monkeypatch.setattr(mock_config_with_restrictions.__class__, "domain", property(lambda _: "example.com"))

    # Internal system user should always be allowed, even with restrictions
    assert is_authorized_sender(
        mock_config_with_restrictions.get_mindroom_user_id(),
        mock_config_with_restrictions,
        "!test:server",
    )

    # Same username from a different domain should NOT be allowed
    wrong_domain_id = mock_config_with_restrictions.get_mindroom_user_id().replace(":example.com", ":different.com")
    assert not is_authorized_sender(wrong_domain_id, mock_config_with_restrictions, "!test:server")


def test_custom_internal_system_user_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that custom configured internal user is always allowed."""
    config = Config(
        agents={
            "assistant": {
                "display_name": "Assistant",
                "role": "Test assistant",
                "rooms": ["test_room"],
            },
        },
        mindroom_user={
            "username": "alice_internal",
            "display_name": "Alice Internal",
        },
        authorization={
            "global_users": [],
            "room_permissions": {},
            "default_room_access": False,
        },
    )
    monkeypatch.setattr(config.__class__, "domain", property(lambda _: "example.com"))

    assert is_authorized_sender("@alice_internal:example.com", config, "!test:server")
    assert not is_authorized_sender("@mindroom_user:example.com", config, "!test:server")


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


@pytest.fixture
def mock_config_with_aliases() -> Config:
    """Config with bridge aliases mapping."""
    return Config(
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
            "default_room_access": False,
            "aliases": {
                "@alice:example.com": ["@telegram_111:example.com", "@signal_111:example.com"],
                "@bob:example.com": ["@telegram_222:example.com"],
            },
        },
    )


def test_bridge_alias_global_user(mock_config_with_aliases: Config) -> None:
    """Test that a bridge alias of a global user gets global access."""
    # Alice's Telegram alias should have global access
    assert is_authorized_sender("@telegram_111:example.com", mock_config_with_aliases, "!room1:example.com")
    assert is_authorized_sender("@telegram_111:example.com", mock_config_with_aliases, "!any_room:example.com")

    # Alice's Signal alias should also work
    assert is_authorized_sender("@signal_111:example.com", mock_config_with_aliases, "!room1:example.com")


def test_bridge_alias_room_permission(mock_config_with_aliases: Config) -> None:
    """Test that a bridge alias inherits room-specific permissions."""
    # Bob's Telegram alias should have access to room1
    assert is_authorized_sender("@telegram_222:example.com", mock_config_with_aliases, "!room1:example.com")

    # But not to other rooms
    assert not is_authorized_sender("@telegram_222:example.com", mock_config_with_aliases, "!room2:example.com")


def test_unknown_bridge_alias_rejected(mock_config_with_aliases: Config) -> None:
    """Test that an unknown alias is not authorized."""
    assert not is_authorized_sender("@telegram_999:example.com", mock_config_with_aliases, "!room1:example.com")


def test_canonical_user_still_works_with_aliases(mock_config_with_aliases: Config) -> None:
    """Test that the canonical user ID still works when aliases are configured."""
    assert is_authorized_sender("@alice:example.com", mock_config_with_aliases, "!room1:example.com")
    assert is_authorized_sender("@bob:example.com", mock_config_with_aliases, "!room1:example.com")


def test_resolve_alias_method() -> None:
    """Test the resolve_alias helper directly."""
    auth = AuthorizationConfig(
        aliases={
            "@alice:example.com": ["@telegram_111:example.com"],
        },
    )
    assert auth.resolve_alias("@telegram_111:example.com") == "@alice:example.com"
    assert auth.resolve_alias("@alice:example.com") == "@alice:example.com"
    assert auth.resolve_alias("@unknown:example.com") == "@unknown:example.com"
