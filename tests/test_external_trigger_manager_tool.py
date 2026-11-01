"""Tests for the local-only external trigger manager tool."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.config.agent import AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    event_serving_client,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_thread_reply_lookup,
    thread_reply_client,
)
from tests.matrix_state_helpers import save_matrix_state
from tests.test_generated_agents import OWNER, _created, _load, _personal, _with_template

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class _Client:
    user_id = "@mindroom_watcher:mindroom.chat"


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _config(
    *,
    admin_users: list[str] | None = None,
    platform_administrators: list[str] | None = None,
    private_watcher: bool = False,
    private_other: bool = False,
) -> Config:
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "agents": {
                "watcher": {
                    "display_name": "Watcher",
                    "role": "Watch external systems.",
                    "model": "default",
                    "rooms": ["lobby"],
                    "access": {
                        "users": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                    },
                },
                "other": {
                    "display_name": "Other",
                    "role": "Other agent.",
                    "model": "default",
                    "rooms": ["other-room"],
                    "access": {
                        "users": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                    },
                },
            },
            "rooms": {"lobby": {"display_name": "Lobby"}, "other-room": {"display_name": "Other"}},
            "external_trigger_policy": {"admin_users": admin_users or []},
            "administrators": platform_administrators or [],
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
    agent_name: str = "watcher",
) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(
            room_id=room_id,
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id=requester_id,
        client=cast("Any", _Client()),
        config=config or _config(),
        runtime_paths=_runtime_paths(tmp_path),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def _record_rooms(tmp_path: Path) -> None:
    """Record lobby and other-room as created, so their keys and aliases resolve to room IDs."""
    state = MatrixState.load(_runtime_paths(tmp_path))
    for key, name in (("lobby", "Lobby"), ("other-room", "Other")):
        state.add_room(key, f"!{key}:example.org", f"#{key}:example.org", name)
    save_matrix_state(state, _runtime_paths(tmp_path))


def _payload(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


@pytest.mark.asyncio
async def test_create_trigger_uses_current_context_and_hides_public_key(tmp_path: Path) -> None:
    """Trigger creation should use the current room and never echo raw key material."""
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        payload = _payload(
            await tool.create_trigger(
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


@pytest.mark.asyncio
@pytest.mark.parametrize("administrator_source", ["trigger", "platform"])
async def test_external_trigger_admin_uses_current_policy(tmp_path: Path, administrator_source: str) -> None:
    """A long-lived tool context must not retain either revoked admin grant."""
    old_config = _config(
        admin_users=["@admin:example.org"] if administrator_source == "trigger" else None,
        platform_administrators=["@admin:example.org"] if administrator_source == "platform" else None,
    )
    current_config = _config(admin_users=[])
    context = replace(
        _context(tmp_path, requester_id="@admin:example.org", config=old_config),
        config_provider=lambda: current_config,
    )

    with tool_runtime_context(context):
        payload = _payload(
            await ExternalTriggerManagerTools().create_trigger(
                "revoked-admin",
                public_key=_PUBLIC_KEY,
                target_agent="other",
            ),
        )

    assert payload["status"] == "error"
    assert "Only external trigger admins" in payload["message"]


@pytest.mark.asyncio
async def test_private_agent_create_trigger_uses_owner_as_scope_owner(tmp_path: Path) -> None:
    """Private current-agent trigger records should stay scoped to the human requester."""
    config = _config(private_watcher=True)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        payload = _payload(
            await tool.create_trigger(
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


@pytest.mark.asyncio
async def test_private_agent_create_trigger_accepts_current_dynamic_room(tmp_path: Path) -> None:
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
            await tool.create_trigger(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "roster", "agent_name"),
    [
        pytest.param(_with_template(), {"entries": [_personal()]}, "mind_alice", id="personal"),
        pytest.param(None, {"created": [_created()]}, "creator_research", id="created"),
    ],
)
async def test_generated_agent_create_trigger_accepts_current_dynamic_room(
    tmp_path: Path,
    data: dict[str, Any] | None,
    roster: dict[str, Any],
    agent_name: str,
) -> None:
    """Personal and created agents may bind a trigger to the room they answer in, such as a personal room."""
    config = _load(tmp_path, data, **roster)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(
        _context(tmp_path, requester_id=OWNER, room_id="!personal:localhost", config=config, agent_name=agent_name),
    ):
        payload = _payload(await tool.create_trigger("dynamic-campground", public_key=_PUBLIC_KEY))

    assert payload["status"] == "ok", payload
    assert payload["trigger"]["target"]["agent"] == agent_name
    assert payload["trigger"]["target"]["room_id"] == "!personal:localhost"


@pytest.mark.asyncio
async def test_generated_agent_create_trigger_rejects_room_configured_for_others(tmp_path: Path) -> None:
    """A room with configured responders answers only them, so a generated agent may not bind a trigger there."""
    config = _load(tmp_path, created=[_created()])
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(
        _context(tmp_path, requester_id=OWNER, room_id="lobby", config=config, agent_name="creator_research"),
    ):
        payload = _payload(await tool.create_trigger("lobby-campground", public_key=_PUBLIC_KEY))

    assert payload["status"] == "error"
    assert "target room must already be configured" in payload["message"]


@pytest.mark.asyncio
async def test_shared_agent_create_trigger_rejects_current_dynamic_room(tmp_path: Path) -> None:
    """Shared agents still need a configured target room, even when it is the current room."""
    config = _config()
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
            await tool.create_trigger(
                "shared-dynamic-campground",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
            ),
        )

    assert payload["status"] == "error"
    assert "target room must already be configured" in payload["message"]


@pytest.mark.asyncio
async def test_private_agent_create_trigger_rejects_other_dynamic_room(tmp_path: Path) -> None:
    """Private agents may not bind triggers to a different dynamic room."""
    config = _config(admin_users=["@admin:example.org"], private_watcher=True)
    config.agents["watcher"].rooms = []
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(
        _context(
            tmp_path,
            requester_id="@admin:example.org",
            room_id="!private:example.org",
            config=config,
        ),
    ):
        payload = _payload(
            await tool.create_trigger(
                "private-other-room",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
                target_room_id="!other:example.org",
            ),
        )

    assert payload["status"] == "error"
    assert "target room must already be configured" in payload["message"]


@pytest.mark.asyncio
async def test_admin_cross_target_private_agent_rejects_current_dynamic_room(tmp_path: Path) -> None:
    """Admins may not use a creator's dynamic room for a different private target agent."""
    config = _config(admin_users=["@admin:example.org"], private_other=True)
    config.agents["other"].rooms = []
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(
        _context(
            tmp_path,
            requester_id="@admin:example.org",
            room_id="!private:example.org",
            config=config,
        ),
    ):
        payload = _payload(
            await tool.create_trigger(
                "private-cross-target-dynamic-room",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
                target_agent="other",
                target_room_id="!private:example.org",
            ),
        )

    assert payload["status"] == "error"
    assert "target room must already be configured" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_agent", "target_room_id"),
    [
        ("other", None),
        (None, "other-room"),
        (None, "#other-room:example.org"),
        (None, "!other-room:example.org"),
        (None, "#elsewhere:example.org"),
    ],
)
async def test_non_admin_cannot_target_other_agent_or_room(
    tmp_path: Path,
    target_agent: str | None,
    target_room_id: str | None,
) -> None:
    """Non-admin callers can only create triggers for the current agent and room, and a refused room is never read."""
    tool = ExternalTriggerManagerTools()
    _record_rooms(tmp_path)
    homeserver = event_serving_client("$other-thread:example.org", content={"msgtype": "m.text", "body": "root"})
    context = replace(
        _context(tmp_path, room_id="!lobby:example.org"),
        relations=make_relation_lookup(client=homeserver),
    )
    with tool_runtime_context(context):
        payload = _payload(
            await tool.create_trigger(
                "campground",
                public_key=_PUBLIC_KEY,
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id="$other-thread:example.org",
            ),
        )

    assert payload["status"] == "error"
    assert "Only external trigger admins" in payload["message"]
    homeserver.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_room_id", ["lobby", "#lobby:example.org", "!lobby:example.org"])
