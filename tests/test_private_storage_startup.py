"""Automatic primary cutover and recovery before runtime admission."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from typer.testing import CliRunner

from mindroom import private_storage_startup as startup
from mindroom import private_storage_upgrade as upgrade
from mindroom.cli.main import app
from mindroom.constants import resolve_runtime_paths
from mindroom.file_locks import file_lock_is_held
from mindroom.workers.backend import WorkerBackendError
from tests.test_private_storage_upgrade import _legacy

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _paths(root: Path, sessions: Path | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=root.parent / "config.yaml",
        storage_path=root,
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(sessions)} if sessions else {},
    )


def _phase_fixture(base: Path, *, scopes: int = 2, separate: bool = True) -> tuple[RuntimePaths, list[Path]]:
    root, sessions = base / "state", base / "sessions" if separate else base / "state"
    root.mkdir(parents=True)
    sessions.mkdir(exist_ok=True)
    sources = []
    for index in range(scopes):
        source = _legacy(root, requester=f"@owner{index}:example.org", scope="user" if index % 2 else "user_agent")
        sources.append(source)
        database = sessions / source.relative_to(root) / "writer/sessions/writer.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        storage = SqliteDb(db_file=str(database), session_table="writer_sessions")
        storage.upsert_session(AgentSession(session_id="retained", agent_id="writer", session_data={"retained": True}))
        if index % 2:
            storage.upsert_run(RunOutput(run_id="retained-run", agent_id="writer", content="retained"), "retained")
        storage.db_engine.dispose()
    return _paths(root, sessions), sources


def _statuses(paths: RuntimePaths) -> list[str | None]:
    return [
        None if (receipt := upgrade._read_journal(root / upgrade._MARKER)) is None else receipt.status
        for root in dict.fromkeys(upgrade._runtime_roots(paths))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [1, 4])
@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_phase_receipt_writes_do_not_grow_with_scope_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scopes: int,
    separate: bool,
    reverse: bool,
) -> None:
    """Full-plan publication is bounded by participant phases, not move count."""
    paths, _sources = _phase_fixture(tmp_path, scopes=scopes, separate=separate)
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    if reverse:
        await startup.ensure_private_storage_ready(paths)
    writer = upgrade.write_json_file_durable
    writes = []

    def record(path: Path, payload: object, *, strict_atomic_replace: bool = False) -> None:
        assert isinstance(payload, dict)
        writes.append((path.parent, payload))
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    monkeypatch.setattr(upgrade, "write_json_file_durable", record)
    if reverse:
        plan = upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan
        upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    else:
        await startup.ensure_private_storage_ready(paths)
    volumes = len(set(upgrade._runtime_roots(paths)))
    phases = ("reversing", "rolled_back") if reverse else ("prepared", "moving", "complete")
    assert [payload["status"] for _root, payload in writes] == [phase for phase in phases for _ in range(volumes)]
    assert all(payload["plan"] == writes[0][1]["plan"] for _root, payload in writes)
    assert {root for root, _payload in writes} == set(upgrade._runtime_roots(paths))


@pytest.mark.asyncio
async def test_phase_optimization_keeps_five_deep_validation_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reducing receipts does not skip content, integrity, schema, or row validation."""
    paths, sources = _phase_fixture(tmp_path)
    inventory, snapshot = upgrade._inventory, upgrade._session_database_snapshot
    inventories, databases = [], []

    def inspected(root: Path, *, owner_temporary: Path | None = None, exclude_owner_record: bool = False) -> str:
        inventories.append(root)
        return inventory(root, owner_temporary=owner_temporary, exclude_owner_record=exclude_owner_record)

    def checked(database: Path, *, include_learning: bool = False) -> str:
        databases.append(database)
        return snapshot(database, include_learning=include_learning)

    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    monkeypatch.setattr(upgrade, "_inventory", inspected)
    monkeypatch.setattr(upgrade, "_session_database_snapshot", checked)
    await startup.ensure_private_storage_ready(paths)
    plan = upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan
    assert len(inventories) == 5 * sum(len(operation.moves) for operation in plan.operations)
    assert len(databases) == 5 * len(sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "moving", "complete", "reversing", "rolled_back"])
