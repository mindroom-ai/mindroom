"""Tests for membership-based responder and authority checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import (
    ReplyMembershipPendingError,
    _ReplyAuthorizationDecision,
    _responder_reply_authorization,
    get_effective_sender_id_for_reply_permissions,
    is_platform_administrator,
    is_sender_allowed_for_agent_credential_management,
    is_sender_allowed_for_agent_reply_in_room,
    is_sender_allowed_for_entity_replies_in_room,
    is_sender_allowed_for_responder,
)
from mindroom.config.access import ResponderAccessConfig
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
    assert is_platform_administrator("@admin:example.com", config, runtime_paths_for(config))


def test_bot_alias_cannot_inherit_human_authority(tmp_path: Path) -> None:
    """A configured bot alias must not become its canonical human principal."""
    human_id = "@owner:example.com"
    bot_id = "@bridgebot:example.com"
    config = membership_config(
        tmp_path,
        administrators=[human_id],
        access={"users": [human_id]},
        credential_managers=[human_id],
    )
    config.bot_accounts = [bot_id]
    config.authorization.aliases = {human_id: [bot_id]}

    assert not _allowed(bot_id, config, AgentReplyMembershipIndex())
    runtime_paths = runtime_paths_for(config)
    assert not is_platform_administrator(bot_id, config, runtime_paths)
    assert not is_sender_allowed_for_agent_credential_management(bot_id, "talent", config, runtime_paths)


def test_managed_entity_alias_cannot_inherit_human_administration(tmp_path: Path) -> None:
    """A managed entity alias must not inherit human control-plane authority."""
    human_id = "@owner:example.com"
    config = membership_config(
        tmp_path,
        administrators=[human_id],
        credential_managers=[human_id],
    )
    runtime_paths = runtime_paths_for(config)
    managed_id = entity_ids(config, runtime_paths)["talent"].full_id
    config.authorization.aliases = {human_id: [managed_id]}

    assert not is_platform_administrator(managed_id, config, runtime_paths)
    assert not is_sender_allowed_for_agent_credential_management(managed_id, "talent", config, runtime_paths)


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

    runtime_paths = runtime_paths_for(config)
    assert is_sender_allowed_for_agent_credential_management("@manager:example.com", "talent", config, runtime_paths)
    assert not _allowed("@manager:example.com", config, AgentReplyMembershipIndex())
    assert _allowed("@member:example.com", config, AgentReplyMembershipIndex())
    assert not is_sender_allowed_for_agent_credential_management("@member:example.com", "talent", config, runtime_paths)


def test_unknown_agent_credential_management_fails_closed(tmp_path: Path) -> None:
    """Credential checks for stale or unknown agent names must deny instead of raising."""
    config = membership_config(tmp_path, administrators=["@admin:example.com"])

    assert not is_sender_allowed_for_agent_credential_management(
        "@admin:example.com",
        "missing",
        config,
        runtime_paths_for(config),
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


@pytest.mark.asyncio
async def test_relevant_membership_certainty_and_alternative_grants(tmp_path: Path) -> None:
    """Unrelated uncertainty cannot block proven grants or turn ready denials into retries."""
    config = membership_config(
        tmp_path,
        administrators=["@admin:example.com"],
        agent_rooms=["grant", "other"],
        access={"members_of_rooms": ["grant", "other"], "users": ["@owner*:example.com"]},
    )
    config.authorization.aliases = {"@owner:example.com": ["@bridge:example.com"]}
    paths = runtime_paths_for(config)
    index = await membership_index(config, {"grant": {"@member:example.com"}, "other": set()})
    index.mark_room_unready(config, paths, "!other:example.com", reason="refresh_failed")
    for sender in (
        "@member:example.com",
        "@admin:example.com",
        "@owner:example.com",
        "@bridge:example.com",
        entity_ids(config, paths)["talent"].full_id,
    ):
        assert is_sender_allowed_for_agent_reply_in_room(
            sender,
            "talent",
            config,
            "!current:example.com",
            paths,
            index,
            require_resolved_membership=True,
        )
    with pytest.raises(ReplyMembershipPendingError):
        is_sender_allowed_for_agent_reply_in_room(
            "@outsider:example.com",
            "talent",
            config,
            "!current:example.com",
            paths,
            index,
            require_resolved_membership=True,
        )
    # Refresh a narrower relevant grant while an unrelated configured room is still unknown.
    config.agents["talent"].access = ResponderAccessConfig(members_of_rooms=["grant"])
    config.router.access = ResponderAccessConfig(members_of_rooms=["other"])
    index = await membership_index(config, {"grant": set(), "other": set()})
    index.mark_room_unready(config, paths, "!other:example.com", reason="refresh_failed")
    assert (
        _responder_reply_authorization(
            "@outsider:example.com",
            "talent",
            "!current:example.com",
            config,
            paths,
            index,
        )
        is _ReplyAuthorizationDecision.DENIED
    )
    assert not is_sender_allowed_for_entity_replies_in_room(
        "@outsider:example.com",
        ("router", "talent"),
        config,
        "!current:example.com",
        paths,
        index,
        require_resolved_membership=True,
    )


@pytest.mark.asyncio
async def test_current_room_absence_differs_from_unknown_joined_rooms(tmp_path: Path) -> None:
    """A missing current room is denied only after authoritative joined-room discovery."""
    config = membership_config(
        tmp_path,
        agent_rooms=["other"],
        access={"current_room_members": True, "members_of_rooms": []},
    )
    paths = runtime_paths_for(config)
    index = await membership_index(config, {"other": set()})
    index.mark_room_unready(config, paths, "!other:example.com", reason="refresh_failed")
    assert (
        _responder_reply_authorization(
            "@outsider:example.com",
            "talent",
            "!absent:example.com",
            config,
            paths,
            index,
        )
        is _ReplyAuthorizationDecision.DENIED
    )
    index.invalidate(config, reason="uncertain_sync_response")
    assert (
        _responder_reply_authorization(
            "@outsider:example.com",
            "talent",
            "!absent:example.com",
            config,
            paths,
            index,
        )
        is _ReplyAuthorizationDecision.PENDING
    )
    assert not _allowed("@outsider:example.com", config, index, room_id="!absent:example.com")
