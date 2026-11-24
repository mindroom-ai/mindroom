"""Room-scoped trigger listing, cross-room watch redaction, owner-only mutations, and re-enable refresh."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle, room_triggers
from mindroom.api.app_state import ExternalTriggerRuntime, ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.api.main import app as main_app
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.external_triggers.store import ExternalTriggerStore, ExternalTriggerTarget
from mindroom.matrix.state import MatrixState
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.reactive_triggers.models import (
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from tests.access_schema_support import joined_members_response
from tests.conftest import test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import Response

ROOM_A = "!room-a:localhost"
ROOM_B = "!room-b:localhost"
ROOM_C = "!room-c:localhost"
UNREACHABLE_ROOM = "!no-bot:localhost"

ALICE = "@alice:localhost"
ALICE_BRIDGE = "@alice_bridge:localhost"
BOB = "@bob:localhost"
CAROL = "@carol:localhost"
DAVE = "@dave:localhost"

TRIGGER_ROW_KEYS = frozenset(
    {
        "trigger_id",
        "kind",
        "relation",
        "description",
        "prompt",
        "agent",
        "target_room_id",
        "target_thread_id",
        "thread_mode",
        "enabled",
        "disabled_reason",
        "once",
        "expires_at",
        "last_fired_at",
        "owner_user_id",
        "can_manage",
    },
)


def _public_key_b64() -> str:
    raw = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _config(*, admins: list[str] | None = None, aliases: dict[str, list[str]] | None = None) -> Config:
    return Config(
        administrators=admins or [],
        authorization=AuthorizationConfig(aliases=aliases or {}),
        external_trigger_policy=ExternalTriggerPolicyConfig(admin_users=admins or []),
        agents={
            "assistant": AgentConfig(display_name="Assistant", rooms=["lobby", ROOM_A, ROOM_B, ROOM_C]),
            "ops": AgentConfig(display_name="Ops", rooms=[ROOM_B]),
        },
    )


class _Harness:
    """Minimal FastAPI app serving room_triggers with live room membership, state, and trigger stores."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        config: Config | None = None,
        room_members_by_id: dict[str, list[str]] | None = None,
        room_state_events: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.paths: RuntimePaths = test_runtime_paths(tmp_path)
        self.config = config or _config()
        matrix_state = MatrixState()
        matrix_state.add_room("lobby", ROOM_A, "#lobby:localhost", "Lobby")
        save_matrix_state(matrix_state, self.paths)

        self.members_by_room = room_members_by_id or {
            ROOM_A: [ALICE, ALICE_BRIDGE, BOB, CAROL],
            ROOM_B: [ALICE, BOB],
            ROOM_C: [ALICE, BOB],
        }
        self.state_events_by_room = room_state_events or {}
        bot_user_id = "@mindroom_router:localhost"

        async def _joined_members(room_id: str) -> object:
            if room_id not in self.members_by_room:
                return nio.JoinedMembersError("not joined")
            return joined_members_response(room_id, bot_user_id, *self.members_by_room[room_id])

        async def _room_get_state(room_id: str) -> object:
            events = self.state_events_by_room.get(room_id, [])
            return nio.RoomGetStateResponse(events, room_id)

        self.client = SimpleNamespace(
            user_id=bot_user_id,
            rooms={rid: nio.MatrixRoom(rid, bot_user_id) for rid in self.members_by_room},
            joined_members=AsyncMock(side_effect=_joined_members),
            room_get_state=AsyncMock(side_effect=_room_get_state),
        )
        self.wake_reactive = MagicMock()

        self.app = FastAPI()
        self.app.include_router(room_triggers.router)
        state = ensure_app_state(self.app)
        state.api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, self.paths, runtime_config=self.config),
        )
        state.external_trigger_runtime = ExternalTriggerRuntime(
            target_bot=lambda _name: None,
            config_generation=1,
            on_reactive_event=self.wake_reactive,
        )
        bot = SimpleNamespace(client=self.client, running=True, config=self.config)
        bind_room_members(self.app, {ROUTER_AGENT_NAME: bot})  # type: ignore[arg-type]

        self.external_store = ExternalTriggerStore(self.paths)
        self.reactive_store = ReactiveTriggerStore(self.paths)
        self.http = TestClient(self.app)

    def _headers(self, user_id: str | None) -> dict[str, str]:
        if user_id is None:
            return {}
        token = client_session_store(self.app).create(user_id).token
        return {"Authorization": f"Bearer {token}"}

    def get_triggers(self, room_id: str, user_id: str | None) -> Response:
        return self.http.get(f"/api/rooms/{room_id}/triggers", headers=self._headers(user_id))

    def patch_trigger(self, room_id: str, trigger_id: str, user_id: str | None, payload: dict[str, object]) -> Response:
        return self.http.patch(
            f"/api/rooms/{room_id}/triggers/{trigger_id}",
            headers=self._headers(user_id),
            json=payload,
        )

    def delete_trigger(self, room_id: str, trigger_id: str, user_id: str | None) -> Response:
        return self.http.delete(f"/api/rooms/{room_id}/triggers/{trigger_id}", headers=self._headers(user_id))


