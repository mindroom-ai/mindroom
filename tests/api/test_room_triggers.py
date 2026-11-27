"""Room-scoped trigger listing, cross-room watch redaction, owner-only mutations, and re-enable refresh."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
from fastapi.testclient import TestClient

from mindroom.api import room_reads, room_triggers
from mindroom.api.app_state import ExternalTriggerRuntime, ensure_app_state
from mindroom.api.main import app as main_app
from mindroom.api.room_triggers import ScriptCheckSummary
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.event_sources import EventSource, EventSourceEmitter, EventSourceRegistry
from mindroom.external_triggers.store import ExternalTriggerRecord, ExternalTriggerStore, ExternalTriggerTarget
from mindroom.matrix.state import MatrixState
from mindroom.reactive_triggers.models import (
    ExternalEventSourceSpec,
    GitHubPRCheckpoint,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ScriptCheckCheckpoint,
    ScriptCheckSourceSpec,
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
from tests.event_source_fakes import FakeEventSource, owner_only_room_check
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
        max_reactive_triggers_per_owner: int = 50,
        max_github_pr_watches_per_owner: int = 20,
        max_script_checks_per_owner: int = 10,
        allow_local_script_checks: bool = False,
    ) -> None:
        self.paths: RuntimePaths = test_runtime_paths(tmp_path)
        assistant_tools = ["triggers", "script"] if allow_local_script_checks else []
        self.config = Config(
            administrators=admins or [],
            authorization=AuthorizationConfig(aliases=aliases or {}),
            defaults=DefaultsConfig(allow_shell_without_worker=allow_local_script_checks),
            external_trigger_policy=ExternalTriggerPolicyConfig(
                admin_users=admins or [],
                max_reactive_triggers_per_owner=max_reactive_triggers_per_owner,
                max_github_pr_watches_per_owner=max_github_pr_watches_per_owner,
                max_script_checks_per_owner=max_script_checks_per_owner,
            ),
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    rooms=["lobby", ROOM_A, ROOM_B, ROOM_C],
                    tools=assistant_tools,
                ),
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

    def use_event_sources(self, *sources: EventSource) -> tuple[EventSourceRegistry, dict[str, EventSourceEmitter]]:
        """Register event sources on this app's embedded runtime."""
        registry = EventSourceRegistry(lambda: self.config, AsyncMock(return_value=0))
        emitters = {src.source_id: registry.register(src) for src in sources}
        ensure_app_state(self.app).embedded = SimpleNamespace(  # type: ignore[assignment]
            reactive_triggers=SimpleNamespace(event_sources=registry),
        )
        return registry, emitters

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
        sc_checkpoint: ScriptCheckCheckpoint | None = extra.pop("initial_script_check_checkpoint", None)
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
            initial_script_check_checkpoint=sc_checkpoint,
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
        "script_check": None,
        "source_id": None,
        "paused_reason": None,
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
    monkeypatch.setattr(room_reads, "_ROOM_READ_TIMEOUT_SECONDS", 0.05)
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


def test_reenabling_github_pr_trigger_over_quota_returns_409(tmp_path: Path) -> None:
    """PATCH enabling a paused github_pr watch returns 409 when max_github_pr_watches_per_owner is reached."""
    h = _Harness(tmp_path, max_github_pr_watches_per_owner=1)
    h.add_reactive("tr_pr_paused", GitHubPRSourceSpec(repo="matrix-org/synapse", number=1), enabled=False)
    h.add_reactive("tr_pr_active", GitHubPRSourceSpec(repo="matrix-org/synapse", number=2), enabled=True)

    resp = h.req("PATCH", ROOM_A, ALICE, "tr_pr_paused", {"enabled": True})
    assert resp.status_code == 409
    assert "GitHub PR watch limit" in resp.json()["detail"]
    paused = h.reactive_store.get_trigger("tr_pr_paused")
    assert paused is not None
    assert paused.enabled is False

    h2 = _Harness(tmp_path / "owner_cap", max_reactive_triggers_per_owner=1, max_github_pr_watches_per_owner=5)
    h2.add_reactive(
        "tr_pr_closed",
        GitHubPRSourceSpec(repo="matrix-org/synapse", number=1),
        enabled=False,
        disabled_reason="pr_closed",
    )
    h2.add_reactive("tr_room_active", RoomMessageSourceSpec(room_id=ROOM_A), enabled=True)
    resp2 = h2.req("PATCH", ROOM_A, ALICE, "tr_pr_closed", {"enabled": True})
    assert resp2.status_code == 409
    assert "reactive trigger limit" in resp2.json()["detail"]