@pytest.mark.parametrize("participant", [0, 1])
async def test_each_phase_publication_recovers_on_both_volumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    participant: int,
) -> None:
    """Each individual durable participant write may be the last one before a crash."""
    paths, sources = _phase_fixture(tmp_path)
    roots = upgrade._runtime_roots(paths)
    original_owners = [(source / upgrade._RECORD_FILENAME).read_bytes() for source in sources]
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    reverse = phase in {"reversing", "rolled_back"}
    if reverse:
        await startup.ensure_private_storage_ready(paths)
    writer = upgrade.write_json_file_durable
    interrupted = False

    def crash(path: Path, payload: object, *, strict_atomic_replace: bool = False) -> None:
        nonlocal interrupted
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        assert isinstance(payload, dict)
        if payload["status"] == phase and path.parent == roots[participant]:
            if phase == "moving":
                assert all(source.exists() for source in sources), "Moving must be durable before the first rename"
            interrupted = True
            message = "phase publication interruption"
            raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "write_json_file_durable", crash)
        with pytest.raises(OSError, match="phase publication interruption"):  # noqa: PT012
            if reverse:
                plan = upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan
                upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
            else:
                await startup.ensure_private_storage_ready(paths)
    assert interrupted
    if reverse:
        with pytest.raises(upgrade.StorageUpgradeError, match=r"[Rr]oll|reversal"):
            await startup.ensure_private_storage_ready(paths)
        assert _statuses(paths) == ["rolled_back", "rolled_back"]
        assert [(source / upgrade._RECORD_FILENAME).read_bytes() for source in sources] == original_owners
    else:
        await startup.ensure_private_storage_ready(paths)
        assert _statuses(paths) == ["complete", "complete"]
        upgrade.verify_storage_upgrade(upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_every_observed_mutation_checkpoint_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reverse: bool,
) -> None:
    """Enumerate the real checkpoint trace, including owner writes and unsynced renames."""
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    paths, _sources = _phase_fixture(tmp_path / "trace")
    if reverse:
        await startup.ensure_private_storage_ready(paths)
    trace = []

    def recorded() -> None:
        frame = inspect.currentframe()
        assert frame is not None
        assert frame.f_back is not None
        trace.append(frame.f_back.f_code.co_name)

    with monkeypatch.context() as recorder:
        recorder.setattr(upgrade, "_checkpoint", recorded)
        if reverse:
            upgrade.rollback_storage_upgrade(
                upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan,
                writers_stopped=True,
            )
        else:
            await startup.ensure_private_storage_ready(paths)
    assert set(trace) == {"_publish", "_rename_offline", "_write_record"}
    plan = upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan
    assert trace.count("_publish") == (2 if reverse else 3) * len(plan.volumes)
    assert trace.count("_rename_offline") == 2 * sum(len(operation.moves) for operation in plan.operations)
    assert trace.count("_write_record") == 4 * len(plan.operations)
    for boundary in range(1, len(trace) + 1):
        paths, sources = _phase_fixture(tmp_path / f"fault-{boundary}")
        originals = [(source / upgrade._RECORD_FILENAME).read_bytes() for source in sources]
        if reverse:
            await startup.ensure_private_storage_ready(paths)
        calls = 0

        def crash(*, stop_at: int = boundary) -> None:
            nonlocal calls
            calls += 1
            if calls == stop_at:
                message = "observed checkpoint interruption"
                raise OSError(message)

        with monkeypatch.context() as fault:
            fault.setattr(upgrade, "_checkpoint", crash)
            with pytest.raises(OSError, match="observed checkpoint interruption"):  # noqa: PT012
                if reverse:
                    upgrade.rollback_storage_upgrade(
                        upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan,
                        writers_stopped=True,
                    )
                else:
                    await startup.ensure_private_storage_ready(paths)
        assert calls == boundary
        if reverse:
            with pytest.raises(upgrade.StorageUpgradeError):
                await startup.ensure_private_storage_ready(paths)
            assert _statuses(paths) == ["rolled_back", "rolled_back"]
            assert [(source / upgrade._RECORD_FILENAME).read_bytes() for source in sources] == originals
        else:
            await startup.ensure_private_storage_ready(paths)
            assert _statuses(paths) == ["complete", "complete"]
            upgrade.verify_storage_upgrade(upgrade._read_journal(paths.storage_root / upgrade._MARKER).plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "reversing"])
