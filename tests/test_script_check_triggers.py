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
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import nio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport

import mindroom.tools  # noqa: F401
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import apply_tool_approval_capability
from mindroom.api import room_triggers
from mindroom.api.app_state import ExternalTriggerRuntime, ensure_app_state
from mindroom.api.script_gateway import bind_script_tool_broker, serve_script_gateway_listener
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.approval.tool_approval import BackgroundScriptToolOrigin, ToolApprovalDecision, evaluate_tool_approval
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.custom_tools.triggers import _ACTION_ALLOWED_PARAMS, TriggersTools
from mindroom.delegation.state import SubagentPersona
from mindroom.event_journal import (
    ApprovalCall,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
)
from mindroom.event_journal.identity import journal_principal_id
from mindroom.hooks import HookBindings, HookRegistry
from mindroom.message_target import MessageTarget
from mindroom.orchestration.script_checks import _SCRIPT_CHECK_BOOTSTRAP_SOURCE, build_script_check_tick_runner
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
    _MAX_SCRIPT_CHECK_RESULT_BYTES,
    _MAX_SCRIPT_CHECK_SOURCE_BYTES,
    _MAX_SCRIPT_CHECK_STATE_BYTES,
    _SCRIPT_CHECK_EVENT_KIND,
    ScriptCheckPoller,
    ScriptCheckTickOutcome,
    _normalize_script_check_event,
    _validate_script_check_state,
    agent_supports_script_check,
    check_script_check_source_access,
    validate_script_check_source,
)
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.triage import TriageOutcome
from mindroom.response_sources import ResponseSources
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker, ScriptToolCallRequest
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import ScriptCallState, ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.policy import resolve_current_script_tool
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.shell.supervisor import _ShellSupervisorManager
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.catalog import builtin_tool_registry
from mindroom.tool_system.invocation import DETACHED_SESSION_ID, ToolCallContext, run_tool_call
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolFunction, ToolGate
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.tools.triggers import _NON_CODE_TRIGGER_ACTIONS
from tests.access_schema_support import joined_members_response
from tests.api.test_plugin_room_readers import _bound, _client
from tests.approval_continuation_helpers import approval_continuation
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    approval_manager_for_test,
    bind_runtime_paths,
    handle_card_response_for_test,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.reply_span_helpers import paused_for_approval
from tests.scripted_agent_helpers import registered_toolkit
from tests.test_plugin_routes import _api_state, _signed_in

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
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
        (b"#" * (_MAX_SCRIPT_CHECK_SOURCE_BYTES + 1), "maximum size"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_script_check_source(bad_src)

    assert _validate_script_check_state({"seen": [1, "a"]}) == {"seen": [1, "a"]}
    for bad_state, match in (
        (["x"], "must be a dict"),
        ({1: "a"}, "string keys"),
        ({"x": math.nan}, "strict finite JSON"),
        ({"big": "a" * (_MAX_SCRIPT_CHECK_STATE_BYTES + 1)}, "maximum size"),
    ):
        with pytest.raises(ValueError, match=match):
            _validate_script_check_state(bad_state)

    assert _normalize_script_check_event("t1", "r1", None, now=2000.0) is None
    plain = _normalize_script_check_event("t1", "r1", {"items": [1]}, now=2000.0)
    assert plain is not None
    assert (plain.kind, plain.event_id, plain.data) == (_SCRIPT_CHECK_EVENT_KIND, "sc:tick:r1", {"items": [1]})

    env = _normalize_script_check_event(
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
        ({"blob": "a" * (_MAX_SCRIPT_CHECK_RESULT_BYTES + 1)}, "maximum size"),
    ):
        with pytest.raises(ValueError, match=match):
            _normalize_script_check_event("t1", "r3", bad_res, now=2000.0)


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

    ev = _normalize_script_check_event("sc_1", "r1", {"items": ["b"]}, now=1100.0)
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
    ev2 = _normalize_script_check_event("sc_1", "r2", {"items": ["c"]}, now=1150.0)
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
            return outcomes.get(
                rule.trigger_id,
                ScriptCheckTickOutcome(status="unauthorized", error_code="source_room_not_joined"),
            )
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
    assert (cp_un.state, cp_un.consecutive_errors, cp_un.last_error_code, cp_un.next_poll_at) == (
        {"k": "t_unauth"},
        0,
        "source_room_not_joined",
        1120.0,
    )


class _CheckProbeToolkit(Toolkit):
    """Test toolkit with one read function and one ToolGate.CONFIRM function."""

    def __init__(self) -> None:
        super().__init__(name="check_probe", tools=[self.read_item, self.confirm_action])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        if function.name == "confirm_action":
            return replace(function, gate=ToolGate.CONFIRM)
        return function

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
        super().__init__(name="live_feed", tools=[self.fetch_items, self.confirm_op])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        if function.name == "confirm_op":
            return replace(function, gate=ToolGate.CONFIRM)
        return function

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


def _tool_context_for_triggers(
    tmp_path: Path,
    *,
    agent_name: str = "coder",
    requester_id: str = _OWNER,
    room_id: str = _ROOM,
    agents: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    allow_local: bool = True,
    with_script_manager: bool = True,
    tool_function_filter: Callable[[object], bool] | None = None,
    persona: SubagentPersona | None = None,
    session_id: str | None = "sess-1",
    correlation_id: str | None = "$ev:example.org",
) -> tuple[ToolRuntimeContext, MagicMock, ScriptRunManager | None]:
    env = {"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"}
    if allow_local:
        env["MINDROOM_SANDBOX_EXECUTION_MODE"] = "local"
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env,
    )
    agent_defs = agents or {
        "coder": {
            "display_name": "Coder",
            "include_default_tools": False,
            "tools": ["triggers", "script"],
            "rooms": [_ROOM],
        },
        "watcher": {
            "display_name": "Watcher",
            "include_default_tools": False,
            "tools": ["triggers"],
            "rooms": [_ROOM],
        },
    }
    raw: dict[str, Any] = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "defaults": {"tools": [], "allow_shell_without_worker": allow_local},
        "agents": agent_defs,
        "external_trigger_policy": policy or {},
    }
    cfg = bind_runtime_paths(Config.model_validate(raw), paths)
    client = make_matrix_client_mock(user_id=f"@mindroom_{agent_name}:example.org")
    joined_rooms = {_ROOM, room_id}
    client.rooms = {
        rid: SimpleNamespace(users={requester_id: object(), client.user_id: object()}, encrypted=False)
        for rid in joined_rooms
    }
    client.room_get_state = AsyncMock(
        side_effect=lambda rid: nio.RoomGetStateResponse(
            events=[{"type": "m.room.member", "state_key": requester_id, "content": {"membership": "join"}}],
            room_id=rid,
        ),
    )
    notify_mock = MagicMock()
    manager: ScriptRunManager | None = None
    if with_script_manager:
        s_store = ScriptRunStore(runtime_paths_for(cfg))
        broker = ScriptToolBroker(store=s_store, runtime_resolver=SimpleNamespace())  # type: ignore[arg-type]
        manager = ScriptRunManager(
            store=s_store,
            broker=broker,
            worker_client=ScriptWorkerClient(),
            worker_backend=None,
            gateway_url="http://127.0.0.1:8765/api/script-gateway",
        )
    memberships = AgentReplyMembershipIndex()
    bot_stub = SimpleNamespace(running=True, client=client, agent_name=agent_name)
    orch = SimpleNamespace(
        agent_bots=dict.fromkeys(cfg.agents, bot_stub),
        agent_reply_memberships=memberships,
        reactive_triggers=SimpleNamespace(notify_work=notify_mock),
        script_runtime=SimpleNamespace(tool_manager=manager),
    )
    target = MessageTarget(
        room_id=room_id,
        source_thread_id="$thread:example.org",
        resolved_thread_id="$thread:example.org",
        reply_to_event_id="$e:example.org",
        session_id=session_id or "placeholder",
    )
    if session_id is None:
        object.__setattr__(target, "session_id", None)
    ctx = make_test_tool_runtime_context(
        agent_name=agent_name,
        target=target,
        requester_id=requester_id,
        client=client,
        config=cfg,
        runtime_paths=runtime_paths_for(cfg),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hooks=HookBindings(registry=HookRegistry.empty()),
        orchestrator=orch,
        agent_reply_memberships=memberships,
        tool_function_filter=tool_function_filter,
        persona=persona,
        correlation_id=correlation_id,
    )
    return ctx, notify_mock, manager


