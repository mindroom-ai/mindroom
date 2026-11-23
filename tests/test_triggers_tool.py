"""Tests for the unified `triggers` toolkit (reactive + direct external triggers)."""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.external_trigger_policy import (
    Ed25519EventSourceConfig,
    ExternalTriggerPolicyConfig,
    GitHubEventSourceConfig,
)
from mindroom.config.main import Config
from mindroom.config.models import lazy_loading_prohibited_reason
from mindroom.config.tool_aliases import TOOL_ALIASES, canonical_tool_name
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.event_journal import ConversationCursor
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.thread_tags import ThreadTagRecord, ThreadTagsError, ThreadTagsState
from mindroom.tool_system.catalog import builtin_tool_registry, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    make_conversation_reader_mock,
    make_relation_lookup,
    make_thread_reply_lookup,
)
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _record_rooms(tmp_path: Path) -> None:
    state = MatrixState.load(_runtime_paths(tmp_path))
    for key, name in (("lobby", "Lobby"), ("other-room", "Other"), ("secret-room", "Secret")):
        state.add_room(key, f"!{key}:example.org", f"#{key}:example.org", name)
    save_matrix_state(state, _runtime_paths(tmp_path))


def _config(
    *,
    admin_users: list[str] | None = None,
    enabled: bool = True,
    reactive_enabled: bool = True,
    max_reactive_triggers_per_owner: int = 50,
    max_fires_per_hour: int = 60,
    event_sources: dict[str, Any] | None = None,
) -> Config:
    return Config.model_validate(
        {
            "models": {
                "default": {"provider": "openai", "id": "gpt-6-astra"},
                "fast": {"provider": "openai", "id": "gpt-6-mini"},
            },
            "agents": {
                "watcher": {
                    "display_name": "Watcher",
                    "role": "Watch rooms and events.",
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
                        "users": ["@owner:example.org", "@admin:example.org"],
                    },
                },
                "restricted": {
                    "display_name": "Restricted",
                    "role": "Restricted agent.",
                    "model": "default",
                    "rooms": ["secret-room"],
                    "access": {
                        "users": ["@admin:example.org"],
                    },
                },
            },
            "rooms": {
                "lobby": {"display_name": "Lobby"},
                "other-room": {"display_name": "Other"},
                "secret-room": {"display_name": "Secret"},
            },
            "external_trigger_policy": ExternalTriggerPolicyConfig(
                enabled=enabled,
                reactive_enabled=reactive_enabled,
                admin_users=admin_users or [],
                max_reactive_triggers_per_owner=max_reactive_triggers_per_owner,
                max_fires_per_hour=max_fires_per_hour,
                event_sources=event_sources or {},
            ).model_dump(mode="json"),
        },
    )


def _room_state_client(
    *,
    user_id: str = "@mindroom_watcher:mindroom.chat",
    room_members: dict[str, list[str]] | None = None,
) -> SimpleNamespace:
    members_map = room_members or {}
    rooms = {
        room_id: SimpleNamespace(room_id=room_id, users={uid: SimpleNamespace(user_id=uid) for uid in members})
        for room_id, members in members_map.items()
    }

    async def room_get_state(room_id: str) -> object:
        if room_id not in members_map:
            return nio.RoomGetStateError(message="not found", status_code="M_NOT_FOUND")
        events = [
            {
                "type": "m.room.member",
                "state_key": uid,
                "sender": uid,
                "event_id": f"$mem_{idx}",
                "origin_server_ts": 1_000,
                "content": {"membership": "join"},
            }
            for idx, uid in enumerate(members_map[room_id])
        ]
        return nio.RoomGetStateResponse(events=events, room_id=room_id)

    return SimpleNamespace(
        user_id=user_id,
        rooms=rooms,
        room_get_state=AsyncMock(side_effect=room_get_state),
    )