def test_script_check_trigger_summary_owner_health_and_cross_room_redaction(tmp_path: Path) -> None:
    """Room triggers API projects script_check summary, owner-only health, and redacts cross-room watches."""
    h = _Harness(tmp_path)
    digest_same = hashlib.sha256(b"same-room-check").hexdigest()
    digest_cross = hashlib.sha256(b"cross-room-check").hexdigest()

    h.add_reactive(
        "tr_sc_same",
        ScriptCheckSourceSpec(
            agent="assistant",
            room_id=ROOM_A,
            source_digest=digest_same,
            poll_interval_seconds=120,
        ),
        target_room=ROOM_A,
        agent="assistant",
        description="Poll same-room feed",
        prompt="Summarize new items",
        initial_script_check_checkpoint=ScriptCheckCheckpoint(
            revision=2,
            source_utf8="def check():\n    return {'secret_same_source': True}\n",
            identity_json={"channel": "matrix", "session_id": "secret-session-same"},
            state={"secret_cursor": "cursor-xyz"},
            next_poll_at=1_764_001_000.0,
            consecutive_errors=2,
            last_error_code="timeout",
            last_run_at=1_764_000_250.5,
            updated_at=1_764_000_250.5,
        ),
    )
    h.add_reactive(
        "tr_sc_cross",
        ScriptCheckSourceSpec(
            agent="ops",
            room_id=ROOM_A,
            source_digest=digest_cross,
            poll_interval_seconds=600,
        ),
        target_room=ROOM_B,
        agent="ops",
        description="Private ops script check",
        prompt="Secret ops escalation",
        created_at=1_764_000_200.0,
        initial_script_check_checkpoint=ScriptCheckCheckpoint(
            revision=1,
            source_utf8="def check():\n    return {'secret_cross_source': True}\n",
            identity_json={"channel": "matrix", "session_id": "secret-session-cross"},
            state={"secret_token": "tok-123"},
            next_poll_at=1_764_001_000.0,
            consecutive_errors=1,
            last_error_code="source_room_state_unreadable",
            last_run_at=1_764_000_300.25,
            updated_at=1_764_000_300.25,
        ),
    )

    # Owner gets summary + health in epoch ms and never receives source/identity/state
    alice_resp = h.req("GET", ROOM_A, ALICE)
    assert alice_resp.status_code == 200
    alice_rows = {row["trigger_id"]: row for row in alice_resp.json()["triggers"]}
    assert alice_rows["tr_sc_same"]["script_check"] == asdict(
        ScriptCheckSummary(
            executing_agent="assistant",
            poll_interval_seconds=120,
            source_digest=digest_same,
            last_run_at=1_764_000_250_500,
            consecutive_errors=2,
            last_error_code="timeout",
        ),
    )
    assert alice_rows["tr_sc_cross"]["relation"] == "watch"
    assert alice_rows["tr_sc_cross"]["script_check"] == {
        "executing_agent": "ops",
        "poll_interval_seconds": 600,
        "source_digest": digest_cross,
        "last_run_at": 1_764_000_300_250,
        "consecutive_errors": 1,
        "last_error_code": "source_room_state_unreadable",
    }
    for forbidden in (
        "secret_same_source",
        "secret_cross_source",
        "secret-session-same",
        "secret-session-cross",
        "secret_cursor",
        "cursor-xyz",
        "secret_token",
        "tok-123",
    ):
        assert forbidden not in alice_resp.text

    # Non-owner joined to both ROOM_A and ROOM_B gets summary with None health and no source/state
    bob_resp = h.req("GET", ROOM_A, BOB)
    assert bob_resp.status_code == 200
    bob_rows = {row["trigger_id"]: row for row in bob_resp.json()["triggers"]}
    assert bob_rows["tr_sc_same"]["can_manage"] is False
    assert bob_rows["tr_sc_same"]["script_check"] == {
        "executing_agent": "assistant",
        "poll_interval_seconds": 120,
        "source_digest": digest_same,
        "last_run_at": None,
        "consecutive_errors": None,
        "last_error_code": None,
    }
    assert bob_rows["tr_sc_cross"]["script_check"] == {
        "executing_agent": "ops",
        "poll_interval_seconds": 600,
        "source_digest": digest_cross,
        "last_run_at": None,
        "consecutive_errors": None,
        "last_error_code": None,
    }
    for forbidden in (
        "secret_same_source",
        "secret_cross_source",
        "cursor-xyz",
        "tok-123",
        "source_room_state_unreadable",
    ):
        assert forbidden not in bob_resp.text

    # Non-owner in ROOM_A who is NOT in ROOM_B gets redacted cross-room watch with script_check=None
    carol_resp = h.req("GET", ROOM_A, CAROL)
    assert carol_resp.status_code == 200
    carol_rows = {row["trigger_id"]: row for row in carol_resp.json()["triggers"]}
    assert carol_rows["tr_sc_same"]["script_check"] == {
        "executing_agent": "assistant",
        "poll_interval_seconds": 120,
        "source_digest": digest_same,
        "last_run_at": None,
        "consecutive_errors": None,
        "last_error_code": None,
    }
    assert carol_rows["tr_sc_cross"]["relation"] == "watch"
    assert carol_rows["tr_sc_cross"]["script_check"] is None
    for leaked in (
        digest_cross,
        "Private ops script check",
        "Secret ops escalation",
        ROOM_B,
        "secret_cross_source",
        "tok-123",
    ):
        assert leaked not in carol_resp.text

    # PATCH returns populated script_check summary for owner
    disable_resp = h.req("PATCH", ROOM_A, ALICE, "tr_sc_same", {"enabled": False})
    assert disable_resp.status_code == 200
    assert disable_resp.json()["script_check"] == {
        "executing_agent": "assistant",
        "poll_interval_seconds": 120,
        "source_digest": digest_same,
        "last_run_at": 1_764_000_250_500,
        "consecutive_errors": 2,
        "last_error_code": "timeout",
    }