@pytest.mark.asyncio
async def test_triggers_tool_watch_script_create_source_path_and_capability_guards(tmp_path: Path) -> None:
    """watch_script supports inline source & workspace path and enforces capability, filter, persona, and placement guards."""
    tool = TriggersTools()
    ctx, notify, manager = _tool_context_for_triggers(tmp_path / "ok")
    assert manager is not None

    # 1. Inline source creation: defaults once=False, clamps poll_interval_seconds, stores checkpoint & wakes poller
    with tool_runtime_context(ctx):
        res = json.loads(
            await tool.triggers(
                action="watch_script",
                trigger_id="sc_inline",
                prompt="Review check output",
                source="def check():\n    return {'ok': True}\n",
                poll_interval_seconds=10,
            ),
        )
    assert res["status"] == "ok"
    assert res["trigger"]["once"] is False
    assert res["trigger"]["source"]["poll_interval_seconds"] == 60
    assert res["trigger"]["source"]["agent"] == "coder"
    assert "source_utf8" not in json.dumps(res)
    assert notify.call_count == 1
    store = ReactiveTriggerStore(ctx.runtime_paths)
    cp = _require_cp(store, "sc_inline")
    assert cp.revision == 1
    assert cp.state == {}
    assert cp.identity_json["session_id"] == "sess-1"
    assert cp.identity_json["room_id"] == _ROOM

    # 2. Workspace path creation snapshots once (later edits to file do not alter stored source_utf8)
    ws = ctx.agent_workspace_root(create=True)
    script_file = ws / "check_feed.py"
    script_file.write_text("def check():\n    return {'v': 1}\n", encoding="utf-8")
    with tool_runtime_context(ctx):
        res_path = json.loads(
            await tool.triggers(
                action="watch_script",
                trigger_id="sc_path",
                prompt="Handle feed",
                path="check_feed.py",
            ),
        )
    assert res_path["status"] == "ok"
    script_file.write_text("def check():\n    return {'v': 999}\n", encoding="utf-8")
    assert "{'v': 1}" in _require_cp(store, "sc_path").source_utf8

    # 3. Unsupported param rejected
    with tool_runtime_context(ctx):
        res_bad = json.loads(
            await tool.triggers(action="watch_script", prompt="x", source=_SAMPLE_SOURCE, repo="o/r"),
        )
    assert res_bad["status"] == "error"
    assert "Unsupported parameter" in res_bad["message"]

    # 4. triggers-only agent, filtered turn, restricted persona, missing session_id, bad placement, unready runtime
    guard_cases = [
        ("watcher_agent", {"agent_name": "watcher"}, "requires 'triggers' plus 'script' or 'workflow'"),
        ("filtered_turn", {"tool_function_filter": lambda _f: True}, "tool-filtered turn"),
        (
            "restricted_persona",
            {
                "persona": SubagentPersona(
                    source_kind="configured",
                    source_name="p",
                    system_prompt="",
                    tools=("triggers",),
                ),
            },
            "tool-restricted persona",
        ),
        (
            "narrowed_persona_with_script",
            {
                "persona": SubagentPersona(
                    source_kind="configured",
                    source_name="p2",
                    system_prompt="",
                    tools=("triggers", "script"),
                ),
            },
            "tool-restricted persona",
        ),
        (
            "background_script_caller",
            {"correlation_id": "background-script:script-1:call-1"},
            "background script or workflow",
        ),
        (
            "workflow_caller",
            {"correlation_id": "workflow:wf-1"},
            "background script or workflow",
        ),
        (
            "script_check_caller",
            {"correlation_id": "script-check:sc1:r1"},
            "background script or workflow",
        ),
        (
            "detached_session",
            {"session_id": DETACHED_SESSION_ID},
            "detached tool contexts",
        ),
        ("no_session", {"session_id": None}, "active session identity"),
        ("no_placement", {"allow_local": False}, "worker"),
        ("unready_runtime", {"with_script_manager": False}, "not ready"),
        ("policy_disabled", {"policy": {"script_check_enabled": False}}, "disabled by policy"),
    ]
    for label, kwargs, expected_msg in guard_cases:
        g_ctx, _, _ = _tool_context_for_triggers(tmp_path / label, **kwargs)
        with (
            patch.object(
                ScriptRunManager,
                "resolve_source",
                AsyncMock(side_effect=AssertionError("must not read path")),
            ),
            tool_runtime_context(g_ctx),
        ):
            out = json.loads(
                await tool.triggers(action="watch_script", prompt="p", path="check_feed.py"),
            )
        assert out["status"] == "error", label
        assert expected_msg.lower() in out["message"].lower(), (label, out["message"])