@pytest.mark.parametrize("participant", [0, 1])
async def test_partial_preparation_is_completed_before_reversal_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    participant: int,
) -> None:
    """An interrupted initial preparation must not become reversing plus a missing marker."""
    paths, sources = _phase_fixture(tmp_path)
    roots = upgrade._runtime_roots(paths)
    originals = [(source / upgrade._RECORD_FILENAME).read_bytes() for source in sources]
    plan = upgrade.plan_storage_upgrade(*roots)
    writer = upgrade.write_json_file_durable
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)

    def first_marker(path: Path, payload: object, *, strict_atomic_replace: bool = False) -> None:
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        message = "initial publication interruption"
        raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "write_json_file_durable", first_marker)
        with pytest.raises(OSError, match="initial publication interruption"):
            upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    assert _statuses(paths) == ["prepared", None]
    interrupted = False

    def reverse_marker(path: Path, payload: object, *, strict_atomic_replace: bool = False) -> None:
        nonlocal interrupted
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        assert isinstance(payload, dict)
        if payload["status"] == phase and path.parent == roots[participant]:
            interrupted = True
            message = "reversal preparation interruption"
            raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "write_json_file_durable", reverse_marker)
        with pytest.raises(OSError, match="reversal preparation interruption"):
            upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert interrupted
    if phase == "reversing":
        assert None not in _statuses(paths)
        with pytest.raises(upgrade.StorageUpgradeError, match="reversal completed"):
            await startup.ensure_private_storage_ready(paths)
        assert _statuses(paths) == ["rolled_back", "rolled_back"]
        assert [(source / upgrade._RECORD_FILENAME).read_bytes() for source in sources] == originals
    else:
        # No reversing receipt exists yet, so forward intent remains authoritative.
        await startup.ensure_private_storage_ready(paths)
        assert _statuses(paths) == ["complete", "complete"]


@pytest.mark.parametrize("intent", ["reversing", "rolled_back"])
def test_missing_participation_never_erases_recorded_reversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
) -> None:
    """Prepared seeding must never turn an already recorded reversal into forward intent."""
    paths, _sources = _phase_fixture(tmp_path)
    roots = upgrade._runtime_roots(paths)
    plan = upgrade.plan_storage_upgrade(*roots)
    for root in roots:
        upgrade.write_json_file_durable(
            root / upgrade._MARKER,
            {"plan": plan.model_dump(mode="json"), "status": intent},
        )
    (roots[1] / upgrade._MARKER).unlink()
    writer = upgrade.write_json_file_durable
    phases = []

    def written(path: Path, payload: object, *, strict_atomic_replace: bool = False) -> None:
        assert isinstance(payload, dict)
        phases.append(payload["status"])
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    monkeypatch.setattr(upgrade, "write_json_file_durable", written)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert "prepared" not in phases
    assert _statuses(paths) == ["rolled_back", "rolled_back"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["user", "user_agent"])