def _make_orchestrator(
    *,
    bots: dict[str, object] | None = None,
) -> SimpleNamespace:
    reactive_triggers = SimpleNamespace(notify_work=MagicMock())
    return SimpleNamespace(
        agent_bots=bots or {},
        reactive_triggers=reactive_triggers,
    )


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@owner:example.org",
    room_id: str = "!lobby:example.org",
    thread_id: str | None = "$thread_root:example.org",
    config: Config | None = None,
    agent_name: str = "watcher",
    client: object | None = None,
    orchestrator: object | None = None,
    conversation_reader: object | None = None,
    relations: object | None = None,
) -> ToolRuntimeContext:
    reader = cast("Any", conversation_reader or make_conversation_reader_mock())
    if not hasattr(reader, "latest_room_cursor"):
        reader.latest_room_cursor = AsyncMock(return_value=None)
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id=requester_id,
        client=cast("Any", client or _room_state_client(room_members={"!lobby:example.org": [requester_id]})),
        config=config or _config(),
        runtime_paths=_runtime_paths(tmp_path),
        relations=cast("Any", relations or make_relation_lookup()),
        conversation_reader=reader,
        orchestrator=cast("Any", orchestrator),
    )


def _payload(raw: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(raw))


@pytest.mark.asyncio
async def test_watch_room_creates_rule_with_origin_default_cursor_and_notify(tmp_path: Path) -> None:
    """watch_room defaults to target_thread_id='origin' in same room, stores journal cursor, and notifies runtime."""
    _record_rooms(tmp_path)
    ahead_ts = int(time.time() * 1000) + 5_000
    reader = make_conversation_reader_mock()
    reader.latest_room_cursor = AsyncMock(
        return_value=ConversationCursor(created_ts=ahead_ts, logical_event_id="$latest:example.org"),
    )
    orchestrator = _make_orchestrator()
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, conversation_reader=reader, orchestrator=orchestrator)):
        payload = _payload(
            await tool.watch_room(
                prompt="Alert me on urgent deploys.",
                pattern=r"\bdeploy\b",
                senders=["@alice:example.org"],
                triage_instructions="Fire only if a production deploy failed.",
                triage_model="fast",
                description="Prod deploy watch",
            ),
        )

    assert payload["status"] == "ok", payload
    assert payload["tool"] == "triggers"
    assert payload["action"] == "watch_room"
    trigger = payload["trigger"]
    assert trigger["trigger_id"].startswith("tr_")
    assert trigger["owner_user_id"] == "@owner:example.org"
    assert trigger["source"] == {
        "source_type": "room_message",
        "room_id": "!lobby:example.org",
        "thread_id": None,
        "senders": ["@alice:example.org"],
        "pattern": r"\bdeploy\b",
        "include_bots": False,
    }
    assert trigger["target"] == {
        "agent": "watcher",
        "room_id": "!lobby:example.org",
        "thread_id": "origin",
        "new_thread": False,
        "thread_key": None,
        "prompt": "Alert me on urgent deploys.",
    }
    assert trigger["triage"]["provider"] == "llm"
    assert trigger["triage"]["model"] == "fast"
    assert trigger["once"] is False

    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    cursor = store.get_room_cursor(trigger["trigger_id"])
    assert cursor is not None
    assert cursor.created_ts == ahead_ts
    assert cursor.logical_event_id == "$latest:example.org"
    orchestrator.reactive_triggers.notify_work.assert_called_once()


@pytest.mark.asyncio
async def test_watch_room_cross_room_target_defaults_to_room_timeline(tmp_path: Path) -> None:
    """When targeting a different room with no target_thread_id, watch_room defaults to room timeline (None)."""
    _record_rooms(tmp_path)
    client = _room_state_client(
        room_members={
            "!lobby:example.org": ["@owner:example.org"],
            "!other-room:example.org": ["@owner:example.org"],
        },
    )
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, client=client)):
        payload = _payload(
            await tool.watch_room(
                prompt="Forward alert to other room.",
                target_agent="other",
                target_room_id="other-room",
            ),
        )

    assert payload["status"] == "ok", payload
    assert payload["trigger"]["target"]["agent"] == "other"
    assert payload["trigger"]["target"]["room_id"] == "!other-room:example.org"
    assert payload["trigger"]["target"]["thread_id"] is None


