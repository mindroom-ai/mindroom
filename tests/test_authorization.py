"""Tests for user authorization mechanism."""

from __future__ import annotations

import pytest

from mindroom.config import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.thread_utils import is_authorized_sender


@pytest.fixture
def mock_config_no_restrictions() -> Config:
    """Config with no authorization restrictions (backward compatible)."""
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
        authorized_users=[],  # Empty list means allow everyone
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
        authorized_users=["@alice:example.com", "@bob:example.com"],
    )


def test_no_restrictions_allows_everyone(mock_config_no_restrictions: Config) -> None:
    """Test that empty authorized_users list allows everyone (backward compatibility)."""
    # Random users should be allowed
    assert is_authorized_sender("@random_user:example.com", mock_config_no_restrictions)
    assert is_authorized_sender("@another_user:different.com", mock_config_no_restrictions)

    # Agents should also be allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_no_restrictions)


def test_authorized_users_allowed(mock_config_with_restrictions: Config) -> None:
    """Test that users in the authorized_users list are allowed."""
    assert is_authorized_sender("@alice:example.com", mock_config_with_restrictions)
    assert is_authorized_sender("@bob:example.com", mock_config_with_restrictions)


def test_unauthorized_users_blocked(mock_config_with_restrictions: Config) -> None:
    """Test that users NOT in the authorized_users list are blocked."""
    assert not is_authorized_sender("@charlie:example.com", mock_config_with_restrictions)
    assert not is_authorized_sender("@random_user:example.com", mock_config_with_restrictions)


def test_agents_always_allowed(mock_config_with_restrictions: Config) -> None:
    """Test that configured agents are always allowed regardless of authorized_users."""
    # Assuming the domain is example.com based on config
    mock_config_with_restrictions._domain = "example.com"  # Set domain for testing

    # Configured agents should be allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_with_restrictions)
    assert is_authorized_sender("@mindroom_analyst:example.com", mock_config_with_restrictions)

    # Non-configured agent should be blocked
    assert not is_authorized_sender("@mindroom_unknown:example.com", mock_config_with_restrictions)


def test_teams_always_allowed(mock_config_with_restrictions: Config) -> None:
    """Test that configured teams are always allowed regardless of authorized_users."""
    mock_config_with_restrictions._domain = "example.com"  # Set domain for testing

    # Configured team should be allowed
    assert is_authorized_sender("@mindroom_test_team:example.com", mock_config_with_restrictions)

    # Non-configured team should be blocked
    assert not is_authorized_sender("@mindroom_unknown_team:example.com", mock_config_with_restrictions)


def test_router_always_allowed(mock_config_with_restrictions: Config) -> None:
    """Test that the router agent is always allowed."""
    mock_config_with_restrictions._domain = "example.com"  # Set domain for testing

    # Router should always be allowed
    assert is_authorized_sender(f"@mindroom_{ROUTER_AGENT_NAME}:example.com", mock_config_with_restrictions)


def test_mixed_authorization_scenarios(mock_config_with_restrictions: Config) -> None:
    """Test various mixed authorization scenarios."""
    mock_config_with_restrictions._domain = "example.com"

    # Authorized users - allowed
    assert is_authorized_sender("@alice:example.com", mock_config_with_restrictions)

    # Unauthorized users - blocked
    assert not is_authorized_sender("@eve:example.com", mock_config_with_restrictions)

    # Agents - allowed
    assert is_authorized_sender("@mindroom_assistant:example.com", mock_config_with_restrictions)

    # Teams - allowed
    assert is_authorized_sender("@mindroom_test_team:example.com", mock_config_with_restrictions)

    # Router - allowed
    assert is_authorized_sender(f"@mindroom_{ROUTER_AGENT_NAME}:example.com", mock_config_with_restrictions)

    # Unknown agent - blocked
    assert not is_authorized_sender("@mindroom_fake_agent:example.com", mock_config_with_restrictions)