def test_script_check_quota_rejected_reenable_preserves_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Astra fresh #3: quota-rejected PATCH re-enable (409) leaves script_check checkpoint untouched."""
    monkeypatch.setattr(
        "mindroom.script_runs.manager.background_script_supervision_supported",
        lambda: True,
    )
    h = _Harness(tmp_path, max_script_checks_per_owner=1, allow_local_script_checks=True)
    digest = hashlib.sha256(b"def check():\n    return None\n").hexdigest()
    h.add_reactive(
        "tr_sc_active",
        ScriptCheckSourceSpec(agent="assistant", room_id=ROOM_A, source_digest=digest, poll_interval_seconds=120),
        target_room=ROOM_A,
        agent="assistant",
        enabled=True,
        initial_script_check_checkpoint=ScriptCheckCheckpoint(
            revision=1,
            source_utf8="def check():\n    return None\n",
            identity_json={"channel": "matrix"},
            next_poll_at=1_764_001_000.0,
            updated_at=1_764_000_000.0,
        ),
    )
    h.add_reactive(
        "tr_sc_disabled",
        ScriptCheckSourceSpec(agent="assistant", room_id=ROOM_A, source_digest=digest, poll_interval_seconds=120),
        target_room=ROOM_A,
        agent="assistant",
        enabled=False,
        disabled_reason="manual",
        initial_script_check_checkpoint=ScriptCheckCheckpoint(
            revision=2,
            source_utf8="def check():\n    return None\n",
            identity_json={"channel": "matrix"},
            next_poll_at=1_764_001_000.0,
            consecutive_errors=3,
            last_error_code="timeout",
            last_run_at=1_764_000_250.5,
            updated_at=1_764_000_250.5,
        ),
    )

    rejected = h.req("PATCH", ROOM_A, ALICE, "tr_sc_disabled", {"enabled": True})
    assert rejected.status_code == 409
    cp_after_409 = h.reactive_store.get_script_check_checkpoint("tr_sc_disabled")
    assert cp_after_409 is not None
    assert (
        cp_after_409.revision,
        cp_after_409.consecutive_errors,
        cp_after_409.last_error_code,
        cp_after_409.next_poll_at,
    ) == (2, 3, "timeout", 1_764_001_000.0)

    # Once quota has room, PATCH re-enable succeeds and resets errors + bumps revision transactionally.
    assert h.req("PATCH", ROOM_A, ALICE, "tr_sc_active", {"enabled": False}).status_code == 200
    allowed = h.req("PATCH", ROOM_A, ALICE, "tr_sc_disabled", {"enabled": True})
    assert allowed.status_code == 200
    assert allowed.json()["script_check"]["consecutive_errors"] == 0
    assert allowed.json()["script_check"]["last_error_code"] is None
    cp_after_200 = h.reactive_store.get_script_check_checkpoint("tr_sc_disabled")
    assert cp_after_200 is not None
    assert (cp_after_200.revision, cp_after_200.consecutive_errors, cp_after_200.last_error_code) == (3, 0, None)