def test_room_triggers_auth_gates(tmp_path: Path) -> None:
    """Routes require a live client session joined to the room, answering 401, 403, and 503."""
    harness = _Harness(tmp_path)

    assert harness.get_triggers(ROOM_A, None).status_code == 401
    assert harness.patch_trigger(ROOM_A, "tr_any", None, {"enabled": False}).status_code == 401
    assert harness.delete_trigger(ROOM_A, "tr_any", None).status_code == 401

    assert harness.get_triggers(ROOM_A, DAVE).status_code == 403
    assert harness.patch_trigger(ROOM_A, "tr_any", DAVE, {"enabled": False}).status_code == 403
    assert harness.delete_trigger(ROOM_A, "tr_any", DAVE).status_code == 403

    ensure_app_state(harness.app).room_members = None
    assert harness.get_triggers(ROOM_A, ALICE).status_code == 503


def _seed_room_a_triggers(harness: _Harness) -> tuple[str, ...]:
    """Populate ROOM_A with webhooks, reactive target/watch triggers, and one ROOM_C trigger; return forbidden strings."""
    pub_key = _public_key_b64()
    cap_hash = hashlib.sha256(b"super-secret-capability-token").hexdigest()

    wh_key = harness.external_store.create_record(
        trigger_id="wh_deploy",
        owner_user_id=ALICE,
        created_by_agent_name="assistant",
        created_in_room_id=ROOM_A,
        created_in_thread_id=None,
        target=ExternalTriggerTarget(room_id="lobby", thread_id="$deploy_thread", agent="assistant"),
        public_key=pub_key,
        key_id="deploy-key-secret-id",
        description="Deploy notifications",
        config=harness.config,
    )
    harness.external_store.record_fired("wh_deploy", expected_uid=wh_key.uid, fired_at=1_764_000_000)

    wh_cap = harness.external_store.create_single_use_capability_record(
        trigger_id="wh_once",
        owner_user_id=ALICE,
        created_by_agent_name="assistant",
        created_in_room_id=ROOM_A,
        created_in_thread_id=None,
        target=ExternalTriggerTarget(room_id="#lobby:localhost", new_thread=True, agent="assistant"),
        capability_token_hash=cap_hash,
        description="One-shot job callback",
        config=harness.config,
    )
    harness.external_store.create_record(
        trigger_id="wh_disabled",
        owner_user_id=BOB,
        created_by_agent_name="assistant",
        created_in_room_id=ROOM_A,
        created_in_thread_id=None,
        target=ExternalTriggerTarget(room_id=ROOM_A, agent="assistant"),
        public_key=_public_key_b64(),
        description="Paused webhook",
        enabled=False,
        config=harness.config,
    )

    now = 1_764_000_100.5
    same_room_rule = harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_same_room",
            owner_user_id=ALICE,
            description="Watch same room",
            source=RoomMessageSourceSpec(room_id=ROOM_A, pattern="urgent"),
            target=TriggerTargetSpec(
                agent="assistant",
                room_id=ROOM_A,
                thread_id="origin",
                prompt="Triage urgent message",
            ),
            enabled=False,
            disabled_reason="once_consumed",
            once=True,
            expires_at=1_764_050_000.25,
            last_fired_at=1_764_000_050.75,
            created_at=now,
            updated_at=now,
        ),
    )
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_ext_event",
            owner_user_id=ALICE,
            description="GitHub PRs",
            source=ExternalEventSourceSpec(source_id="github", kind="pull_request"),
            target=TriggerTargetSpec(agent="assistant", room_id=ROOM_A, prompt="Review PR"),
            created_at=now + 1,
            updated_at=now + 1,
        ),
    )
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_watch_msg",
            owner_user_id=ALICE,
            description="Private ops escalation",
            source=RoomMessageSourceSpec(room_id=ROOM_A),
            target=TriggerTargetSpec(
                agent="ops",
                room_id=ROOM_B,
                thread_id="$ops_thread",
                prompt="Secret escalation instructions",
            ),
            created_at=now + 2,
            updated_at=now + 2,
        ),
    )
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_watch_tag",
            owner_user_id=ALICE,
            description="Hidden tag watch",
            source=ThreadTagSourceSpec(room_id=ROOM_A, thread_id="$t1", tag="resolved"),
            target=TriggerTargetSpec(
                agent="ops",
                room_id=UNREACHABLE_ROOM,
                new_thread=True,
                prompt="Secret tag prompt",
            ),
            created_at=now + 3,
            updated_at=now + 3,
        ),
    )
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_other_room",
            owner_user_id=ALICE,
            description="Other room",
            source=RoomMessageSourceSpec(room_id=ROOM_C),
            target=TriggerTargetSpec(agent="assistant", room_id=ROOM_C, prompt="Other"),
            created_at=now + 4,
            updated_at=now + 4,
        ),
    )
    return (
        pub_key,
        "deploy-key-secret-id",
        wh_key.public_key_fingerprint or "",
        cap_hash,
        wh_key.uid,
        wh_cap.uid,
        same_room_rule.rule_uid,
        "ed25519",
        "capability",
        "public_key",
        "key_id",
        "capability_token_hash",
    )


