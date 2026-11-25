"""Room-scoped trigger listing, cross-room watch redaction, owner-only mutations, and re-enable refresh."""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import nio
from fastapi.testclient import TestClient

from mindroom import plugin_http
from mindroom.api import room_triggers
from mindroom.api.app_state import ExternalTriggerRuntime, ensure_app_state
from mindroom.api.main import app as main_app
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
from mindroom.config.main import Config
from mindroom.external_triggers.store import ExternalTriggerRecord, ExternalTriggerStore, ExternalTriggerTarget
from mindroom.matrix.state import MatrixState
from mindroom.reactive_triggers.models import (
    GitHubPRCheckpoint,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriggerSourceSpec,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from tests.access_schema_support import joined_members_response
from tests.api.test_plugin_room_readers import _bound, _client, _never_answers
from tests.conftest import test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state
from tests.test_external_trigger_store import _PUBLIC_KEY
from tests.test_plugin_routes import _api_state, _signed_in

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from httpx import Response

    from mindroom.constants import RuntimePaths

ROOM_A, ROOM_B, ROOM_C, UNREACHABLE = "!room-a:localhost", "!room-b:localhost", "!room-c:localhost", "!no-bot:localhost"
ALICE, ALICE_BRIDGE, BOB, CAROL, DAVE = (
    "@alice:localhost",
    "@alice_bridge:localhost",
    "@bob:localhost",
    "@carol:localhost",
    "@dave:localhost",
)


class _Harness:
    """FastAPI app serving room_triggers with live room membership, state, and trigger stores."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        admins: list[str] | None = None,
        aliases: dict[str, list[str]] | None = None,
        room_state_events: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.paths: RuntimePaths = test_runtime_paths(tmp_path)
        self.config = Config(
            administrators=admins or [],
            authorization=AuthorizationConfig(aliases=aliases or {}),
            external_trigger_policy=ExternalTriggerPolicyConfig(admin_users=admins or []),
            agents={
                "assistant": AgentConfig(display_name="Assistant", rooms=["lobby", ROOM_A, ROOM_B, ROOM_C]),
                "ops": AgentConfig(display_name="Ops", rooms=[ROOM_B]),
            },
        )
        matrix_state = MatrixState()
        matrix_state.add_room("lobby", ROOM_A, "#lobby:localhost", "Lobby")
        save_matrix_state(matrix_state, self.paths)

        members = {ROOM_A: [ALICE, ALICE_BRIDGE, BOB, CAROL], ROOM_B: [ALICE, BOB], ROOM_C: [ALICE, BOB]}
        events = room_state_events or {}
        self.client = _client(joined_rooms=tuple(members))

        async def _joined(room_id: str) -> object:
            return (
                joined_members_response(room_id, self.client.user_id, *members[room_id])
                if room_id in members
                else nio.JoinedMembersError("not joined")
            )

        self.client.joined_members.side_effect = _joined
        self.client.room_get_state.side_effect = lambda rid: nio.RoomGetStateResponse(events.get(rid, []), rid)
        self.wake_reactive = MagicMock()
        self.app = _bound(self.client)
        self.app.include_router(room_triggers.router)
        state = ensure_app_state(self.app)
        state.api_state = _api_state(self.config, self.paths)
        state.external_trigger_runtime = ExternalTriggerRuntime(
            target_bot=lambda _name: None,
            config_generation=1,
            on_reactive_event=self.wake_reactive,
        )
        self.external_store = ExternalTriggerStore(self.paths)
        self.reactive_store = ReactiveTriggerStore(self.paths)
        self.http = TestClient(self.app)

    def add_webhook(
        self,
        trigger_id: str,
        *,
        owner: str = ALICE,
        room_id: str = ROOM_A,
        capability_hash: str | None = None,
        **extra: Any,  # noqa: ANN401
    ) -> ExternalTriggerRecord:
        target = ExternalTriggerTarget(
            room_id=room_id,
            thread_id=extra.pop("thread_id", None),
            new_thread=extra.pop("new_thread", False),
            reopen_on_fire=extra.pop("reopen_on_fire", False),
            agent="assistant",
        )
        base: dict[str, Any] = {
            "trigger_id": trigger_id,
            "owner_user_id": owner,
            "created_by_agent_name": "assistant",
            "created_in_room_id": ROOM_A,
            "created_in_thread_id": None,
            "target": target,
            "config": self.config,
            **extra,
        }
        if capability_hash is not None:
            return self.external_store.create_single_use_capability_record(
                **base,
                capability_token_hash=capability_hash,
            )
        base.setdefault("public_key", _PUBLIC_KEY)
        return self.external_store.create_record(**base)

    def add_reactive(
        self,
        trigger_id: str,
        source: TriggerSourceSpec,
        *,
        target_room: str = ROOM_A,
        agent: str = "assistant",
        prompt: str = "Handle trigger",
        **extra: Any,  # noqa: ANN401
    ) -> ReactiveTriggerRecord:
        ts = float(extra.pop("created_at", 1_764_000_100.0))
        cursor: RoomCursor | None = extra.pop("initial_cursor", None)
        checkpoint: ThreadCheckpoint | None = extra.pop("initial_checkpoint", None)
        gh_checkpoint: GitHubPRCheckpoint | None = extra.pop("initial_github_pr_checkpoint", None)
        target = TriggerTargetSpec(
            agent=agent,
            room_id=target_room,
            thread_id=extra.pop("thread_id", None),
            new_thread=extra.pop("new_thread", False),
            reopen_on_fire=extra.pop("reopen_on_fire", False),
            prompt=prompt,
        )
        record = ReactiveTriggerRecord(
            trigger_id=trigger_id,
            owner_user_id=ALICE,
            source=source,
            target=target,
            created_at=ts,
            updated_at=ts,
            **extra,
        )
        return self.reactive_store.create_trigger(
            record,
            initial_cursor=cursor,
            initial_checkpoint=checkpoint,
            initial_github_pr_checkpoint=gh_checkpoint,
        )

    def req(
        self,
        method: str,
        room_id: str,
        user_id: str | None,
        trigger_id: str | None = None,
        json: dict[str, object] | None = None,
    ) -> Response:
        path = f"/api/rooms/{room_id}/triggers" + (f"/{trigger_id}" if trigger_id else "")
        headers = {} if user_id is None else _signed_in(self.app, user_id)
        return self.http.request(method, path, headers=headers, json=json)


def test_room_triggers_routes_mounted_and_auth_gated(tmp_path: Path) -> None:
    """Routes are mounted on main app and enforce 401/403/503 via require_room_member."""
    main_http = TestClient(main_app)
    assert main_http.get(f"/api/rooms/{ROOM_A}/triggers").status_code == 401
    assert main_http.patch(f"/api/rooms/{ROOM_A}/triggers/tr_any", json={"enabled": False}).status_code == 401
    assert main_http.delete(f"/api/rooms/{ROOM_A}/triggers/tr_any").status_code == 401

    h = _Harness(tmp_path)
    assert h.req("GET", ROOM_A, DAVE).status_code == 403
    ensure_app_state(h.app).room_members = None
    assert h.req("GET", ROOM_A, ALICE).status_code == 503


def test_list_allowlist_aliases_redaction_and_no_secrets(tmp_path: Path) -> None:
    """GET returns allowlisted target/watch rows, resolves aliases, strips secrets, and redacts cross-room watches."""
    h = _Harness(tmp_path)
    pub_key, cap_hash = _PUBLIC_KEY, hashlib.sha256(b"super-secret-capability-token").hexdigest()

    wh_key = h.add_webhook(
        "wh_deploy",
        room_id="lobby",
        thread_id="$deploy_thread",
        reopen_on_fire=True,
        public_key=pub_key,
        key_id="deploy-key-secret-id",
        description="Deploy notifications",
    )
    h.external_store.record_fired("wh_deploy", expected_uid=wh_key.uid, fired_at=1_764_000_000)
    wh_cap = h.add_webhook("wh_once", owner=BOB, room_id="#lobby:localhost", new_thread=True, capability_hash=cap_hash)
    same_room = h.add_reactive(
        "tr_same_room",
        RoomMessageSourceSpec(room_id=ROOM_A),
        thread_id="origin",
        prompt="Triage urgent",
        description="Watch same room",
        enabled=False,
        disabled_reason="once_consumed",
        once=True,
        expires_at=1_764_050_000.25,
        last_fired_at=1_764_000_050.75,
    )
    h.add_reactive(
        "tr_watch_msg",
        RoomMessageSourceSpec(room_id=ROOM_A),
        target_room=ROOM_B,
        agent="ops",
        thread_id="$ops_thread",
        reopen_on_fire=True,
        description="Private ops escalation",
        prompt="Secret escalation instructions",
        created_at=2.0,
    )
    h.add_reactive(
        "tr_watch_unreachable",
        ThreadTagSourceSpec(room_id=ROOM_A, thread_id="$t1", tag="resolved"),
        target_room=UNREACHABLE,
        agent="ops",
        new_thread=True,
        prompt="Secret tag prompt",
        created_at=3.0,
    )
    h.add_reactive("tr_other_room", RoomMessageSourceSpec(room_id=ROOM_C), target_room=ROOM_C, created_at=4.0)

    alice_resp = h.req("GET", ROOM_A, ALICE)
    assert alice_resp.status_code == 200
    triggers = alice_resp.json()["triggers"]
    alice_rows = {row["trigger_id"]: row for row in triggers}
    assert len(triggers) == 5
    assert set(alice_rows) == {"wh_deploy", "wh_once", "tr_same_room", "tr_watch_msg", "tr_watch_unreachable"}

    expected_wh_deploy = {
        "trigger_id": "wh_deploy",
        "kind": "webhook",
        "relation": "target",
        "description": "Deploy notifications",
        "prompt": None,
        "agent": "assistant",
        "target_room_id": ROOM_A,
        "target_thread_id": "$deploy_thread",
        "thread_mode": "thread",
        "reopen_on_fire": True,
        "enabled": True,
        "disabled_reason": None,
        "once": False,
        "expires_at": None,
        "last_fired_at": 1_764_000_000_000,
        "owner_user_id": ALICE,
        "can_manage": True,
    }
    assert alice_rows["wh_deploy"] == expected_wh_deploy
    for row in triggers:
        assert set(row) == set(expected_wh_deploy)
    for forbidden in (
        pub_key,
        "deploy-key-secret-id",
        wh_key.public_key_fingerprint or "",
        cap_hash,
        wh_key.uid,
        wh_cap.uid,
        same_room.rule_uid,
        "ed25519",
        "capability",
        "public_key",
        "key_id",
        "capability_token_hash",
    ):
        assert forbidden not in alice_resp.text

    assert {
        "kind": "room_message",
        "relation": "target",
        "prompt": "Triage urgent",
        "target_thread_id": None,
        "thread_mode": "origin",
        "reopen_on_fire": False,
        "enabled": False,
        "disabled_reason": "once_consumed",
        "once": True,
        "expires_at": 1_764_050_000_250,
        "last_fired_at": 1_764_000_050_750,
    }.items() <= alice_rows["tr_same_room"].items()
    assert {
        "once": True,
        "thread_mode": "new_thread",
        "target_room_id": ROOM_A,
        "can_manage": False,
    }.items() <= alice_rows["wh_once"].items()
    assert alice_rows["tr_watch_msg"]["prompt"] == "Secret escalation instructions"
    assert alice_rows["tr_watch_msg"]["reopen_on_fire"] is True
    assert alice_rows["tr_watch_unreachable"]["prompt"] == "Secret tag prompt"

    bob_rows = {row["trigger_id"]: row for row in h.req("GET", ROOM_A, BOB).json()["triggers"]}
    assert bob_rows["tr_watch_msg"]["prompt"] == "Secret escalation instructions"
    assert bob_rows["tr_watch_msg"]["reopen_on_fire"] is True
    assert bob_rows["tr_watch_unreachable"]["prompt"] is None
    assert bob_rows["tr_watch_unreachable"]["target_room_id"] is None

    carol_resp = h.req("GET", ROOM_A, CAROL)
    assert {
        "relation": "watch",
        "description": "",
        "prompt": None,
        "agent": None,
        "target_room_id": None,
        "target_thread_id": None,
        "thread_mode": "thread",
        "reopen_on_fire": False,
        "can_manage": False,
    }.items() <= {row["trigger_id"]: row for row in carol_resp.json()["triggers"]}["tr_watch_msg"].items()
    for leaked in ("Secret escalation instructions", "Private ops escalation", ROOM_B, "$ops_thread"):
        assert leaked not in carol_resp.text


def test_owner_only_patch_and_delete_reject_admins_and_foreign_room_triggers(tmp_path: Path) -> None:
    """PATCH/DELETE enforce 404 for foreign-room triggers and strict 403 for non-owners (including admins)."""
    h = _Harness(tmp_path, admins=[BOB], aliases={ALICE: [ALICE_BRIDGE]})
    h.add_webhook("wh_room_a", room_id="lobby")
    h.add_reactive("tr_watch_a_to_b", RoomMessageSourceSpec(room_id=ROOM_A), target_room=ROOM_B, agent="ops")
    h.add_reactive("tr_room_c", RoomMessageSourceSpec(room_id=ROOM_C), target_room=ROOM_C)

    for caller in (ALICE, BOB):
        assert h.req("PATCH", ROOM_A, caller, "tr_room_c", {"enabled": False}).status_code == 404
        assert h.req("DELETE", ROOM_A, caller, "tr_room_c").status_code == 404
    assert h.req("PATCH", ROOM_A, ALICE, "missing", {"enabled": False}).status_code == 404

    for trigger_id in ("wh_room_a", "tr_watch_a_to_b"):
        assert h.req("PATCH", ROOM_A, BOB, trigger_id, {"enabled": False}).status_code == 403
        assert h.req("DELETE", ROOM_A, BOB, trigger_id).status_code == 403

    alias_rows = {row["trigger_id"]: row for row in h.req("GET", ROOM_A, ALICE_BRIDGE).json()["triggers"]}
    assert alias_rows["wh_room_a"]["can_manage"] is True
    assert alias_rows["tr_watch_a_to_b"]["can_manage"] is True

    off = h.req("PATCH", ROOM_A, ALICE_BRIDGE, "wh_room_a", {"enabled": False})
    assert (off.status_code, off.json()["enabled"], off.json()["disabled_reason"], off.json()["thread_mode"]) == (
        200,
        False,
        "manual",
        "room",
    )
    on = h.req("PATCH", ROOM_A, ALICE_BRIDGE, "wh_room_a", {"enabled": True})
    assert (on.status_code, on.json()["enabled"], on.json()["disabled_reason"]) == (200, True, None)

    assert h.req("DELETE", ROOM_A, ALICE_BRIDGE, "wh_room_a").status_code == 204
    assert h.req("DELETE", ROOM_A, ALICE_BRIDGE, "tr_watch_a_to_b").status_code == 204
    assert h.req("GET", ROOM_A, ALICE).json() == {"triggers": []}


def test_reenable_refreshes_cursor_and_tag_checkpoint_wakes_drain_and_bounds_tag_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-enabling disabled reactive triggers refreshes cursor/checkpoint, wakes drain, and times out to 503."""
    tag_event = {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": '["$thread_root","resolved"]',
        "content": {"set_by": ALICE, "set_at": "2026-11-24T12:00:00Z"},
    }
    h = _Harness(tmp_path, room_state_events={ROOM_A: [tag_event]})
    h.add_reactive(
        "tr_msg",
        RoomMessageSourceSpec(room_id=ROOM_A),
        enabled=False,
        disabled_reason="manual",
        initial_cursor=RoomCursor(created_ts=1_000, logical_event_id="$stale"),
    )
    h.add_reactive(
        "tr_tag",
        ThreadTagSourceSpec(room_id=ROOM_A, thread_id="$thread_root", tag="resolved"),
        enabled=False,
        disabled_reason="once_consumed",
        once=True,
        initial_checkpoint=ThreadCheckpoint(tag_present=False, state_event_id="$old", updated_at=100.0),
    )

    before_ms = int(time.time() * 1000)
    msg_resp = h.req("PATCH", ROOM_A, ALICE, "tr_msg", {"enabled": True})
    assert (msg_resp.status_code, msg_resp.json()["enabled"], msg_resp.json()["disabled_reason"]) == (200, True, None)
    cursor = h.reactive_store.get_room_cursor("tr_msg")
    assert cursor is not None
    assert cursor.created_ts >= before_ms
    assert cursor.logical_event_id == ""
    assert h.wake_reactive.call_count == 1

    tag_resp = h.req("PATCH", ROOM_A, ALICE, "tr_tag", {"enabled": True})
    assert (tag_resp.status_code, tag_resp.json()["enabled"], tag_resp.json()["disabled_reason"]) == (200, True, None)
    checkpoint = h.reactive_store.get_thread_checkpoint("tr_tag")
    assert checkpoint is not None
    assert checkpoint.tag_present is True
    assert h.wake_reactive.call_count == 2

    disable_resp = h.req("PATCH", ROOM_A, ALICE, "tr_tag", {"enabled": False})
    assert (disable_resp.status_code, disable_resp.json()["disabled_reason"]) == (200, "manual")
    assert h.wake_reactive.call_count == 2

    h.client.room_get_state.side_effect = nio.LocalProtocolError("connection reset")
    assert h.req("PATCH", ROOM_A, ALICE, "tr_tag", {"enabled": True}).status_code == 503

    h.client.room_get_state = _never_answers
    monkeypatch.setattr(plugin_http, "ROOM_READ_TIMEOUT_SECONDS", 0.05)
    assert h.req("PATCH", ROOM_A, ALICE, "tr_tag", {"enabled": True}).status_code == 503


def test_github_pr_trigger_projects_with_pr_closed_reopen_on_fire_and_reenable_reseeds(tmp_path: Path) -> None:
    """Room triggers API surfaces github_pr rows cleanly and re-enabling resets seeded=False on the checkpoint."""
    h = _Harness(tmp_path)
    h.add_reactive(
        "tr_pr_42",
        GitHubPRSourceSpec(repo="matrix-org/synapse", number=42),
        description="Watch PR matrix-org/synapse#42",
        prompt="Inspect PR #42 update",
        thread_id="$thread_root",
        reopen_on_fire=True,
        enabled=False,
        disabled_reason="pr_closed",
        initial_github_pr_checkpoint=GitHubPRCheckpoint(
            head_sha="deadbeef",
            pr_state="closed",
            seeded=True,
            next_poll_at=9_999_999_999.0,
            consecutive_errors=3,
            updated_at=1_764_000_100.0,
        ),
    )

    resp = h.req("GET", ROOM_A, ALICE)
    assert resp.status_code == 200
    rows = resp.json()["triggers"]
    assert len(rows) == 1
    row = rows[0]
    assert row["trigger_id"] == "tr_pr_42"
    assert row["kind"] == "github_pr"
    assert row["relation"] == "target"
    assert row["reopen_on_fire"] is True
    assert row["enabled"] is False
    assert row["disabled_reason"] == "pr_closed"
    assert row["target_thread_id"] == "$thread_root"

    reenable = h.req("PATCH", ROOM_A, ALICE, "tr_pr_42", {"enabled": True})
    assert reenable.status_code == 200
    assert (reenable.json()["enabled"], reenable.json()["disabled_reason"]) == (True, None)
    cp = h.reactive_store.get_github_pr_checkpoint("tr_pr_42")
    assert cp is not None
    assert cp.seeded is False
    assert cp.consecutive_errors == 0
    assert cp.next_poll_at <= time.time() + 1.0
    assert h.wake_reactive.call_count == 1