@pytest.mark.asyncio
async def test_watch_room_rejects_unauthorized_or_unjoined_other_room(tmp_path: Path) -> None:
    """Watching another room requires both room membership and agent access in that room."""
    _record_rooms(tmp_path)
    client_not_joined = _room_state_client(
        room_members={
            "!lobby:example.org": ["@owner:example.org"],
            "!other-room:example.org": ["@admin:example.org"],
        },
    )
    client_no_access = _room_state_client(
        room_members={
            "!lobby:example.org": ["@owner:example.org"],
            "!secret-room:example.org": ["@owner:example.org", "@admin:example.org"],
        },
    )
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, client=client_not_joined)):
        not_joined = _payload(await tool.watch_room(prompt="Spy on other room", room_id="other-room"))
    with tool_runtime_context(_context(tmp_path, client=client_no_access)):
        no_access = _payload(await tool.watch_room(prompt="Spy on secret room", room_id="secret-room"))

    assert not_joined["status"] == "error"
    assert "joined" in not_joined["message"].lower()
    assert no_access["status"] == "error"
    assert "authorized" in no_access["message"].lower()


@pytest.mark.asyncio
async def test_watch_room_validation_triage_caps_expiry_and_quota(tmp_path: Path) -> None:
    """watch_room validates triage model aliases, policy caps, expiry bounds, thread conflicts, and quotas."""
    _record_rooms(tmp_path)
    config = _config(max_fires_per_hour=10, max_reactive_triggers_per_owner=1)
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, config=config)):
        unknown_model = _payload(
            await tool.watch_room(
                prompt="Watch",
                triage_instructions="Check urgency",
                triage_model="nonexistent",
            ),
        )
        over_cap = _payload(await tool.watch_room(prompt="Watch", max_fires_per_hour=11))
        bad_expiry = _payload(await tool.watch_room(prompt="Watch", expires_in_hours=0))
        huge_expiry = _payload(await tool.watch_room(prompt="Watch", expires_in_hours=9000))
        nan_expiry = _payload(await tool.watch_room(prompt="Watch", expires_in_hours=math.nan))
        conflict = _payload(
            await tool.watch_room(prompt="Watch", target_thread_id="$t1:example.org", new_thread=True),
        )
        ok = _payload(await tool.watch_room(prompt="Watch", expires_in_hours=24, max_fires_per_hour=10))
        over_quota = _payload(await tool.watch_room(prompt="Watch second"))

    assert unknown_model["status"] == "error"
    assert "fast" in unknown_model["message"]
    assert over_cap["status"] == "error"
    assert "10" in over_cap["message"]
    assert bad_expiry["status"] == "error"
    assert huge_expiry["status"] == "error"
    assert nan_expiry["status"] == "error"
    assert conflict["status"] == "error"
    assert ok["status"] == "ok", ok
    assert ok["trigger"]["expires_at"] is not None
    assert ok["trigger"]["expires_at"] > time.time() + 23 * 3600
    assert over_quota["status"] == "error"
    assert "limit" in over_quota["message"].lower()


@pytest.mark.asyncio
async def test_watch_room_rejects_disabled_policy_and_cross_store_id_collision(tmp_path: Path) -> None:
    """watch_room enforces reactive_enabled and prevents trigger_id collisions across direct and reactive stores."""
    _record_rooms(tmp_path)
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, config=_config(reactive_enabled=False))):
        disabled = _payload(await tool.watch_room(prompt="Watch"))
    assert disabled["status"] == "error"
    assert "disabled" in disabled["message"].lower()

    with tool_runtime_context(_context(tmp_path)):
        direct = _payload(await tool.create_trigger("shared-id", public_key=_PUBLIC_KEY))
        assert direct["status"] == "ok", direct
        reactive_collision = _payload(await tool.watch_room(prompt="Watch", trigger_id="shared-id"))
        assert reactive_collision["status"] == "error"

        reactive_ok = _payload(await tool.watch_room(prompt="Watch", trigger_id="reactive-id"))
        assert reactive_ok["status"] == "ok", reactive_ok
        direct_collision = _payload(await tool.create_trigger("reactive-id", public_key=_PUBLIC_KEY))
        assert direct_collision["status"] == "error"