async def test_non_admin_can_name_the_current_room_by_key_alias_or_id(tmp_path: Path, target_room_id: str) -> None:
    """A non-admin naming the current room in any form it resolves from targets that room, not a different one."""
    tool = ExternalTriggerManagerTools()
    _record_rooms(tmp_path)
    with tool_runtime_context(_context(tmp_path, room_id="!lobby:example.org")):
        payload = _payload(
            await tool.create_trigger("lobby-trigger", public_key=_PUBLIC_KEY, target_room_id=target_room_id),
        )

    assert payload["status"] == "ok", payload
    assert payload["trigger"]["target"]["room_id"] == target_room_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requester_id", "target_agent", "target_room_id", "matrix_room_id"),
    [
        ("@owner:example.org", None, "lobby", "!lobby:example.org"),
        ("@admin:example.org", "other", "other-room", "!other-room:example.org"),
    ],
    ids=["current-room", "admin-other-room"],
)
async def test_create_trigger_stores_a_reply_thread_id_as_its_thread_root(
    tmp_path: Path,
    requester_id: str,
    target_agent: str | None,
    target_room_id: str,
    matrix_room_id: str,
) -> None:
    """A reply event ID as target_thread_id is stored as its thread root, since Matrix rejects a reply as a thread root."""
    tool = ExternalTriggerManagerTools()
    _record_rooms(tmp_path)
    homeserver = thread_reply_client("$reply:example.org", "$root:example.org")
    context = replace(
        _context(
            tmp_path,
            requester_id=requester_id,
            room_id="!lobby:example.org",
            config=_config(admin_users=["@admin:example.org"]),
        ),
        relations=make_relation_lookup(client=homeserver),
    )
    with tool_runtime_context(context):
        payload = _payload(
            await tool.create_trigger(
                "replies",
                public_key=_PUBLIC_KEY,
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id="$reply:example.org",
            ),
        )
        listed = _payload(tool.list_triggers())

    assert payload["status"] == "ok", payload
    assert payload["trigger"]["target"]["thread_id"] == "$root:example.org"
    assert [trigger["target"]["thread_id"] for trigger in listed["triggers"]] == ["$root:example.org"]
    homeserver.room_get_event.assert_awaited_with(matrix_room_id, "$reply:example.org")


