"""Tests for script_check reactive triggers, script_runs check mode, and gateway check routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, patch

import httpx
import nio
import pytest
from agno.tools import Toolkit
from agno.tools.function import Function
from fastapi import FastAPI
from httpx import ASGITransport

import mindroom.tools  # noqa: F401
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.script_gateway import bind_script_tool_broker, serve_script_gateway_listener
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.approval.tool_approval import BackgroundScriptToolOrigin, ToolApprovalDecision
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.hooks import HookBindings, HookRegistry
from mindroom.message_target import MessageTarget
from mindroom.orchestration.script_checks import build_script_check_tick_runner
from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle, _LiveScriptRuntimeResolver
from mindroom.reactive_triggers import (
    GitHubPRCheckpoint,
    GitHubPRSourceSpec,
    ReactiveTriggerLimitError,
    ReactiveTriggerRecord,
    ReactiveTriggerStore,
    ReactiveTriggerStoreError,
    RoomMessageSourceSpec,
    ScriptCheckCheckpoint,
    ScriptCheckSourceSpec,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.dispatch import check_reactive_source_access
from mindroom.reactive_triggers.management import refresh_reactive_state_on_reenable, set_trigger_enabled_sync
from mindroom.reactive_triggers.models import TriageSpec, TriggerEvent
from mindroom.reactive_triggers.script_check import (
    MAX_SCRIPT_CHECK_RESULT_BYTES,
    MAX_SCRIPT_CHECK_SOURCE_BYTES,
    MAX_SCRIPT_CHECK_STATE_BYTES,
    SCRIPT_CHECK_EVENT_KIND,
    ScriptCheckPoller,
    ScriptCheckTickOutcome,
    agent_supports_script_check,
    check_script_check_source_access,
    normalize_script_check_event,
    validate_script_check_source,
    validate_script_check_state,
)
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.triage import TriageOutcome
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker, ScriptToolCallRequest
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import ScriptCallState, ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.policy import resolve_current_script_tool
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.shell.supervisor import _ShellSupervisorManager
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
)
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_OWNER = "@alice:example.org"
_OTHER_OWNER = "@bob:example.org"
_ROOM = "!room:example.org"
_SAMPLE_SOURCE = "def check():\n    return None\n"
_SAMPLE_DIGEST = hashlib.sha256(_SAMPLE_SOURCE.encode()).hexdigest()


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"},
    )


def _config(*, policy: dict[str, Any] | None = None) -> Config:
    raw: dict[str, Any] = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {
            "coder": {"display_name": "Coder", "include_default_tools": False, "tools": ["triggers", "script"]},
            "planner": {"display_name": "Planner", "include_default_tools": False, "tools": ["triggers", "workflow"]},
            "watcher": {"display_name": "Watcher", "include_default_tools": False, "tools": ["triggers"]},
        },
    }
    if policy is not None:
        raw["external_trigger_policy"] = policy
    return Config.model_validate(raw)


def _sc_rule(
    trigger_id: str = "sc_1",
    *,
    owner: str = _OWNER,
    agent: str = "coder",
    poll_s: int = 300,
    enabled: bool = True,
    expires_at: float | None = None,
) -> ReactiveTriggerRecord:
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=owner,
        source=ScriptCheckSourceSpec(
            agent=agent,
            room_id=_ROOM,
            source_digest=_SAMPLE_DIGEST,
            poll_interval_seconds=poll_s,
        ),
        target=TriggerTargetSpec(agent="watcher", room_id=_ROOM, prompt="Handle check result"),
        enabled=enabled,
        expires_at=expires_at,
        created_at=1000.0,
        updated_at=1000.0,
    )


def _sc_cp(
    *,
    rev: int = 1,
    state: dict[str, Any] | None = None,
    next_poll_at: float | None = 1000.0,
    errors: int = 0,
    code: str | None = None,
) -> ScriptCheckCheckpoint:
    return ScriptCheckCheckpoint(
        revision=rev,
        source_utf8=_SAMPLE_SOURCE,
        identity_json={"requester_id": _OWNER, "room_id": _ROOM},
        state={} if state is None else state,
        next_poll_at=next_poll_at,
        consecutive_errors=errors,
        last_error_code=code,
        updated_at=1000.0,
    )


def _require_cp(store: ReactiveTriggerStore, trigger_id: str) -> ScriptCheckCheckpoint:
    cp = store.get_script_check_checkpoint(trigger_id)
    assert cp is not None
    return cp


def test_script_check_policy_models_and_access(tmp_path: Path) -> None:
    """Policy bounds, model validators, agent capability, and source access checks enforce all invariants."""
    pol = _config().external_trigger_policy
    assert (pol.script_check_enabled, pol.min_script_check_poll_interval_seconds, pol.max_script_checks_per_owner) == (
        True,
        60,
        10,
    )
    assert (pol.script_check_timeout_seconds, pol.script_check_allowed_tools) == (30, None)
    assert _config(
        policy={"script_check_allowed_tools": [" gmail ", "cal"]},
    ).external_trigger_policy.script_check_allowed_tools == ["gmail", "cal"]

    for bad, match in (
        ({"min_script_check_poll_interval_seconds": 10}, "min_script_check_poll_interval_seconds"),
        ({"max_script_checks_per_owner": 0}, "max_script_checks_per_owner"),
        ({"script_check_timeout_seconds": 3}, "script_check_timeout_seconds"),
        ({"script_check_allowed_tools": ["  "]}, "script_check_allowed_tools"),
    ):
        with pytest.raises(ValueError, match=match):
            _config(policy=bad)

    with pytest.raises(ValueError, match="source_digest"):
        ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest="ABCD" * 16)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest=_SAMPLE_DIGEST, poll_interval_seconds=True)  # type: ignore[arg-type]

    paths, cfg = _runtime_paths(tmp_path), _config()
    assert (agent_supports_script_check(cfg, "coder"), agent_supports_script_check(cfg, "planner")) == (True, True)
    assert (agent_supports_script_check(cfg, "watcher"), agent_supports_script_check(cfg, "missing")) == (False, False)
    src = ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest=_SAMPLE_DIGEST)
    assert (
        check_script_check_source_access(owner_user_id=_OWNER, source_spec=src, config=cfg, runtime_paths=paths) is None
    )
    assert (
        check_script_check_source_access(
            owner_user_id=_OWNER,
            source_spec=src,
            config=_config(policy={"script_check_enabled": False}),
            runtime_paths=paths,
        )
        == "script_check_disabled"
    )
    assert (
        check_script_check_source_access(
            owner_user_id=_OWNER,
            source_spec=ScriptCheckSourceSpec(agent="watcher", room_id=_ROOM, source_digest=_SAMPLE_DIGEST),
            config=cfg,
            runtime_paths=paths,
        )
        == "agent_script_check_unavailable"
    )


def test_validate_source_state_and_normalize_event() -> None:
    """Source AST validation, finite JSON state checks, and event normalization enforce size, type, and dedupe contracts."""
    assert validate_script_check_source(_SAMPLE_SOURCE.encode()) == (_SAMPLE_SOURCE, _SAMPLE_DIGEST)
    for bad_src, match in (
        (b"", "empty"),
        (b"\xff\xfe", "UTF-8"),
        (b"def check(:\n", "Invalid Python syntax"),
        (b"async def check():\n    return None\n", "synchronous"),
        (b"class C:\n    def check():\n        return None\n", "top-level synchronous"),
        (b"def check(state):\n    return None\n", "no required or positional"),
        (b"def check(state=None):\n    return None\n", "no required or positional"),
        (b"def check(*, req):\n    return None\n", "no required or positional"),
        (b"#" * (MAX_SCRIPT_CHECK_SOURCE_BYTES + 1), "maximum size"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_script_check_source(bad_src)

    assert validate_script_check_state({"seen": [1, "a"]}) == {"seen": [1, "a"]}
    for bad_state, match in (
        (["x"], "must be a dict"),
        ({1: "a"}, "string keys"),
        ({"x": math.nan}, "strict finite JSON"),
        ({"big": "a" * (MAX_SCRIPT_CHECK_STATE_BYTES + 1)}, "maximum size"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_script_check_state(bad_state)

    assert normalize_script_check_event("t1", "r1", None, now=2000.0) is None
    plain = normalize_script_check_event("t1", "r1", {"items": [1]}, now=2000.0)
    assert plain is not None
    assert (plain.kind, plain.event_id, plain.data) == (SCRIPT_CHECK_EVENT_KIND, "sc:tick:r1", {"items": [1]})

    env = normalize_script_check_event(
        "t1",
        "r2",
        {"title": "Alert", "summary": "S", "text": "T", "actor": "alice", "dedupe_key": "k1", "data": {"id": 1}},
        now=2000.0,
    )
    assert env is not None
    assert env.event_id == f"sc:key:{hashlib.sha256(b't1:k1').hexdigest()[:32]}"
    assert (env.title, env.summary, env.text, env.actor, env.data) == ("Alert", "S", "T", "alice", {"id": 1})

    for bad_res, match in (
        (True, "must be a dict"),
        (1, "must be a dict"),
        ("s", "must be a dict"),
        (["a"], "must be a dict"),
        ({"data": {"a": 1}, "extra": 2}, "cannot combine 'data'"),
        ({"title": "x" * 501}, "field 'title' exceeds"),
        ({"dedupe_key": "  "}, "must not be empty"),
        ({"x": math.inf}, "strict finite JSON"),
        ({"blob": "a" * (MAX_SCRIPT_CHECK_RESULT_BYTES + 1)}, "maximum size"),
    ):
        with pytest.raises(ValueError, match=match):
            normalize_script_check_event("t1", "r3", bad_res, now=2000.0)


@pytest.mark.parametrize("legacy_version", [1, 2])
def test_sqlite_migration_v1_and_v2_to_v3(tmp_path: Path, legacy_version: int) -> None:
    """Both v1 and v2 reactive_triggers.db upgrade cleanly to v3 without losing existing rows."""
    paths = _runtime_paths(tmp_path)
    store = ReactiveTriggerStore(paths)
    store.create_trigger(
        ReactiveTriggerRecord(
            trigger_id="legacy_room",
            owner_user_id=_OWNER,
            source=RoomMessageSourceSpec(room_id=_ROOM),
            target=TriggerTargetSpec(agent="watcher", room_id=_ROOM, prompt="Ping"),
            created_at=1000.0,
            updated_at=1000.0,
        ),
    )
    if legacy_version == 2:
        store.create_trigger(
            ReactiveTriggerRecord(
                trigger_id="legacy_pr",
                owner_user_id=_OWNER,
                source=GitHubPRSourceSpec(repo="org/repo", number=7),
                target=TriggerTargetSpec(agent="watcher", room_id=_ROOM, prompt="PR"),
                created_at=1000.0,
                updated_at=1000.0,
            ),
            initial_github_pr_checkpoint=GitHubPRCheckpoint(seeded=True, updated_at=1000.0),
        )
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("DROP TABLE script_check_checkpoints")
        if legacy_version == 1:
            conn.execute("DROP TABLE github_pr_checkpoints")
        conn.execute("UPDATE schema_version SET version = ?", (legacy_version,))
        conn.commit()

    upgraded = ReactiveTriggerStore(paths)
    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"github_pr_checkpoints", "script_check_checkpoints"} <= tables
    assert upgraded.get_trigger("legacy_room") is not None
    if legacy_version == 2:
        assert upgraded.get_github_pr_checkpoint("legacy_pr") is not None

    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
        conn.commit()
    with pytest.raises(ReactiveTriggerStoreError, match="Unsupported reactive triggers schema version: 99"):
        ReactiveTriggerStore(paths)


def test_store_script_check_quotas_atomic_revision_and_reenable(tmp_path: Path) -> None:
    """Store enforces active quota, atomic revision/uid/enabled guards, and management re-enable refresh."""
    paths = _runtime_paths(tmp_path)
    store = ReactiveTriggerStore(paths)
    r1, cp1 = _sc_rule("sc_1"), _sc_cp(state={"seen": ["a"]})
    store.create_trigger(r1, initial_script_check_checkpoint=cp1, max_script_check_per_owner=1)
    assert [r.trigger_id for r in store.list_triggers(source_type="script_check", source_id="coder")] == ["sc_1"]
    assert store.list_triggers(source_type="script_check", source_room_id=_ROOM) == []

    with pytest.raises(ReactiveTriggerLimitError, match="script_check trigger limit"):
        store.create_trigger(_sc_rule("sc_2"), max_script_check_per_owner=1)
    exp = _sc_rule("sc_exp", expires_at=900.0)
    store.create_trigger(exp, initial_script_check_checkpoint=_sc_cp(), max_script_check_per_owner=1)
    with pytest.raises(ReactiveTriggerLimitError, match="script_check trigger limit"):
        store.update_trigger(
            exp.model_copy(update={"expires_at": 2000.0, "updated_at": 1050.0}),
            max_script_check_per_owner=1,
        )

    ev = normalize_script_check_event("sc_1", "r1", {"items": ["b"]}, now=1100.0)
    nxt = cp1.model_copy(update={"state": {"seen": ["a", "b"]}, "next_poll_at": 1400.0, "updated_at": 1100.0})
    assert (
        store.update_script_check_checkpoint_and_enqueue(
            "sc_1",
            nxt,
            event=ev,
            expected_rule_uid=r1.rule_uid,
            expected_revision=1,
        )
        is True
    )
    saved = _require_cp(store, "sc_1")
    assert (saved.revision, saved.state) == (2, {"seen": ["a", "b"]})
    assert len(store.list_due_firings(now=1200.0)) == 1

    # Stale revision rejected without mutating state or enqueueing
    ev2 = normalize_script_check_event("sc_1", "r2", {"items": ["c"]}, now=1150.0)
    assert (
        store.update_script_check_checkpoint_and_enqueue(
            "sc_1",
            nxt.model_copy(update={"state": {"stale": 1}}),
            event=ev2,
            expected_rule_uid=r1.rule_uid,
            expected_revision=1,
        )
        is False
    )
    assert store.get_script_check_checkpoint("sc_1").state == {"seen": ["a", "b"]}  # type: ignore[union-attr]
    assert len(store.list_due_firings(now=1200.0)) == 1

    # Disabled trigger rejects commit; refresh_reactive_state_on_reenable preserves state and bumps revision
    assert store.set_enabled("sc_1", enabled=False) is not None
    assert (
        store.update_script_check_checkpoint_and_enqueue(
            "sc_1",
            nxt,
            expected_rule_uid=r1.rule_uid,
            expected_revision=2,
        )
        is False
    )
    store.upsert_script_check_checkpoint(
        "sc_1",
        saved.model_copy(update={"consecutive_errors": 3, "last_error_code": "timeout"}),
    )
    refresh_reactive_state_on_reenable(store, store.get_trigger("sc_1"), now=1300.0, refreshed_tag_present=None)  # type: ignore[arg-type]
    refreshed = store.get_script_check_checkpoint("sc_1")
    assert refreshed is not None
    assert (
        refreshed.revision,
        refreshed.state,
        refreshed.consecutive_errors,
        refreshed.last_error_code,
        refreshed.next_poll_at,
    ) == (3, {"seen": ["a", "b"]}, 0, None, 1300.0)

    store.create_trigger(_sc_rule("sc_active"), initial_script_check_checkpoint=_sc_cp())
    with pytest.raises(ReactiveTriggerLimitError, match="script_check trigger limit"):
        set_trigger_enabled_sync(
            paths,
            _config(policy={"max_script_checks_per_owner": 1}),
            "sc_1",
            enabled=True,
            actor_user_id=_OWNER,
            authorize_reactive=lambda _r: None,
        )


@pytest.mark.asyncio
async def test_script_check_poller_outcomes_concurrency_and_inflight(tmp_path: Path) -> None:
    """Poller enforces per-owner concurrency 1, in-flight guard, quiet state commit, error backoff, defer, and unauthorized."""
    paths = _runtime_paths(tmp_path)
    store = ReactiveTriggerStore(paths)
    cfg = _config(policy={"min_script_check_poll_interval_seconds": 120})
    enqueued_signals = 0
    for tid, owner in (
        ("t_ok", _OWNER),
        ("t_err", _OWNER),
        ("t_quiet", _OWNER),
        ("t_def", _OTHER_OWNER),
        ("t_unauth", _OTHER_OWNER),
        ("t_bad", _OTHER_OWNER),
    ):
        store.create_trigger(
            _sc_rule(tid, owner=owner, poll_s=60),
            initial_script_check_checkpoint=_sc_cp(state={"k": tid}),
        )

    active_owner: dict[str, int] = {}
    max_owner: dict[str, int] = {}
    started, release = asyncio.Event(), asyncio.Event()
    seen_calls: list[str] = []

    async def _runner(
        rule: ReactiveTriggerRecord,
        _cp: ScriptCheckCheckpoint,
        _c: Config,
        _now: float,
    ) -> ScriptCheckTickOutcome:
        seen_calls.append(rule.trigger_id)
        active_owner[rule.owner_user_id] = active_owner.get(rule.owner_user_id, 0) + 1
        max_owner[rule.owner_user_id] = max(max_owner.get(rule.owner_user_id, 0), active_owner[rule.owner_user_id])
        try:
            if rule.trigger_id == "t_ok":
                started.set()
                await release.wait()
                return ScriptCheckTickOutcome(status="ok", run_id="r1", result={"n": 1}, state={"k": "new"})
            await asyncio.sleep(0.01)
            outcomes = {
                "t_quiet": ScriptCheckTickOutcome(status="ok", run_id="rq", result=None, state={"k": "quiet"}),
                "t_err": ScriptCheckTickOutcome(status="error", error_code="boom"),
                "t_bad": ScriptCheckTickOutcome(status="ok", run_id="rb", result=["invalid"], state={"k": "dropped"}),
                "t_def": ScriptCheckTickOutcome(status="deferred"),
            }
            return outcomes.get(rule.trigger_id, ScriptCheckTickOutcome(status="unauthorized"))
        finally:
            active_owner[rule.owner_user_id] -= 1

    def _on_enq() -> None:
        nonlocal enqueued_signals
        enqueued_signals += 1

    poller = ScriptCheckPoller(
        store=store,
        config_provider=lambda: cfg,
        runtime_paths=paths,
        tick_runner=_runner,
        on_enqueued=_on_enq,
    )
    task = asyncio.create_task(poller.poll_due_once(now=1000.0))
    await started.wait()
    assert await poller.poll_due_once(now=1000.0) == 0  # All 6 triggers in-flight -> skipped
    release.set()
    assert await task == 1
    assert enqueued_signals == 1
    assert max_owner == {_OWNER: 1, _OTHER_OWNER: 1}
    assert seen_calls.count("t_ok") == 1

    cp_ok = _require_cp(store, "t_ok")
    assert (cp_ok.state, cp_ok.revision) == ({"k": "new"}, 2)
    assert cp_ok.next_poll_at is not None
    assert 1120.0 <= cp_ok.next_poll_at <= 1132.0
    assert (_require_cp(store, "t_quiet").state, _require_cp(store, "t_quiet").revision) == ({"k": "quiet"}, 2)
    cp_err = _require_cp(store, "t_err")
    assert (cp_err.state, cp_err.consecutive_errors, cp_err.last_error_code, cp_err.next_poll_at) == (
        {"k": "t_err"},
        1,
        "boom",
        1240.0,
    )
    cp_bad = _require_cp(store, "t_bad")
    assert (cp_bad.state, cp_bad.consecutive_errors, cp_bad.last_error_code) == ({"k": "t_bad"}, 1, "invalid_result")
    cp_def = _require_cp(store, "t_def")
    assert (cp_def.state, cp_def.consecutive_errors, cp_def.next_poll_at) == ({"k": "t_def"}, 0, 1030.0)
    cp_un = _require_cp(store, "t_unauth")
    assert (cp_un.state, cp_un.consecutive_errors, cp_un.next_poll_at) == ({"k": "t_unauth"}, 0, 1120.0)


class _CheckProbeToolkit(Toolkit):
    """Test toolkit with one read function and one ToolGate.CONFIRM function."""

    def __init__(self) -> None:
        super().__init__(name="check_probe")
        self.register(Function(name="read_item", entrypoint=self.read_item))
        self.register(Function(name="confirm_action", entrypoint=self.confirm_action, requires_confirmation=True))

    def read_item(self) -> str:
        return "ok-value"

    def confirm_action(self) -> str:
        return "confirmed"


def _script_context(
    tmp_path: Path,
    *,
    tools: list[str],
    policy: dict[str, Any] | None = None,
    tool_approval: dict[str, Any] | None = None,
    allow_local: bool = False,
) -> ToolRuntimeContext:
    env = {"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"}
    if allow_local:
        env["MINDROOM_SANDBOX_EXECUTION_MODE"] = "local"
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env,
    )
    raw: dict[str, Any] = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "defaults": {"tools": [], "allow_shell_without_worker": allow_local},
        "agents": {"coder": {"display_name": "Coder", "include_default_tools": False, "tools": tools}},
    }
    if policy is not None:
        raw["external_trigger_policy"] = policy
    if tool_approval is not None:
        raw["tool_approval"] = tool_approval
    cfg = bind_runtime_paths(Config.model_validate(raw), paths)
    return make_test_tool_runtime_context(
        agent_name="coder",
        target=MessageTarget.resolve(
            room_id=_ROOM,
            thread_id="$thread:example.org",
            reply_to_event_id="$e:example.org",
        ),
        requester_id=_OWNER,
        client=SimpleNamespace(),
        config=cfg,
        runtime_paths=runtime_paths_for(cfg),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hooks=HookBindings(registry=HookRegistry.empty()),
    )


class _SimpleResolver:
    def __init__(self, context: ToolRuntimeContext) -> None:
        self.context = context
        self.approval_requests = 0
        self.settled_runs: list[str] = []

    def is_authorized(self, _run: ScriptRunRecord, *, config: Config | None = None) -> bool | None:
        del config
        return True

    def resolve(self, _run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        return replace(self.context, correlation_id=correlation_id)

    def resolve_worker_authority(
        self,
        _run: ScriptRunRecord,
        *,
        context: ToolRuntimeContext,
    ) -> ScriptRuntimeWorkerAuthority:
        return ScriptRuntimeWorkerAuthority(
            worker_id=None,
            local_unsafe=True,
            worker_target=context.resolve_worker_target(),
        )

    async def request_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        context: ToolRuntimeContext,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> ToolApprovalDecision:
        del origin, context, grant, arguments, timeout_seconds
        self.approval_requests += 1
        return ToolApprovalDecision(approved=True)

    async def settle_approval(self, origin: BackgroundScriptToolOrigin, *, reason: str) -> None:
        del origin, reason

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del reason
        self.settled_runs.append(run_id)


def _create_script_run(
    store: ScriptRunStore,
    ctx: ToolRuntimeContext,
    run_id: str,
    *,
    workflow_run_id: str | None = None,
    script_check_rule_uid: str | None = None,
) -> tuple[ScriptRunRecord, str]:
    token, token_hash = mint_script_capability()
    run = ScriptRunRecord(
        run_id=run_id,
        agent_name=ctx.agent_name,
        owner_user_id=ctx.requester_id,
        room_id=ctx.room_id,
        thread_root_event_id=ctx.resolved_thread_id,
        execution_identity=serialize_tool_execution_identity(ctx.execution_identity),
        source_digest=_SAMPLE_DIGEST,
        grants=(),
        token_hash=token_hash,
        workflow_run_id=workflow_run_id,
        script_check_rule_uid=script_check_rule_uid,
        local_unsafe=True,
    )
    return store.create_run(run), token


def test_script_runs_store_migrates_script_check_rule_uid(tmp_path: Path) -> None:
    """ScriptRunStore adds script_check_rule_uid to legacy DBs and enforces model discriminator invariants."""
    paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(paths)
    ctx = _script_context(tmp_path, tools=["calculator"])
    plain, _ = _create_script_run(store, ctx, f"script-{'1' * 32}")

    # Simulate legacy table without script_check_rule_uid
    with closing(sqlite3.connect(store.database_path)) as conn:
        conn.execute("ALTER TABLE script_runs DROP COLUMN script_check_rule_uid")
        conn.commit()

    migrated = ScriptRunStore(paths)
    loaded_plain = migrated.get_run(plain.run_id)
    assert loaded_plain.script_check_rule_uid is None
    assert (loaded_plain.is_plain_script, loaded_plain.is_script_check) == (True, False)

    sc_run, _ = _create_script_run(migrated, ctx, f"script-{'2' * 32}", script_check_rule_uid="rule-uid-1")
    loaded_sc = migrated.get_run(sc_run.run_id)
    assert (loaded_sc.script_check_rule_uid, loaded_sc.is_script_check, loaded_sc.is_plain_script, loaded_sc.entry) == (
        "rule-uid-1",
        True,
        False,
        "script",
    )
    assert loaded_sc.launch_allows(ScriptToolGrant("calculator", "add")) is True

    with pytest.raises(ValueError, match="both workflow_run_id and script_check_rule_uid"):
        ScriptRunRecord(
            run_id=f"script-{'3' * 32}",
            agent_name="coder",
            owner_user_id=_OWNER,
            room_id=_ROOM,
            source_digest=_SAMPLE_DIGEST,
            grants=(),
            token_hash="0" * 64,
            workflow_run_id="wf-1",
            script_check_rule_uid="rule-1",
        )


def test_script_check_policy_restricts_toolkits_and_honors_allowlist(tmp_path: Path) -> None:
    """script_check blocks restricted toolkits and enforces optional script_check_allowed_tools allowlist."""
    ctx_open = _script_context(tmp_path / "open", tools=["calculator", "website", "triggers", "scheduler", "terminal"])
    assert resolve_current_script_tool(ctx_open, ScriptToolGrant("calculator", "add"), script_check=True) is not None
    assert resolve_current_script_tool(ctx_open, ScriptToolGrant("website", "read_url"), script_check=True) is not None
    for blocked_tk, fn in (("triggers", "triggers"), ("scheduler", "schedule_task"), ("terminal", "run_command")):
        assert resolve_current_script_tool(ctx_open, ScriptToolGrant(blocked_tk, fn), script_check=True) is None

    # Allowlist narrows to allowed toolkits while restricted toolkits remain blocked even if listed.
    ctx_limited = _script_context(
        tmp_path / "limited",
        tools=["calculator", "website", "triggers"],
        policy={"script_check_allowed_tools": ["calculator", "triggers"]},
    )
    assert resolve_current_script_tool(ctx_limited, ScriptToolGrant("calculator", "add"), script_check=True) is not None
    assert resolve_current_script_tool(ctx_limited, ScriptToolGrant("website", "read_url"), script_check=True) is None
    assert resolve_current_script_tool(ctx_limited, ScriptToolGrant("triggers", "triggers"), script_check=True) is None


@pytest.mark.asyncio
async def test_script_check_broker_fails_closed_on_confirm_and_policy_approval(tmp_path: Path) -> None:
    """script_check ticks fail closed on ToolGate.CONFIRM and policy approval without posting cards or settling approvals."""
    with registered_toolkit("check_probe", _CheckProbeToolkit):
        ctx_default = _script_context(tmp_path / "d", tools=["check_probe"])
        store = ScriptRunStore(ctx_default.runtime_paths)
        resolver = _SimpleResolver(ctx_default)
        broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
        broker.open_call_admission()

        sc_run, token = _create_script_run(store, ctx_default, f"script-{'a' * 32}", script_check_rule_uid="rule-1")
        auth = f"Bearer {token}"
        with broker.check_tick(
            sc_run.run_id,
            source=_SAMPLE_SOURCE,
            source_digest=_SAMPLE_DIGEST,
            initial_state={},
        ) as slot:
            # Auto-approved read tool succeeds cleanly
            ok_req = ScriptToolCallRequest(sc_run.run_id, "c1", ScriptToolGrant("check_probe", "read_item"), {})
            await broker.accept_authenticated(ok_req, auth)
            ok_rec = await broker._tasks[(sc_run.run_id, "c1")]
            assert (ok_rec.state, ok_rec.result, slot.approval_denied) == (ScriptCallState.COMPLETED, "ok-value", False)

            # ToolGate.CONFIRM fails closed immediately, records approval_denied on slot, 0 approval requests
            cfm_req = ScriptToolCallRequest(sc_run.run_id, "c2", ScriptToolGrant("check_probe", "confirm_action"), {})
            await broker.accept_authenticated(cfm_req, auth)
            cfm_rec = await broker._tasks[(sc_run.run_id, "c2")]
            assert cfm_rec.state is ScriptCallState.FAILED
            assert isinstance(cfm_rec.error, dict)
            assert cfm_rec.error["kind"] == "approval_denied"
            assert slot.approval_denied is True
            assert resolver.approval_requests == 0

        # cancel_run skips settle_run_approvals for script_check runs, but still calls it for plain runs
        await broker.cancel_run(sc_run.run_id)
        assert resolver.settled_runs == []
        plain_run, _ = _create_script_run(store, ctx_default, f"script-{'b' * 32}")
        await broker.cancel_run(plain_run.run_id)
        assert resolver.settled_runs == [plain_run.run_id]

        # Policy require_approval also fails closed without requesting interactive approval
        ctx_strict = _script_context(
            tmp_path / "s",
            tools=["check_probe"],
            tool_approval={"default": "require_approval"},
        )
        store_s = ScriptRunStore(ctx_strict.runtime_paths)
        resolver_s = _SimpleResolver(ctx_strict)
        broker_s = ScriptToolBroker(store=store_s, runtime_resolver=resolver_s)
        broker_s.open_call_admission()
        sc_strict, token_s = _create_script_run(
            store_s,
            ctx_strict,
            f"script-{'c' * 32}",
            script_check_rule_uid="rule-2",
        )
        with broker_s.check_tick(
            sc_strict.run_id,
            source=_SAMPLE_SOURCE,
            source_digest=_SAMPLE_DIGEST,
            initial_state={},
        ) as slot_s:
            req = ScriptToolCallRequest(sc_strict.run_id, "c3", ScriptToolGrant("check_probe", "read_item"), {})
            await broker_s.accept_authenticated(req, f"Bearer {token_s}")
            rec = await broker_s._tasks[(sc_strict.run_id, "c3")]
            assert rec.state is ScriptCallState.FAILED
            assert isinstance(rec.error, dict)
            assert rec.error["kind"] == "approval_denied"
            assert slot_s.approval_denied is True
            assert resolver_s.approval_requests == 0


@pytest.mark.asyncio
async def test_script_gateway_check_routes_auth_conflict_and_bounds(tmp_path: Path) -> None:
    """Gateway /check-input and /check-result enforce auth, slot presence, idempotency, 409 conflict, 422, 413, and 503."""
    ctx = _script_context(tmp_path, tools=["calculator"])
    store = ScriptRunStore(ctx.runtime_paths)
    broker = ScriptToolBroker(store=store, runtime_resolver=_SimpleResolver(ctx))
    broker.open_call_admission()

    sc_run, sc_tok = _create_script_run(store, ctx, f"script-{'d' * 32}", script_check_rule_uid="rule-gw")
    plain_run, plain_tok = _create_script_run(store, ctx, f"script-{'e' * 32}")
    wf_run, wf_tok = _create_script_run(store, ctx, f"script-{'f' * 32}", workflow_run_id="wf-1")

    app = FastAPI()
    app.include_router(script_gateway_router)
    bind_script_tool_broker(app, broker)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing active slot -> 404
        assert (
            await client.get(
                f"/api/script-gateway/runs/{sc_run.run_id}/check-input",
                headers={"Authorization": f"Bearer {sc_tok}"},
            )
        ).status_code == 404

        with broker.check_tick(
            sc_run.run_id,
            source=_SAMPLE_SOURCE,
            source_digest=_SAMPLE_DIGEST,
            initial_state={"k": 1},
        ) as slot:
            # Plain script and workflow tokens rejected -> 404
            for rid, tok in ((plain_run.run_id, plain_tok), (wf_run.run_id, wf_tok)):
                assert (
                    await client.get(
                        f"/api/script-gateway/runs/{rid}/check-input",
                        headers={"Authorization": f"Bearer {tok}"},
                    )
                ).status_code == 404
                assert (
                    await client.post(
                        f"/api/script-gateway/runs/{rid}/check-result",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={},
                    )
                ).status_code == 404

            # Valid check-input -> 200
            inp = await client.get(
                f"/api/script-gateway/runs/{sc_run.run_id}/check-input",
                headers={"Authorization": f"Bearer {sc_tok}"},
            )
            assert inp.status_code == 200
            assert inp.json() == {"source": _SAMPLE_SOURCE, "source_digest": _SAMPLE_DIGEST, "state": {"k": 1}}

            # Invalid extra field & NaN -> 422; oversized > 128 KiB -> 413
            hdr = {"Authorization": f"Bearer {sc_tok}"}
            url = f"/api/script-gateway/runs/{sc_run.run_id}/check-result"
            assert (await client.post(url, headers=hdr, json={"extra": 1})).status_code == 422
            assert (
                await client.post(url, headers={**hdr, "Content-Type": "application/json"}, content='{"result": NaN}')
            ).status_code == 422
            assert (
                await client.post(
                    url,
                    headers={**hdr, "Content-Type": "application/json"},
                    content=json.dumps({"result": "x" * (129 * 1024)}),
                )
            ).status_code == 413

            # Valid submit -> 200, idempotent repeat -> 200, conflicting second submit -> 409
            body = {"result": {"hits": 2}, "state": {"k": 2}, "state_changed": True, "error": None}
            assert (await client.post(url, headers=hdr, json=body)).status_code == 200
            assert slot.done.is_set()
            assert (slot.result, slot.state, slot.state_changed, slot.error) == ({"hits": 2}, {"k": 2}, True, None)
            assert (await client.post(url, headers=hdr, json=body)).status_code == 200
            assert (await client.post(url, headers=hdr, json={**body, "result": {"hits": 9}})).status_code == 409

        # Closed admission -> 503; unbound broker -> 503
        broker.close_call_admission()
        with broker.check_tick(sc_run.run_id, source=_SAMPLE_SOURCE, source_digest=_SAMPLE_DIGEST, initial_state={}):
            assert (
                await client.get(
                    f"/api/script-gateway/runs/{sc_run.run_id}/check-input",
                    headers={"Authorization": f"Bearer {sc_tok}"},
                )
            ).status_code == 503
        bind_script_tool_broker(app, None)
        assert (
            await client.get(
                f"/api/script-gateway/runs/{sc_run.run_id}/check-input",
                headers={"Authorization": f"Bearer {sc_tok}"},
            )
        ).status_code == 503


@pytest.mark.asyncio
async def test_script_run_manager_hides_script_check_runs_and_skips_plain_limits(tmp_path: Path) -> None:
    """ScriptRunManager accepts run_id/script_check_rule_uid/max_runtime_seconds and hides ticks from plain script APIs."""
    ctx = _script_context(tmp_path, tools=["calculator"], allow_local=True)
    store = ScriptRunStore(ctx.runtime_paths)
    broker = ScriptToolBroker(store=store, runtime_resolver=_SimpleResolver(ctx))
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
    )

    explicit_id = f"script-{'9' * 32}"
    with (
        patch("mindroom.script_runs.manager.background_script_supervision_supported", return_value=True),
        patch("mindroom.script_runs.manager.ensure_shell_supervisor", return_value=tmp_path / "sup.sock"),
        patch("mindroom.script_runs.manager.launch_supervised_script", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(ScriptRunManagerError, match="both workflow_run_id and script_check_rule_uid"):
            await manager.run(ctx, source=_SAMPLE_SOURCE, workflow_run_id="wf-1", script_check_rule_uid="r-1")
        with pytest.raises(ScriptRunManagerError, match="Script run ID"):
            await manager.run(ctx, source=_SAMPLE_SOURCE, run_id="bad-id", script_check_rule_uid="r-1")

        sc_run = await manager.run(
            ctx,
            source=_SAMPLE_SOURCE,
            run_id=explicit_id,
            script_check_rule_uid="rule-uid-9",
            max_runtime_seconds=25,
        )
        assert (
            sc_run.run_id,
            sc_run.script_check_rule_uid,
            sc_run.grants,
            sc_run.recovery_signature,
            sc_run.max_runtime_seconds,
        ) == (
            explicit_id,
            "rule-uid-9",
            (),
            None,
            25,
        )
        assert await manager.list(ctx) == []
        with pytest.raises(ScriptRunManagerError, match="not found"):
            await manager.status(ctx, run_id=explicit_id)
        with pytest.raises(ScriptRunManagerError, match="not found"):
            await manager.cancel(ctx, run_id=explicit_id)

        # Plain script with max_concurrent_runs=1 still launches alongside active script_check run
        plain = await manager.run(ctx, source=_SAMPLE_SOURCE, limits=ScriptRunLimits(max_concurrent_runs=1))
        assert [r.run_id for r in await manager.list(ctx)] == [plain.run_id]

        # workflow_placement refuses local execution when allow_shell_without_worker is disabled without a worker
        ctx_no_local = _script_context(tmp_path / "nolocal", tools=["calculator"], allow_local=False)
        with pytest.raises(ScriptRunManagerError, match=r"worker|sandbox|local|shell"):
            await manager.run(ctx_no_local, source=_SAMPLE_SOURCE, script_check_rule_uid="rule-denied")


class _LiveFeedToolkit(Toolkit):
    items: ClassVar[list[str]] = ["a"]

    def __init__(self) -> None:
        super().__init__(name="live_feed")
        self.register(Function(name="fetch_items", entrypoint=self.fetch_items))
        self.register(Function(name="confirm_op", entrypoint=self.confirm_op, requires_confirmation=True))

    def fetch_items(self) -> str:
        return json.dumps({"items": list(self.items)})

    def confirm_op(self) -> str:
        return "confirmed"


class _FakeBot:
    def __init__(self, ctx: ToolRuntimeContext, *, send_log: list[dict[str, Any]]) -> None:
        self.agent_name = ctx.agent_name
        self.running = True
        self.config = ctx.config
        self.send_log = send_log
        self.conversation_reader = ctx.conversation_reader
        self.orchestrator: Any = None
        client = make_matrix_client_mock(user_id="@mindroom_coder:example.org")
        client.rooms = {
            _ROOM: SimpleNamespace(users={_OWNER: object(), "@mindroom_coder:example.org": object()}, encrypted=False),
        }
        client.room_send = AsyncMock(side_effect=self._room_send)
        client.room_get_state = AsyncMock(
            return_value=nio.RoomGetStateResponse(
                events=[{"type": "m.room.member", "state_key": _OWNER, "content": {"membership": "join"}}],
                room_id=_ROOM,
            ),
        )
        self.client = client
        self.tool_runtime_support = SimpleNamespace(
            build_context=lambda target, **kw: replace(
                ctx,
                target=target,
                requester_id=kw.get("user_id") or ctx.requester_id,
                agent_name=kw.get("agent_name") or ctx.agent_name,
                correlation_id=kw.get("correlation_id"),
                client=self.client,
            ),
        )

    async def _room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> nio.RoomSendResponse:
        self.send_log.append({"room_id": room_id, "message_type": message_type, "content": content})
        return nio.RoomSendResponse(event_id=f"$sent_{len(self.send_log)}", room_id=room_id)


@asynccontextmanager
async def _live_script_check_harness(
    tmp_path: Path,
    *,
    tools: list[str],
    policy: dict[str, Any] | None = None,
) -> AsyncIterator[SimpleNamespace]:
    ctx = _script_context(tmp_path, tools=tools, policy=policy, allow_local=True)
    send_log: list[dict[str, Any]] = []
    bot = _FakeBot(ctx, send_log=send_log)
    memberships = AgentReplyMembershipIndex()
    script_store = ScriptRunStore(ctx.runtime_paths)
    resolver = _SimpleResolver(ctx)
    broker = ScriptToolBroker(store=script_store, runtime_resolver=resolver)
    broker.open_call_admission()
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    listener_paths = resolve_primary_runtime_paths(
        config_path=ctx.runtime_paths.config_path,
        storage_path=ctx.runtime_paths.storage_root,
        process_env={**ctx.runtime_paths.process_env, "MINDROOM_SCRIPT_GATEWAY_PORT": str(port)},
    )
    manager = ScriptRunManager(
        store=script_store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url=f"http://127.0.0.1:{port}/api/script-gateway",
    )
    trig_store = ReactiveTriggerStore(ctx.runtime_paths)
    runner = build_script_check_tick_runner(
        runtime_paths=ctx.runtime_paths,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        manager_provider=lambda: manager,
        broker=broker,
    )
    service = ReactiveTriggerService(
        runtime_paths=ctx.runtime_paths,
        config_provider=lambda: ctx.config,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        store=trig_store,
        script_check_runner=runner,
    )
    supervisor = _ShellSupervisorManager()
    socket_path = supervisor.ensure()
    try:
        with (
            patch("mindroom.script_runs.manager.ensure_shell_supervisor", return_value=socket_path),
            patch("mindroom.script_runs.manager.background_script_supervision_supported", return_value=True),
        ):
            async with serve_script_gateway_listener(
                listener_paths,
                host="127.0.0.1",
                broker=broker,
                log_level="WARNING",
                agent_cli_registry=None,
                workflow_engine=None,
            ):
                yield SimpleNamespace(
                    ctx=ctx,
                    bot=bot,
                    send_log=send_log,
                    script_store=script_store,
                    trig_store=trig_store,
                    resolver=resolver,
                    broker=broker,
                    manager=manager,
                    service=service,
                )
    finally:
        await service.stop()
        supervisor.shutdown()


def _create_live_rule(
    harness: SimpleNamespace,
    trigger_id: str,
    source: str,
    *,
    state: dict[str, Any] | None = None,
    triage: TriageSpec | None = None,
    reopen_on_fire: bool = False,
) -> ReactiveTriggerRecord:
    digest = hashlib.sha256(source.encode()).hexdigest()
    rule = ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=_OWNER,
        source=ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest=digest, poll_interval_seconds=60),
        triage=triage,
        target=TriggerTargetSpec(
            agent="coder",
            room_id=_ROOM,
            thread_id="$thread:example.org",
            reopen_on_fire=reopen_on_fire,
            prompt="Check alert",
        ),
        created_at=1000.0,
        updated_at=1000.0,
    )
    cp = ScriptCheckCheckpoint(
        revision=1,
        source_utf8=source,
        identity_json=dict(serialize_tool_execution_identity(harness.ctx.execution_identity)),
        state={} if state is None else state,
        next_poll_at=0.0,
        updated_at=1000.0,
    )
    return harness.trig_store.create_trigger(rule, initial_script_check_checkpoint=cp)


@pytest.mark.asyncio
async def test_script_check_e2e_live_subprocess_ticks_state_triage_dispatch_and_reopen(tmp_path: Path) -> None:
    """Real subprocess executes check(): None commits state with 0 LLM calls; dict flows through triage/dispatch/reopen."""
    _LiveFeedToolkit.items = ["a"]
    src = (
        "import json\n"
        "def check():\n"
        "    items = json.loads(tool('live_feed', 'fetch_items'))['items']\n"
        "    seen = set(state.get('seen', []))\n"
        "    new = [x for x in items if x not in seen]\n"
        "    state.set('seen', items)\n"
        "    state.set('ticks', state.get('ticks', 0) + 1)\n"
        "    return {'title': 'Fresh items', 'items': new} if new else None\n"
    )
    with registered_toolkit("live_feed", _LiveFeedToolkit):
        async with _live_script_check_harness(
            tmp_path,
            tools=["triggers", "script", "live_feed"],
        ) as h:
            rule = _create_live_rule(
                h,
                "sc_live",
                src,
                state={"seen": ["a"]},
                triage=TriageSpec(provider="llm", instructions="Act on new items", model="default"),
                reopen_on_fire=True,
            )
            triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="new item"))
            reopen_mock = AsyncMock(return_value=True)
            with (
                patch("mindroom.reactive_triggers.service.evaluate_triage", triage_mock),
                patch("mindroom.reactive_triggers.dispatch.reopen_thread_if_resolved", reopen_mock),
            ):
                # Tick 1: returns None -> 0 LLM calls, state preserved, script_runs row pruned
                assert await h.service.poll_script_checks_once() == 0
                await h.service.drain_due_firings_once()
                assert triage_mock.await_count == 0
                assert h.send_log == []
                assert _require_cp(h.trig_store, rule.trigger_id).state == {"seen": ["a"], "ticks": 1}
                assert h.script_store.list_runs(include_finished=True) == []

                # Tick 2: live tool returns fresh data ["a", "b"] (no cache) -> dict flows through triage/dispatch/reopen
                _LiveFeedToolkit.items = ["a", "b"]
                cp1 = _require_cp(h.trig_store, rule.trigger_id)
                h.trig_store.upsert_script_check_checkpoint(
                    rule.trigger_id,
                    cp1.model_copy(update={"next_poll_at": 0.0}),
                    expected_rule_uid=rule.rule_uid,
                )
                assert await h.service.poll_script_checks_once() == 1
                assert _require_cp(h.trig_store, rule.trigger_id).state == {"seen": ["a", "b"], "ticks": 2}
                await h.service.drain_due_firings_once()
                assert triage_mock.await_count == 1
                assert reopen_mock.await_count == 1
                assert len(h.send_log) == 1
                assert '"b"' in h.send_log[0]["content"]["body"]
                assert h.script_store.list_runs(include_finished=True) == []


@pytest.mark.asyncio
async def test_script_check_e2e_guards_approval_agent_module_load_and_stale_revision(tmp_path: Path) -> None:
    """Swallowed approval denial, agent(), module-level tool/state, and stale revisions never commit bad state."""
    with registered_toolkit("live_feed", _LiveFeedToolkit):
        async with _live_script_check_harness(tmp_path, tools=["triggers", "script", "live_feed"]) as h:
            # 1. Swallowed approval denial fails tick without committing state or requesting cards
            swallow_src = (
                "def check():\n"
                "    try:\n"
                "        tool('live_feed', 'confirm_op')\n"
                "    except Exception:\n"
                "        state.set('leaked', 1)\n"
                "        return None\n"
                "    return {'ok': True}\n"
            )
            r_app = _create_live_rule(h, "sc_app", swallow_src, state={"safe": 1})
            assert await h.service.poll_script_checks_once() == 0
            cp_app = _require_cp(h.trig_store, r_app.trigger_id)
            assert (cp_app.state, cp_app.last_error_code, cp_app.consecutive_errors) == (
                {"safe": 1},
                "approval_required",
                1,
            )
            assert h.resolver.approval_requests == 0
            assert h.script_store.list_runs(include_finished=True) == []

            # 2. agent() blocked, module-level tool() blocked, module-level state.set() blocked
            for tid, bad_src in (
                ("sc_agent", "def check():\n    agent('coder', 'hi')\n"),
                ("sc_mod_tool", "tool('live_feed', 'fetch_items')\ndef check():\n    return None\n"),
                ("sc_mod_state", "state.set('x', 1)\ndef check():\n    return None\n"),
            ):
                r_bad = _create_live_rule(h, tid, bad_src, state={"orig": 1})
                assert await h.service.poll_script_checks_once() == 0
                cp_bad = _require_cp(h.trig_store, r_bad.trigger_id)
                assert (cp_bad.state, cp_bad.last_error_code, cp_bad.consecutive_errors) == (
                    {"orig": 1},
                    "script_error",
                    1,
                )

            # 3. Stale revision rejected if checkpoint is updated while tick is executing
            marker = tmp_path / "tick_started"
            slow_src = (
                "import pathlib, time\n"
                "def check():\n"
                f"    pathlib.Path({str(marker)!r}).write_text('1', encoding='utf-8')\n"
                "    time.sleep(0.5)\n"
                "    state.set('from_stale_tick', True)\n"
                "    return {'stale': True}\n"
            )
            r_rev = _create_live_rule(h, "sc_rev", slow_src, state={"v": 1})
            poll_task = asyncio.create_task(h.service.poll_script_checks_once())
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.05)
            assert marker.exists()
            cp_live = _require_cp(h.trig_store, r_rev.trigger_id)
            h.trig_store.upsert_script_check_checkpoint(
                r_rev.trigger_id,
                cp_live.model_copy(update={"revision": cp_live.revision + 1, "state": {"v": 2}, "next_poll_at": 9e9}),
                expected_rule_uid=r_rev.rule_uid,
                expected_revision=cp_live.revision,
            )
            assert await poll_task == 0
            cp_after = _require_cp(h.trig_store, r_rev.trigger_id)
            assert (cp_after.revision, cp_after.state) == (2, {"v": 2})
            assert h.trig_store.list_firings(trigger_id=r_rev.trigger_id) == []


@pytest.mark.asyncio
async def test_script_check_e2e_timeout_and_shutdown_cancellation(tmp_path: Path) -> None:
    """Timeout kills hung subprocess & prunes row; stop_script_checks() cancels active tick cleanly."""
    marker = tmp_path / "hung_pid"
    hang_src = (
        "import os, pathlib, time\n"
        "def check():\n"
        f"    pathlib.Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "    time.sleep(30)\n"
        "    return {'never': True}\n"
    )
    async with _live_script_check_harness(
        tmp_path,
        tools=["triggers", "script"],
        policy={"script_check_timeout_seconds": 5},
    ) as h:
        # 1. Shutdown cancellation via stop_script_checks()
        r_cancel = _create_live_rule(h, "sc_cancel", hang_src, state={"s": 1})
        h.service._schedule_script_check_poll()
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists()
        pid_cancelled = int(marker.read_text(encoding="utf-8"))
        await h.service.stop_script_checks()
        assert h.script_store.list_runs(include_finished=True) == []
        with pytest.raises(ProcessLookupError):
            os.kill(pid_cancelled, 0)
        assert _require_cp(h.trig_store, r_cancel.trigger_id).state == {"s": 1}

        # 2. Timeout kills hung process and prunes script_runs row
        h.service.start()
        await h.service.stop()
        h.service._script_checks_stopped = False
        marker.unlink()
        assert await h.service.poll_script_checks_once() == 0
        cp_to = _require_cp(h.trig_store, r_cancel.trigger_id)
        assert (cp_to.last_error_code, cp_to.consecutive_errors, cp_to.state) == ("timeout", 1, {"s": 1})
        assert h.script_store.list_runs(include_finished=True) == []
        pid_timed_out = int(marker.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid_timed_out, 0)


@pytest.mark.asyncio
async def test_script_runtime_lifecycle_and_dispatch_source_access_for_script_check(tmp_path: Path) -> None:
    """Lifecycle prunes/hides/revokes script_check runs properly and dispatch verifies live source access."""
    ctx_both = _script_context(tmp_path / "lc", tools=["triggers", "script", "workflow"], allow_local=True)
    store = ScriptRunStore(ctx_both.runtime_paths)
    bot = _FakeBot(ctx_both, send_log=[])
    memberships = AgentReplyMembershipIndex()
    bot.orchestrator = SimpleNamespace(agent_reply_memberships=memberships)
    cfg_box = [ctx_both.config]
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=ctx_both.runtime_paths,
        bot_provider=lambda name: bot if name == "coder" else None,  # type: ignore[arg-type,return-value]
        worker_backend_provider=lambda _run: None,
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
    )
    lifecycle = ScriptRuntimeLifecycle(
        runtime_paths=ctx_both.runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: cfg_box[0],
        worker_lease_provider=lambda _loc: None,
        api_enabled=False,
    )

    # Active script_check runs are hidden from active_runs() while plain scripts remain visible
    plain, _ = _create_script_run(store, ctx_both, f"script-{'1' * 32}")
    sc_active, _ = _create_script_run(store, ctx_both, f"script-{'2' * 32}", script_check_rule_uid="rule-2")
    assert [r.run_id for r in await lifecycle.active_runs()] == [plain.run_id]

    # Terminal script_check runs prune immediately without calling prune_approvals; recent plain scripts stay
    plain_done, _ = _create_script_run(store, ctx_both, f"script-{'3' * 32}")
    sc_done, _ = _create_script_run(store, ctx_both, f"script-{'4' * 32}", script_check_rule_uid="rule-4")
    store.transition_run(plain_done.run_id, state=ScriptRunState.EXITED, exit_code=0)
    store.transition_run(sc_done.run_id, state=ScriptRunState.EXITED, exit_code=0)
    prune_approvals_mock = AsyncMock(return_value=True)
    with patch.object(_LiveScriptRuntimeResolver, "prune_approvals", prune_approvals_mock):
        await lifecycle._prune_pass()
    assert prune_approvals_mock.await_count == 0
    remaining_ids = {r.run_id for r in store.list_runs(include_finished=True)}
    assert sc_done.run_id not in remaining_ids
    assert plain_done.run_id in remaining_ids

    # Dropping 'script' while keeping 'triggers'+'workflow' does NOT revoke script_check (only plain script)
    cfg_wf_only = _script_context(tmp_path / "lc", tools=["triggers", "workflow"], allow_local=True).config
    with patch.object(ScriptRuntimeLifecycle, "_interrupt_runs", AsyncMock()) as interrupt_mock:
        await lifecycle._apply_update(
            cfg_wf_only,
            removed_entities=frozenset(),
            authorization_changed=False,
            plugins_changed=False,
        )
        assert interrupt_mock.await_count == 1
        assert [r.run_id for r in interrupt_mock.await_args.args[0]] == [plain.run_id]

    # Removing 'triggers' revokes script_check runs (even when 'workflow' remains)
    cfg_no_trig = _script_context(tmp_path / "lc", tools=["workflow"], allow_local=True).config
    with patch.object(ScriptRuntimeLifecycle, "_interrupt_runs", AsyncMock()) as interrupt_mock:
        await lifecycle._apply_update(
            cfg_no_trig,
            removed_entities=frozenset(),
            authorization_changed=False,
            plugins_changed=False,
        )
        assert {r.run_id for r in interrupt_mock.await_args.args[0]} == {plain.run_id, sc_active.run_id}

    # Disabling script_check_enabled DOES revoke active script_check runs
    cfg_disabled = _script_context(
        tmp_path / "lc",
        tools=["triggers", "script", "workflow"],
        policy={"script_check_enabled": False},
        allow_local=True,
    ).config
    with patch.object(ScriptRuntimeLifecycle, "_interrupt_runs", AsyncMock()) as interrupt_mock:
        await lifecycle._apply_update(
            cfg_disabled,
            removed_entities=frozenset(),
            authorization_changed=False,
            plugins_changed=False,
        )
        assert [r.run_id for r in interrupt_mock.await_args.args[0]] == [sc_active.run_id]

    # Startup cleanup retires & prunes orphaned script_check runs
    store.transition_run(plain.run_id, state=ScriptRunState.EXITED, exit_code=0)
    with patch.object(
        ScriptRunManager,
        "reconcile_durable",
        AsyncMock(
            side_effect=lambda *, run_id, **_kw: store.transition_run(
                run_id,
                state=ScriptRunState.INTERRUPTED,
            ),
        ),
    ):
        await lifecycle._startup_cleanup_pass()
    assert sc_active.run_id not in {r.run_id for r in store.list_runs(include_finished=True)}


@pytest.mark.asyncio
async def test_dispatch_check_reactive_source_access_for_script_check(tmp_path: Path) -> None:
    """dispatch.check_reactive_source_access verifies policy, capability, and live room membership."""
    ctx_both = _script_context(tmp_path / "disp", tools=["triggers", "script", "workflow"], allow_local=True)
    cfg_disabled = _script_context(
        tmp_path / "disp",
        tools=["triggers", "script", "workflow"],
        policy={"script_check_enabled": False},
        allow_local=True,
    ).config
    bot = _FakeBot(ctx_both, send_log=[])
    memberships = AgentReplyMembershipIndex()
    rule = _sc_rule("sc_disp", agent="coder")
    ev = TriggerEvent(
        source_type="script_check",
        source_id=rule.trigger_id,
        kind="script_check.match",
        event_id="e1",
        timestamp=1.0,
    )
    assert (
        await check_reactive_source_access(
            rule=rule,
            event=ev,
            source_bot=bot,  # type: ignore[arg-type]
            config=ctx_both.config,
            runtime_paths=ctx_both.runtime_paths,
            memberships=memberships,
        )
        is None
    )
    with patch(
        "mindroom.reactive_triggers.dispatch.is_sender_allowed_for_agent_reply_in_room",
        side_effect=ReplyMembershipPendingError(),
    ):
        res = await check_reactive_source_access(
            rule=rule,
            event=ev,
            source_bot=bot,  # type: ignore[arg-type]
            config=ctx_both.config,
            runtime_paths=ctx_both.runtime_paths,
            memberships=memberships,
        )
        assert res is not None
        assert res.status == "retry"
    with patch(
        "mindroom.reactive_triggers.dispatch.is_sender_allowed_for_agent_reply_in_room",
        return_value=False,
    ):
        res = await check_reactive_source_access(
            rule=rule,
            event=ev,
            source_bot=bot,  # type: ignore[arg-type]
            config=ctx_both.config,
            runtime_paths=ctx_both.runtime_paths,
            memberships=memberships,
        )
        assert res is not None
        assert res.status == "skipped"
    res_dis = await check_reactive_source_access(
        rule=rule,
        event=ev,
        source_bot=bot,  # type: ignore[arg-type]
        config=cfg_disabled,
        runtime_paths=ctx_both.runtime_paths,
        memberships=memberships,
    )
    assert res_dis is not None
    assert res_dis.status == "skipped"