@pytest.mark.parametrize("with_runs", [False, True])
async def test_automatic_startup_preserves_named_sessions_and_quiesces_before_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    with_runs: bool,
) -> None:
    """Real application tables survive both-volume startup and later application writes."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    source = _legacy(root, scope=scope)
    database = sessions / source.relative_to(root) / "writer/sessions.db"
    database.parent.mkdir(parents=True)
    storage = SqliteDb(db_file=str(database), session_table="writer_sessions")
    storage.upsert_session(AgentSession(session_id="original", agent_id="writer", session_data={"note": "retained"}))
    if with_runs:
        storage.upsert_run(
            RunOutput(run_id="original-run", agent_id="writer", session_id="original", content="retained"),
            "original",
        )
    storage.db_engine.dispose()
    events = []
    inventory = upgrade._inventory

    def stopped(runtime_paths: RuntimePaths, *, timeout_seconds: float) -> None:
        assert runtime_paths == _paths(root, sessions)
        assert timeout_seconds > 0
        assert source.is_dir()
        assert file_lock_is_held(root / upgrade._LOCK)
        assert file_lock_is_held(sessions / upgrade._LOCK)
        events.append("stopped")

    def inspected(root: Path, *, owner_temporary: Path | None = None, exclude_owner_record: bool = False) -> str:
        assert events == ["stopped"]
        return inventory(root, owner_temporary=owner_temporary, exclude_owner_record=exclude_owner_record)

    monkeypatch.setattr(startup, "_quiesce_workers", stopped)
    monkeypatch.setattr(upgrade, "_inventory", inspected)
    await startup.ensure_private_storage_ready(_paths(root, sessions))
    receipt = upgrade._read_journal(root / upgrade._MARKER)
    assert receipt.status == "complete"
    destination = root / "private_instances" / receipt.plan.operations[0].destination
    assert not source.exists()
    assert (destination / "writer/workspace/note.md").read_text() == "original private note"
    moved = SqliteDb(
        db_file=str(sessions / destination.relative_to(root) / "writer/sessions.db"),
        session_table="writer_sessions",
    )
    session = moved.get_session("original", SessionType.AGENT)
    assert session.session_data["note"] == "retained"
    if with_runs:
        assert session.runs[0].content == "retained"
    moved.upsert_session(AgentSession(session_id="new", agent_id="writer"))
    moved.db_engine.dispose()
    (destination / "writer/workspace/note.md").write_text("new application traffic")
    monkeypatch.setattr(upgrade, "_inventory", lambda *_a, **_k: pytest.fail("old inventory compared after traffic"))
    await startup.ensure_private_storage_ready(_paths(root, sessions))
    assert events == ["stopped"]


@pytest.mark.asyncio
async def test_fresh_start_does_not_create_roots_or_stop_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh boot preserves lazy initialization and does not touch worker resources."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: pytest.fail("unnecessary worker stop"))
    await startup.ensure_private_storage_ready(_paths(root, sessions))
    assert not root.exists()
    assert not sessions.exists()


@pytest.mark.asyncio
async def test_secondary_only_state_is_not_a_fresh_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery refuses unpaired session history even when primary storage is empty."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    orphan = sessions / "private_instances/unpaired"
    orphan.mkdir(parents=True)
    (orphan / "retained").write_text("preserved")
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: pytest.fail("unproven state reached workers"))
    with pytest.raises(upgrade.StorageUpgradeError):
        await startup.ensure_private_storage_ready(_paths(root, sessions))
    assert (orphan / "retained").read_text() == "preserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_startup_uses_actual_quiescence_dispatch(tmp_path: Path, external: bool) -> None:
    """The real lazy helper admits contained local execution and refuses an external runner."""
    source = _legacy(tmp_path)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": "static", "MINDROOM_SANDBOX_PROXY_URL": "https://runner.example.org"}
        if external
        else {},
    )
    if external:
        with pytest.raises(WorkerBackendError, match="external runner"):
            await startup.ensure_private_storage_ready(paths)
        assert source.exists()
        assert not (tmp_path / upgrade._MARKER).exists()
    else:
        await startup.ensure_private_storage_ready(paths)
        assert not source.exists()


@pytest.mark.asyncio
async def test_configured_control_root_must_exist_for_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing separately configured control volume cannot hide active script handles."""
    _legacy(tmp_path)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONTROL_STATE_PATH": str(tmp_path / "missing-control"),
        },
    )
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: pytest.fail("missing mount reached workers"))
    with pytest.raises(upgrade.StorageUpgradeError):
        await startup.ensure_private_storage_ready(paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", range(1, 19))
async def test_automatic_resume_uses_embedded_two_volume_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    """Every interrupted cross-volume publication resumes without an external manifest."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    source = _legacy(root)
    mirror = sessions / source.relative_to(root)
    mirror.mkdir(parents=True)
    (mirror / "retained").write_bytes(b"session state")
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    calls = 0

    def crash() -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            message = "simulated interruption"
            raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "_checkpoint", crash)
        with suppress(OSError):
            await startup.ensure_private_storage_ready(_paths(root, sessions))
    original = upgrade._read_journal(root / upgrade._MARKER)
    await startup.ensure_private_storage_ready(_paths(root, sessions))
    receipt = upgrade._read_journal(root / upgrade._MARKER)
    assert receipt.plan == original.plan
    assert receipt.status == "complete"
    upgrade.verify_storage_upgrade(receipt.plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["workers", "missing_volume", "conflict", "unknown_owner", "active_script"])
async def test_automatic_startup_failure_preserves_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Unproven quiescence, ownership, or storage can never reach a move."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    source = _legacy(root)
    original = (source / "writer/workspace/note.md").read_bytes()
    if failure == "missing_volume":
        sessions.rmdir()
    elif failure == "unknown_owner":
        (source / upgrade._RECORD_FILENAME).unlink()
    elif failure == "conflict":
        plan = upgrade.plan_storage_upgrade(root, sessions)
        (source.parent / plan.operations[0].destination).mkdir()
    elif failure == "active_script":
        control = root / "control_state/script_runs"
        control.mkdir(parents=True)
        with sqlite3.connect(control / "script_runs.sqlite3") as connection:
            connection.execute("CREATE TABLE script_runs (state TEXT)")
            connection.execute("INSERT INTO script_runs VALUES ('running')")

    def stopped(*_args: object, **_kwargs: object) -> None:
        if failure == "workers":
            message = "worker termination timed out"
            raise RuntimeError(message)

    monkeypatch.setattr(startup, "_quiesce_workers", stopped)
    monkeypatch.setattr(upgrade, "_rename_offline", lambda *_a, **_k: pytest.fail("unsafe rename"))
    with pytest.raises((upgrade.StorageUpgradeError, RuntimeError, OSError)):
        await startup.ensure_private_storage_ready(_paths(root, sessions))
    assert (source / "writer/workspace/note.md").read_bytes() == original
    assert not (root / upgrade._MARKER).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["sessions", "control", "missing_marker", "different_plan"])
async def test_recorded_participants_must_match_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    """Configured participants cannot redirect a recorded transaction to different roots."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    _legacy(root)
    plan = upgrade.plan_storage_upgrade(root, sessions)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    paths = _paths(root, sessions)
    if changed == "sessions":
        paths = _paths(root, tmp_path / "other-sessions")
    elif changed == "control":
        paths = resolve_runtime_paths(
            config_path=paths.config_path,
            storage_path=root,
            process_env={
                "MINDROOM_SESSION_STORAGE_PATH": str(sessions),
                "MINDROOM_CONTROL_STATE_PATH": str(tmp_path / "other-control"),
            },
        )
    elif changed == "missing_marker":
        (sessions / upgrade._MARKER).unlink()
    else:
        receipt = upgrade._read_journal(sessions / upgrade._MARKER)
        replacement = receipt.model_copy(update={"plan": plan.model_copy(update={"unresolved_worker_files": 99})})
        (sessions / upgrade._MARKER).write_text(replacement.model_dump_json())
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: pytest.fail("invalid receipt reached workers"))
    with pytest.raises(upgrade.StorageUpgradeError):
        await startup.ensure_private_storage_ready(paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [1, 4, 8, 12])