@pytest.mark.asyncio
async def test_create_trigger_refuses_a_reply_whose_thread_root_is_blank(tmp_path: Path) -> None:
    """A reply naming a blank thread root is refused like a blank target_thread_id, and the store stays readable."""
    tool = ExternalTriggerManagerTools()
    context = replace(_context(tmp_path), relations=make_thread_reply_lookup("$reply:example.org", " "))
    with tool_runtime_context(context):
        payload = _payload(
            await tool.create_trigger("blank-root", public_key=_PUBLIC_KEY, target_thread_id="$reply:example.org"),
        )
        listed = _payload(tool.list_triggers())

    assert payload["status"] == "error"
    assert "thread_id" in payload["message"]
    assert listed == {**listed, "status": "ok", "triggers": []}


@pytest.mark.asyncio
async def test_rotate_trigger_key_returns_the_new_key_fingerprint(tmp_path: Path) -> None:
    """Rotation reports the fingerprint of the key it stored, never the key itself."""
    new_key_bytes = bytes(range(32))
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        created = _payload(await tool.create_trigger("rotating", public_key=_PUBLIC_KEY))
        rotated = _payload(
            tool.rotate_trigger_key("rotating", public_key=base64.b64encode(new_key_bytes).decode("ascii")),
        )

    assert rotated["status"] == "ok"
    assert rotated["public_key_fingerprint"] == f"sha256:{hashlib.sha256(new_key_bytes).hexdigest()}"
    assert rotated["public_key_fingerprint"] != created["public_key_fingerprint"]
    assert rotated["trigger"]["public_key_fingerprint"] == rotated["public_key_fingerprint"]
    assert "public_key" not in rotated["trigger"]


@pytest.mark.asyncio
async def test_create_trigger_validation_error_returns_tool_error_payload(tmp_path: Path) -> None:
    """Bad model-backed tool input should remain a structured tool error."""
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path)):
        empty_thread_payload = _payload(
            await tool.create_trigger(
                "campground",
                public_key=_PUBLIC_KEY,
                target_thread_id="  ",
            ),
        )
        conflict_payload = _payload(
            await tool.create_trigger(
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


@pytest.mark.asyncio
async def test_manager_requires_live_tool_context() -> None:
    """Trigger management is available only inside a live Matrix tool call."""
    payload = _payload(await ExternalTriggerManagerTools().create_trigger("no-context", public_key=_PUBLIC_KEY))

    assert payload["status"] == "error"
    assert payload["message"] == "External trigger manager requires live Matrix tool context."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requester_id", "message"),
    [
        (_Client.user_id, "human requester"),
        ("@mindroom_other:mindroom.chat", "human requester"),
        ("@mindroom_router:mindroom.chat", "human requester"),
        ("@bridgebot:example.org", "human requester"),
        ("@alias-bot:example.org", "human requester"),
        ("", "Invalid Matrix ID"),
    ],
    ids=["own-account", "other-agent", "router", "bot-account", "bot-alias-of-admin", "empty"],
)
async def test_every_manager_function_refuses_a_requester_who_cannot_own_triggers(
    tmp_path: Path,
    requester_id: str,
    message: str,
) -> None:
    """MindRoom's own accounts, bot accounts, and malformed IDs can neither own nor administer triggers."""
    config = _config(admin_users=["@bridgebot:example.org", "@trigger-admin:example.org"])
    config.bot_accounts = ["@bridgebot:example.org", "@alias-bot:example.org"]
    config.authorization.aliases = {"@trigger-admin:example.org": ["@alias-bot:example.org"]}
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path, config=config)):
        assert _payload(await tool.create_trigger("owned", public_key=_PUBLIC_KEY))["status"] == "ok"

    with tool_runtime_context(_context(tmp_path, requester_id=requester_id, config=config)):
        payloads = [
            _payload(await tool.create_trigger("refused", public_key=_PUBLIC_KEY)),
            _payload(tool.list_triggers()),
            _payload(tool.disable_trigger("owned")),
            _payload(tool.delete_trigger("owned")),
            _payload(tool.rotate_trigger_key("owned", public_key=_PUBLIC_KEY)),
        ]

    assert [payload["status"] for payload in payloads] == ["error"] * len(payloads)
    assert all(message in payload["message"] for payload in payloads)
    with tool_runtime_context(_context(tmp_path, config=config)):
        triggers = _payload(tool.list_triggers())["triggers"]
    assert [(trigger["trigger_id"], trigger["enabled"], trigger["version"]) for trigger in triggers] == [
        ("owned", True, 1),
    ]


