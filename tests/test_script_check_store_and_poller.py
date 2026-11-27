"""Tests for script_check policy/models, SQLite v4 migration, store revision/interval race, and poller."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.reactive_triggers.management import refresh_reactive_state_on_reenable
from mindroom.reactive_triggers.models import (
    GitHubPRCheckpoint,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    RoomMessageSourceSpec,
    ScriptCheckCheckpoint,
    ScriptCheckSourceSpec,
    TriggerEvent,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.script_check import (
    SCRIPT_CHECK_BOOTSTRAP_SOURCE,
    _agent_supports_script_check,
    build_script_check_dry_run_event,
    check_script_check_access,
    clamp_script_check_poll_interval,
    is_owner_joined_in_state,
    normalize_script_check_event,
    script_check_health_summary,
    validate_script_check_source,
    validate_script_check_state,
)
from mindroom.reactive_triggers.script_check_poller import ScriptCheckPoller, ScriptCheckTickOutcome
from mindroom.reactive_triggers.store import (
    ReactiveTriggerLimitError,
    ReactiveTriggerStore,
    ReactiveTriggerStoreError,
)
from mindroom.script_runs.models import ScriptRunRecord
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from tests.conftest import bind_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "@alice:example.org"
_ROOM = "!room:example.org"
_SRC = "def check():\n    return None\n"
_DIGEST = hashlib.sha256(_SRC.encode()).hexdigest()


def _paths(tmp_path: Path, *, allow_local: bool = False) -> RuntimePaths:
    env = {"MATRIX_HOMESERVER": "https://example.org", "MATRIX_SERVER_NAME": "example.org"}
    if allow_local:
        env["MINDROOM_ALLOW_UNSAFE_LOCAL_SCRIPTS"] = "1"
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env,
    )


def _cfg(paths: RuntimePaths, *, policy: dict[str, Any] | None = None, allow_local: bool = False) -> Config:
    return bind_runtime_paths(
        Config.model_validate(
            {
                "agents": {
                    "coder": {"display_name": "Coder", "tools": ["triggers", "script"]},
                    "planner": {"display_name": "Planner", "tools": ["triggers", "workflow"]},
                    "watcher": {"display_name": "Watcher", "tools": ["triggers"]},
                },
                "defaults": {"allow_shell_without_worker": allow_local},
                "external_trigger_policy": policy or {},
            },
        ),
        paths,
    )


def _sc_rule(tid: str = "sc_1", *, owner: str = _OWNER, poll_s: int = 300) -> ReactiveTriggerRecord:
    return ReactiveTriggerRecord(
        trigger_id=tid,
        owner_user_id=owner,
        source=ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest=_DIGEST, poll_interval_seconds=poll_s),
        target=TriggerTargetSpec(agent="coder", room_id=_ROOM, prompt="Handle check"),
        created_at=1000.0,
        updated_at=1000.0,
    )


def _sc_cp(
    *,
    source: str = _SRC,
    next_poll_at: float | None = 100.0,
    last_run_at: float | None = None,
    errors: int = 0,
    code: str | None = None,
    state: dict[str, Any] | None = None,
) -> ScriptCheckCheckpoint:
    return ScriptCheckCheckpoint(
        source_utf8=source,
        state=state or {},
        last_run_at=last_run_at,
        next_poll_at=next_poll_at,
        consecutive_errors=errors,
        last_error_code=code,
        updated_at=1000.0,
    )


def _req_cp(store: ReactiveTriggerStore, tid: str) -> ScriptCheckCheckpoint:
    cp = store.get_script_check_checkpoint(tid)
    assert cp is not None
    return cp


def test_script_check_policy_models_and_access(tmp_path: Path) -> None:
    """Policy defaults/validators, spec/checkpoint models, and unified check_script_check_access enforce bounds."""
    pol = ExternalTriggerPolicyConfig()
    assert (pol.script_check_enabled, pol.min_script_check_poll_interval_seconds, pol.max_script_checks_per_owner) == (
        True,
        60,
        10,
    )
    assert (pol.script_check_timeout_seconds, pol.script_check_allowed_tools) == (30, None)
    assert ExternalTriggerPolicyConfig(enabled=True, reactive_enabled=False).is_reactive_active is False
    for kwargs in (
        {"min_script_check_poll_interval_seconds": 9},
        {"min_script_check_poll_interval_seconds": 86401},
        {"script_check_timeout_seconds": 4},
    ):
        with pytest.raises(ValidationError):
            ExternalTriggerPolicyConfig(**kwargs)
    with pytest.raises(ValidationError):
        ScriptCheckSourceSpec(agent="coder", room_id=_ROOM, source_digest="not-hex")

    assert (
        clamp_script_check_poll_interval(None, min_seconds=60),
        clamp_script_check_poll_interval(15, min_seconds=60),
    ) == (300, 60)
    assert clamp_script_check_poll_interval(999999, min_seconds=60) == 86400
    with pytest.raises(TypeError, match="must be an integer"):
        clamp_script_check_poll_interval(True, min_seconds=60)

    p_no, p_yes = _paths(tmp_path / "no"), _paths(tmp_path / "yes", allow_local=True)
    cfg, cfg_local = _cfg(p_no), _cfg(p_yes, allow_local=True)
    assert (
        _agent_supports_script_check(cfg, "coder"),
        _agent_supports_script_check(cfg, "planner"),
        _agent_supports_script_check(cfg, "watcher"),
    ) == (
        True,
        True,
        False,
    )
    for pol_over, expected_code in (
        ({"enabled": False}, "external_triggers_disabled"),
        ({"reactive_enabled": False}, "reactive_triggers_disabled"),
        ({"script_check_enabled": False}, "script_check_disabled"),
    ):
        fail = check_script_check_access(_cfg(p_yes, policy=pol_over, allow_local=True), p_yes, "coder")
        assert fail is not None
        assert fail.code == expected_code
    f_bot = check_script_check_access(cfg, p_yes, "coder", owner_user_id="@mindroom_coder:example.org")
    assert f_bot is not None
    assert f_bot.code == "owner_not_human"
    f_agent = check_script_check_access(cfg, p_yes, "watcher", owner_user_id=_OWNER)
    assert f_agent is not None
    assert f_agent.category == "agent"
    f_place = check_script_check_access(cfg, p_no, "coder", owner_user_id=_OWNER, require_placement=True)
    assert f_place is not None
    assert f_place.category == "placement"
    assert check_script_check_access(cfg, p_no, "coder", owner_user_id=_OWNER, require_placement=False) is None
    assert check_script_check_access(cfg_local, p_yes, "coder", owner_user_id=_OWNER, require_placement=True) is None

    joined = RoomStateSnapshot(
        room_id=_ROOM,
        creators=frozenset(),
        events={("m.room.member", _OWNER): {"membership": "join"}},
    )
    left = RoomStateSnapshot(
        room_id=_ROOM,
        creators=frozenset(),
        events={("m.room.member", _OWNER): {"membership": "leave"}},
    )
    assert is_owner_joined_in_state(joined, owner_user_id=_OWNER, config=cfg, runtime_paths=p_yes) is True
    assert is_owner_joined_in_state(left, owner_user_id=_OWNER, config=cfg, runtime_paths=p_yes) is False


def test_validate_source_state_and_normalize_event() -> None:
    """AST validation, finite-JSON state checks, event normalization, and bootstrap result cap work cleanly."""
    assert "64 * 1024" in SCRIPT_CHECK_BOOTSTRAP_SOURCE
    text, digest = validate_script_check_source(b"def check():\n    return {'ok': True}\n")
    assert digest == hashlib.sha256(text.encode()).hexdigest()
    validate_script_check_source(b"def check(*, y=2, **kw):\n    return None\n")
    for bad in (
        b"",
        b"x = 1\n",
        b"async def check():\n    return None\n",
        b"def check(a):\n    return None\n",
        b"def check(:\n",
        b"\xff",
        b"#" * (65 * 1024),
    ):
        with pytest.raises(ValueError, match=r"script_check|check|UTF-8|syntax"):
            validate_script_check_source(bad)

    assert validate_script_check_state({"cursor": "abc", "n": 3}) == {"cursor": "abc", "n": 3}
    for bad_state in ([1, 2], {"x": float("nan")}, {1: "a"}, {"big": "x" * (33 * 1024)}):
        with pytest.raises(ValueError, match=r"state|JSON|NaN|string|bytes"):
            validate_script_check_state(bad_state)

    ev_raw = normalize_script_check_event("sc_1", "run-1", {"count": 5}, now=1234.0)
    assert (ev_raw.source_type, ev_raw.kind, ev_raw.event_id, ev_raw.data) == (
        "script_check",
        "script_check.match",
        "sc:tick:run-1",
        {"count": 5},
    )
    ev_env = normalize_script_check_event(
        "sc_1",
        "run-2",
        {
            "title": "Deploy",
            "summary": "Done",
            "text": "Details",
            "actor": "ci",
            "dedupe_key": "d-1",
            "data": {"ok": True},
        },
        now=1235.0,
    )
    assert ev_env.event_id.startswith("sc:key:")
    assert (
        normalize_script_check_event("sc_1", "run-3", {"dedupe_key": "d-1", "data": {}}, now=1236.0).event_id
        == ev_env.event_id
    )
    assert (ev_env.title, ev_env.summary, ev_env.text, ev_env.actor, ev_env.data) == (
        "Deploy",
        "Done",
        "Details",
        "ci",
        {"ok": True},
    )
    for bad_res in (
        [1, 2],
        {"x": float("inf")},
        {"big": "z" * (65 * 1024)},
        {"title": 123, "data": {}},
        {"title": "ok", "data": [1]},
    ):
        with pytest.raises(ValueError, match=r"result|JSON|object|string|bytes|NaN|Infinity"):
            normalize_script_check_event("sc_1", "run-x", bad_res, now=1.0)

    dry = build_script_check_dry_run_event(
        "sc_1",
        actor="alice",
        text="hi",
        sample_data={"title": "T", "k": 1},
        now=99.0,
    )
    assert (dry.event_id, dry.title, dry.actor, dry.data) == ("dry_run", "T", "alice", {"title": "T", "k": 1})
    assert script_check_health_summary(None) is None
    assert script_check_health_summary(_sc_cp(errors=2, code="timeout", last_run_at=50.0)) == {
        "consecutive_errors": 2,
        "last_error_code": "timeout",
        "last_run_at": 50.0,
    }


@pytest.mark.parametrize("legacy_version", [1, 2, 3])
def test_sqlite_migration_v1_v2_v3_to_v4_preserves_applied_migrations(tmp_path: Path, legacy_version: int) -> None:
    """SQLite auto-migrates v1/v2/v3 -> v4 idempotently, creating script_check_checkpoints and preserving applied_migrations."""
    paths = _paths(tmp_path)
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
    if legacy_version >= 2:
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
    if legacy_version >= 3:
        store.import_triggers_once("slack_watches_v1", [], now=1000.0)
        assert store.has_migration("slack_watches_v1") is True

    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("DROP TABLE script_check_checkpoints")
        if legacy_version < 3:
            conn.execute("DROP TABLE applied_migrations")
        if legacy_version < 2:
            conn.execute("DROP TABLE github_pr_checkpoints")
        conn.execute("UPDATE schema_version SET version = ?", (legacy_version,))
        conn.commit()

    upgraded = ReactiveTriggerStore(paths)
    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"github_pr_checkpoints", "applied_migrations", "script_check_checkpoints"} <= tables
    assert upgraded.get_trigger("legacy_room") is not None
    if legacy_version >= 2:
        assert upgraded.get_github_pr_checkpoint("legacy_pr") is not None
    if legacy_version >= 3:
        assert upgraded.has_migration("slack_watches_v1") is True

    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
        conn.commit()
    with pytest.raises(ReactiveTriggerStoreError, match="Unsupported reactive triggers schema version: 99"):
        ReactiveTriggerStore(paths)


def test_store_script_check_quotas_atomic_revision_and_interval_race(tmp_path: Path) -> None:
    """Store enforces quotas, atomic revision checks, and bumps checkpoint.revision on interval edits (Astra #6)."""
    store = ReactiveTriggerStore(_paths(tmp_path))
    r1 = store.create_trigger(
        _sc_rule("sc_1", poll_s=300),
        initial_script_check_checkpoint=_sc_cp(next_poll_at=100.0, last_run_at=1000.0, state={"cursor": 7}),
        max_script_check_per_owner=1,
    )
    with pytest.raises(ReactiveTriggerLimitError, match="script_check trigger limit"):
        store.create_trigger(_sc_rule("sc_2"), initial_script_check_checkpoint=_sc_cp(), max_script_check_per_owner=1)
    assert [r.trigger_id for r, _ in store.list_due_script_check_triggers(now=200.0)] == ["sc_1"]

    # Astra #6: editing poll_interval_seconds bumps checkpoint.revision atomically inside store.update_trigger
    edited = r1.model_copy(
        update={"source": r1.source.model_copy(update={"poll_interval_seconds": 60}), "updated_at": 1020.0},
    )
    updated = store.update_trigger(edited, expected_rule_uid=r1.rule_uid)
    assert updated is not None
    cp_edit = _req_cp(store, "sc_1")
    assert (cp_edit.revision, cp_edit.state, cp_edit.next_poll_at) == (2, {"cursor": 7}, 1060.0)

    # Stale in-flight tick (expected_revision=1) cannot overwrite next_poll_at with the old 300s interval
    stale_cp = cp_edit.model_copy(update={"revision": 1, "next_poll_at": 1320.0, "state": {"cursor": 9}})
    ev = TriggerEvent(
        source_type="script_check",
        source_id="sc_1",
        kind="script_check.match",
        event_id="sc:stale",
        timestamp=1025.0,
    )
    assert (
        store.update_script_check_checkpoint_and_enqueue(
            "sc_1",
            stale_cp,
            event=ev,
            expected_rule_uid=r1.rule_uid,
            expected_revision=1,
        )
        is False
    )
    assert (_req_cp(store, "sc_1").next_poll_at, store.list_firings(trigger_id="sc_1")) == (1060.0, [])

    # Fresh tick (expected_revision=2) commits state + firing atomically
    fresh_cp = cp_edit.model_copy(update={"last_run_at": 1060.0, "next_poll_at": 1120.0, "state": {"cursor": 10}})
    assert (
        store.update_script_check_checkpoint_and_enqueue(
            "sc_1",
            fresh_cp,
            event=ev,
            expected_rule_uid=r1.rule_uid,
            expected_revision=2,
        )
        is True
    )
    assert (_req_cp(store, "sc_1").state, len(store.list_firings(trigger_id="sc_1"))) == ({"cursor": 10}, 1)

    # Re-enable via update_trigger and refresh_reactive_state_on_reenable both bump revision and clear errors
    store.set_enabled("sc_1", False)
    store.upsert_script_check_checkpoint(
        "sc_1",
        _req_cp(store, "sc_1").model_copy(update={"consecutive_errors": 4, "last_error_code": "timeout"}),
    )
    reenabled = store.update_trigger(
        updated.model_copy(update={"enabled": True, "disabled_reason": None, "updated_at": 1200.0}),
        expected_rule_uid=r1.rule_uid,
        update_enabled=True,
    )
    assert reenabled is not None
    assert reenabled.enabled is True
    cp_re = _req_cp(store, "sc_1")
    assert (cp_re.revision, cp_re.consecutive_errors, cp_re.last_error_code, cp_re.next_poll_at) == (4, 0, None, 1200.0)

    store.upsert_script_check_checkpoint(
        "sc_1",
        cp_re.model_copy(update={"consecutive_errors": 2, "last_error_code": "script_error"}),
    )
    refresh_reactive_state_on_reenable(store, reenabled, now=1300.0, refreshed_tag_present=None)
    cp_ref = _req_cp(store, "sc_1")
    assert (cp_ref.revision, cp_ref.consecutive_errors, cp_ref.last_error_code, cp_ref.next_poll_at) == (
        5,
        0,
        None,
        1300.0,
    )
    assert (store.delete_trigger("sc_1"), store.get_script_check_checkpoint("sc_1")) == (True, None)


@pytest.mark.asyncio
async def test_script_check_poller_outcomes_concurrency_and_live_recheck(tmp_path: Path) -> None:
    """Poller handles all tick outcomes, deferred error codes, per-owner serialization, and live recheck after slot wait."""
    paths = _paths(tmp_path, allow_local=True)
    store = ReactiveTriggerStore(paths)
    cfg = _cfg(paths, allow_local=True)
    enqueued_cb = MagicMock()

    store.create_trigger(_sc_rule("sc_ok", poll_s=120), initial_script_check_checkpoint=_sc_cp(next_poll_at=100.0))
    outcomes = iter(
        [
            ScriptCheckTickOutcome(status="ok", run_id="r1", result=None, state={"seen": 1}),
            ScriptCheckTickOutcome(status="ok", run_id="r2", result={"hits": 2}, state={"seen": 2}),
            ScriptCheckTickOutcome(status="deferred", run_id="r3", error_code="runtime_unavailable"),
            ScriptCheckTickOutcome(status="error", run_id="r4", error_code="timeout"),
            ScriptCheckTickOutcome(status="unauthorized", run_id="r5", error_code="owner_not_authorized"),
        ],
    )

    async def runner(
        _rule: ReactiveTriggerRecord,
        _cp: ScriptCheckCheckpoint,
        _cfg: Config,
        _now: float,
    ) -> ScriptCheckTickOutcome:
        return next(outcomes)

    poller = ScriptCheckPoller(
        store=store,
        config_provider=lambda: cfg,
        runtime_paths=paths,
        tick_runner=runner,
        on_enqueued=enqueued_cb,
    )
    poller_off = ScriptCheckPoller(
        store=store,
        config_provider=lambda: _cfg(paths, policy={"reactive_enabled": False}, allow_local=True),
        runtime_paths=paths,
        tick_runner=runner,
    )
    assert await poller_off.poll_due_once(now=200.0) == 0

    assert await poller.poll_due_once(now=1000.0) == 0
    cp1 = _req_cp(store, "sc_ok")
    assert cp1.state == {"seen": 1}
    assert cp1.next_poll_at is not None
    assert 1108.0 <= cp1.next_poll_at <= 1132.0

    assert await poller.poll_due_once(now=1200.0) == 1
    assert (enqueued_cb.call_count, _req_cp(store, "sc_ok").state) == (1, {"seen": 2})

    assert await poller.poll_due_once(now=1400.0) == 0
    cp_def = _req_cp(store, "sc_ok")
    assert (cp_def.consecutive_errors, cp_def.last_error_code, cp_def.next_poll_at) == (
        0,
        "runtime_unavailable",
        1430.0,
    )

    assert await poller.poll_due_once(now=1500.0) == 0
    cp_err = _req_cp(store, "sc_ok")
    assert (cp_err.consecutive_errors, cp_err.last_error_code) == (1, "timeout")
    assert cp_err.next_poll_at is not None
    assert cp_err.next_poll_at >= 1500.0 + 120.0 * 1.5

    assert await poller.poll_due_once(now=5000.0) == 0
    cp_unauth = _req_cp(store, "sc_ok")
    assert (cp_unauth.consecutive_errors, cp_unauth.last_error_code) == (1, "owner_not_authorized")


@pytest.mark.asyncio
async def test_script_check_poller_owner_serialization_and_live_recheck(tmp_path: Path) -> None:
    """Different owners run concurrently while same-owner ticks serialize and recheck live rule after slot wait."""
    paths = _paths(tmp_path, allow_local=True)
    store = ReactiveTriggerStore(paths)
    cfg = _cfg(paths, allow_local=True)
    store.create_trigger(_sc_rule("a1", owner=_OWNER), initial_script_check_checkpoint=_sc_cp(next_poll_at=100.0))
    r_a2 = store.create_trigger(
        _sc_rule("a2", owner=_OWNER),
        initial_script_check_checkpoint=_sc_cp(next_poll_at=101.0),
    )
    store.create_trigger(
        _sc_rule("b1", owner="@bob:example.org"),
        initial_script_check_checkpoint=_sc_cp(next_poll_at=102.0),
    )

    a1_started, b1_started, release_a1 = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ran: list[str] = []

    async def serial_runner(
        rule: ReactiveTriggerRecord,
        _cp: ScriptCheckCheckpoint,
        _cfg: Config,
        _now: float,
    ) -> ScriptCheckTickOutcome:
        ran.append(rule.trigger_id)
        if rule.trigger_id == "a1":
            a1_started.set()
            await release_a1.wait()
        elif rule.trigger_id == "b1":
            b1_started.set()
        return ScriptCheckTickOutcome(status="ok", run_id=f"run-{rule.trigger_id}")

    poller = ScriptCheckPoller(
        store=store,
        config_provider=lambda: cfg,
        runtime_paths=paths,
        tick_runner=serial_runner,
        max_concurrency=4,
    )
    poll_task = asyncio.create_task(poller.poll_due_once(now=200.0))
    await asyncio.wait_for(asyncio.gather(a1_started.wait(), b1_started.wait()), timeout=2.0)
    assert "a2" not in ran
    store.set_enabled(r_a2.trigger_id, False)
    release_a1.set()
    await poll_task
    assert set(ran) == {"a1", "b1"}


def test_script_runs_store_migrates_script_check_rule_uid(tmp_path: Path) -> None:
    """ScriptRunStore adds script_check_rule_uid to legacy DBs and enforces plain/workflow/script_check discriminators."""
    paths = _paths(tmp_path)
    store = ScriptRunStore(paths)
    _, tok_hash = mint_script_capability()
    base = {
        "agent_name": "coder",
        "owner_user_id": _OWNER,
        "room_id": _ROOM,
        "thread_root_event_id": None,
        "execution_identity": {"channel": "matrix", "agent_name": "coder", "requester_id": _OWNER, "room_id": _ROOM},
        "source_digest": _DIGEST,
        "grants": (),
        "token_hash": tok_hash,
        "local_unsafe": True,
    }
    plain = store.create_run(ScriptRunRecord(run_id=f"script-{'1' * 32}", **base))  # type: ignore[arg-type]
    assert (plain.is_plain_script, plain.is_script_check) == (True, False)

    with closing(sqlite3.connect(store.database_path)) as conn:
        assert "script_check_rule_uid" in {row[1] for row in conn.execute("PRAGMA table_info(script_runs)").fetchall()}
        conn.execute("ALTER TABLE script_runs DROP COLUMN script_check_rule_uid")
        conn.commit()
    reopened = ScriptRunStore(paths)
    assert reopened.get_run(plain.run_id).script_check_rule_uid is None

    sc = reopened.create_run(ScriptRunRecord(run_id=f"script-{'2' * 32}", script_check_rule_uid="uid-2", **base))  # type: ignore[arg-type]
    assert (sc.is_script_check, sc.is_plain_script) == (True, False)
    with pytest.raises(ValueError, match="workflow_run_id and script_check_rule_uid"):
        ScriptRunRecord(run_id=f"script-{'3' * 32}", workflow_run_id="wf-1", script_check_rule_uid="uid-3", **base)  # type: ignore[arg-type]