@pytest.mark.asyncio
async def test_triggers_tool_script_check_list_owner_health_and_admin_redaction(tmp_path: Path) -> None:
    """update, set_enabled, list, history, and synthetic test enforce all script_check contracts and redaction."""
    tool = TriggersTools()
    ctx, _, _ = _tool_context_for_triggers(
        tmp_path,
        policy={"admin_users": [_OTHER_OWNER]},
    )
    with tool_runtime_context(ctx):
        created = json.loads(
            await tool.triggers(
                action="watch_script",
                trigger_id="sc_u",
                prompt="Alert",
                source="def check():\n    return None\n",
                triage_instructions="Only fire on urgent alerts",
                triage_model="default",
            ),
        )
    assert created["status"] == "ok"
    store = ReactiveTriggerStore(ctx.runtime_paths)
    rule = store.get_trigger("sc_u")
    assert rule is not None

    # Seed non-empty state & error counters in checkpoint plus a firing for history tests
    cp0 = _require_cp(store, "sc_u")
    store.upsert_script_check_checkpoint(
        "sc_u",
        cp0.model_copy(
            update={"state": {"secret_cursor": "tok-123"}, "consecutive_errors": 2, "last_error_code": "timeout"},
        ),
        expected_rule_uid=rule.rule_uid,
    )
    ev = TriggerEvent(
        source_type="script_check",
        source_id="sc_u",
        kind=_SCRIPT_CHECK_EVENT_KIND,
        event_id="sc:tick:r1",
        timestamp=1234.0,
        title="Alert",
        data={"hits": 3},
    )
    store.update_script_check_checkpoint_and_enqueue(
        "sc_u",
        _require_cp(store, "sc_u").model_copy(update={"last_run_at": 1234.0}),
        event=ev,
        expected_rule_uid=rule.rule_uid,
        expected_revision=1,
    )

    # 1. list: owner gets compact health, never source_utf8/identity_json/state; non-owner admin gets metadata only
    with tool_runtime_context(ctx):
        owner_list = json.loads(await tool.triggers(action="list"))
    owner_item = next(t for t in owner_list["reactive_triggers"] if t["trigger_id"] == "sc_u")
    assert owner_item["health"] == {"consecutive_errors": 2, "last_error_code": "timeout", "last_run_at": 1234.0}
    raw_owner = json.dumps(owner_list)
    assert all(secret not in raw_owner for secret in ("source_utf8", "identity_json", "secret_cursor", "def check"))

    admin_ctx, _, _ = _tool_context_for_triggers(
        tmp_path,
        requester_id=_OTHER_OWNER,
        policy={"admin_users": [_OTHER_OWNER]},
    )
    with tool_runtime_context(admin_ctx):
        admin_list = json.loads(await tool.triggers(action="list"))
    admin_item = next(t for t in admin_list["reactive_triggers"] if t["trigger_id"] == "sc_u")
    assert "health" not in admin_item
    assert admin_item["source"]["agent"] == "coder"
    raw_admin = json.dumps(admin_list)
    assert all(secret not in raw_admin for secret in ("source_utf8", "identity_json", "secret_cursor", "def check"))


@pytest.mark.asyncio
async def test_triggers_tool_script_check_update_reenable_history_and_test(tmp_path: Path) -> None:
    """update, set_enabled, history, and synthetic test enforce script_check invariants and never run Python."""
    tool = TriggersTools()
    ctx, _, _ = _tool_context_for_triggers(tmp_path)
    with tool_runtime_context(ctx):
        assert (
            json.loads(
                await tool.triggers(
                    action="watch_script",
                    trigger_id="sc_u",
                    prompt="Alert",
                    source="def check():\n    return None\n",
                    triage_instructions="Only fire on urgent alerts",
                    triage_model="default",
                ),
            )["status"]
            == "ok"
        )
    store = ReactiveTriggerStore(ctx.runtime_paths)
    rule = store.get_trigger("sc_u")
    assert rule is not None
    ev = TriggerEvent(
        source_type="script_check",
        source_id="sc_u",
        kind=_SCRIPT_CHECK_EVENT_KIND,
        event_id="sc:tick:r1",
        timestamp=1234.0,
        title="Alert",
        data={"hits": 3},
    )
    store.update_script_check_checkpoint_and_enqueue(
        "sc_u",
        _require_cp(store, "sc_u").model_copy(
            update={"state": {"old": 1}, "consecutive_errors": 2, "last_error_code": "timeout"},
        ),
        event=ev,
        expected_rule_uid=rule.rule_uid,
        expected_revision=1,
    )

    # 2. update source resets state/errors & increments revision; poll_interval_seconds clamps cleanly
    new_src = "def check():\n    return {'v': 2}\n"
    with tool_runtime_context(ctx):
        upd = json.loads(
            await tool.triggers(action="update", trigger_id="sc_u", source=new_src, poll_interval_seconds=15),
        )
    assert upd["status"] == "ok"
    assert upd["trigger"]["source"]["poll_interval_seconds"] == 60
    assert upd["trigger"]["source"]["source_digest"] == hashlib.sha256(new_src.encode()).hexdigest()
    cp_after = _require_cp(store, "sc_u")
    assert (cp_after.revision, cp_after.state, cp_after.consecutive_errors, cp_after.last_error_code) == (
        3,
        {},
        0,
        None,
    )
    assert cp_after.source_utf8 == new_src

    # 3. Disable & re-enable preserves state when authorized, refuses when policy/capability/placement revoked
    store.upsert_script_check_checkpoint(
        "sc_u",
        cp_after.model_copy(update={"state": {"keep": 1}}),
        expected_rule_uid=rule.rule_uid,
    )
    with tool_runtime_context(ctx):
        assert json.loads(await tool.triggers(action="set_enabled", trigger_id="sc_u", enabled=False))["status"] == "ok"
    dis_ctx, _, _ = _tool_context_for_triggers(tmp_path, policy={"script_check_enabled": False})
    with tool_runtime_context(dis_ctx):
        assert (
            json.loads(await tool.triggers(action="set_enabled", trigger_id="sc_u", enabled=True))["status"] == "error"
        )
    with tool_runtime_context(ctx):
        assert json.loads(await tool.triggers(action="set_enabled", trigger_id="sc_u", enabled=True))["status"] == "ok"
    assert _require_cp(store, "sc_u").state == {"keep": 1}

    # 4. history: visible when authorized, refused when script_check disabled
    with tool_runtime_context(ctx):
        hist_ok = json.loads(await tool.triggers(action="history", trigger_id="sc_u"))
    assert hist_ok["status"] == "ok"
    assert len(hist_ok["firings"]) == 1
    with tool_runtime_context(dis_ctx):
        hist_denied = json.loads(await tool.triggers(action="history", trigger_id="sc_u"))
    assert hist_denied["status"] == "error"

    # 5. test: synthetic dry-run evaluates triage without executing Python or mutating state
    triage_mock = AsyncMock(return_value=TriageOutcome(act=True, reason="urgent match"))
    with (
        patch("mindroom.custom_tools.triggers.evaluate_triage", triage_mock),
        tool_runtime_context(ctx),
    ):
        tested = json.loads(
            await tool.triggers(
                action="test",
                trigger_id="sc_u",
                text="Urgent",
                actor=_OWNER,
                sample_data={"title": "Page", "hits": 5},
            ),
        )
    assert tested["status"] == "ok"
    assert tested["matched"] is True
    assert tested["would_fire"] is True
    assert tested["triage_decision"] is True
    assert "without executing the script" in tested["note"]
    assert triage_mock.await_count == 1
    assert _require_cp(store, "sc_u").state == {"keep": 1}