def test_manager_refuses_a_runtime_without_primary_control_state(tmp_path: Path) -> None:
    """A runtime without primary control state, such as a worker's, cannot manage triggers."""
    context = _context(tmp_path)
    context = replace(context, runtime_paths=replace(context.runtime_paths, control_state_root=None))

    with tool_runtime_context(context):
        payload = _payload(ExternalTriggerManagerTools().list_triggers())

    assert payload["status"] == "error"
    assert "requires primary control state" in payload["message"]


@pytest.mark.asyncio
async def test_admin_can_create_trigger_for_configured_cross_target(tmp_path: Path) -> None:
    """Trigger admins can target another configured agent and room explicitly."""
    config = _config(admin_users=["@admin:example.org"])
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(
            await tool.create_trigger(
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


@pytest.mark.asyncio
async def test_platform_admin_can_create_trigger_for_configured_cross_target(tmp_path: Path) -> None:
    """Platform administrators must receive trigger administration without a second allowlist."""
    requester = "@platform-admin:example.org"
    config = _config(platform_administrators=[requester])
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id=requester, config=config)):
        payload = _payload(
            await tool.create_trigger(
                "platform-admin-target",
                public_key=_PUBLIC_KEY,
                target_agent="other",
                target_room_id="other-room",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == requester
    assert payload["trigger"]["target"]["agent"] == "other"


@pytest.mark.asyncio
async def test_external_trigger_admin_alias_has_canonical_authority(tmp_path: Path) -> None:
    """A configured bridge alias must receive its canonical trigger administrator authority."""
    canonical_admin = "@trigger-admin:example.org"
    alias_admin = "@bridge-trigger-admin:example.org"
    config = _config(admin_users=[canonical_admin])
    config.authorization.aliases = {canonical_admin: [alias_admin]}
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id=alias_admin, config=config)):
        payload = _payload(
            await tool.create_trigger(
                "alias-admin-target",
                public_key=_PUBLIC_KEY,
                target_agent="other",
                target_room_id="other-room",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == alias_admin
    assert payload["trigger"]["target"]["agent"] == "other"


@pytest.mark.asyncio
async def test_admin_create_trigger_for_private_cross_target_keeps_admin_owner(tmp_path: Path) -> None:
    """Admin-created private cross-target triggers should stay owned by the admin requester."""
    config = _config(admin_users=["@admin:example.org"], private_other=True)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(
            await tool.create_trigger(
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


@pytest.mark.asyncio
async def test_admin_list_triggers_sees_all_owners(tmp_path: Path) -> None:
    """Admins should be able to inspect trigger records across owners."""
    config = _config(admin_users=["@admin:example.org"])
    tool = ExternalTriggerManagerTools()
    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        assert _payload(await tool.create_trigger("owner", public_key=_PUBLIC_KEY))["status"] == "ok"
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        assert _payload(await tool.create_trigger("other", public_key=_PUBLIC_KEY))["status"] == "ok"
    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(tool.list_triggers())

    assert payload["status"] == "ok"
    assert {trigger["trigger_id"] for trigger in payload["triggers"]} == {"owner", "other"}