def test_list_returns_target_and_watch_rows_resolves_aliases_and_strips_secrets(tmp_path: Path) -> None:
    """GET returns allowlisted target/watch rows, resolves webhook room keys/aliases, and leaks no secrets."""
    harness = _Harness(tmp_path)
    forbidden_strings = _seed_room_a_triggers(harness)

    alice_resp = harness.get_triggers(ROOM_A, ALICE)
    assert alice_resp.status_code == 200
    triggers = alice_resp.json()["triggers"]
    alice_rows = {row["trigger_id"]: row for row in triggers}
    assert len(triggers) == 7
    assert set(alice_rows) == {
        "wh_deploy",
        "wh_once",
        "wh_disabled",
        "tr_same_room",
        "tr_ext_event",
        "tr_watch_msg",
        "tr_watch_tag",
    }
    for row in triggers:
        assert set(row) == TRIGGER_ROW_KEYS
    for forbidden in forbidden_strings:
        assert forbidden not in alice_resp.text

    assert alice_rows["wh_deploy"] == {
        "trigger_id": "wh_deploy",
        "kind": "webhook",
        "relation": "target",
        "description": "Deploy notifications",
        "prompt": None,
        "agent": "assistant",
        "target_room_id": ROOM_A,
        "target_thread_id": "$deploy_thread",
        "thread_mode": "thread",
        "enabled": True,
        "disabled_reason": None,
        "once": False,
        "expires_at": None,
        "last_fired_at": 1_764_000_000_000,
        "owner_user_id": ALICE,
        "can_manage": True,
    }
    assert alice_rows["wh_once"]["once"] is True
    assert alice_rows["wh_once"]["thread_mode"] == "new_thread"
    assert alice_rows["wh_once"]["target_thread_id"] is None
    assert alice_rows["wh_once"]["target_room_id"] == ROOM_A
    assert alice_rows["wh_disabled"]["enabled"] is False
    assert alice_rows["wh_disabled"]["disabled_reason"] == "manual"
    assert alice_rows["wh_disabled"]["thread_mode"] == "room"
    assert alice_rows["wh_disabled"]["can_manage"] is False
    assert alice_rows["tr_ext_event"]["kind"] == "external_event"
    assert alice_rows["tr_ext_event"]["relation"] == "target"
    assert alice_rows["tr_same_room"] == {
        "trigger_id": "tr_same_room",
        "kind": "room_message",
        "relation": "target",
        "description": "Watch same room",
        "prompt": "Triage urgent message",
        "agent": "assistant",
        "target_room_id": ROOM_A,
        "target_thread_id": None,
        "thread_mode": "origin",
        "enabled": False,
        "disabled_reason": "once_consumed",
        "once": True,
        "expires_at": 1_764_050_000_250,
        "last_fired_at": 1_764_000_050_750,
        "owner_user_id": ALICE,
        "can_manage": True,
    }