@pytest.mark.asyncio
async def test_watch_thread_initializes_checkpoint_and_reports_already_in_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch_thread reads current tags, initializes checkpoint without firing, and reports already_in_state."""
    _record_rooms(tmp_path)
    now = datetime.now(UTC)
    tagged_state = ThreadTagsState(
        room_id="!lobby:example.org",
        thread_root_id="$thread_root:example.org",
        tags={"resolved": ThreadTagRecord(set_by="@owner:example.org", set_at=now)},
    )
    get_tags_mock = AsyncMock(side_effect=[None, tagged_state, ThreadTagsError("homeserver down")])
    monkeypatch.setattr("mindroom.custom_tools.triggers.get_thread_tags", get_tags_mock)

    tool = TriggersTools()
    with tool_runtime_context(
        _context(tmp_path, relations=make_thread_reply_lookup("$reply:example.org", "$thread_root:example.org")),
    ):
        not_tagged = _payload(
            await tool.watch_thread(
                prompt="Follow up once resolved.",
                thread_id="$reply:example.org",
                trigger_id="watch-unresolved",
            ),
        )
        already_tagged = _payload(
            await tool.watch_thread(
                prompt="Follow up once resolved again.",
                trigger_id="watch-resolved",
            ),
        )
        tag_error = _payload(
            await tool.watch_thread(
                prompt="Will fail reading state.",
                trigger_id="watch-error",
            ),
        )

    assert not_tagged["status"] == "ok", not_tagged
    assert not_tagged["already_in_state"] is False
    assert "note" not in not_tagged
    assert not_tagged["trigger"]["source"]["thread_id"] == "$thread_root:example.org"
    assert not_tagged["trigger"]["target"]["thread_id"] == "$thread_root:example.org"
    assert not_tagged["trigger"]["once"] is True

    assert already_tagged["status"] == "ok", already_tagged
    assert already_tagged["already_in_state"] is True
    assert "future transition" in already_tagged["note"]

    assert tag_error["status"] == "error"
    assert "homeserver down" in tag_error["message"]

    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    cp1 = store.get_thread_checkpoint("watch-unresolved")
    cp2 = store.get_thread_checkpoint("watch-resolved")
    assert cp1 is not None
    assert cp1.tag_present is False
    assert cp2 is not None
    assert cp2.tag_present is True
    assert store.list_due_firings() == []


@pytest.mark.asyncio
async def test_watch_thread_requires_thread_id_outside_thread(tmp_path: Path) -> None:
    """watch_thread returns a clear error when called outside a thread without explicit thread_id."""
    _record_rooms(tmp_path)
    tool = TriggersTools()
    with tool_runtime_context(_context(tmp_path, thread_id=None)):
        payload = _payload(await tool.watch_thread(prompt="Wake me"))
    assert payload["status"] == "error"
    assert "thread_id" in payload["message"]


@pytest.mark.asyncio
async def test_subscribe_event_authorization_and_validation(tmp_path: Path) -> None:
    """subscribe_event enforces subscriber access, hides unauthorized sources, and validates kind/match."""
    _record_rooms(tmp_path)
    config = _config(
        admin_users=["@admin:example.org"],
        event_sources={
            "github": GitHubEventSourceConfig(
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["org/app"],
                allowed_events=["pull_request", "release"],
                subscribers=["@owner:example.org"],
            ),
            "secret_ops": Ed25519EventSourceConfig(
                public_key=_PUBLIC_KEY,
                allowed_kinds=["deploy.done"],
                subscribers=["@admin:example.org"],
            ),
        },
    )
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        missing = _payload(await tool.subscribe_event("nope", "pull_request.merged", "Notify me"))
        unauthorized = _payload(await tool.subscribe_event("secret_ops", "deploy.done", "Notify me"))
        bad_repo = _payload(
            await tool.subscribe_event(
                "github",
                "pull_request.merged",
                "Notify me",
                match={"repo": "evil/repo"},
            ),
        )
        bad_event = _payload(await tool.subscribe_event("github", "push", "Notify me"))
        ok = _payload(
            await tool.subscribe_event(
                "github",
                "pull_request.merged",
                "Summarize merged PR",
                match={"repo": "org/app", "number": 42},
                triage_instructions="Only if breaking change",
                triage_provider="typesafe",
                triage_threshold=0.9,
            ),
        )

    assert missing["status"] == "error"
    assert missing["message"] == "Event source 'nope' is unavailable. Available event sources: github"
    assert "secret_ops" not in missing["message"]
    assert unauthorized["status"] == "error"
    assert unauthorized["message"] == "Event source 'secret_ops' is unavailable. Available event sources: github"
    assert bad_repo["status"] == "error"
    assert bad_event["status"] == "error"
    assert ok["status"] == "ok", ok
    assert ok["trigger"]["source"] == {
        "source_type": "external_event",
        "source_id": "github",
        "kind": "pull_request.merged",
        "match": {"repo": "org/app", "number": 42},
    }
    assert ok["trigger"]["triage"]["provider"] == "typesafe"
    assert ok["trigger"]["triage"]["threshold"] == 0.9
    assert ok["trigger"]["once"] is True

    # Admin can subscribe to sources where they are not explicitly listed in subscribers
    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        admin_ok = _payload(
            await tool.subscribe_event("github", "release.published", "Announce release"),
        )
    assert admin_ok["status"] == "ok", admin_ok


@pytest.mark.asyncio
async def test_unified_list_disable_delete_and_rotate(tmp_path: Path) -> None:
    """list_triggers, disable_trigger, delete_trigger, and rotate_trigger_key work cleanly across both stores."""
    _record_rooms(tmp_path)
    config = _config(admin_users=["@admin:example.org"])
    orchestrator = _make_orchestrator()
    tool = TriggersTools()

    with tool_runtime_context(
        _context(tmp_path, requester_id="@owner:example.org", config=config, orchestrator=orchestrator),
    ):
        assert _payload(await tool.create_trigger("direct-1", public_key=_PUBLIC_KEY))["status"] == "ok"
        assert _payload(await tool.watch_room(prompt="Watch", trigger_id="reactive-1"))["status"] == "ok"

        listed = _payload(tool.list_triggers())
        assert listed["status"] == "ok"
        assert {item["trigger_id"] for item in listed["triggers"]} == {"direct-1", "reactive-1"}
        assert [item["trigger_id"] for item in listed["external_triggers"]] == ["direct-1"]
        assert listed["external_triggers"][0]["trigger_kind"] == "direct"
        assert [item["trigger_id"] for item in listed["reactive_triggers"]] == ["reactive-1"]
        assert listed["reactive_triggers"][0]["trigger_kind"] == "reactive"

        rotate_reactive = _payload(tool.rotate_trigger_key("reactive-1", public_key=_PUBLIC_KEY))
        assert rotate_reactive["status"] == "error"
        assert rotate_reactive["message"] == "Key rotation applies only to direct signed external triggers."

        disabled_reactive = _payload(tool.disable_trigger("reactive-1", enabled=False))
        assert disabled_reactive["status"] == "ok"
        assert disabled_reactive["trigger"]["enabled"] is False

        disabled_direct = _payload(tool.disable_trigger("direct-1", enabled=False))
        assert disabled_direct["status"] == "ok"
        assert disabled_direct["trigger"]["enabled"] is False

        active_only = _payload(tool.list_triggers(include_disabled=False))
        assert active_only["status"] == "ok"
        assert active_only["triggers"] == []

        orchestrator.reactive_triggers.notify_work.reset_mock()
        reenabled = _payload(tool.disable_trigger("reactive-1", enabled=True))
        assert reenabled["status"] == "ok"
        assert reenabled["trigger"]["enabled"] is True
        orchestrator.reactive_triggers.notify_work.assert_called_once()

    # Non-owner non-admin cannot disable or delete another owner's reactive trigger
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        other_list = _payload(tool.list_triggers())
        assert other_list["triggers"] == []
        assert _payload(tool.disable_trigger("reactive-1", enabled=False))["status"] == "error"
        assert _payload(tool.delete_trigger("reactive-1"))["status"] == "error"

    # Admin can inspect and delete both direct and reactive triggers
    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        admin_list = _payload(tool.list_triggers())
        assert {item["trigger_id"] for item in admin_list["triggers"]} == {"direct-1", "reactive-1"}
        assert _payload(tool.delete_trigger("reactive-1"))["status"] == "ok"
        assert _payload(tool.delete_trigger("direct-1"))["status"] == "ok"
        assert _payload(tool.list_triggers())["triggers"] == []


def test_triggers_registration_alias_and_control_plane_policy(tmp_path: Path) -> None:
    """`triggers` is registered with alias `external_trigger_manager`, primary-runtime-only, and non-deferrable."""
    assert TOOL_ALIASES["external_trigger_manager"] == "triggers"
    assert canonical_tool_name("external_trigger_manager") == "triggers"
    assert lazy_loading_prohibited_reason("triggers") is not None
    assert lazy_loading_prohibited_reason("external_trigger_manager") is not None

    registry = builtin_tool_registry()
    assert "triggers" in registry.entries
    assert "external_trigger_manager" not in registry.entries
    meta = registry.entries["triggers"]
    assert meta.requires_primary_runtime is True
    assert meta.requires_room_context is True
    assert set(meta.function_names) == {
        "watch_room",
        "watch_thread",
        "subscribe_event",
        "create_trigger",
        "list_triggers",
        "disable_trigger",
        "delete_trigger",
        "rotate_trigger_key",
    }

    runtime_paths = _runtime_paths(tmp_path)
    built_by_alias = get_tool_by_name(
        "external_trigger_manager",
        runtime_paths,
        tools=registry,
        worker_target=None,
    )
    assert isinstance(built_by_alias, TriggersTools)
    assert issubclass(ExternalTriggerManagerTools, TriggersTools)


@pytest.mark.asyncio
async def test_watch_room_new_thread_with_thread_key_and_non_human_rejection(tmp_path: Path) -> None:
    """watch_room supports new_thread + thread_key, rejects thread_key without new_thread, and rejects bots."""
    _record_rooms(tmp_path)
    config = _config()
    config.bot_accounts = ["@bridgebot:example.org"]
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, config=config)):
        bad_key = _payload(await tool.watch_room(prompt="Watch", new_thread=False, thread_key="grp-1"))
        ok_key = _payload(await tool.watch_room(prompt="Watch", new_thread=True, thread_key="grp-1"))

    assert bad_key["status"] == "error"
    assert "thread_key" in bad_key["message"]
    assert ok_key["status"] == "ok", ok_key
    assert ok_key["trigger"]["target"]["new_thread"] is True
    assert ok_key["trigger"]["target"]["thread_id"] is None
    assert ok_key["trigger"]["target"]["thread_key"] == "grp-1"

    with tool_runtime_context(_context(tmp_path, requester_id="@bridgebot:example.org", config=config)):
        bot_watch_room = _payload(await tool.watch_room(prompt="Bot watch"))
        bot_watch_thread = _payload(await tool.watch_thread(prompt="Bot thread"))
        bot_subscribe = _payload(await tool.subscribe_event("github", "push", "Bot sub"))

    for res in (bot_watch_room, bot_watch_thread, bot_subscribe):
        assert res["status"] == "error"
        assert "human requester" in res["message"]


@pytest.mark.asyncio
async def test_watch_thread_removed_transition_and_origin_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch_thread supports transition='removed' and rejects target_thread_id='origin'."""
    _record_rooms(tmp_path)
    now = datetime.now(UTC)
    tagged_state = ThreadTagsState(
        room_id="!lobby:example.org",
        thread_root_id="$thread_root:example.org",
        tags={"blocked": ThreadTagRecord(set_by="@owner:example.org", set_at=now)},
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.triggers.get_thread_tags",
        AsyncMock(side_effect=[None, tagged_state]),
    )
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path)):
        already_absent = _payload(
            await tool.watch_thread(
                prompt="Alert when unblocked.",
                tag="blocked",
                transition="removed",
                trigger_id="rem-absent",
            ),
        )
        currently_present = _payload(
            await tool.watch_thread(
                prompt="Alert when unblocked.",
                tag="blocked",
                transition="removed",
                trigger_id="rem-present",
            ),
        )
        origin_rejected = _payload(
            await tool.watch_thread(
                prompt="Invalid origin",
                target_thread_id="origin",
            ),
        )

    assert already_absent["status"] == "ok", already_absent
    assert already_absent["already_in_state"] is True
    assert "future transition" in already_absent["note"]

    assert currently_present["status"] == "ok", currently_present
    assert currently_present["already_in_state"] is False
    assert "note" not in currently_present

    assert origin_rejected["status"] == "error"
    assert "origin" in origin_rejected["message"]