@pytest.mark.asyncio
async def test_triggers_approval_alias_and_room_triggers_api_for_script_check(tmp_path: Path) -> None:
    """ApprovalAlias judges watch_script and code-bearing update as start_script; Room Triggers API guards re-enable & redacts."""
    triggers_meta = builtin_tool_registry().get("triggers")
    assert triggers_meta is not None
    assert triggers_meta.executes_code is False
    alias = triggers_meta.approval_aliases["triggers"]
    assert alias.judged_as == "start_script"
    assert alias.judge({"action": "watch_script", "source": "def check(): return None", "trigger_id": "t1"}) == [
        {"source": "def check(): return None", "path": None, "name": "t1"},
    ]
    assert alias.judge({"action": "update", "trigger_id": "t1", "path": "c.py"}) == [
        {"source": None, "path": "c.py", "name": "t1"},
    ]
    for non_code in (
        {"action": "update", "trigger_id": "t1", "prompt": "new"},
        {"action": "watch_pr", "repo": "o/r", "number": 1},
        {"action": "watch_room", "prompt": "p"},
        {"action": "list"},
        {"action": "history"},
        {"action": "test", "trigger_id": "t1"},
    ):
        assert alias.judge(non_code) == []
    for bad in ({}, {"action": None}, {"action": "  "}, {"action": 42}, {"action": "unknown_action"}):
        assert alias.judge(bad) is None

    # End-to-end through evaluate_tool_approval with a start_script require_approval rule
    ctx_app = _script_context(
        tmp_path / "app",
        tools=["triggers", "script"],
        tool_approval={"default": "auto_approve", "rules": [{"match": "start_script", "action": "require_approval"}]},
    )
    for args, expected in (
        ({"action": "watch_script", "source": _SAMPLE_SOURCE}, True),
        ({"action": "update", "trigger_id": "t1", "path": "a.py"}, True),
        ({"action": "update", "trigger_id": "t1", "prompt": "p"}, False),
        ({"action": "watch_pr", "repo": "o/r", "number": 1}, False),
        ({"action": ""}, True),
        ({"action": "unknown_action"}, True),
    ):
        asks, _ = await evaluate_tool_approval(ctx_app.config, ctx_app.runtime_paths, "triggers", args, "coder", alias)
        assert asks is expected

    # Room Triggers API: GET redacts source/state; PATCH enforces live policy/capability/placement on re-enable
    ctx_api = _script_context(tmp_path / "api", tools=["triggers", "script"], allow_local=True)
    store = ReactiveTriggerStore(ctx_api.runtime_paths)
    rule = ReactiveTriggerRecord(
        trigger_id="sc_api",
        owner_user_id=_OWNER,
        source=ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest=_SAMPLE_DIGEST),
        target=TriggerTargetSpec(agent="coder", room_id=_ROOM, prompt="Check"),
        enabled=False,
        disabled_reason="manual",
        created_at=1000.0,
        updated_at=1000.0,
    )
    cp = ScriptCheckCheckpoint(
        revision=1,
        source_utf8="def check():\n    return {'secret_code': 1}\n",
        identity_json={"session_id": "s1"},
        state={"secret_state": 9},
        next_poll_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(rule, initial_script_check_checkpoint=cp)

    client = _client(joined_rooms=(_ROOM,))
    client.joined_members.side_effect = lambda rid: joined_members_response(rid, client.user_id, _OWNER)
    app = _bound(client)
    app.include_router(room_triggers.router)
    app_state = ensure_app_state(app)
    app_state.external_trigger_runtime = ExternalTriggerRuntime(
        target_bot=lambda _n: None,
        config_generation=1,
        on_reactive_event=MagicMock(),
    )
    app_state.api_state = _api_state(ctx_api.config, ctx_api.runtime_paths)

    with TestClient(app) as tc:
        headers = _signed_in(app, _OWNER)
        listed = tc.get(f"/api/rooms/{_ROOM}/triggers", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["triggers"][0]["kind"] == "script_check"
        assert all(s not in listed.text for s in ("secret_code", "secret_state", "source_utf8", "identity_json"))

        # Policy disabled -> 403
        cfg_dis = _script_context(
            tmp_path / "api",
            tools=["triggers", "script"],
            policy={"script_check_enabled": False},
            allow_local=True,
        ).config
        app_state.api_state = _api_state(cfg_dis, ctx_api.runtime_paths)
        assert (
            tc.patch(f"/api/rooms/{_ROOM}/triggers/sc_api", json={"enabled": True}, headers=headers).status_code == 403
        )

        # Placement unavailable -> 409
        ctx_no_place = _script_context(tmp_path / "api", tools=["triggers", "script"], allow_local=False)
        app_state.api_state = _api_state(ctx_no_place.config, ctx_no_place.runtime_paths)
        assert (
            tc.patch(f"/api/rooms/{_ROOM}/triggers/sc_api", json={"enabled": True}, headers=headers).status_code == 409
        )

        # Authorized & placement ready -> 200
        app_state.api_state = _api_state(ctx_api.config, ctx_api.runtime_paths)
        ok = tc.patch(f"/api/rooms/{_ROOM}/triggers/sc_api", json={"enabled": True}, headers=headers)
        assert ok.status_code == 200
        assert ok.json()["enabled"] is True


@pytest.mark.asyncio
async def test_script_check_cross_agent_cross_room_detached_and_prompt_persona_guards(tmp_path: Path) -> None:
    """Fix 1 & Fix 2: block cross-agent/cross-room code update, detached/broker callers, and allow prompt-only personas."""
    tool = TriggersTools()
    other_room = "!other:example.org"
    agents = {
        "coder": {
            "display_name": "Coder",
            "include_default_tools": False,
            "tools": ["triggers", "script"],
            "rooms": [_ROOM, other_room],
        },
        "coder2": {
            "display_name": "Coder2",
            "include_default_tools": False,
            "tools": ["triggers", "script"],
            "rooms": [_ROOM, other_room],
        },
    }
    ctx_orig, _, _ = _tool_context_for_triggers(tmp_path, agent_name="coder", room_id=_ROOM, agents=agents)
    with tool_runtime_context(ctx_orig):
        created = json.loads(
            await tool.triggers(
                action="watch_script",
                trigger_id="sc_owner_bound",
                prompt="Alert",
                source="def check():\n    return None\n",
            ),
        )
    assert created["status"] == "ok"

    # Prompt/model-only persona (tools=None) IS allowed to author script_check
    ctx_prompt_persona, _, _ = _tool_context_for_triggers(
        tmp_path,
        agent_name="coder",
        room_id=_ROOM,
        agents=agents,
        persona=SubagentPersona(source_kind="configured", source_name="prompt_only", system_prompt="Be concise"),
    )
    with tool_runtime_context(ctx_prompt_persona):
        prompt_created = json.loads(
            await tool.triggers(
                action="watch_script",
                trigger_id="sc_prompt_persona",
                prompt="Alert",
                source="def check():\n    return None\n",
            ),
        )
    assert prompt_created["status"] == "ok"

    # Cross-agent code replacement MUST be refused (even when other agent also has triggers + script)
    ctx_other_agent, _, _ = _tool_context_for_triggers(tmp_path, agent_name="coder2", room_id=_ROOM, agents=agents)
    with tool_runtime_context(ctx_other_agent):
        bad_agent = json.loads(
            await tool.triggers(
                action="update",
                trigger_id="sc_owner_bound",
                source="def check():\n    return {'hijacked': 1}\n",
            ),
        )
    assert bad_agent["status"] == "error"
    assert "executing agent 'coder'" in bad_agent["message"]

    # Cross-room code replacement MUST be refused (even from same agent)
    ctx_other_room, _, _ = _tool_context_for_triggers(tmp_path, agent_name="coder", room_id=other_room, agents=agents)
    with tool_runtime_context(ctx_other_room):
        bad_room = json.loads(
            await tool.triggers(
                action="update",
                trigger_id="sc_owner_bound",
                source="def check():\n    return {'hijacked': 2}\n",
            ),
        )
        # Non-code metadata update from another room remains allowed
        meta_ok = json.loads(
            await tool.triggers(action="update", trigger_id="sc_owner_bound", description="updated desc"),
        )
    assert bad_room["status"] == "error"
    assert f"source room {_ROOM}" in bad_room["message"]
    assert meta_ok["status"] == "ok"

    # Background script / workflow broker context blocks code update & watch_script, while keeping non-code actions usable
    ctx_broker, _, _ = _tool_context_for_triggers(
        tmp_path,
        agent_name="coder",
        room_id=_ROOM,
        agents=agents,
        correlation_id="background-script:script-abc:call-1",
    )
    with tool_runtime_context(ctx_broker):
        broker_upd = json.loads(
            await tool.triggers(
                action="update",
                trigger_id="sc_owner_bound",
                source="def check():\n    return {'broker': 1}\n",
            ),
        )
        broker_list = json.loads(await tool.triggers(action="list"))
    assert broker_upd["status"] == "error"
    assert "background script or workflow" in broker_upd["message"]
    assert broker_list["status"] == "ok"

    # Active detached ToolCallContext (as used by ScriptToolBroker / MCP gateway) blocks watch_script, allows list
    adapted = adapt_toolkit(tool, key="triggers", registered="triggers")
    fn = adapted.functions[0]
    detached_call_ctx = ToolCallContext(run_id="detached-1", session_id=DETACHED_SESSION_ID, user_id=_OWNER)
    with tool_runtime_context(ctx_orig):
        detached_watch = await run_tool_call(
            fn,
            {"action": "watch_script", "prompt": "p", "source": _SAMPLE_SOURCE},
            detached_call_ctx,
            middleware=(),
        )
        detached_list = await run_tool_call(fn, {"action": "list"}, detached_call_ctx, middleware=())
    assert json.loads(str(detached_watch.value))["status"] == "error"
    assert "detached tool contexts" in json.loads(str(detached_watch.value))["message"]
    assert json.loads(str(detached_list.value))["status"] == "ok"


@pytest.mark.asyncio
async def test_triggers_timed_approval_grant_never_reused_for_script_check_code(tmp_path: Path) -> None:
    """Fix 3: timed triggers/update metadata grant never matches watch_script or code-bearing triggers/update."""
    cfg = _config()
    registry = builtin_tool_registry()
    meta_update_op = grant_operation(
        cfg,
        "triggers",
        {"action": "update", "trigger_id": "t1", "description": "desc"},
        tools=registry,
        toolkit_name="triggers",
    )
    assert meta_update_op is not None
    assert (meta_update_op.mcp_server_id, meta_update_op.mcp_tool_name) == ("triggers", "update")
    for code_args in (
        {"action": "watch_script", "prompt": "p", "source": _SAMPLE_SOURCE},
        {"action": "watch_script", "prompt": "p", "path": "check.py"},
        {"action": "update", "trigger_id": "t1", "source": _SAMPLE_SOURCE},
        {"action": "update", "trigger_id": "t1", "path": "check.py"},
    ):
        assert grant_operation(cfg, "triggers", code_args, tools=registry, toolkit_name="triggers") is None

    # End-to-end timed grant reuse check via EventJournalStore + ApprovalManager
    journal = EventJournalStore.open_sqlite(tmp_path / "event_journal.db")
    manager = approval_manager_for_test(
        test_runtime_paths(tmp_path),
        cards=journal.principal("router@shared"),
        prepare_approval_event=AsyncMock(side_effect=lambda _r, _t, content, _s: content),
        send_approval_delivery=AsyncMock(
            side_effect=lambda d: "$" + d.delivery_id + ("-edit" if d.stage is DeliveryStage.FINAL else ""),
        ),
        sender_bot=lambda _e, _r: object(),
        responder_principal_id=lambda entity_name: journal_principal_id("agent", entity_name),
    )

    async def _submit_card(name: str, args: dict[str, object]) -> str:
        responder = journal.principal(journal_principal_id("agent", "coder"))
        await responder.admit(
            InboundEvent(
                event_id="$src-" + name,
                room_id=_ROOM,
                thread_id="$thread",
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender=_OWNER,
                origin_server_ts=1000,
                source={"type": "m.room.message", "content": {"msgtype": "m.text", "body": "run"}},
            ),
        )
        cont = approval_continuation(
            approval_id=name,
            run_id="run-" + name,
            session_id="sess-" + name,
            entity_name="coder",
            room_id=_ROOM,
            thread_id="$thread",
            requester_id=_OWNER,
            response_event_id="$wait-" + name,
            sources=ResponseSources(("$src-" + name,), ("$src-" + name,)),
            calls=(
                ApprovalCall(
                    tool_call_id="call-" + name,
                    tool_name="triggers",
                    invoking_agent="coder",
                    expires_at_ns=9_000_000_000_000_000_000,
                ),
            ),
            state="waiting",
            runtime_generation="runtime",
        )
        assert await paused_for_approval(responder, cont) is not None
        op = grant_operation(cfg, "triggers", args, tools=registry, toolkit_name="triggers")
        card = await manager.prepare_reply_card(
            approval_id="card-" + name,
            continuation_id=name,
            continuation_generation=0,
            entity_name="coder",
            response_event_id="$wait-" + name,
            tool_call_id="call-" + name,
            tool_name="triggers",
            arguments=args,
            room_id=_ROOM,
            requester_id=_OWNER,
            approver_user_id=_OWNER,
            expires_at_ns=9_000_000_000_000_000_000,
            agent_name="coder",
            thread_id="$thread",
            grant_operation=op,
        )
        assert card is not None
        assert await manager.reserve_and_publish(
            continuation_principal_id=responder.principal_id,
            continuation_id=name,
            continuation_generation=0,
            cards=(card,),
        )
        return "$card-" + name

    async def _call_decision(name: str) -> tuple[object, object]:
        row = await journal.backend.read(
            lambda tx: tx.fetchone("SELECT decision, resolved_by FROM approval_calls WHERE owner_id = ?", (name,)),
        )
        assert row is not None
        return row["decision"], row["resolved_by"]

    try:
        first_card = await _submit_card("upd-meta-1", {"action": "update", "trigger_id": "t1", "description": "a"})
        approved = await handle_card_response_for_test(
            manager,
            room_id=_ROOM,
            sender_id=_OWNER,
            card_event_id=first_card,
            status="approved",
            auto_approve_seconds=600,
            authorize_responder=lambda _a: True,
        )
        assert approved.consumed
        # Another metadata-only update reuses the timed grant
        await _submit_card("upd-meta-2", {"action": "update", "trigger_id": "t1", "description": "b"})
        assert await _call_decision("upd-meta-2") == ("approved", _OWNER)
        # Code replacement via source/path and watch_script MUST NOT reuse the timed grant
        await _submit_card("upd-source", {"action": "update", "trigger_id": "t1", "source": _SAMPLE_SOURCE})
        await _submit_card("upd-path", {"action": "update", "trigger_id": "t1", "path": "check.py"})
        await _submit_card("watch-script", {"action": "watch_script", "prompt": "p", "source": _SAMPLE_SOURCE})
        assert await _call_decision("upd-source") == (None, None)
        assert await _call_decision("upd-path") == (None, None)
        assert await _call_decision("watch-script") == (None, None)
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_authoritative_room_membership_and_placement_across_tick_dispatch_and_history(tmp_path: Path) -> None:
    """Fix 4: live source-room membership, reply authority, and placement are enforced across tick, dispatch, and history."""
    # Direct user grant: is_sender_allowed_for_agent_reply_in_room returns True even after the owner leaves the room
    ctx = _script_context(tmp_path / "auth", tools=["triggers", "script"], allow_local=True)
    ctx_no_place = _script_context(tmp_path / "auth_no_place", tools=["triggers", "script"], allow_local=False)
    bot = _FakeBot(ctx, send_log=[])
    memberships = AgentReplyMembershipIndex()
    s_store = ScriptRunStore(ctx.runtime_paths)
    broker = ScriptToolBroker(store=s_store, runtime_resolver=_SimpleResolver(ctx))
    manager = ScriptRunManager(
        store=s_store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="http://127.0.0.1:8765",
    )
    runner = build_script_check_tick_runner(
        runtime_paths=ctx.runtime_paths,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        manager_provider=lambda: manager,
        broker=broker,
    )
    rule = _sc_rule("sc_auth", agent="coder")
    cp = _sc_cp().model_copy(
        update={"identity_json": {**serialize_tool_execution_identity(ctx.execution_identity), "room_id": _ROOM}},
    )
    ev = TriggerEvent(
        source_type="script_check",
        source_id=rule.trigger_id,
        kind=_SCRIPT_CHECK_EVENT_KIND,
        event_id="e_auth",
        timestamp=1.0,
    )

    # 1. Owner left source room -> tick unauthorized & dispatch skipped with source_room_not_joined (even with reply allow)
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[{"type": "m.room.member", "state_key": _OWNER, "content": {"membership": "leave"}}],
            room_id=_ROOM,
        ),
    )
    with patch("mindroom.orchestration.script_checks.is_sender_allowed_for_agent_reply_in_room", return_value=True):
        tick_left = await runner(rule, cp, ctx.config, 1000.0)
    assert (tick_left.status, tick_left.error_code) == ("unauthorized", "source_room_not_joined")
    with patch("mindroom.reactive_triggers.dispatch.is_sender_allowed_for_agent_reply_in_room", return_value=True):
        disp_left = await check_reactive_source_access(
            rule=rule,
            event=ev,
            source_bot=bot,  # type: ignore[arg-type]
            config=ctx.config,
            runtime_paths=ctx.runtime_paths,
            memberships=memberships,
        )
    assert disp_left is not None
    assert (disp_left.status, disp_left.reason) == ("skipped", "source_room_not_joined")

    # 2. Unreadable room state -> tick deferred & dispatch retry with source_room_state_unreadable
    bot.client.room_get_state = AsyncMock(return_value=nio.RoomGetStateError(message="denied"))
    tick_unread = await runner(rule, cp, ctx.config, 1000.0)
    assert (tick_unread.status, tick_unread.error_code) == ("deferred", "source_room_state_unreadable")
    disp_unread = await check_reactive_source_access(
        rule=rule,
        event=ev,
        source_bot=bot,  # type: ignore[arg-type]
        config=ctx.config,
        runtime_paths=ctx.runtime_paths,
        memberships=memberships,
    )
    assert disp_unread is not None
    assert (disp_unread.status, disp_unread.reason) == ("retry", "source_room_state_unreadable")

    # 3. Placement unavailable -> tick unauthorized & dispatch skipped with placement_unavailable
    bot.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[{"type": "m.room.member", "state_key": _OWNER, "content": {"membership": "join"}}],
            room_id=_ROOM,
        ),
    )
    runner_no_place = build_script_check_tick_runner(
        runtime_paths=ctx_no_place.runtime_paths,
        bots=lambda: {"coder": bot},  # type: ignore[arg-type,return-value]
        memberships=memberships,
        manager_provider=lambda: manager,
        broker=broker,
    )
    tick_np = await runner_no_place(rule, cp, ctx_no_place.config, 1000.0)
    assert (tick_np.status, tick_np.error_code) == ("unauthorized", "placement_unavailable")
    disp_np = await check_reactive_source_access(
        rule=rule,
        event=ev,
        source_bot=bot,  # type: ignore[arg-type]
        config=ctx_no_place.config,
        runtime_paths=ctx_no_place.runtime_paths,
        memberships=memberships,
    )
    assert disp_np is not None
    assert (disp_np.status, disp_np.reason) == ("skipped", "placement_unavailable")

    # 4. History denies access when owner left source.room_id or lost reply authority for source.agent
    tool = TriggersTools()
    h_ctx, _, _ = _tool_context_for_triggers(tmp_path / "hist")
    with tool_runtime_context(h_ctx):
        assert (
            json.loads(
                await tool.triggers(
                    action="watch_script",
                    trigger_id="sc_hist",
                    prompt="Alert",
                    source=_SAMPLE_SOURCE,
                ),
            )["status"]
            == "ok"
        )
    h_store = ReactiveTriggerStore(h_ctx.runtime_paths)
    h_rule = h_store.get_trigger("sc_hist")
    assert h_rule is not None
    h_store.update_script_check_checkpoint_and_enqueue(
        "sc_hist",
        _require_cp(h_store, "sc_hist"),
        event=ev.model_copy(update={"source_id": "sc_hist"}),
        expected_rule_uid=h_rule.rule_uid,
        expected_revision=1,
    )
    # Joined & authorized -> history allowed
    with tool_runtime_context(h_ctx):
        assert json.loads(await tool.triggers(action="history", trigger_id="sc_hist"))["status"] == "ok"
    # Owner left room -> history denied
    h_ctx.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[{"type": "m.room.member", "state_key": _OWNER, "content": {"membership": "leave"}}],
            room_id=_ROOM,
        ),
    )
    with tool_runtime_context(h_ctx):
        assert json.loads(await tool.triggers(action="history", trigger_id="sc_hist"))["status"] == "error"
    # Rejoined room but reply authority revoked -> history denied
    h_ctx.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            events=[{"type": "m.room.member", "state_key": _OWNER, "content": {"membership": "join"}}],
            room_id=_ROOM,
        ),
    )
    with (
        patch("mindroom.custom_tools.triggers.is_sender_allowed_for_agent_reply_in_room", return_value=False),
        tool_runtime_context(h_ctx),
    ):
        assert json.loads(await tool.triggers(action="history", trigger_id="sc_hist"))["status"] == "error"


