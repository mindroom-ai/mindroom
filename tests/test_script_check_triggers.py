"""Tests for script_check reactive triggers, script_runs check mode, and gateway check routes."""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
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
from mindroom.reactive_triggers.management import refresh_reactive_state_on_reenable, set_trigger_enabled_sync
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

if TYPE_CHECKING:
    from pathlib import Path

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
