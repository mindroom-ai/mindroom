"""Tests for the unified `triggers` toolkit (reactive + direct external triggers)."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.external_trigger_policy import (
    Ed25519EventSourceConfig,
    ExternalTriggerPolicyConfig,
    GitHubEventSourceConfig,
    HmacEventSourceConfig,
)
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import lazy_loading_prohibited_reason
from mindroom.config.tool_aliases import TOOL_ALIASES, canonical_tool_name
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.event_journal import ConversationCursor
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers.models import RoomCursor, TriggerEvent
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.reactive_triggers.triage import TriageOutcome
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.thread_tags import ThreadTagRecord, ThreadTagsError, ThreadTagsState
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.catalog import builtin_tool_registry, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tools import triggers_tools
from mindroom.tools.triggers import triggers_tools as module_triggers_tools
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
            await tool.triggers(
                action="watch_room",
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
            await tool.triggers(
                action="watch_room",
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
        not_joined = _payload(
            await tool.triggers(action="watch_room", prompt="Spy on other room", room_id="other-room"),
        )
    with tool_runtime_context(_context(tmp_path, client=client_no_access)):
        no_access = _payload(
            await tool.triggers(action="watch_room", prompt="Spy on secret room", room_id="secret-room"),
        )

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
            await tool.triggers(
                action="watch_room",
                prompt="Watch",
                triage_instructions="Check urgency",
                triage_model="nonexistent",
            ),
        )
        over_cap = _payload(await tool.triggers(action="watch_room", prompt="Watch", max_fires_per_hour=11))
        bad_expiry = _payload(await tool.triggers(action="watch_room", prompt="Watch", expires_in_hours=0))
        huge_expiry = _payload(await tool.triggers(action="watch_room", prompt="Watch", expires_in_hours=9000))
        nan_expiry = _payload(await tool.triggers(action="watch_room", prompt="Watch", expires_in_hours=math.nan))
        conflict = _payload(
            await tool.triggers(
                action="watch_room",
                prompt="Watch",
                target_thread_id="$t1:example.org",
                new_thread=True,
            ),
        )
        ok = _payload(
            await tool.triggers(action="watch_room", prompt="Watch", expires_in_hours=24, max_fires_per_hour=10),
        )
        over_quota = _payload(await tool.triggers(action="watch_room", prompt="Watch second"))

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
        disabled = _payload(await tool.triggers(action="watch_room", prompt="Watch"))
    assert disabled["status"] == "error"
    assert "disabled" in disabled["message"].lower()

    with tool_runtime_context(_context(tmp_path)):
        direct = _payload(await tool.triggers(action="create", trigger_id="shared-id", public_key=_PUBLIC_KEY))
        assert direct["status"] == "ok", direct
        reactive_collision = _payload(await tool.triggers(action="watch_room", prompt="Watch", trigger_id="shared-id"))
        assert reactive_collision["status"] == "error"

        reactive_ok = _payload(await tool.triggers(action="watch_room", prompt="Watch", trigger_id="reactive-id"))
        assert reactive_ok["status"] == "ok", reactive_ok
        direct_collision = _payload(
            await tool.triggers(action="create", trigger_id="reactive-id", public_key=_PUBLIC_KEY),
        )
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
            await tool.triggers(
                action="watch_thread",
                prompt="Follow up once resolved.",
                thread_id="$reply:example.org",
                trigger_id="watch-unresolved",
            ),
        )
        already_tagged = _payload(
            await tool.triggers(
                action="watch_thread",
                prompt="Follow up once resolved again.",
                trigger_id="watch-resolved",
            ),
        )
        tag_error = _payload(
            await tool.triggers(
                action="watch_thread",
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
        payload = _payload(await tool.triggers(action="watch_thread", prompt="Wake me"))
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
        missing = _payload(
            await tool.triggers(
                action="subscribe_event",
                source_id="nope",
                kind="pull_request.merged",
                prompt="Notify me",
            ),
        )
        unauthorized = _payload(
            await tool.triggers(
                action="subscribe_event",
                source_id="secret_ops",
                kind="deploy.done",
                prompt="Notify me",
            ),
        )
        bad_repo = _payload(
            await tool.triggers(
                action="subscribe_event",
                source_id="github",
                kind="pull_request.merged",
                prompt="Notify me",
                match={"repo": "evil/repo"},
            ),
        )
        bad_event = _payload(
            await tool.triggers(action="subscribe_event", source_id="github", kind="push", prompt="Notify me"),
        )
        ok = _payload(
            await tool.triggers(
                action="subscribe_event",
                source_id="github",
                kind="pull_request.merged",
                prompt="Summarize merged PR",
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
            await tool.triggers(
                action="subscribe_event",
                source_id="github",
                kind="release.published",
                prompt="Announce release",
            ),
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
        assert (
            _payload(await tool.triggers(action="create", trigger_id="direct-1", public_key=_PUBLIC_KEY))["status"]
            == "ok"
        )
        assert (
            _payload(await tool.triggers(action="watch_room", prompt="Watch", trigger_id="reactive-1"))["status"]
            == "ok"
        )

        listed = _payload(await tool.triggers(action="list"))
        assert listed["status"] == "ok"
        assert {item["trigger_id"] for item in listed["triggers"]} == {"direct-1", "reactive-1"}
        assert [item["trigger_id"] for item in listed["external_triggers"]] == ["direct-1"]
        assert listed["external_triggers"][0]["trigger_kind"] == "direct"
        assert [item["trigger_id"] for item in listed["reactive_triggers"]] == ["reactive-1"]
        assert listed["reactive_triggers"][0]["trigger_kind"] == "reactive"

        rotate_reactive = _payload(
            await tool.triggers(action="rotate_key", trigger_id="reactive-1", public_key=_PUBLIC_KEY),
        )
        assert rotate_reactive["status"] == "error"
        assert rotate_reactive["message"] == "Key rotation applies only to direct signed external triggers."

        disabled_reactive = _payload(await tool.triggers(action="set_enabled", trigger_id="reactive-1", enabled=False))
        assert disabled_reactive["status"] == "ok"
        assert disabled_reactive["trigger"]["enabled"] is False

        disabled_direct = _payload(await tool.triggers(action="set_enabled", trigger_id="direct-1", enabled=False))
        assert disabled_direct["status"] == "ok"
        assert disabled_direct["trigger"]["enabled"] is False

        active_only = _payload(await tool.triggers(action="list", include_disabled=False))
        assert active_only["status"] == "ok"
        assert active_only["triggers"] == []

        orchestrator.reactive_triggers.notify_work.reset_mock()
        reenabled = _payload(await tool.triggers(action="set_enabled", trigger_id="reactive-1", enabled=True))
        assert reenabled["status"] == "ok"
        assert reenabled["trigger"]["enabled"] is True
        orchestrator.reactive_triggers.notify_work.assert_called_once()

    # Non-owner non-admin cannot disable or delete another owner's reactive trigger
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        other_list = _payload(await tool.triggers(action="list"))
        assert other_list["triggers"] == []
        assert (
            _payload(await tool.triggers(action="set_enabled", trigger_id="reactive-1", enabled=False))["status"]
            == "error"
        )
        assert _payload(await tool.triggers(action="delete", trigger_id="reactive-1"))["status"] == "error"

    # Admin can inspect and delete both direct and reactive triggers
    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        admin_list = _payload(await tool.triggers(action="list"))
        assert {item["trigger_id"] for item in admin_list["triggers"]} == {"direct-1", "reactive-1"}
        assert _payload(await tool.triggers(action="delete", trigger_id="reactive-1"))["status"] == "ok"
        assert _payload(await tool.triggers(action="delete", trigger_id="direct-1"))["status"] == "ok"
        assert _payload(await tool.triggers(action="list"))["triggers"] == []


def test_triggers_registration_alias_and_deferral_policy(tmp_path: Path) -> None:
    """`triggers` is registered with alias `external_trigger_manager`, primary-runtime-only, unified, and deferrable."""
    assert TOOL_ALIASES["external_trigger_manager"] == "triggers"
    assert canonical_tool_name("external_trigger_manager") == "triggers"
    assert lazy_loading_prohibited_reason("triggers") is None
    assert lazy_loading_prohibited_reason("external_trigger_manager") is None

    registry = builtin_tool_registry()
    assert "triggers" in registry.entries
    assert "external_trigger_manager" not in registry.entries
    meta = registry.entries["triggers"]
    assert meta.requires_primary_runtime is True
    assert meta.requires_room_context is True
    assert meta.function_names == ("triggers",)

    runtime_paths = _runtime_paths(tmp_path)
    built_by_alias = get_tool_by_name(
        "external_trigger_manager",
        runtime_paths,
        tools=registry,
        worker_target=None,
    )
    assert isinstance(built_by_alias, TriggersTools)
    assert triggers_tools() is TriggersTools


@pytest.mark.asyncio
async def test_watch_room_new_thread_with_thread_key_and_non_human_rejection(tmp_path: Path) -> None:
    """watch_room supports new_thread + thread_key, rejects thread_key without new_thread, and rejects bots."""
    _record_rooms(tmp_path)
    config = _config()
    config.bot_accounts = ["@bridgebot:example.org"]
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, config=config)):
        bad_key = _payload(
            await tool.triggers(action="watch_room", prompt="Watch", new_thread=False, thread_key="grp-1"),
        )
        ok_key = _payload(await tool.triggers(action="watch_room", prompt="Watch", new_thread=True, thread_key="grp-1"))

    assert bad_key["status"] == "error"
    assert "thread_key" in bad_key["message"]
    assert ok_key["status"] == "ok", ok_key
    assert ok_key["trigger"]["target"]["new_thread"] is True
    assert ok_key["trigger"]["target"]["thread_id"] is None
    assert ok_key["trigger"]["target"]["thread_key"] == "grp-1"

    with tool_runtime_context(_context(tmp_path, requester_id="@bridgebot:example.org", config=config)):
        bot_watch_room = _payload(await tool.triggers(action="watch_room", prompt="Bot watch"))
        bot_watch_thread = _payload(await tool.triggers(action="watch_thread", prompt="Bot thread"))
        bot_subscribe = _payload(
            await tool.triggers(action="subscribe_event", source_id="github", kind="push", prompt="Bot sub"),
        )

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
            await tool.triggers(
                action="watch_thread",
                prompt="Alert when unblocked.",
                tag="blocked",
                transition="removed",
                trigger_id="rem-absent",
            ),
        )
        currently_present = _payload(
            await tool.triggers(
                action="watch_thread",
                prompt="Alert when unblocked.",
                tag="blocked",
                transition="removed",
                trigger_id="rem-present",
            ),
        )
        origin_rejected = _payload(
            await tool.triggers(
                action="watch_thread",
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


def test_config_canonicalizes_external_trigger_manager_and_allows_defer() -> None:
    """Config authoring canonicalizes external_trigger_manager to triggers and allows deferring either spelling."""
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
        deferred_cfg = Config.model_validate(
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
        assert len(deferred_cfg.agents["watcher"].tools) == 1
        assert deferred_cfg.agents["watcher"].tools[0].name == "triggers"
        assert deferred_cfg.agents["watcher"].tools[0].defer is True


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
        older_res = _payload(await tool.triggers(action="watch_room", prompt="Watch older", trigger_id="cursor-older"))
    assert older_res["status"] == "ok", older_res

    # Newer journal head ahead of now_ms -> preserved
    ahead_reader = make_conversation_reader_mock()
    ahead_reader.latest_room_cursor = AsyncMock(
        return_value=ConversationCursor(created_ts=ahead_ts, logical_event_id="$ahead:example.org"),
    )
    with tool_runtime_context(_context(tmp_path, conversation_reader=ahead_reader)):
        ahead_res = _payload(await tool.triggers(action="watch_room", prompt="Watch ahead", trigger_id="cursor-ahead"))
    assert ahead_res["status"] == "ok", ahead_res

    # Failing latest_room_cursor -> warning logged and falls back to now_ms
    failing_reader = make_conversation_reader_mock()
    failing_reader.latest_room_cursor = AsyncMock(side_effect=RuntimeError("journal offline"))
    warning_mock = MagicMock()
    monkeypatch.setattr("mindroom.custom_tools.triggers.logger.warning", warning_mock)
    before_fail_ms = int(time.time() * 1000)
    with tool_runtime_context(_context(tmp_path, conversation_reader=failing_reader)):
        fail_res = _payload(await tool.triggers(action="watch_room", prompt="Watch fail", trigger_id="cursor-fail"))
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
        assert (
            _payload(await tool.triggers(action="create", trigger_id="alias-direct", public_key=_PUBLIC_KEY))["status"]
            == "ok"
        )
        assert (
            _payload(await tool.triggers(action="watch_room", prompt="Alias watch", trigger_id="alias-reactive"))[
                "status"
            ]
            == "ok"
        )

    with tool_runtime_context(_context(tmp_path, requester_id="@bridge-owner:example.org", config=config)):
        listed = _payload(await tool.triggers(action="list"))

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
        created = _payload(await tool.triggers(action="watch_room", prompt="Pause test", trigger_id="paused-watch"))
        assert created["status"] == "ok", created
        assert (
            _payload(await tool.triggers(action="set_enabled", trigger_id="paused-watch", enabled=False))["status"]
            == "ok"
        )

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
        reenabled = _payload(await tool.triggers(action="set_enabled", trigger_id="paused-watch", enabled=True))
    assert reenabled["status"] == "ok", reenabled
    assert reenabled["trigger"]["enabled"] is True

    advanced_cursor = store.get_room_cursor("paused-watch")
    assert advanced_cursor is not None
    assert advanced_cursor.created_ts >= before_reenable_ms
    assert advanced_cursor.logical_event_id == ""

    # If the cursor is already ahead of now_ms, re-enabling preserves the newer cursor
    ahead_ts = int(time.time() * 1000) + 60_000
    with tool_runtime_context(_context(tmp_path)):
        assert (
            _payload(await tool.triggers(action="set_enabled", trigger_id="paused-watch", enabled=False))["status"]
            == "ok"
        )
    with store._write_transaction() as conn:
        conn.execute(
            "UPDATE room_cursors SET created_ts = ?, logical_event_id = ? WHERE trigger_id = ?",
            (ahead_ts, "$future:example.org", "paused-watch"),
        )
    with tool_runtime_context(_context(tmp_path)):
        assert (
            _payload(await tool.triggers(action="set_enabled", trigger_id="paused-watch", enabled=True))["status"]
            == "ok"
        )
    kept_cursor = store.get_room_cursor("paused-watch")
    assert kept_cursor is not None
    assert kept_cursor.created_ts == ahead_ts
    assert kept_cursor.logical_event_id == "$future:example.org"


@pytest.mark.asyncio
async def test_triggers_tool_factory_and_async_disable_trigger(tmp_path: Path) -> None:
    """The triggers tool factory exports TriggersTools without duplicate alias metadata and uses async disable_trigger."""
    registry = builtin_tool_registry()
    assert triggers_tools is module_triggers_tools
    assert triggers_tools() is TriggersTools
    assert "triggers" in registry.entries
    assert "external_trigger_manager" not in registry.entries

    _record_rooms(tmp_path)
    tool = TriggersTools()
    absent_state = ThreadTagsState(room_id="!lobby:example.org", thread_root_id="$thread1", tags={})
    with (
        tool_runtime_context(_context(tmp_path)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=absent_state)),
    ):
        created_direct = _payload(
            await tool.triggers(action="create", trigger_id="direct-toggle", public_key=_PUBLIC_KEY),
        )
        assert created_direct["tool"] == "triggers"
        assert created_direct["status"] == "ok"
        created_tag = _payload(
            await tool.triggers(
                action="watch_thread",
                prompt="Follow up",
                thread_id="$thread1",
                trigger_id="tag-toggle",
            ),
        )
        assert created_tag["tool"] == "triggers"
        assert created_tag["status"] == "ok"

        disabled_direct = _payload(await tool.triggers(action="set_enabled", trigger_id="direct-toggle", enabled=False))
        assert disabled_direct["tool"] == "triggers"
        assert disabled_direct["trigger"]["enabled"] is False
        reenabled_direct = _payload(await tool.triggers(action="set_enabled", trigger_id="direct-toggle", enabled=True))
        assert reenabled_direct["tool"] == "triggers"
        assert reenabled_direct["trigger"]["enabled"] is True

        disabled_tag = _payload(await tool.triggers(action="set_enabled", trigger_id="tag-toggle", enabled=False))
        assert disabled_tag["tool"] == "triggers"
        assert disabled_tag["trigger"]["enabled"] is False
        reenabled_tag = _payload(await tool.triggers(action="set_enabled", trigger_id="tag-toggle", enabled=True))
        assert reenabled_tag["tool"] == "triggers"
        assert reenabled_tag["trigger"]["enabled"] is True


@pytest.mark.asyncio
async def test_watch_thread_normalizes_tag_names_and_rejects_invalid_tags(tmp_path: Path) -> None:
    """watch_thread normalizes tag='needs_review' to 'needs-review' and rejects invalid tag names cleanly."""
    _record_rooms(tmp_path)
    config = _config()
    tool = TriggersTools()
    existing_state = ThreadTagsState(
        room_id="!lobby:example.org",
        thread_root_id="$thread1",
        tags={"needs-review": ThreadTagRecord(set_by="@owner:example.org", set_at=datetime.now(UTC))},
    )

    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=existing_state)),
    ):
        created = _payload(
            await tool.triggers(
                action="watch_thread",
                prompt="Follow up on review",
                tag="needs_review",
                transition="added",
                trigger_id="norm-tag-watch",
            ),
        )
        assert created["status"] == "ok", created
        assert created["trigger"]["source"]["tag"] == "needs-review"
        assert created["already_in_state"] is True

        for bad_tag in ("", "   ", "bad!tag", "!!!"):
            bad = _payload(await tool.triggers(action="watch_thread", prompt="Bad", tag=bad_tag))
            assert bad["status"] == "error", (bad_tag, bad)


@pytest.mark.asyncio
async def test_reenabling_thread_tag_trigger_refreshes_checkpoint_from_live_room_state(tmp_path: Path) -> None:
    """Re-enabling a disabled thread_tag rule refreshes thread_checkpoints from live room state without firing."""
    _record_rooms(tmp_path)
    config = _config()
    tool = TriggersTools()
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    # Create watch_thread(once=True) with resolved initially absent
    absent_state = ThreadTagsState(room_id="!lobby:example.org", thread_root_id="$thread1", tags={})
    present_state = ThreadTagsState(
        room_id="!lobby:example.org",
        thread_root_id="$thread1",
        tags={"resolved": ThreadTagRecord(set_by="@owner:example.org", set_at=datetime.now(UTC))},
    )

    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=absent_state)),
    ):
        res = _payload(
            await tool.triggers(
                action="watch_thread",
                prompt="On resolve",
                tag="resolved",
                once=True,
                trigger_id="rearm-tag",
            ),
        )
        assert res["status"] == "ok", res

    # Simulate first firing consumed once=True with checkpoint tag_present=True
    rule = store.get_trigger("rearm-tag")
    assert rule is not None
    store.update_thread_checkpoint_and_enqueue(
        "rearm-tag",
        tag_present=True,
        state_event_id="$res1",
        firing_dedupe_key="$res1",
        event=TriggerEvent(
            source_type="thread_tag",
            source_id="!lobby:example.org",
            kind="thread_tag.added",
            event_id="$res1",
            timestamp=time.time(),
            room_id="!lobby:example.org",
            thread_id="$thread1",
        ),
        expected_rule_uid=rule.rule_uid,
    )
    assert store.claim_firing(1) == "claimed"
    assert store.mark_delivered(1, "$sent1") is True

    # Scenario A: thread reopened offline while disabled -> disable_trigger(enabled=True) refreshes checkpoint to False
    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=absent_state)),
    ):
        reenabled = _payload(await tool.triggers(action="set_enabled", trigger_id="rearm-tag", enabled=True))
    assert reenabled["status"] == "ok", reenabled
    cp = store.get_thread_checkpoint("rearm-tag")
    assert cp is not None
    assert cp.tag_present is False
    assert store.list_due_firings() == []

    # Scenario B: disabled again -> tag added while disabled -> re-enable refreshes checkpoint to True WITHOUT firing
    with tool_runtime_context(_context(tmp_path, config=config)):
        assert (
            _payload(await tool.triggers(action="set_enabled", trigger_id="rearm-tag", enabled=False))["status"] == "ok"
        )
    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=present_state)),
    ):
        reenabled2 = _payload(await tool.triggers(action="set_enabled", trigger_id="rearm-tag", enabled=True))
    assert reenabled2["status"] == "ok", reenabled2
    cp2 = store.get_thread_checkpoint("rearm-tag")
    assert cp2 is not None
    assert cp2.tag_present is True
    assert store.list_due_firings() == []


@pytest.mark.asyncio
async def test_concurrent_cross_store_trigger_id_creation_is_serialized(tmp_path: Path) -> None:
    """Concurrent direct + reactive trigger creation with the same trigger_id allows at most one winner."""
    _record_rooms(tmp_path)
    config = _config()
    tool = TriggersTools()
    ctx = _context(tmp_path, config=config)

    async def _create_direct() -> dict[str, Any]:
        with tool_runtime_context(ctx):
            return _payload(await tool.triggers(action="create", trigger_id="race-id", public_key=_PUBLIC_KEY))

    async def _create_reactive() -> dict[str, Any]:
        with tool_runtime_context(ctx):
            return _payload(await tool.triggers(action="watch_room", prompt="Watch", trigger_id="race-id"))

    res_direct, res_reactive = await asyncio.gather(_create_direct(), _create_reactive())
    statuses = sorted([res_direct["status"], res_reactive["status"]])
    assert statuses == ["error", "ok"], (res_direct, res_reactive)


@pytest.mark.asyncio
async def test_unified_triggers_schema_size_and_required_arg_validation(tmp_path: Path) -> None:
    """TriggersTools exposes one compact unified `triggers` tool and validates required arguments per action."""
    _record_rooms(tmp_path)
    tool = TriggersTools()
    adapted = adapt_toolkit(tool)
    assert [fn.name for fn in adapted.functions] == ["triggers"]
    serialized_schema = json.dumps(
        {
            "name": adapted.functions[0].name,
            "description": adapted.functions[0].description,
            "parameters": adapted.functions[0].parameters,
        },
    )
    assert len(serialized_schema) < 7500

    with tool_runtime_context(_context(tmp_path)):
        cases: list[tuple[dict[str, Any], str]] = [
            ({"action": "watch_room"}, "prompt is required"),
            ({"action": "watch_thread"}, "prompt is required"),
            ({"action": "subscribe_event", "kind": "push", "prompt": "Go"}, "source_id is required"),
            ({"action": "subscribe_event", "source_id": "gh", "prompt": "Go"}, "kind is required"),
            ({"action": "subscribe_event", "source_id": "gh", "kind": "push"}, "prompt is required"),
            ({"action": "create", "public_key": _PUBLIC_KEY}, "trigger_id is required"),
            ({"action": "create", "trigger_id": "x"}, "public_key is required"),
            ({"action": "update", "prompt": "New"}, "trigger_id is required"),
            ({"action": "set_enabled"}, "trigger_id is required"),
            ({"action": "delete"}, "trigger_id is required"),
            ({"action": "rotate_key", "public_key": _PUBLIC_KEY}, "trigger_id is required"),
            ({"action": "rotate_key", "trigger_id": "x"}, "public_key is required"),
            ({"action": "test"}, "trigger_id is required"),
            ({"action": "bogus"}, "Unsupported triggers action"),
        ]
        for kwargs, expected_msg in cases:
            res = _payload(await tool.triggers(**kwargs))
            assert res["status"] == "error", (kwargs, res)
            assert expected_msg in res["message"], (kwargs, res)


@pytest.mark.asyncio
async def test_list_returns_safe_authorized_event_sources_metadata(tmp_path: Path) -> None:
    """Action `list` returns sorted authorized event_sources metadata without secrets, keys, or unauthorized sources."""
    _record_rooms(tmp_path)
    config = _config(
        admin_users=["@admin:example.org"],
        event_sources={
            "zeta_hmac": HmacEventSourceConfig(
                secret_env="ZETA_SECRET",  # noqa: S106
                allowed_kinds=["alert.fired", "alert.resolved"],
                subscribers=["@owner:example.org"],
            ),
            "alpha_gh": GitHubEventSourceConfig(
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["mindroom-ai/mindroom"],
                allowed_events=["pull_request", "release"],
                subscribers=["@owner:example.org"],
            ),
            "secret_ed25519": Ed25519EventSourceConfig(
                public_key=_PUBLIC_KEY,
                allowed_kinds=["ops.deploy"],
                subscribers=["@admin:example.org"],
            ),
        },
    )
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        owner_list = _payload(await tool.triggers(action="list"))
    assert owner_list["status"] == "ok"
    assert owner_list["event_sources"] == [
        {
            "source_id": "alpha_gh",
            "kind": "github",
            "allowed_repos": ["mindroom-ai/mindroom"],
            "allowed_events": ["pull_request", "release"],
        },
        {
            "source_id": "zeta_hmac",
            "kind": "hmac",
            "allowed_kinds": ["alert.fired", "alert.resolved"],
        },
    ]
    serialized = json.dumps(owner_list)
    assert "GH_SECRET" not in serialized
    assert "ZETA_SECRET" not in serialized
    assert _PUBLIC_KEY not in serialized
    assert "secret_ed25519" not in serialized

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        admin_list = _payload(await tool.triggers(action="list"))
    assert [src["source_id"] for src in admin_list["event_sources"]] == ["alpha_gh", "secret_ed25519", "zeta_hmac"]
    assert "public_key" not in json.dumps(admin_list["event_sources"])
    assert "secret_env" not in json.dumps(admin_list["event_sources"])

    disabled_config = _config(reactive_enabled=False, event_sources=config.external_trigger_policy.event_sources)
    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=disabled_config)):
        disabled_list = _payload(await tool.triggers(action="list"))
    assert disabled_list["event_sources"] == []


@pytest.mark.asyncio
async def test_update_modifies_reactive_rule_in_place_preserving_state_and_clearing_fields(tmp_path: Path) -> None:
    """Action `update` mutates fields in place, preserves rule_uid/fire_count/cursors/thread_keys, and clears fields."""
    _record_rooms(tmp_path)
    orchestrator = _make_orchestrator()
    tool = TriggersTools()
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    with tool_runtime_context(_context(tmp_path, orchestrator=orchestrator)):
        created = _payload(
            await tool.triggers(
                action="watch_room",
                trigger_id="edit-room",
                prompt="Initial prompt",
                description="Initial desc",
                pattern=r"\berror\b",
                senders=["@alice:example.org"],
                include_bots=True,
                triage_instructions="Only prod",
                triage_model="fast",
                new_thread=True,
                thread_key="alerts",
            ),
        )
        assert created["status"] == "ok", created
        assert created["trigger"]["source"]["include_bots"] is True

    before = store.get_trigger("edit-room")
    assert before is not None
    saved_cursor = RoomCursor(created_ts=int(time.time() * 1000) + 10_000, logical_event_id="$cur1")
    assert store.advance_room_cursor("edit-room", saved_cursor, expected_rule_uid=before.rule_uid) is True
    assert (
        store.save_thread_key_binding("edit-room", "alerts", "$root_alert", expected_rule_uid=before.rule_uid) is True
    )
    store.enqueue_firing(
        "edit-room",
        "$ev1",
        TriggerEvent(
            source_type="room_message",
            source_id="!lobby:example.org",
            kind="room.message",
            event_id="$ev1",
            timestamp=time.time(),
            room_id="!lobby:example.org",
            actor="@alice:example.org",
            text="error happened",
        ),
    )
    assert store.claim_firing(1) == "claimed"
    assert store.mark_delivered(1, "$sent1") is True
    after_fire = store.get_trigger("edit-room")
    assert after_fire is not None

    orchestrator.reactive_triggers.notify_work.reset_mock()
    with tool_runtime_context(_context(tmp_path, orchestrator=orchestrator)):
        updated = _payload(
            await tool.triggers(
                action="update",
                trigger_id="edit-room",
                prompt="Updated prompt",
                description="Updated desc",
                pattern="",
                senders=[],
                include_bots=False,
                triage_instructions="",
                cooldown_seconds=30,
                max_fires_per_hour=5,
                once=True,
            ),
        )
        assert updated["status"] == "ok", updated
        tr = updated["trigger"]
        assert (tr["target"]["prompt"], tr["description"], tr["source"]["pattern"], tr["source"]["senders"]) == (
            "Updated prompt",
            "Updated desc",
            None,
            [],
        )
        assert (tr["source"]["include_bots"], tr["triage"], tr["cooldown_seconds"], tr["max_fires_per_hour"]) == (
            False,
            None,
            30,
            5,
        )
        assert (tr["once"], tr["fire_count"], tr["last_fired_at"]) == (True, 1, after_fire.last_fired_at)
        orchestrator.reactive_triggers.notify_work.assert_called_once()

        tweak = _payload(await tool.triggers(action="update", trigger_id="edit-room", description="Tweaked"))
        assert tweak["status"] == "ok"
        assert (tweak["trigger"]["target"]["prompt"], tweak["trigger"]["cooldown_seconds"]) == ("Updated prompt", 30)

    persisted = store.get_trigger("edit-room")
    assert persisted is not None
    assert (persisted.rule_uid, persisted.fire_count) == (before.rule_uid, 1)
    assert store.get_room_cursor("edit-room") == saved_cursor
    assert store.get_thread_key_binding("edit-room", "alerts") == "$root_alert"


@pytest.mark.asyncio
async def test_update_validates_external_match_and_rejects_invalid_updates(tmp_path: Path) -> None:
    """Action `update` re-validates external match filters and rejects direct, immutable, mismatched, or unauthorized edits."""
    _record_rooms(tmp_path)
    config = _config(
        event_sources={
            "github": GitHubEventSourceConfig(
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["org/app", "org/other"],
                allowed_events=["pull_request"],
                subscribers=["@owner:example.org"],
            ),
        },
    )
    tool = TriggersTools()
    with tool_runtime_context(_context(tmp_path, config=config)):
        assert _payload(await tool.triggers(action="watch_room", trigger_id="r1", prompt="Watch"))["status"] == "ok"
        assert _payload(await tool.triggers(action="create", trigger_id="d1", public_key=_PUBLIC_KEY))["status"] == "ok"
        assert (
            _payload(
                await tool.triggers(
                    action="subscribe_event",
                    trigger_id="e1",
                    source_id="github",
                    kind="pull_request.merged",
                    prompt="PR",
                    match={"repo": "org/app"},
                ),
            )["status"]
            == "ok"
        )
        assert (
            _payload(await tool.triggers(action="update", trigger_id="e1", match={"repo": "evil/repo"}))["status"]
            == "error"
        )
        good_match = _payload(await tool.triggers(action="update", trigger_id="e1", match={"repo": "org/other"}))
        assert good_match["status"] == "ok"
        assert good_match["trigger"]["source"]["match"] == {"repo": "org/other"}

        for bad_kwargs in (
            {"trigger_id": "d1", "prompt": "x"},
            {"trigger_id": "r1"},
            {"trigger_id": "r1", "room_id": "other-room", "prompt": "x"},
            {"trigger_id": "r1", "match": {"repo": "org/app"}},
            {"trigger_id": "e1", "pattern": "abc"},
            {"trigger_id": "e1", "include_bots": True},
        ):
            assert _payload(await tool.triggers(action="update", **bad_kwargs))["status"] == "error", bad_kwargs

    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        assert _payload(await tool.triggers(action="update", trigger_id="r1", prompt="Hijack"))["status"] == "error"


@pytest.mark.asyncio
async def test_history_pagination_owner_isolation_admin_visibility_and_truncation(tmp_path: Path) -> None:
    """Action `history` returns newest-first firings, clamps limit, enforces owner/admin access, and truncates text."""
    _record_rooms(tmp_path)
    config = _config(admin_users=["@admin:example.org"])
    tool = TriggersTools()
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        assert (
            _payload(await tool.triggers(action="watch_room", prompt="Owner", trigger_id="owner-r"))["status"] == "ok"
        )
        assert (
            _payload(await tool.triggers(action="create", trigger_id="direct-r", public_key=_PUBLIC_KEY))["status"]
            == "ok"
        )
    with tool_runtime_context(_context(tmp_path, requester_id="@other-owner:example.org", config=config)):
        assert (
            _payload(await tool.triggers(action="watch_room", prompt="Other", trigger_id="other-r"))["status"] == "ok"
        )

    long_text = "x" * 400
    for idx, (tid, text) in enumerate([("owner-r", "first"), ("other-r", "secret"), ("owner-r", long_text)], start=1):
        store.enqueue_firing(
            tid,
            f"$ev{idx}",
            TriggerEvent(
                source_type="room_message",
                source_id="!lobby:example.org",
                kind="room.message",
                event_id=f"$ev{idx}",
                timestamp=1_000.0 + idx,
                room_id="!lobby:example.org",
                actor="@owner:example.org",
                text=text,
            ),
        )

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        owner_hist = _payload(await tool.triggers(action="history"))
        assert owner_hist["status"] == "ok"
        assert owner_hist["count"] == 2
        assert [row["dedupe_key"] for row in owner_hist["firings"]] == ["$ev3", "$ev1"]
        preview = owner_hist["firings"][0]["event"]["text"]
        assert len(preview) == 240
        assert preview.endswith("...")

        clamped = _payload(await tool.triggers(action="history", trigger_id="owner-r", limit=0))
        assert clamped["status"] == "ok"
        assert clamped["count"] == 1
        assert clamped["firings"][0]["dedupe_key"] == "$ev3"

        # Cannot inspect another owner's trigger or direct trigger
        assert _payload(await tool.triggers(action="history", trigger_id="other-r"))["status"] == "error"
        assert _payload(await tool.triggers(action="history", trigger_id="direct-r"))["status"] == "error"
        assert _payload(await tool.triggers(action="history", trigger_id="missing-r"))["status"] == "error"

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        admin_hist = _payload(await tool.triggers(action="history", limit=500))
    assert admin_hist["status"] == "ok"
    assert [row["dedupe_key"] for row in admin_hist["firings"]] == ["$ev3", "$ev2", "$ev1"]


@pytest.mark.asyncio
async def test_dry_run_matches_and_runs_triage_without_side_effects(tmp_path: Path) -> None:
    """Action `test` runs deterministic matching and live triage without mutating SQLite or posting to Matrix."""
    _record_rooms(tmp_path)
    config = _config(
        event_sources={
            "github": GitHubEventSourceConfig(
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["org/app"],
                allowed_events=["pull_request"],
                subscribers=["@owner:example.org"],
            ),
        },
    )
    tool = TriggersTools()
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    absent_state = ThreadTagsState(room_id="!lobby:example.org", thread_root_id="$thread_root:example.org", tags={})

    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=absent_state)),
    ):
        await tool.triggers(
            action="watch_room",
            trigger_id="test-room",
            prompt="Wake on deploy",
            pattern=r"\bdeploy\b",
            triage_instructions="Only prod failures",
            triage_model="fast",
            once=True,
        )
        await tool.triggers(
            action="subscribe_event",
            trigger_id="test-ext",
            source_id="github",
            kind="pull_request.merged",
            prompt="Merged",
            match={"repo": "org/app", "number": 7},
        )
        await tool.triggers(action="watch_thread", trigger_id="test-tag", prompt="Resolved", tag="resolved")
        await tool.triggers(action="create", trigger_id="test-direct", public_key=_PUBLIC_KEY)

    triage_mock = AsyncMock(
        side_effect=[
            TriageOutcome(act=True, reason="llm (gpt-6-mini)"),
            TriageOutcome(act=False, failure="timeout"),
        ],
    )
    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.evaluate_triage", new=triage_mock),
    ):
        miss = _payload(await tool.triggers(action="test", trigger_id="test-room", text="hello world"))
        assert (miss["matched"], miss["triage_decision"], miss["would_fire"]) == (False, None, False)
        assert triage_mock.await_count == 0

        hit = _payload(await tool.triggers(action="test", trigger_id="test-room", text="prod deploy failed"))
        assert (hit["matched"], hit["triage_decision"], hit["triage_reason"], hit["would_fire"]) == (
            True,
            True,
            "llm (gpt-6-mini)",
            True,
        )

        fail_closed = _payload(await tool.triggers(action="test", trigger_id="test-room", text="deploy check"))
        assert (
            fail_closed["matched"],
            fail_closed["triage_decision"],
            fail_closed["triage_reason"],
            fail_closed["would_fire"],
        ) == (
            True,
            False,
            "timeout",
            False,
        )

        ext_hit = _payload(
            await tool.triggers(action="test", trigger_id="test-ext", sample_data={"repo": "org/app", "number": 7}),
        )
        ext_miss = _payload(
            await tool.triggers(action="test", trigger_id="test-ext", sample_data={"repo": "org/app", "number": 8}),
        )
        assert (ext_hit["would_fire"], ext_miss["matched"]) == (True, False)

        tag_hit = _payload(
            await tool.triggers(
                action="test",
                trigger_id="test-tag",
                sample_data={"tag": "resolved", "transition": "added"},
            ),
        )
        tag_miss = _payload(
            await tool.triggers(
                action="test",
                trigger_id="test-tag",
                sample_data={"tag": "resolved", "transition": "removed"},
            ),
        )
        assert (tag_hit["would_fire"], tag_miss["matched"]) == (True, False)

        assert _payload(await tool.triggers(action="test", trigger_id="test-direct"))["status"] == "error"

        await tool.triggers(action="set_enabled", trigger_id="test-ext", enabled=False)
        disabled_res = _payload(
            await tool.triggers(action="test", trigger_id="test-ext", sample_data={"repo": "org/app", "number": 7}),
        )
        assert (disabled_res["matched"], disabled_res["would_fire"]) == (True, False)
        assert "disabled" in disabled_res["note"]

    room_rule = store.get_trigger("test-room")
    assert room_rule is not None
    assert (room_rule.enabled, room_rule.fire_count) == (True, 0)
    assert store.list_firings() == []


def _load_trigger_approval_rules(tmp_path: Path, *rules: str | tuple[str, str]) -> Config:
    runtime_paths = _runtime_paths(tmp_path)
    pairs = [(rule, "require_approval") if isinstance(rule, str) else rule for rule in rules]
    data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": ["triggers"]}},
        "tool_approval": {"rules": [{"match": match, "action": action} for match, action in pairs]},
    }
    return Config.validate_with_runtime(data, runtime_paths)


@pytest.mark.parametrize(
    ("match", "retired"),
    [
        ("watch_room", "watch_room is now triggers"),
        ("watch_thread", "watch_thread is now triggers"),
        ("subscribe_event", "subscribe_event is now triggers"),
        ("create_trigger", "create_trigger is now triggers"),
        ("list_triggers", "list_triggers is now triggers"),
        ("disable_trigger", "disable_trigger is now triggers"),
        ("delete_trigger", "delete_trigger is now triggers"),
        ("rotate_trigger_key", "rotate_trigger_key is now triggers"),
        ("watch_*", "watch_room is now triggers"),
        ("*_trigger", "create_trigger is now triggers"),
        ("*_trigger*", "create_trigger is now triggers"),
    ],
)
def test_an_approval_rule_that_stops_deciding_a_retired_trigger_function_fails(
    tmp_path: Path,
    match: str,
    retired: str,
) -> None:
    """A rule matching a retired trigger function that does not also decide `triggers` fails config load."""
    expected = rf"tool_approval\.rules\[1\]\.match {re.escape(repr(match))} .*{re.escape(retired)}"
    with pytest.raises(ConfigRuntimeValidationError, match=expected):
        _load_trigger_approval_rules(tmp_path, "run_shell_command", match)


def test_an_earlier_approval_rule_that_takes_triggers_from_a_retired_gate_fails(tmp_path: Path) -> None:
    """An earlier rule matching `triggers` ahead of a retired trigger gate fails config load."""
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"rules\[1\]\.match 'create_trigger' .*create_trigger is now triggers",
    ):
        _load_trigger_approval_rules(tmp_path, ("triggers", "auto_approve"), ("create_trigger", "require_approval"))
    with pytest.raises(ConfigRuntimeValidationError, match=r"rules\[0\]\.match 'create_trigger'"):
        _load_trigger_approval_rules(tmp_path, "create_trigger", "*")
    assert _load_trigger_approval_rules(tmp_path, ("*trigger*", "require_approval"), ("triggers", "auto_approve"))


@pytest.mark.parametrize("match", ["triggers", "*", "*trigger*", "trigger*"])
def test_approval_rules_that_decide_triggers_load(tmp_path: Path, match: str) -> None:
    """Rules deciding `triggers` directly (or deciding both retired and new names at the same index) load."""
    assert _load_trigger_approval_rules(tmp_path, match).tool_approval.rules[0].match == match


@pytest.mark.parametrize("tool_name", ["triggers", "external_trigger_manager"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"exclude_tools": ["create_trigger"]},
        {"include_tools": "watch_room, list_triggers"},
    ],
)
def test_retired_trigger_include_exclude_overrides_fail_config_load(
    tmp_path: Path,
    tool_name: str,
    overrides: dict[str, object],
) -> None:
    """include_tools/exclude_tools naming a retired trigger function fails config load with migration guidance."""
    data = {
        "models": {"default": {"provider": "openai", "id": "x"}},
        "agents": {"a": {"display_name": "A", "tools": [{tool_name: overrides}]}},
    }
    with pytest.raises(ConfigRuntimeValidationError, match=r"triggers\.(in|ex)clude_tools: .* was removed; .*triggers"):
        Config.validate_with_runtime(data, _runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_history_hides_and_refuses_firings_after_owner_loses_source_access(tmp_path: Path) -> None:
    """History refuses/omits pending or finished firings once a non-admin owner loses source room or event access."""
    _record_rooms(tmp_path)
    gh_source = GitHubEventSourceConfig(
        secret_env="GH_SECRET",  # noqa: S106
        allowed_repos=["org/app"],
        allowed_events=["pull_request"],
        subscribers=["@owner:example.org"],
    )
    config = _config(admin_users=["@admin:example.org"], event_sources={"github": gh_source})
    tool = TriggersTools()
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))

    live_members: dict[str, list[str]] = {
        "!lobby:example.org": ["@owner:example.org"],
        "!other-room:example.org": ["@owner:example.org"],
    }
    client = _room_state_client(room_members=live_members)
    other_bot = SimpleNamespace(agent_name="other", running=True, client=client)
    orch = _make_orchestrator(bots={"other": other_bot})
    absent_state = ThreadTagsState(
        room_id="!other-room:example.org",
        thread_root_id="$priv_thread:example.org",
        tags={},
    )

    # Owner watches private room A (!other-room) targeting room B (!lobby), plus lobby and github
    with (
        tool_runtime_context(_context(tmp_path, config=config, client=client, orchestrator=orch)),
        patch("mindroom.custom_tools.triggers.get_thread_tags", new=AsyncMock(return_value=absent_state)),
    ):
        assert (
            _payload(
                await tool.triggers(
                    action="watch_thread",
                    trigger_id="watch-priv-tag",
                    room_id="!other-room:example.org",
                    thread_id="$priv_thread:example.org",
                    tag="resolved",
                    prompt="Report resolution",
                    target_room_id="!lobby:example.org",
                ),
            )["status"]
            == "ok"
        )
        assert (
            _payload(
                await tool.triggers(
                    action="watch_room",
                    trigger_id="watch-priv-msg",
                    room_id="!other-room:example.org",
                    prompt="Report msg",
                    target_room_id="!lobby:example.org",
                ),
            )["status"]
            == "ok"
        )
        assert (
            _payload(await tool.triggers(action="watch_room", trigger_id="watch-lobby", prompt="Lobby"))["status"]
            == "ok"
        )
        assert (
            _payload(
                await tool.triggers(
                    action="subscribe_event",
                    trigger_id="watch-gh",
                    source_id="github",
                    kind="pull_request.merged",
                    prompt="PR merged",
                    match={"repo": "org/app"},
                ),
            )["status"]
            == "ok"
        )

    # Record pending firings across all rules (including pending thread_tag firing in private room A)
    for trigger_id, event in (
        (
            "watch-lobby",
            TriggerEvent(
                source_type="room_message",
                source_id="!lobby:example.org",
                kind="room.message",
                event_id="$ev_lobby",
                timestamp=1_001.0,
                room_id="!lobby:example.org",
                actor="@owner:example.org",
                text="public lobby note",
            ),
        ),
        (
            "watch-priv-tag",
            TriggerEvent(
                source_type="thread_tag",
                source_id="!other-room:example.org",
                kind="thread_tag.added",
                event_id="$ev_priv_tag",
                timestamp=1_002.0,
                room_id="!other-room:example.org",
                thread_id="$priv_thread:example.org",
                actor="@secret_user:example.org",
                text="private resolution note",
            ),
        ),
        (
            "watch-priv-msg",
            TriggerEvent(
                source_type="room_message",
                source_id="!other-room:example.org",
                kind="room.message",
                event_id="$ev_priv_msg",
                timestamp=1_003.0,
                room_id="!other-room:example.org",
                actor="@secret_user:example.org",
                text="private room message",
            ),
        ),
        (
            "watch-gh",
            TriggerEvent(
                source_type="external_event",
                source_id="github",
                kind="pull_request.merged",
                event_id="$ev_gh",
                timestamp=1_004.0,
                actor="octocat",
                text="merged PR",
                data={"repo": "org/app"},
            ),
        ),
    ):
        store.enqueue_firing(trigger_id, event.event_id, event)

    # While still joined to room A, unscoped history returns all 4 firings and caches room A's state check once
    client.room_get_state.reset_mock()
    with tool_runtime_context(_context(tmp_path, config=config, client=client, orchestrator=orch)):
        before = _payload(await tool.triggers(action="history"))
    assert before["status"] == "ok"
    assert [row["dedupe_key"] for row in before["firings"]] == ["$ev_gh", "$ev_priv_msg", "$ev_priv_tag", "$ev_lobby"]
    assert client.room_get_state.await_count == 1

    # Owner leaves private room A (stale client.rooms.users intentionally still contains owner!)
    live_members["!other-room:example.org"] = ["@someone_else:example.org"]
    with tool_runtime_context(_context(tmp_path, config=config, client=client, orchestrator=orch)):
        scoped_left = _payload(await tool.triggers(action="history", trigger_id="watch-priv-tag"))
        unscoped_left = _payload(await tool.triggers(action="history"))
    assert scoped_left["status"] == "error"
    assert "no longer have access to the source for trigger watch-priv-tag" in scoped_left["message"].lower()
    assert unscoped_left["status"] == "ok"
    assert [row["dedupe_key"] for row in unscoped_left["firings"]] == ["$ev_gh", "$ev_lobby"]

    # Unreadable room state fails closed even if stale cached client.rooms still lists the owner
    del live_members["!other-room:example.org"]
    with tool_runtime_context(_context(tmp_path, config=config, client=client, orchestrator=orch)):
        scoped_unreadable = _payload(await tool.triggers(action="history", trigger_id="watch-priv-tag"))
        unscoped_unreadable = _payload(await tool.triggers(action="history"))
    assert scoped_unreadable["status"] == "error"
    assert [row["dedupe_key"] for row in unscoped_unreadable["firings"]] == ["$ev_gh", "$ev_lobby"]

    # Revoking external event source subscriber access also blocks scoped and unscoped history
    revoked_config = _config(
        admin_users=["@admin:example.org"],
        event_sources={
            "github": GitHubEventSourceConfig(
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["org/app"],
                allowed_events=["pull_request"],
                subscribers=["@other-owner:example.org"],
            ),
        },
    )
    with tool_runtime_context(_context(tmp_path, config=revoked_config, client=client, orchestrator=orch)):
        scoped_gh = _payload(await tool.triggers(action="history", trigger_id="watch-gh"))
        unscoped_gh = _payload(await tool.triggers(action="history"))
    assert scoped_gh["status"] == "error"
    assert "no longer have access to the source for trigger watch-gh" in scoped_gh["message"].lower()
    assert [row["dedupe_key"] for row in unscoped_gh["firings"]] == ["$ev_lobby"]

    # Admins still see all firings
    with tool_runtime_context(
        _context(tmp_path, requester_id="@admin:example.org", config=revoked_config, client=client, orchestrator=orch),
    ):
        admin_hist = _payload(await tool.triggers(action="history"))
    assert admin_hist["status"] == "ok"
    assert [row["dedupe_key"] for row in admin_hist["firings"]] == [
        "$ev_gh",
        "$ev_priv_msg",
        "$ev_priv_tag",
        "$ev_lobby",
    ]


@pytest.mark.asyncio
async def test_dry_run_skips_target_self_and_non_human_bots_without_invoking_triage(tmp_path: Path) -> None:
    """Dry-run `test` applies live target-self and include_bots=False exclusions before matching and triage."""
    _record_rooms(tmp_path)
    runtime_paths = _runtime_paths(tmp_path)
    domain = runtime_matrix_domain(runtime_paths)
    base_data = _config().model_dump(mode="json")
    base_data["teams"] = {
        "ops_team": {
            "display_name": "Ops Team",
            "role": "Ops",
            "agents": ["watcher", "other"],
            "rooms": ["lobby"],
            "access": {"users": ["@owner:example.org"]},
        },
    }
    config = Config.model_validate(base_data)
    tool = TriggersTools()

    with tool_runtime_context(_context(tmp_path, config=config)):
        await tool.triggers(
            action="watch_room",
            trigger_id="dr-no-bots",
            prompt="React",
            pattern=r"deploy",
            include_bots=False,
            triage_instructions="Only prod",
            triage_model="fast",
        )
        await tool.triggers(
            action="watch_room",
            trigger_id="dr-with-bots",
            prompt="React",
            pattern=r"deploy",
            include_bots=True,
            triage_instructions="Only prod",
            triage_model="fast",
        )
        await tool.triggers(
            action="watch_room",
            trigger_id="dr-team-bots",
            prompt="Team react",
            pattern=r"deploy",
            include_bots=True,
            target_agent="ops_team",
            triage_instructions="Only prod",
            triage_model="fast",
        )

    watcher_id = f"@mindroom_watcher:{domain}"
    other_bot_id = f"@mindroom_other:{domain}"
    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="llm (gpt-6-mini)"))

    with (
        tool_runtime_context(_context(tmp_path, config=config)),
        patch("mindroom.custom_tools.triggers.evaluate_triage", new=triage_mock),
    ):
        # Target agent's own ID is skipped for BOTH include_bots=False and include_bots=True without calling triage
        for tid in ("dr-no-bots", "dr-with-bots"):
            res = _payload(await tool.triggers(action="test", trigger_id=tid, text="prod deploy", actor=watcher_id))
            assert (res["matched"], res["would_fire"], res["triage_decision"]) == (False, False, None)
            assert "target agent or team" in res["note"].lower()
        assert triage_mock.await_count == 0

        # Target team also skips member agent messages even when include_bots=True
        team_res = _payload(
            await tool.triggers(action="test", trigger_id="dr-team-bots", text="prod deploy", actor=other_bot_id),
        )
        assert (team_res["matched"], team_res["would_fire"], team_res["triage_decision"]) == (False, False, None)
        assert "target agent or team" in team_res["note"].lower()
        assert triage_mock.await_count == 0

        # Other bot is skipped when include_bots=False without invoking triage
        bot_skip = _payload(
            await tool.triggers(action="test", trigger_id="dr-no-bots", text="prod deploy", actor=other_bot_id),
        )
        assert (bot_skip["matched"], bot_skip["would_fire"], bot_skip["triage_decision"]) == (False, False, None)
        assert "include_bots=false" in bot_skip["note"].lower()
        assert triage_mock.await_count == 0

        # Other bot IS allowed when include_bots=True and invokes triage
        bot_allow = _payload(
            await tool.triggers(action="test", trigger_id="dr-with-bots", text="prod deploy", actor=other_bot_id),
        )
        assert (bot_allow["matched"], bot_allow["would_fire"], bot_allow["triage_decision"]) == (True, True, True)
        assert triage_mock.await_count == 1
