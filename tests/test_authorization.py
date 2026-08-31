"""Tests for membership-based responder and authority checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import (
    get_effective_sender_id_for_reply_permissions,
    is_platform_administrator,
    is_sender_allowed_for_agent_credential_management,
    is_sender_allowed_for_agent_invite,
    is_sender_allowed_for_agent_reply_in_room,
    is_sender_allowed_for_responder,
)
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from tests.access_schema_support import membership_config, membership_index, unresolved_membership_index
from tests.conftest import runtime_paths_for
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path


def _allowed(
    sender_id: str,
    config: object,
    memberships: AgentReplyMembershipIndex,
    *,
    room_id: str = "!current:example.com",
) -> bool:
    from mindroom.config.main import Config  # noqa: PLC0415

    assert isinstance(config, Config)
    return is_sender_allowed_for_responder(
        sender_id,
        "talent",
        room_id,
        config,
        runtime_paths_for(config),
        memberships,
    )


def test_explicit_user_and_alias_can_use_responder(tmp_path: Path) -> None:
    """Static grants must resolve bridge aliases before matching."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    config.authorization.aliases = {"@owner:example.com": ["@bridge-owner:example.com"]}
    memberships = AgentReplyMembershipIndex()

    assert _allowed("@owner:example.com", config, memberships)
    assert _allowed("@bridge-owner:example.com", config, memberships)
    assert not _allowed("@outsider:example.com", config, memberships)


def test_explicit_user_glob_can_use_responder(tmp_path: Path) -> None:
    """Static responder grants must retain glob matching."""
    config = membership_config(tmp_path, access={"users": ["@partner_*:example.com"]})
    memberships = AgentReplyMembershipIndex()

    assert _allowed("@partner_42:example.com", config, memberships)
    assert not _allowed("@partner_42:other.example", config, memberships)


def test_administrator_bypasses_responder_policy(tmp_path: Path) -> None:
    """Platform administrators must be able to use every responder."""
    config = membership_config(tmp_path, administrators=["@admin:example.com"], access={"users": []})

    assert _allowed("@admin:example.com", config, AgentReplyMembershipIndex())
    assert is_platform_administrator("@admin:example.com", config)


@pytest.mark.asyncio
async def test_current_room_member_can_use_responder(tmp_path: Path) -> None:
    """Current-room membership must grant access only when explicitly enabled."""
    sender_id = "@member:example.com"
    config = membership_config(
        tmp_path,
        agent_rooms=["talent"],
        access={"current_room_members": True, "members_of_rooms": []},
    )
    memberships = await membership_index(config, {"talent": {sender_id}})

    assert _allowed(sender_id, config, memberships, room_id="!talent:example.com")
    assert not _allowed(sender_id, config, memberships, room_id="!other:example.com")


def test_current_invite_access_is_bound_to_authenticated_inviter(tmp_path: Path) -> None:
    """Current-room invite evidence must authorize only its exact inviter."""
    sender_id = "@member:example.com"
    config = membership_config(
        tmp_path,
        access={"current_room_members": True, "members_of_rooms": []},
    )
    memberships = AgentReplyMembershipIndex()
    args = (
        sender_id,
        "talent",
        config,
        "!project:example.com",
        runtime_paths_for(config),
        memberships,
    )

    assert is_sender_allowed_for_agent_invite(*args, current_inviter_id=sender_id)
    assert not is_sender_allowed_for_agent_invite(
        *args,
        current_inviter_id="@different-member:example.com",
    )
    assert not is_sender_allowed_for_agent_invite(*args, current_inviter_id=None)


@pytest.mark.asyncio
async def test_grant_room_member_can_use_responder_elsewhere(tmp_path: Path) -> None:
    """A configured grant-room membership must authorize other conversation rooms."""
    sender_id = "@member:example.com"
    config = membership_config(
        tmp_path,
        agent_rooms=["grant"],
        access={"current_room_members": False, "members_of_rooms": ["grant"]},
    )
    memberships = await membership_index(config, {"grant": {sender_id}})

    assert _allowed(sender_id, config, memberships)


def test_unresolved_grant_room_fails_closed(tmp_path: Path) -> None:
    """An unresolved managed grant room must never authorize a sender."""
    config = membership_config(
        tmp_path,
        agent_rooms=["grant"],
        access={"members_of_rooms": ["grant"]},
    )

    assert not _allowed(
        "@member:example.com",
        config,
        unresolved_membership_index(config),
    )


def test_internal_identity_bypasses_responder_policy(tmp_path: Path) -> None:
    """Current runtime-owned identities must remain trusted participants."""
    config = membership_config(tmp_path, access={"users": []})
    sender_id = entity_ids(config, runtime_paths_for(config))["talent"].full_id

    assert _allowed(sender_id, config, AgentReplyMembershipIndex())


def test_credential_authority_is_separate_from_conversation_access(tmp_path: Path) -> None:
    """Credential managers must not gain responder access and vice versa."""
    config = membership_config(
        tmp_path,
        access={"users": ["@member:example.com"]},
        credential_managers=["@manager:example.com"],
    )

    assert is_sender_allowed_for_agent_credential_management("@manager:example.com", "talent", config)
    assert not _allowed("@manager:example.com", config, AgentReplyMembershipIndex())
    assert _allowed("@member:example.com", config, AgentReplyMembershipIndex())
    assert not is_sender_allowed_for_agent_credential_management("@member:example.com", "talent", config)


def test_unknown_agent_credential_management_fails_closed(tmp_path: Path) -> None:
    """Credential checks for stale or unknown agent names must deny instead of raising."""
    config = membership_config(tmp_path, administrators=["@admin:example.com"])

    assert not is_sender_allowed_for_agent_credential_management(
        "@admin:example.com",
        "missing",
        config,
    )


@pytest.mark.asyncio
async def test_room_reply_check_uses_same_responder_policy(tmp_path: Path) -> None:
    """Room-scoped reply checks must not apply a second authorization model."""
    sender_id = "@member:example.com"
    config = membership_config(
        tmp_path,
        agent_rooms=["talent"],
        access={"current_room_members": True, "members_of_rooms": []},
    )
    memberships = await membership_index(config, {"talent": {sender_id}})

    assert is_sender_allowed_for_agent_reply_in_room(
        sender_id,
        "talent",
        config,
        "!talent:example.com",
        runtime_paths_for(config),
        memberships,
    )


def test_effective_sender_uses_trusted_internal_relay_metadata(tmp_path: Path) -> None:
    """A current internal sender may relay the original requester identity."""
    config = membership_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    internal_sender = entity_ids(config, runtime_paths)["talent"].full_id
    event_source = {
        "content": {
            ORIGINAL_SENDER_KEY: "@owner:example.com",
            SOURCE_KIND_KEY: "trusted_internal_relay",
        },
    }

    assert (
        get_effective_sender_id_for_reply_permissions(
            internal_sender,
            event_source,
            config,
            runtime_paths,
        )
        == "@owner:example.com"
    )


def test_human_sender_cannot_spoof_original_requester(tmp_path: Path) -> None:
    """Original-sender metadata from a human sender must be ignored."""
    config = membership_config(tmp_path)
    event_source = {
        "content": {
            ORIGINAL_SENDER_KEY: "@owner:example.com",
            SOURCE_KIND_KEY: "trusted_internal_relay",
        },
    }

    assert (
        get_effective_sender_id_for_reply_permissions(
            "@human:example.com",
            event_source,
            config,
            runtime_paths_for(config),
        )
        == "@human:example.com"
    )