def test_list_redacts_cross_room_watches_for_non_members_and_unreachable_targets(tmp_path: Path) -> None:
    """Owners and target-room members see cross-room watch details; non-members and unreadable targets get redacted."""
    harness = _Harness(tmp_path)
    _seed_room_a_triggers(harness)

    # Owner always sees unredacted watch rows, even for rooms without a running bot.
    alice_rows = {row["trigger_id"]: row for row in harness.get_triggers(ROOM_A, ALICE).json()["triggers"]}
    assert alice_rows["tr_watch_msg"]["relation"] == "watch"
    assert alice_rows["tr_watch_msg"]["prompt"] == "Secret escalation instructions"
    assert alice_rows["tr_watch_msg"]["target_room_id"] == ROOM_B
    assert alice_rows["tr_watch_msg"]["target_thread_id"] == "$ops_thread"
    assert alice_rows["tr_watch_tag"]["prompt"] == "Secret tag prompt"

    # Bob is joined to ROOM_B (unredacted) but NOT to UNREACHABLE_ROOM (redacted fail-closed).
    bob_rows = {row["trigger_id"]: row for row in harness.get_triggers(ROOM_A, BOB).json()["triggers"]}
    assert bob_rows["tr_watch_msg"]["can_manage"] is False
    assert bob_rows["tr_watch_msg"]["prompt"] == "Secret escalation instructions"
    assert bob_rows["tr_watch_msg"]["target_room_id"] == ROOM_B
    assert bob_rows["tr_watch_tag"]["description"] == ""
    assert bob_rows["tr_watch_tag"]["prompt"] is None
    assert bob_rows["tr_watch_tag"]["agent"] is None
    assert bob_rows["tr_watch_tag"]["target_room_id"] is None
    assert bob_rows["tr_watch_tag"]["target_thread_id"] is None

    # Carol is NOT joined to ROOM_B: tr_watch_msg is redacted completely.
    carol_resp = harness.get_triggers(ROOM_A, CAROL)
    assert carol_resp.status_code == 200
    carol_rows = {row["trigger_id"]: row for row in carol_resp.json()["triggers"]}
    assert carol_rows["tr_watch_msg"] == {
        "trigger_id": "tr_watch_msg",
        "kind": "room_message",
        "relation": "watch",
        "description": "",
        "prompt": None,
        "agent": None,
        "target_room_id": None,
        "target_thread_id": None,
        "thread_mode": "thread",
        "enabled": True,
        "disabled_reason": None,
        "once": False,
        "expires_at": None,
        "last_fired_at": None,
        "owner_user_id": ALICE,
        "can_manage": False,
    }
    for leaked in ("Secret escalation instructions", "Private ops escalation", ROOM_B, "$ops_thread"):
        assert leaked not in carol_resp.text