def test_config_canonicalizes_external_trigger_manager_and_prohibits_defer() -> None:
    """Config authoring canonicalizes external_trigger_manager to triggers and rejects deferring either spelling."""
    cfg = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "agents": {
                "watcher": {
                    "display_name": "Watcher",
                    "tools": ["external_trigger_manager"],
                },
            },
        },
    )
    assert [entry.name for entry in cfg.agents["watcher"].tools] == ["triggers"]

    for tool_name in ("triggers", "external_trigger_manager"):
        with pytest.raises(ValueError, match="control-plane tool"):
            Config.model_validate(
                {
                    "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                    "agents": {
                        "watcher": {
                            "display_name": "Watcher",
                            "tools": [{tool_name: {"defer": True}}],
                        },
                    },
                },
            )


@pytest.mark.asyncio
async def test_watch_room_initial_cursor_clamps_older_journal_head_and_logs_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch_room never sets an initial cursor older than now_ms, preserves newer heads, and logs cursor read errors."""
    _record_rooms(tmp_path)
    tool = TriggersTools()
    now_ms = int(time.time() * 1000)
    older_ts = now_ms - 60_000
    ahead_ts = now_ms + 30_000

    # Older journal head -> clamped forward to now_ms with empty logical_event_id
    older_reader = make_conversation_reader_mock()
    older_reader.latest_room_cursor = AsyncMock(
        return_value=ConversationCursor(created_ts=older_ts, logical_event_id="$older:example.org"),
    )
    before_older_ms = int(time.time() * 1000)
    with tool_runtime_context(_context(tmp_path, conversation_reader=older_reader)):
        older_res = _payload(await tool.watch_room(prompt="Watch older", trigger_id="cursor-older"))
    assert older_res["status"] == "ok", older_res

    # Newer journal head ahead of now_ms -> preserved
    ahead_reader = make_conversation_reader_mock()
    ahead_reader.latest_room_cursor = AsyncMock(
        return_value=ConversationCursor(created_ts=ahead_ts, logical_event_id="$ahead:example.org"),
    )
    with tool_runtime_context(_context(tmp_path, conversation_reader=ahead_reader)):
        ahead_res = _payload(await tool.watch_room(prompt="Watch ahead", trigger_id="cursor-ahead"))
    assert ahead_res["status"] == "ok", ahead_res

    # Failing latest_room_cursor -> warning logged and falls back to now_ms
    failing_reader = make_conversation_reader_mock()
    failing_reader.latest_room_cursor = AsyncMock(side_effect=RuntimeError("journal offline"))
    warning_mock = MagicMock()
    monkeypatch.setattr("mindroom.custom_tools.triggers.logger.warning", warning_mock)
    before_fail_ms = int(time.time() * 1000)
    with tool_runtime_context(_context(tmp_path, conversation_reader=failing_reader)):
        fail_res = _payload(await tool.watch_room(prompt="Watch fail", trigger_id="cursor-fail"))
    assert fail_res["status"] == "ok", fail_res
    warning_mock.assert_called_once()

    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    older_cursor = store.get_room_cursor("cursor-older")
    assert older_cursor is not None
    assert older_cursor.created_ts >= before_older_ms
    assert older_cursor.logical_event_id == ""

    ahead_cursor = store.get_room_cursor("cursor-ahead")
    assert ahead_cursor is not None
    assert ahead_cursor.created_ts == ahead_ts
    assert ahead_cursor.logical_event_id == "$ahead:example.org"

    fail_cursor = store.get_room_cursor("cursor-fail")
    assert fail_cursor is not None
    assert fail_cursor.created_ts >= before_fail_ms
    assert fail_cursor.logical_event_id == ""


@pytest.mark.asyncio
async def test_list_triggers_includes_equivalent_requester_aliases(tmp_path: Path) -> None:
    """Non-admin list_triggers returns triggers owned by any equivalent alias of the requester."""
    _record_rooms(tmp_path)
    config = _config()
    config.authorization.aliases = {"@owner:example.org": ["@bridge-owner:example.org"]}
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        assert _payload(await tool.create_trigger("alias-direct", public_key=_PUBLIC_KEY))["status"] == "ok"
        assert _payload(await tool.watch_room(prompt="Alias watch", trigger_id="alias-reactive"))["status"] == "ok"

    with tool_runtime_context(_context(tmp_path, requester_id="@bridge-owner:example.org", config=config)):
        listed = _payload(tool.list_triggers())

    assert listed["status"] == "ok", listed
    assert {item["trigger_id"] for item in listed["triggers"]} == {"alias-direct", "alias-reactive"}
    assert [item["trigger_id"] for item in listed["external_triggers"]] == ["alias-direct"]
    assert [item["trigger_id"] for item in listed["reactive_triggers"]] == ["alias-reactive"]


@pytest.mark.asyncio
async def test_reenabling_disabled_room_message_trigger_advances_cursor_forward(tmp_path: Path) -> None:
    """Re-enabling a paused room_message trigger advances a stale cursor to now_ms without regressing newer cursors."""
    _record_rooms(tmp_path)
    tool = TriggersTools()
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    with tool_runtime_context(_context(tmp_path)):
        created = _payload(await tool.watch_room(prompt="Pause test", trigger_id="paused-watch"))
        assert created["status"] == "ok", created
        assert _payload(tool.disable_trigger("paused-watch", enabled=False))["status"] == "ok"

    # Simulate an old cursor from days ago by rewriting the cursor row in SQLite directly
    with store._write_transaction() as conn:
        conn.execute(
            "UPDATE room_cursors SET created_ts = ?, logical_event_id = ? WHERE trigger_id = ?",
            (1_000_000, "$stale:example.org", "paused-watch"),
        )
    stale_cursor = store.get_room_cursor("paused-watch")
    assert stale_cursor is not None
    assert stale_cursor.created_ts == 1_000_000

    before_reenable_ms = int(time.time() * 1000)
    with tool_runtime_context(_context(tmp_path)):
        reenabled = _payload(tool.disable_trigger("paused-watch", enabled=True))
    assert reenabled["status"] == "ok", reenabled
    assert reenabled["trigger"]["enabled"] is True

    advanced_cursor = store.get_room_cursor("paused-watch")
    assert advanced_cursor is not None
    assert advanced_cursor.created_ts >= before_reenable_ms
    assert advanced_cursor.logical_event_id == ""

    # If the cursor is already ahead of now_ms, re-enabling preserves the newer cursor
    ahead_ts = int(time.time() * 1000) + 60_000
    with tool_runtime_context(_context(tmp_path)):
        assert _payload(tool.disable_trigger("paused-watch", enabled=False))["status"] == "ok"
    with store._write_transaction() as conn:
        conn.execute(
            "UPDATE room_cursors SET created_ts = ?, logical_event_id = ? WHERE trigger_id = ?",
            (ahead_ts, "$future:example.org", "paused-watch"),
        )
    with tool_runtime_context(_context(tmp_path)):
        assert _payload(tool.disable_trigger("paused-watch", enabled=True))["status"] == "ok"
    kept_cursor = store.get_room_cursor("paused-watch")
    assert kept_cursor is not None
    assert kept_cursor.created_ts == ahead_ts
    assert kept_cursor.logical_event_id == "$future:example.org"