def _private_room_source(source_id: str) -> FakeEventSource:
    return FakeEventSource(source_id, private_room_only=True, delivery_check=owner_only_room_check)


def _member_event(user_id: str, event_id: str, ts: int = 1_000) -> dict[str, object]:
    return {
        "type": "m.room.member",
        "state_key": user_id,
        "sender": user_id,
        "event_id": event_id,
        "origin_server_ts": ts,
        "content": {"membership": "join"},
    }


def test_registered_event_source_hides_private_room_triggers_and_reports_owner_paused_reason(
    tmp_path: Path,
) -> None:
    """GET hides private_room_only triggers from non-owners, computes live owner paused_reason, and omits match/binding."""
    room_events: dict[str, list[dict[str, object]]] = {ROOM_A: [_member_event(ALICE, "$m1")]}
    h = _Harness(tmp_path, room_state_events=room_events)
    h.use_event_sources(_private_room_source("slack_priv"), FakeEventSource("public_src"))

    h.add_reactive(
        "tr_slack",
        ExternalEventSourceSpec(
            source_id="slack_priv",
            kind="message",
            match={"channel_id": ["C_SECRET"]},
            binding={"team_id": "T_SECRET", "slack_user_id": "U_SECRET"},
            source_origin="registered",
        ),
        description="My private Slack watch",
        prompt="Summarize private Slack DM",
        created_at=1.0,
    )
    h.add_reactive(
        "tr_public",
        ExternalEventSourceSpec(
            source_id="public_src",
            kind="deploy",
            match={"env": "prod"},
            binding={"token": "BIND_SECRET"},
            source_origin="registered",
        ),
        description="Deploy alerts",
        prompt="Announce deploy",
        created_at=2.0,
    )
    h.add_reactive(
        "tr_yaml",
        ExternalEventSourceSpec(source_id="github_wh", kind="push", match={"repo": "org/app"}),
        description="GitHub push",
        prompt="Review push",
        created_at=3.0,
    )

    # Owner sees all 3 triggers with source_id, live paused_reason=None, and zero match/binding leakage.
    h.client.room_get_state.reset_mock()
    alice_resp = h.req("GET", ROOM_A, ALICE)
    assert alice_resp.status_code == 200
    alice_rows = {row["trigger_id"]: row for row in alice_resp.json()["triggers"]}
    assert set(alice_rows) == {"tr_slack", "tr_public", "tr_yaml"}
    assert {tid: (row["source_id"], row["paused_reason"]) for tid, row in alice_rows.items()} == {
        "tr_slack": ("slack_priv", None),
        "tr_public": ("public_src", None),
        "tr_yaml": ("github_wh", None),
    }
    assert h.client.room_get_state.call_count == 1
    for secret in ("binding", "match", "C_SECRET", "T_SECRET", "U_SECRET", "BIND_SECRET"):
        assert secret not in alice_resp.text

    # When another person joins ROOM_A, owner sees live paused_reason and non-owner cannot see private_room_only trigger.
    room_events[ROOM_A].append(_member_event(BOB, "$m2", 2_000))
    alice_paused = {row["trigger_id"]: row for row in h.req("GET", ROOM_A, ALICE).json()["triggers"]}
    assert (alice_paused["tr_slack"]["paused_reason"], alice_paused["tr_public"]["paused_reason"]) == (
        "Room has another member",
        None,
    )

    bob_resp = h.req("GET", ROOM_A, BOB)
    assert bob_resp.status_code == 200
    bob_rows = {row["trigger_id"]: row for row in bob_resp.json()["triggers"]}
    assert set(bob_rows) == {"tr_public", "tr_yaml"}
    assert "tr_slack" not in bob_resp.text
    assert "My private Slack watch" not in bob_resp.text
    assert (bob_rows["tr_public"]["source_id"], bob_rows["tr_public"]["paused_reason"]) == ("public_src", None)

    # Owner PATCH also projects source_id and live paused_reason.
    patched = h.req("PATCH", ROOM_A, ALICE, "tr_slack", {"enabled": False})
    assert (patched.status_code, patched.json()["source_id"], patched.json()["paused_reason"]) == (
        200,
        "slack_priv",
        "Room has another member",
    )

    # Unreadable room state fails closed into paused_reason instead of failing GET with 503.
    h.client.room_get_state.side_effect = nio.LocalProtocolError("connection reset")
    alice_unreadable = h.req("GET", ROOM_A, ALICE)
    assert alice_unreadable.status_code == 200
    rows_unreadable = {row["trigger_id"]: row for row in alice_unreadable.json()["triggers"]}
    assert rows_unreadable["tr_slack"]["paused_reason"] == "Cannot verify private room state"