def test_owner_only_patch_and_delete_reject_admins_and_foreign_room_triggers(tmp_path: Path) -> None:
    """PATCH/DELETE enforce room membership (404 first) and strict owner-only access (403 even for admins)."""
    config = _config(admins=[BOB], aliases={ALICE: [ALICE_BRIDGE]})
    harness = _Harness(tmp_path, config=config)

    harness.external_store.create_record(
        trigger_id="wh_room_a",
        owner_user_id=ALICE,
        created_by_agent_name="assistant",
        created_in_room_id=ROOM_A,
        created_in_thread_id=None,
        target=ExternalTriggerTarget(room_id="lobby", agent="assistant"),
        public_key=_public_key_b64(),
        description="Lobby webhook",
        config=config,
    )
    now = time.time()
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_watch_a_to_b",
            owner_user_id=ALICE,
            description="Cross-room watch",
            source=RoomMessageSourceSpec(room_id=ROOM_A),
            target=TriggerTargetSpec(agent="ops", room_id=ROOM_B, prompt="Notify ops"),
            created_at=now,
            updated_at=now,
        ),
    )
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_room_c",
            owner_user_id=ALICE,
            description="Room C trigger",
            source=RoomMessageSourceSpec(room_id=ROOM_C),
            target=TriggerTargetSpec(agent="assistant", room_id=ROOM_C, prompt="In C"),
            created_at=now,
            updated_at=now,
        ),
    )

    # Trigger in another room -> 404 (even for its owner and for non-owners).
    assert harness.patch_trigger(ROOM_A, "tr_room_c", ALICE, {"enabled": False}).status_code == 404
    assert harness.patch_trigger(ROOM_A, "tr_room_c", BOB, {"enabled": False}).status_code == 404
    assert harness.delete_trigger(ROOM_A, "tr_room_c", ALICE).status_code == 404
    assert harness.delete_trigger(ROOM_A, "tr_room_c", BOB).status_code == 404
    assert harness.patch_trigger(ROOM_A, "does_not_exist", ALICE, {"enabled": False}).status_code == 404
    assert harness.delete_trigger(ROOM_A, "does_not_exist", ALICE).status_code == 404

    # Non-owner member gets 403, even when configured as both platform and external trigger admin.
    assert harness.patch_trigger(ROOM_A, "wh_room_a", BOB, {"enabled": False}).status_code == 403
    assert harness.patch_trigger(ROOM_A, "tr_watch_a_to_b", BOB, {"enabled": False}).status_code == 403
    assert harness.delete_trigger(ROOM_A, "wh_room_a", BOB).status_code == 403
    assert harness.delete_trigger(ROOM_A, "tr_watch_a_to_b", BOB).status_code == 403
    assert harness.external_store.get_record("wh_room_a") is not None
    assert harness.reactive_store.get_trigger("tr_watch_a_to_b") is not None

    # Invalid PATCH payloads fail validation.
    assert harness.patch_trigger(ROOM_A, "wh_room_a", ALICE, {"enabled": "false"}).status_code == 422
    assert harness.patch_trigger(ROOM_A, "wh_room_a", ALICE, {"enabled": False, "extra": 1}).status_code == 422

    # Equivalent bridge alias of owner sees can_manage=True and can toggle & delete both webhook and watch triggers.
    alias_list = {row["trigger_id"]: row for row in harness.get_triggers(ROOM_A, ALICE_BRIDGE).json()["triggers"]}
    assert alias_list["wh_room_a"]["can_manage"] is True
    assert alias_list["tr_watch_a_to_b"]["can_manage"] is True

    disabled = harness.patch_trigger(ROOM_A, "wh_room_a", ALICE_BRIDGE, {"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["disabled_reason"] == "manual"

    reenabled = harness.patch_trigger(ROOM_A, "wh_room_a", ALICE_BRIDGE, {"enabled": True})
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True
    assert reenabled.json()["disabled_reason"] is None

    assert harness.delete_trigger(ROOM_A, "wh_room_a", ALICE_BRIDGE).status_code == 204
    assert harness.delete_trigger(ROOM_A, "tr_watch_a_to_b", ALICE_BRIDGE).status_code == 204
    assert harness.external_store.get_record("wh_room_a") is None
    assert harness.reactive_store.get_trigger("tr_watch_a_to_b") is None
    assert harness.get_triggers(ROOM_A, ALICE).json() == {"triggers": []}


def test_reenabling_reactive_triggers_refreshes_cursor_and_tag_checkpoint_and_wakes_drain(tmp_path: Path) -> None:
    """Re-enabling disabled room_message and thread_tag triggers refreshes state and wakes the reactive drain."""
    tag_event = {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": '["$thread_root","resolved"]',
        "content": {"set_by": ALICE, "set_at": "2026-11-24T12:00:00Z"},
    }
    harness = _Harness(tmp_path, room_state_events={ROOM_A: [tag_event]})
    now = time.time()

    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_msg",
            owner_user_id=ALICE,
            description="Room watcher",
            source=RoomMessageSourceSpec(room_id=ROOM_A),
            target=TriggerTargetSpec(agent="assistant", room_id=ROOM_A, prompt="Handle msg"),
            enabled=False,
            disabled_reason="manual",
            created_at=now,
            updated_at=now,
        ),
        initial_cursor=RoomCursor(created_ts=1_000, logical_event_id="$stale"),
    )
    harness.reactive_store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="tr_tag",
            owner_user_id=ALICE,
            description="Tag watcher",
            source=ThreadTagSourceSpec(room_id=ROOM_A, thread_id="$thread_root", tag="resolved"),
            target=TriggerTargetSpec(agent="assistant", room_id=ROOM_A, prompt="Handle tag"),
            enabled=False,
            disabled_reason="once_consumed",
            once=True,
            created_at=now,
            updated_at=now,
        ),
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id="$old", updated_at=100.0),
    )

    before_ms = int(time.time() * 1000)
    msg_resp = harness.patch_trigger(ROOM_A, "tr_msg", ALICE, {"enabled": True})
    assert msg_resp.status_code == 200
    assert msg_resp.json()["enabled"] is True
    assert msg_resp.json()["disabled_reason"] is None
    refreshed_cursor = harness.reactive_store.get_room_cursor("tr_msg")
    assert refreshed_cursor is not None
    assert refreshed_cursor.created_ts >= before_ms
    assert refreshed_cursor.logical_event_id == ""
    assert harness.wake_reactive.call_count == 1

    tag_resp = harness.patch_trigger(ROOM_A, "tr_tag", ALICE, {"enabled": True})
    assert tag_resp.status_code == 200
    assert tag_resp.json()["enabled"] is True
    assert tag_resp.json()["disabled_reason"] is None
    refreshed_checkpoint = harness.reactive_store.get_thread_checkpoint("tr_tag")
    assert refreshed_checkpoint is not None
    assert refreshed_checkpoint.tag_present is True
    assert harness.wake_reactive.call_count == 2

    # Disabling sets disabled_reason="manual" and does not wake the drain.
    disable_resp = harness.patch_trigger(ROOM_A, "tr_msg", ALICE, {"enabled": False})
    assert disable_resp.status_code == 200
    assert disable_resp.json()["enabled"] is False
    assert disable_resp.json()["disabled_reason"] == "manual"
    assert harness.wake_reactive.call_count == 2


def test_room_triggers_routes_are_mounted_on_main_app() -> None:
    """The main FastAPI application exposes all three room triggers endpoints."""
    routes = {
        (route.path, method)
        for route in main_app.routes
        if hasattr(route, "methods") and hasattr(route, "path")
        for method in route.methods
    }
    assert ("/api/rooms/{room_id}/triggers", "GET") in routes
    assert ("/api/rooms/{room_id}/triggers/{trigger_id}", "PATCH") in routes
    assert ("/api/rooms/{room_id}/triggers/{trigger_id}", "DELETE") in routes
