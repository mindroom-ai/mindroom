"""Tests for the membership-based access configuration and policy resolvers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.access_policy import resolve_responder_access, resolve_room_policy
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME


def test_room_list_override_replaces_default() -> None:
    """A room list override must not retain default invitation candidates."""
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "room_defaults": {
                "join_policy": "knock",
                "listed": True,
                "invite_users": ["@default:example.com"],
                "admins": ["@default-admin:example.com"],
            },
            "rooms": {
                "project": {
                    "invite_users": ["@owner:example.com"],
                    "admins": [],
                },
            },
        },
    )

    policy = resolve_room_policy(config, "project")

    assert policy.join_policy == "knock"
    assert policy.listed is True
    assert policy.invite_users == ("@owner:example.com",)
    assert policy.admins == ()


def test_room_without_metadata_inherits_all_defaults() -> None:
    """Implicit managed rooms must receive the complete default room policy."""
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "room_defaults": {
                "join_policy": "public",
                "listed": True,
                "encrypted": True,
                "invite_users": ["@member:example.com"],
                "admins": ["@admin:example.com"],
            },
            "agents": {
                "research": {
                    "display_name": "Research",
                    "rooms": ["research"],
                },
            },
        },
    )

    policy = resolve_room_policy(config, "research")

    assert policy.join_policy == "public"
    assert policy.listed is True
    assert policy.encrypted is True
    assert policy.invite_users == ("@member:example.com",)
    assert policy.admins == ("@admin:example.com",)


def test_omitted_agent_access_uses_configured_rooms() -> None:
    """An agent without an access block must inherit its assigned rooms as grants."""
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "agents": {
                "research": {
                    "display_name": "Research",
                    "rooms": ["research"],
                },
            },
        },
    )

    access = resolve_responder_access(config, "research")

    assert access.members_of_rooms == ("research",)
    assert access.current_room_members is False
    assert access.users == ()


def test_explicit_empty_agent_room_grants_disable_inference() -> None:
    """An authored empty room-grant list must disable assigned-room inference."""
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "agents": {
                "research": {
                    "display_name": "Research",
                    "rooms": ["research"],
                    "access": {"members_of_rooms": []},
                },
            },
        },
    )

    access = resolve_responder_access(config, "research")

    assert access.members_of_rooms == ()


def test_team_and_router_use_their_distinct_access_defaults() -> None:
    """Teams infer assigned rooms while the router grants current-room members."""
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "agents": {"research": {"display_name": "Research"}},
            "teams": {
                "reviewers": {
                    "display_name": "Reviewers",
                    "role": "Review",
                    "agents": ["research"],
                    "rooms": ["reviews"],
                },
            },
        },
    )

    assert resolve_responder_access(config, "reviewers").members_of_rooms == ("reviews",)
    assert resolve_responder_access(config, ROUTER_AGENT_NAME).current_room_members is True


@pytest.mark.parametrize(
    ("payload", "duplicate"),
    [
        ({"administrators": ["@admin:example.com", "@admin:example.com"]}, "administrators"),
        ({"room_defaults": {"invite_users": ["@user:example.com", "@user:example.com"]}}, "invite_users"),
        ({"rooms": {"one": {"admins": ["@admin:example.com", "@admin:example.com"]}}}, "admins"),
        (
            {
                "agents": {
                    "one": {
                        "display_name": "One",
                        "access": {"members_of_rooms": ["one", "one"]},
                    },
                },
            },
            "members_of_rooms",
        ),
        (
            {
                "agents": {
                    "one": {
                        "display_name": "One",
                        "credential_managers": ["@manager:example.com", "@manager:example.com"],
                    },
                },
            },
            "credential_managers",
        ),
    ],
)
def test_membership_schema_rejects_duplicate_entries(payload: dict[str, object], duplicate: str) -> None:
    """Duplicate access entries must fail validation instead of changing policy silently."""
    with pytest.raises(ValidationError, match=duplicate):
        Config.model_validate({"access_model": "room_membership", **payload})


@pytest.mark.parametrize(
    ("payload", "field_name"),
    [
        ({"administrators": ["*"]}, "administrators"),
        (
            {
                "agents": {
                    "one": {
                        "display_name": "One",
                        "credential_managers": ["@manager:*"],
                    },
                },
            },
            "credential_managers",
        ),
    ],
)
def test_authority_fields_require_concrete_matrix_ids(payload: dict[str, object], field_name: str) -> None:
    """Administrative authority must never be granted through wildcard identities."""
    with pytest.raises(ValidationError, match=field_name):
        Config.model_validate({"access_model": "room_membership", **payload})


def test_responder_access_rejects_unknown_managed_room() -> None:
    """A misspelled grant room must fail closed during configuration parsing."""
    with pytest.raises(ValidationError, match="missing"):
        Config.model_validate(
            {
                "access_model": "room_membership",
                "agents": {
                    "research": {
                        "display_name": "Research",
                        "rooms": ["research"],
                        "access": {"members_of_rooms": ["missing"]},
                    },
                },
            },
        )


def test_policy_resolvers_reject_unknown_targets() -> None:
    """Runtime callers must not receive permissive policies for unknown targets."""
    config = Config.model_validate({"access_model": "room_membership"})

    with pytest.raises(ValueError, match="Unknown managed room"):
        resolve_room_policy(config, "missing")
    with pytest.raises(ValueError, match="Unknown responder"):
        resolve_responder_access(config, "missing")