def test_unregistered_private_event_source_stays_hidden_from_non_owners(tmp_path: Path) -> None:
    """Disabling/unregistering a private_room_only plugin source keeps its stored rule hidden from non-owners."""
    h = _Harness(tmp_path)
    registry, emitters = h.use_event_sources(_private_room_source("slack_priv"), FakeEventSource("public_src"))
    h.add_reactive(
        "tr_slack",
        ExternalEventSourceSpec(
            source_id="slack_priv",
            kind="message",
            binding={"team_id": "T1"},
            source_origin="registered",
        ),
        description="Private Slack watch",
        prompt="Summarize",
        created_at=1.0,
    )
    h.add_reactive(
        "tr_yaml",
        ExternalEventSourceSpec(source_id="github_wh", kind="push"),
        description="GitHub push",
        prompt="Review",
        created_at=2.0,
    )

    assert registry.unregister("slack_priv", emitters["slack_priv"].token) is True
    bob_resp = h.req("GET", ROOM_A, BOB)
    assert bob_resp.status_code == 200
    assert {row["trigger_id"] for row in bob_resp.json()["triggers"]} == {"tr_yaml"}
    assert "tr_slack" not in bob_resp.text
    alice_rows = {row["trigger_id"]: row for row in h.req("GET", ROOM_A, ALICE).json()["triggers"]}
    assert "tr_slack" in alice_rows
    assert alice_rows["tr_slack"]["paused_reason"] is None


