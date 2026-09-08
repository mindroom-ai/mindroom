"""Automatic primary cutover and recovery before runtime admission."""

from __future__ import annotations

import asyncio
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