@pytest.mark.asyncio
async def test_non_resumable_triggers_surface_preserves_non_code_actions_and_refuses_script_check(
    tmp_path: Path,
) -> None:
    """Fix 5 & Fix 9: non-resumable triggers stays offered under start_script: require_approval and fails closed."""
    # Verify _NON_CODE_TRIGGER_ACTIONS covers all non-code actions in _ACTION_ALLOWED_PARAMS
    assert _NON_CODE_TRIGGER_ACTIONS | {"watch_script", "update"} == set(_ACTION_ALLOWED_PARAMS)

    registry = builtin_tool_registry()
    triggers_alias = registry.entries["triggers"].approval_aliases["triggers"]
    for unknown in ("bogus_action", "watch_script_v2", "exec"):
        assert triggers_alias.judge({"action": unknown}) is None

    ctx, _, _ = _tool_context_for_triggers(tmp_path)
    cfg_start_script = bind_runtime_paths(
        ctx.config.model_copy(
            update={
                "tool_approval": ToolApprovalConfig(
                    default="auto_approve",
                    rules=[ApprovalRuleConfig(match="start_script", action="require_approval")],
                ),
            },
        ),
        ctx.runtime_paths,
    )
    ctx_gated = replace(ctx, config=cfg_start_script)
    toolkit = TriggersTools()
    adapted = adapt_toolkit(toolkit, key="triggers", registered="triggers")
    offered = apply_tool_approval_capability(
        adapted,
        toolkit,
        cfg_start_script,
        tools=registry,
        registered_tool_name="triggers",
        resumable=False,
        approvals_as_jobs=False,
    )
    assert [f.name for f in offered.functions] == ["triggers"]
    fn = offered.functions[0]
    assert fn.gate is None

    # Non-code actions succeed; watch_script, code-bearing update, and unknown action are refused
    with tool_runtime_context(ctx_gated):
        list_res = json.loads(await fn.entrypoint(action="list"))
        assert list_res["status"] == "ok"

        # watch_pr passes the non-resumable wrapper (reaches TriggersTools validation instead of approval refusal)
        watch_pr_res = json.loads(await fn.entrypoint(action="watch_pr"))
        assert watch_pr_res["status"] == "error"
        assert "repo is required" in watch_pr_res["message"]

        # watch_script refused by approval wrapper
        ws_res = json.loads(await fn.entrypoint(action="watch_script", prompt="p", source=_SAMPLE_SOURCE))
        assert ws_res["status"] == "error"
        assert "non-resumable" in ws_res["message"]

        # code-bearing update refused by approval wrapper
        upd_src_res = json.loads(await fn.entrypoint(action="update", trigger_id="t1", source=_SAMPLE_SOURCE))
        assert upd_src_res["status"] == "error"
        assert "non-resumable" in upd_src_res["message"]
        upd_path_res = json.loads(await fn.entrypoint(action="update", trigger_id="t1", path="check.py"))
        assert upd_path_res["status"] == "error"
        assert "non-resumable" in upd_path_res["message"]

        # unknown action fails closed in non-resumable wrapper
        unknown_res = json.loads(await fn.entrypoint(action="unknown_action"))
        assert unknown_res["status"] == "error"
        assert "non-resumable" in unknown_res["message"]

    # Without runtime context (context is None), empty judge sequence runs original; non-empty/None is refused
    ws_no_ctx = json.loads(await fn.entrypoint(action="watch_script", prompt="p", source=_SAMPLE_SOURCE))
    assert ws_no_ctx["status"] == "error"
    assert "non-resumable" in ws_no_ctx["message"]
    list_no_ctx = json.loads(await fn.entrypoint(action="list"))
    assert list_no_ctx["status"] == "error"
    assert "live Matrix tool context" in list_no_ctx["message"]

    # When triggers' OWN rule requires approval, non-resumable still hides triggers completely
    cfg_own = bind_runtime_paths(
        ctx.config.model_copy(
            update={
                "tool_approval": ToolApprovalConfig(
                    default="auto_approve",
                    rules=[ApprovalRuleConfig(match="triggers", action="require_approval")],
                ),
            },
        ),
        ctx.runtime_paths,
    )
    hidden = apply_tool_approval_capability(
        adapted,
        toolkit,
        cfg_own,
        tools=registry,
        registered_tool_name="triggers",
        resumable=False,
        approvals_as_jobs=False,
    )
    assert hidden.functions == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["disabled", "deleted", "expired", "replaced", "revision_changed"])
