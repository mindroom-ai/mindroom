"""Tests for the local-only external trigger manager tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.config.agent import AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class _Client:
    user_id = "@mindroom_watcher:example.org"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _config(
    *,
    admin_users: list[str] | None = None,
    private_watcher: bool = False,
    private_other: bool = False,
) -> Config:
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
            "agents": {
                "watcher": {
                    "display_name": "Watcher",
                    "role": "Watch external systems.",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "other": {
                    "display_name": "Other",
                    "role": "Other agent.",
                    "model": "default",
                    "rooms": ["other-room"],
                },
            },
            "rooms": {"lobby": {"display_name": "Lobby"}, "other-room": {"display_name": "Other"}},
            "external_trigger_policy": {"admin_users": admin_users or []},
            "authorization": {
                "global_users": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                "agent_reply_permissions": {
                    "*": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                },
            },
        },
    )
    if private_watcher:
        config.agents["watcher"].private = AgentPrivateConfig(per="user", root="watcher_data")
    if private_other:
        config.agents["other"].private = AgentPrivateConfig(per="user", root="other_data")
    return config


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@owner:example.org",
    room_id: str = "lobby",
    config: Config | None = None,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="watcher",
        room_id=room_id,
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id=requester_id,
        client=cast("Any", _Client()),
        config=config or _config(),
        runtime_paths=_runtime_paths(tmp_path),
        event_cache=cast("Any", object()),
        conversation_cache=cast("Any", object()),
    )


def _payload(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


def test_create_trigger_uses_current_context_and_hides_public_key(tmp_path: Path) -> None:
    """Trigger creation should use the current room and never echo raw key material."""
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(
            tool.create_trigger(
                "campground",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
                allowed_kinds=["campground.availability"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["endpoint_path"] == "/api/triggers/campground"
    assert payload["trigger"]["owner_user_id"] == "@owner:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "watcher",
        "new_thread": False,
        "room_id": "lobby",
        "thread_id": None,
    }
    assert "public_key" not in payload["trigger"]
    assert payload["public_key_fingerprint"].startswith("sha256:")


def test_private_agent_create_trigger_uses_owner_as_scope_owner(tmp_path: Path) -> None:
    """Private current-agent trigger records should stay scoped to the human requester."""
    config = _config(private_watcher=True)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        payload = _payload(
            tool.create_trigger(
                "private-campground",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
                allowed_kinds=["campground.availability"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == "@owner:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "watcher",
        "new_thread": False,
        "room_id": "lobby",
        "thread_id": None,
    }


def test_private_agent_create_trigger_accepts_current_dynamic_room(tmp_path: Path) -> None:
    """Private agents may bind triggers to their current live room without static rooms config."""
    config = _config(private_watcher=True)
    config.agents["watcher"].rooms = []
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(
        _context(
            tmp_path,
            requester_id="@owner:example.org",
            room_id="!private:example.org",
            config=config,
        ),
    ):
        payload = _payload(
            tool.create_trigger(
                "private-dynamic-campground",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
                allowed_kinds=["campground.availability"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == "@owner:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "watcher",
        "new_thread": False,
        "room_id": "!private:example.org",
        "thread_id": None,
    }


def test_non_admin_cannot_target_other_agent_or_room(tmp_path: Path) -> None:
    """Non-admin callers can only create triggers for the current agent and room."""
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(
            tool.create_trigger(
                "campground",
                public_key=_PUBLIC_KEY,
                target_agent="other",
                target_room_id="other-room",
            ),
        )

    assert payload["status"] == "error"
    assert "Only external trigger admins" in payload["message"]


def test_create_trigger_validation_error_returns_tool_error_payload(tmp_path: Path) -> None:
    """Bad model-backed tool input should remain a structured tool error."""
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        empty_thread_payload = _payload(
            tool.create_trigger(
                "campground",
                public_key=_PUBLIC_KEY,
                target_thread_id="  ",
            ),
        )
        conflict_payload = _payload(
            tool.create_trigger(
                "thread-conflict",
                public_key=_PUBLIC_KEY,
                target_thread_id="$thread",
                new_thread=True,
            ),
        )

    assert empty_thread_payload["status"] == "error"
    assert "thread_id" in empty_thread_payload["message"]
    assert conflict_payload["status"] == "error"
    assert "thread_id and new_thread" in conflict_payload["message"]


def test_manager_requires_live_human_requester_context(tmp_path: Path) -> None:
    """Trigger creation is available only to live human Matrix requesters."""
    tool = ExternalTriggerManagerTools()

    no_context_payload = _payload(tool.create_trigger("no-context", public_key=_PUBLIC_KEY))
    with tool_runtime_context(_context(tmp_path, requester_id=_Client.user_id)):
        bot_payload = _payload(tool.create_trigger("bot-requester", public_key=_PUBLIC_KEY))
    with tool_runtime_context(_context(tmp_path, requester_id="")):
        empty_payload = _payload(tool.create_trigger("empty-requester", public_key=_PUBLIC_KEY))

    assert no_context_payload["status"] == "error"
    assert "live Matrix tool context" in no_context_payload["message"]
    assert bot_payload["status"] == "error"
    assert "human Matrix requester" in bot_payload["message"]
    assert empty_payload["status"] == "error"
    assert "human Matrix requester" in empty_payload["message"]


def test_admin_can_create_trigger_for_configured_cross_target(tmp_path: Path) -> None:
    """Trigger admins can target another configured agent and room explicitly."""
    config = _config(admin_users=["@admin:example.org"])
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(
            tool.create_trigger(
                "admin-target",
                public_key=_PUBLIC_KEY,
                target_agent="other",
                target_room_id="other-room",
                target_thread_id="$target-thread",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == "@admin:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "other",
        "new_thread": False,
        "room_id": "other-room",
        "thread_id": "$target-thread",
    }


def test_admin_create_trigger_for_private_cross_target_keeps_admin_owner(tmp_path: Path) -> None:
    """Admin-created private cross-target triggers should stay owned by the admin requester."""
    config = _config(admin_users=["@admin:example.org"], private_other=True)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(
            tool.create_trigger(
                "admin-private-target",
                public_key=_PUBLIC_KEY,
                target_agent="other",
                target_room_id="other-room",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == "@admin:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "other",
        "new_thread": False,
        "room_id": "other-room",
        "thread_id": None,
    }


def test_admin_list_triggers_sees_all_owners(tmp_path: Path) -> None:
    """Admins should be able to inspect trigger records across owners."""
    config = _config(admin_users=["@admin:example.org"])
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        assert _payload(tool.create_trigger("owner", public_key=_PUBLIC_KEY))["status"] == "ok"
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        assert _payload(tool.create_trigger("other", public_key=_PUBLIC_KEY))["status"] == "ok"
    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(tool.list_triggers())

    assert payload["status"] == "ok"
    assert {trigger["trigger_id"] for trigger in payload["triggers"]} == {"owner", "other"}