async def test_startup_finishes_reversal_but_keeps_runtime_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    """An explicit rollback remains authoritative across process restarts."""
    source = _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    calls = 0

    def crash() -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            message = "reverse interruption"
            raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "_checkpoint", crash)
        with suppress(OSError):
            upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    with pytest.raises(upgrade.StorageUpgradeError, match=r"[Rr]oll|[Rr]ever"):
        await startup.ensure_private_storage_ready(_paths(tmp_path))
    assert source.is_dir()
    assert upgrade._read_journal(tmp_path / upgrade._MARKER).status == "rolled_back"
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: pytest.fail("rolled back storage resumed"))
    with pytest.raises(upgrade.StorageUpgradeError):
        await startup.ensure_private_storage_ready(_paths(tmp_path))


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation cannot abandon the mutation thread or allow early admission."""
    _legacy(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def stopped(*_args: object, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(startup, "_quiesce_workers", stopped)
    task = asyncio.create_task(startup.ensure_private_storage_ready(_paths(tmp_path)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert upgrade._read_journal(tmp_path / upgrade._MARKER).status == "complete"


@pytest.mark.asyncio
async def test_cli_recovery_accepts_automatic_participation_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic startup leaves a usable optional recovery input without a separate manifest."""
    source = _legacy(tmp_path)
    monkeypatch.setattr(startup, "_quiesce_workers", lambda *_a, **_k: None)
    await startup.ensure_private_storage_ready(_paths(tmp_path))
    runner = CliRunner()
    receipt = str(tmp_path / upgrade._MARKER)
    refused = runner.invoke(app, ["storage-upgrade", "rollback", receipt])
    assert refused.exit_code != 0
    result = runner.invoke(app, ["storage-upgrade", "rollback", receipt, "--writers-stopped"])
    assert result.exit_code == 0, result.output
    assert source.exists()