async def test_poller_rechecks_live_rule_and_checkpoint_after_owner_slot(tmp_path: Path, mutation: str) -> None:
    """Fix 6: a queued rule mutated while waiting on the per-owner slot is skipped without launching."""
    paths = _runtime_paths(tmp_path)
    store = ReactiveTriggerStore(paths)
    cfg = _config()
    r1 = _sc_rule("t_first", owner=_OWNER)
    r2 = _sc_rule("t_second", owner=_OWNER)
    store.create_trigger(r1, initial_script_check_checkpoint=_sc_cp(next_poll_at=900.0))
    store.create_trigger(r2, initial_script_check_checkpoint=_sc_cp(next_poll_at=901.0))

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    launched: list[str] = []

    async def _runner(
        rule: ReactiveTriggerRecord,
        _cp: ScriptCheckCheckpoint,
        _c: Config,
        _now: float,
    ) -> ScriptCheckTickOutcome:
        launched.append(rule.trigger_id)
        if rule.trigger_id == "t_first":
            first_started.set()
            await release_first.wait()
        return ScriptCheckTickOutcome(status="ok", run_id=f"r-{rule.trigger_id}", result=None)

    poller = ScriptCheckPoller(
        store=store,
        config_provider=lambda: cfg,
        runtime_paths=paths,
        tick_runner=_runner,
    )
    task = asyncio.create_task(poller.poll_due_once(now=1000.0))
    await first_started.wait()

    if mutation == "disabled":
        store.set_enabled("t_second", False)
    elif mutation == "deleted":
        store.delete_trigger("t_second")
    elif mutation == "expired":
        store.update_trigger(r2.model_copy(update={"expires_at": 999.0}), expected_rule_uid=r2.rule_uid)
    elif mutation == "replaced":
        store.delete_trigger("t_second")
        store.create_trigger(
            _sc_rule("t_second", owner=_OWNER),
            initial_script_check_checkpoint=_sc_cp(next_poll_at=901.0),
        )
    elif mutation == "revision_changed":
        cp2 = _require_cp(store, "t_second")
        store.upsert_script_check_checkpoint(
            "t_second",
            cp2.model_copy(update={"revision": cp2.revision + 1}),
            expected_rule_uid=r2.rule_uid,
            expected_revision=cp2.revision,
        )

    release_first.set()
    assert await task == 0
    assert launched == ["t_first"]