def test_dynamic_descriptor_private_room_only_hides_trigger_in_room_api(tmp_path: Path) -> None:
    """Persisted descriptor-level private_room_only hides a trigger in GET room triggers even when class-level is False."""
    h = _Harness(tmp_path)
    h.use_event_sources(
        FakeEventSource(
            "dyn_private",
            allowed_kinds=("alert",),
            private_room_only=False,
            descriptor_private_room_only=True,
        ),
    )
    h.add_reactive(
        "dyn_priv_api",
        ExternalEventSourceSpec(
            source_id="dyn_private",
            kind="alert",
            match={"env": "staging"},
            source_origin="registered",
            private_room_only=True,
        ),
        description="Dynamic private rule",
        prompt="Secret dynamic alert",
        created_at=1.0,
    )
    alice_rows = {row["trigger_id"]: row for row in h.req("GET", ROOM_A, ALICE).json()["triggers"]}
    bob_resp = h.req("GET", ROOM_A, BOB)
    assert bob_resp.status_code == 200
    assert "dyn_priv_api" in alice_rows
    assert "dyn_priv_api" not in bob_resp.text


def test_broken_plugin_check_delivery_access_returns_source_error_without_500(tmp_path: Path) -> None:
    """A raising check_delivery_access hook degrades to paused_reason='source_error' instead of 500."""
    h = _Harness(tmp_path)
    h.use_event_sources(FakeEventSource("exploding_src", delivery_error=RuntimeError("plugin crashed")))
    h.add_reactive(
        "tr_broken",
        ExternalEventSourceSpec(source_id="exploding_src", kind="alert", source_origin="registered"),
        description="Broken source trigger",
        prompt="Handle alert",
        created_at=1.0,
    )

    resp = h.req("GET", ROOM_A, ALICE)
    assert resp.status_code == 200
    rows = {row["trigger_id"]: row for row in resp.json()["triggers"]}
    assert rows["tr_broken"]["paused_reason"] == "source_error"


def test_hidden_private_trigger_returns_404_on_patch_and_delete_for_non_owner(tmp_path: Path) -> None:
    """Non-owners get 404 (matching absent IDs) on PATCH/DELETE of hidden private triggers, while visible triggers still return 403."""
    h = _Harness(tmp_path)
    registry, emitters = h.use_event_sources(_private_room_source("slack"))
    h.add_reactive(
        "tr_slack_priv",
        ExternalEventSourceSpec(
            source_id="slack",
            kind="message",
            binding={"team_id": "T1", "slack_user_id": "U1"},
            source_origin="registered",
            private_room_only=True,
        ),
        description="Private Slack watch",
        prompt="Summarize",
        created_at=1.0,
    )
    h.add_reactive(
        "tr_visible",
        ExternalEventSourceSpec(source_id="github_wh", kind="push"),
        description="GitHub push",
        prompt="Review",
        created_at=2.0,
    )

    assert h.req("PATCH", ROOM_A, BOB, "tr_slack_priv", {"enabled": False}).status_code == 404
    assert h.req("DELETE", ROOM_A, BOB, "tr_slack_priv").status_code == 404
    assert h.req("PATCH", ROOM_A, BOB, "tr_visible", {"enabled": False}).status_code == 403
    assert h.req("DELETE", ROOM_A, BOB, "tr_visible").status_code == 403

    # Still 404 after the registered source is unregistered.
    assert registry.unregister("slack", emitters["slack"].token) is True
    assert h.req("PATCH", ROOM_A, BOB, "tr_slack_priv", {"enabled": False}).status_code == 404
    assert h.req("DELETE", ROOM_A, BOB, "tr_slack_priv").status_code == 404

    # Owner can still toggle and delete their own private trigger.
    assert h.req("PATCH", ROOM_A, ALICE, "tr_slack_priv", {"enabled": False}).status_code == 200
    assert h.req("DELETE", ROOM_A, ALICE, "tr_slack_priv").status_code == 204