def test_atomic_script_check_source_update_and_bootstrap_result_cap(tmp_path: Path) -> None:
    """Fix 7 & Fix 8: update_trigger commits rule+checkpoint atomically on matching revision and rolls back on conflict."""
    store = ReactiveTriggerStore(_runtime_paths(tmp_path))
    rule = _sc_rule("sc_atom")
    store.create_trigger(rule, initial_script_check_checkpoint=_sc_cp(state={"keep": 1}))
    cp1 = _require_cp(store, "sc_atom")
    new_src = "def check():\n    return {'v': 2}\n"
    new_digest = hashlib.sha256(new_src.encode()).hexdigest()
    assert isinstance(rule.source, ScriptCheckSourceSpec)
    updated_rule = rule.model_copy(update={"source": rule.source.model_copy(update={"source_digest": new_digest})})
    updated_cp = cp1.model_copy(update={"revision": 2, "source_utf8": new_src, "state": {}})

    # Revision conflict raises and rolls back BOTH reactive_triggers and script_check_checkpoints
    with pytest.raises(ReactiveTriggerStoreError, match="revision conflict"):
        store.update_trigger(
            updated_rule,
            expected_rule_uid=rule.rule_uid,
            updated_script_check_checkpoint=updated_cp,
            expected_script_check_revision=999,
        )
    live_rule = store.get_trigger("sc_atom")
    assert live_rule is not None
    assert isinstance(live_rule.source, ScriptCheckSourceSpec)
    assert live_rule.source.source_digest == _SAMPLE_DIGEST
    assert (_require_cp(store, "sc_atom").revision, _require_cp(store, "sc_atom").state) == (1, {"keep": 1})

    # Matching revision commits both atomically
    committed = store.update_trigger(
        updated_rule,
        expected_rule_uid=rule.rule_uid,
        updated_script_check_checkpoint=updated_cp,
        expected_script_check_revision=1,
    )
    assert committed is not None
    assert isinstance(committed.source, ScriptCheckSourceSpec)
    assert committed.source.source_digest == new_digest
    cp2 = _require_cp(store, "sc_atom")
    assert (cp2.revision, cp2.source_utf8, cp2.state) == (2, new_src, {})

    # Bootstrap result cap matches server _MAX_SCRIPT_CHECK_RESULT_BYTES (64 KiB)
    assert "64 * 1024" in _SCRIPT_CHECK_BOOTSTRAP_SOURCE
    assert _MAX_SCRIPT_CHECK_RESULT_BYTES == 64 * 1024
