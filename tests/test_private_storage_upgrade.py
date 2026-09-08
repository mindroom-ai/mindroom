"""Offline private-state continuity, ownership, and crash recovery."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing, suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import errno

import pytest
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from fastapi import FastAPI
from typer.testing import CliRunner

from mindroom import orchestrator
from mindroom import private_storage_upgrade as upgrade
from mindroom.api import main as api_main
from mindroom.api import sandbox_runner
from mindroom.cli.main import app
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.private_instance_identity import private_instances_for_agent
from mindroom.private_instance_identity_store import load_private_instance_identity
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.thread_export.workspace_sync import (
    WorkspaceThreadExportDeps,
    WorkspaceThreadExportRunner,
    _clear_disabled_agent_exports,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, private_instance_scope_root_path


def _legacy(root: Path, requester: str = "@alice:example.org", scope: str = "user", agent: str = "writer") -> Path:
    normalized = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", requester.strip()).strip("_") or "default"
    key = f"v1:default:{scope}:{normalized}" + (f":{agent}" if scope == "user_agent" else "")
    source = private_instance_scope_root_path(root, key)
    source.mkdir(parents=True)
    (source / ".mindroom-private-instance.json").write_text(
        json.dumps(
            {
                "format": "mindroom-private-instance",
                "version": 1,
                "worker_key": key,
                "requester_id": requester,
            },
        ),
    )
    for name in ("writer", "reader") if scope == "user" else (agent,):
        workspace = source / name / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "note.md").write_text("original private note")
    return source


@pytest.mark.parametrize("scope", ["user", "user_agent"])
@pytest.mark.parametrize("separate", [False, True])
def test_populated_state_and_sessions_survive(tmp_path: Path, scope: str, separate: bool) -> None:
    """Whole owner scopes and populated Agno rows survive cutover and reversal."""
    root = tmp_path / "state"
    root.mkdir()
    sessions = tmp_path / "sessions" if separate else root
    sessions.mkdir(exist_ok=True)
    old = _legacy(root, scope=scope)
    original = (old / upgrade._RECORD_FILENAME).read_bytes()
    session_scope = sessions / old.relative_to(root)
    session_scope.mkdir(parents=True, exist_ok=True)
    db_file = session_scope / "writer" / "sessions.db"
    db_file.parent.mkdir(exist_ok=True)
    storage = SqliteDb(db_file=str(db_file))
    storage.upsert_session(AgentSession(session_id="original", agent_id="writer", session_data={"note": "retained"}))
    storage.upsert_run(
        run=RunOutput(run_id="retained-run", agent_id="writer", session_id="original", content="retained response"),
        session_id="original",
    )
    storage.db_engine.dispose()
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        upgrade.check_storage_upgrade(root, sessions)
    plan = upgrade.plan_storage_upgrade(root, sessions)
    assert len(plan.operations) == 1
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.check_storage_upgrade(root, sessions)
    destination = root / "private_instances" / plan.operations[0].destination
    assert not old.exists()
    assert load_private_instance_identity(root, destination).requester_id == "@alice:example.org"
    assert (destination / "writer/workspace/note.md").read_text() == "original private note"
    new_db = sessions / destination.relative_to(root) / "writer/sessions.db"
    moved_storage = SqliteDb(db_file=str(new_db))
    assert moved_storage.get_session("original", SessionType.AGENT).session_data["note"] == "retained"
    assert moved_storage.get_session("original", SessionType.AGENT).runs[0].content == "retained response"
    moved_storage.db_engine.dispose()
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert (old / upgrade._RECORD_FILENAME).read_bytes() == original


@pytest.mark.parametrize("requester", ["requester/a", "requester_a", "@alice:example.org:8448", "a%b", " a ", "___"])
def test_exact_raw_owner(tmp_path: Path, requester: str) -> None:
    """The recorded exact requester alone owns the relocated scope."""
    old = _legacy(tmp_path, requester)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    assert plan.operations[0].requester_id == requester
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    destination = tmp_path / "private_instances" / plan.operations[0].destination
    assert load_private_instance_identity(tmp_path, destination).requester_id == requester
    assert not old.exists()


@pytest.mark.parametrize("kind", ["recordless", "invalid", "duplicate", "symlink", "wrong_hash", "oversized"])
def test_invalid_sources_never_adopted(tmp_path: Path, kind: str) -> None:
    """Unproven private roots cannot be adopted automatically."""
    old = _legacy(tmp_path)
    record = old / upgrade._RECORD_FILENAME
    if kind == "recordless":
        record.unlink()
    elif kind == "invalid":
        record.write_text("{}")
    elif kind == "duplicate":
        record.write_text('{"version":1,"version":1}')
    elif kind == "symlink":
        record.rename(old / "record")
        record.symlink_to("record")
    elif kind == "wrong_hash":
        old.rename(old.with_name("wrong"))
    else:
        record.write_text(" " * 70000)
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.plan_storage_upgrade(tmp_path)


def test_existing_destination_and_changed_source_refused(tmp_path: Path) -> None:
    """Neither a destination conflict nor changed source can be overwritten."""
    old = _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    destination = old.parent / plan.operations[0].destination
    destination.mkdir()
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    destination.rmdir()
    (old / "writer/workspace/note.md").write_text("changed after planning")
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)


@pytest.mark.parametrize("boundary", range(1, 13))
def test_interrupted_writes_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int) -> None:
    """Recovery follows the durable receipt across interrupted transitions."""
    _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    original = upgrade._checkpoint
    calls = 0

    def crash() -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            message = "simulated power loss"
            raise OSError(message)

    monkeypatch.setattr(upgrade, "_checkpoint", crash)
    try:
        upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    except OSError:
        if upgrade._read_journal(tmp_path / upgrade._MARKER).status != "complete":
            with pytest.raises(upgrade._StorageUpgradeRequiredError):
                upgrade.check_storage_upgrade(tmp_path)
    monkeypatch.setattr(upgrade, "_checkpoint", original)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.check_storage_upgrade(tmp_path)
    upgrade.verify_storage_upgrade(plan)


def test_post_cutover_write_refuses_rollback(tmp_path: Path) -> None:
    """Fresh candidate writes make an old snapshot rollback unsafe."""
    _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    destination = tmp_path / "private_instances" / plan.operations[0].destination
    (destination / "writer/workspace/note.md").write_text("new traffic")
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.rollback_storage_upgrade(plan, writers_stopped=True)


def test_missing_session_mount_and_symlinked_namespace_refused(tmp_path: Path) -> None:
    """Required mounts cannot silently become an empty session tree."""
    root = tmp_path / "state"
    root.mkdir()
    _legacy(root)
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.plan_storage_upgrade(root, tmp_path / "missing")
    namespace = root / "private_instances"
    namespace.rename(root / "elsewhere")
    namespace.symlink_to(root / "elsewhere", target_is_directory=True)
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.plan_storage_upgrade(root)


def test_relative_links_modes_and_worker_credentials_preserved(tmp_path: Path) -> None:
    """Moving private state neither adopts nor touches independently owned credentials."""
    source = _legacy(tmp_path)
    note = source / "writer/workspace/note.md"
    note.chmod(0o640)
    (note.parent / "relative").symlink_to("note.md")
    credentials = tmp_path / "workers/old-worker/credentials/token.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text('{"synthetic": "value"}')
    plan = upgrade.plan_storage_upgrade(tmp_path)
    assert plan.unresolved_worker_files == 1
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    moved = source.with_name(plan.operations[0].destination)
    assert (moved / "writer/workspace/relative").read_text() == "original private note"
    assert (moved / "writer/workspace/note.md").stat().st_mode & 0o777 == 0o640
    assert credentials.read_text() == '{"synthetic": "value"}'


def test_absolute_symlink_blocks_but_historical_paths_remain_opaque(tmp_path: Path) -> None:
    """Historical references remain unchanged; interpreted absolute links need repair."""
    source = _legacy(tmp_path)
    reference = source / "writer/workspace/reference"
    reference.write_text(str(source / "writer/workspace/note.md"))
    original = reference.read_bytes()
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    moved = source.with_name(plan.operations[0].destination)
    assert (moved / "writer/workspace/reference").read_bytes() == original
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    reference.unlink()
    reference.symlink_to(source / "writer/workspace/note.md")
    with pytest.raises(upgrade.StorageUpgradeError, match="Absolute symlink"):
        upgrade._inventory(source)


@pytest.mark.parametrize("boundary", range(1, 19))
def test_two_volume_recovery_and_reverse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int) -> None:
    """Any interrupted cross-volume transition can be reversed using its receipt."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    source = _legacy(root)
    mirror = sessions / source.relative_to(root)
    mirror.mkdir(parents=True)
    (mirror / "session.bin").write_bytes(b"retained session")
    original = (source / upgrade._RECORD_FILENAME).read_bytes()
    plan = upgrade.plan_storage_upgrade(root, sessions)
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
            upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert (source / upgrade._RECORD_FILENAME).read_bytes() == original
    assert (mirror / "session.bin").read_bytes() == b"retained session"
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        upgrade.check_storage_upgrade(root, sessions)


def test_manual_export_cleanup_preserves_legacy_exports(tmp_path: Path) -> None:
    """Both direct cleanup and owner discovery stop before deleting legacy exports."""
    source = _legacy(tmp_path)
    exports = source / "writer/workspace/thread_exports"
    exports.mkdir()
    export = exports / "edited.yaml"
    export.write_text("original: edited export\n")
    runtime = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"writer": AgentConfig(display_name="Writer", private=AgentPrivateConfig(per="user"))})
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        private_instances_for_agent(tmp_path, "writer", "user")
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        _clear_disabled_agent_exports(config, runtime, frozenset())
    assert export.read_text() == "original: edited export\n"


def test_active_script_handles_block_preflight(tmp_path: Path) -> None:
    """Old live script identities cannot be transplanted onto new workers."""
    _legacy(tmp_path)
    control = tmp_path / "control_state/script_runs"
    control.mkdir(parents=True)
    with sqlite3.connect(control / "script_runs.sqlite3") as database:
        database.execute("CREATE TABLE script_runs (state TEXT)")
        database.execute("INSERT INTO script_runs VALUES ('running')")
    with pytest.raises(upgrade.StorageUpgradeError, match=r"[Ss]cript"):
        upgrade.plan_storage_upgrade(tmp_path)


@pytest.mark.parametrize("surface", ["orchestrator", "api", "sandbox"])
@pytest.mark.asyncio
async def test_startup_fence_precedes_storage_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    """Every independently started runtime refuses old private state before setup."""
    _legacy(tmp_path)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    with pytest.raises(upgrade._StorageUpgradeRequiredError):  # noqa: PT012 - one selected startup surface
        if surface == "orchestrator":
            await orchestrator.main("INFO", paths)
        elif surface == "api":
            monkeypatch.setattr(api_main, "_app_runtime_paths", lambda _app: paths)
            async with api_main._lifespan(FastAPI()):
                pytest.fail("legacy storage reached API startup")
        else:
            sandbox_runner.initialize_sandbox_runner_app(FastAPI(), paths)
    assert not paths.config_path.exists()
    assert not (tmp_path / "credentials").exists()


@pytest.mark.parametrize("failure", ["fsync", "EXDEV", "EEXIST"])
def test_os_failures_never_copy_or_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """Durability and rename errors retain data and a recoverable fence."""
    source = _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    destination = source.with_name(plan.operations[0].destination)

    def fail(*_args: object, **_kwargs: object) -> None:
        if failure == "EEXIST":
            destination.mkdir()
            (destination / "unrelated").write_text("preserve competing destination")
            raise FileExistsError(errno.EEXIST, "existing destination")
        raise OSError(errno.EXDEV if failure == "EXDEV" else errno.EIO, "simulated OS failure")

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "fsync_directory_durable" if failure == "fsync" else "_rename_offline", fail)
        with pytest.raises(OSError, match=r"existing destination|simulated OS failure"):
            upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        upgrade.check_storage_upgrade(tmp_path)
    if failure == "EEXIST":
        assert (source / "writer/workspace/note.md").read_text() == "original private note"
        assert (destination / "unrelated").read_text() == "preserve competing destination"
    else:
        upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
        upgrade.verify_storage_upgrade(plan)


def test_missing_participant_after_complete_fences_runtime(tmp_path: Path) -> None:
    """A complete marker cannot hide a missing or replaced session volume."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    _legacy(root)
    plan = upgrade.plan_storage_upgrade(root, sessions)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    sessions.rename(tmp_path / "unmounted")
    sessions.mkdir()
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        upgrade.check_storage_upgrade(root, sessions)


def test_clean_install_and_identical_resolved_session_root(tmp_path: Path) -> None:
    """New installs retain lazy storage creation, and one volume moves only once."""
    upgrade.check_storage_upgrade(tmp_path / "new", tmp_path / "new-sessions")
    _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path, tmp_path / ".")
    assert len(plan.volumes) == 1
    assert len(plan.operations[0].moves) == 1


def test_collision_other_raw_identity_cannot_resolve_migrated_data(tmp_path: Path) -> None:
    """An old normalization collision cannot authorize the other raw requester."""
    _legacy(tmp_path, "requester/a")
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    config = Config(agents={"writer": AgentConfig(display_name="Writer", private=AgentPrivateConfig(per="user"))})
    runtime = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    identity = ToolExecutionIdentity(
        channel="openai_compat",
        agent_name="writer",
        requester_id="requester_a",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    resolved = resolve_agent_storage("writer", config, runtime, identity)
    assert not resolved.state_root.exists()


def test_completed_reversal_can_be_upgraded_again(tmp_path: Path) -> None:
    """The original protected receipt supports a fresh retry after full reversal."""
    _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.verify_storage_upgrade(plan)


def test_unplanned_session_tree_blocks_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery refuses a new mirror omitted from the prepared plan."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    _legacy(root)
    plan = upgrade.plan_storage_upgrade(root, sessions)

    def stop() -> None:
        message = "interrupted preparation"
        raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "_checkpoint", stop)
        with pytest.raises(OSError, match="interrupted preparation"):
            upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    mirror = sessions / "private_instances" / plan.operations[0].source
    mirror.mkdir(parents=True)
    (mirror / "new-data").write_text("unexpected writer")
    with pytest.raises(upgrade.StorageUpgradeError, match="unplanned session"):
        upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)


def test_cli_manifest_is_private_and_never_overwritten(tmp_path: Path) -> None:
    """Registered commands preserve private mappings and require offline acknowledgments."""
    _legacy(tmp_path)
    manifest = tmp_path / "upgrade-plan.json"
    runner = CliRunner()
    result = runner.invoke(app, ["storage-upgrade", "plan", "--storage", str(tmp_path), "--manifest", str(manifest)])
    assert result.exit_code == 0, result.output
    assert "@alice" not in result.output
    assert manifest.stat().st_mode & 0o777 == 0o600
    original = manifest.read_bytes()
    duplicate = runner.invoke(app, ["storage-upgrade", "plan", "--storage", str(tmp_path), "--manifest", str(manifest)])
    assert duplicate.exit_code != 0
    assert manifest.read_bytes() == original
    refused = runner.invoke(app, ["storage-upgrade", "apply", str(manifest)])
    assert refused.exit_code != 0
    applied = runner.invoke(app, ["storage-upgrade", "apply", str(manifest), "--writers-stopped", "--backup-verified"])
    assert applied.exit_code == 0, applied.output
    verified = runner.invoke(app, ["storage-upgrade", "verify", str(manifest)])
    assert verified.exit_code == 0, verified.output


@pytest.mark.parametrize("kind", ["invalid", "wrong_schema", "wal", "journal"])
def test_unreadable_or_unsettled_session_database_blocks_planning(tmp_path: Path, kind: str) -> None:
    """Immutable verification must never ignore uncheckpointed or corrupt session state."""
    source = _legacy(tmp_path)
    database = source / "writer/sessions.db"
    if kind == "invalid":
        database.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        if kind in {"wal", "journal"}:
            database.with_name(database.name + "-" + kind).write_bytes(b"unsettled sidecar")
    before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
    with pytest.raises(upgrade.StorageUpgradeError, match=r"[Ss]ession|[Ss]idecar"):
        upgrade.plan_storage_upgrade(tmp_path)
    assert {path: path.read_bytes() for path in source.rglob("*") if path.is_file()} == before


def test_posix_offline_rename_revalidates_source_and_destination(tmp_path: Path) -> None:
    """The portable transition relies on stopped writers and never replaces observed roots."""
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    info = source.stat()
    destination.mkdir()
    with pytest.raises(upgrade.StorageUpgradeError, match="Destination"):
        upgrade._rename_offline(source, destination, device=info.st_dev, inode=info.st_ino)
    destination.rmdir()
    with pytest.raises(upgrade.StorageUpgradeError, match="identity"):
        upgrade._rename_offline(source, destination, device=info.st_dev, inode=info.st_ino + 1)
    upgrade._rename_offline(source, destination, device=info.st_dev, inode=info.st_ino)
    assert destination.stat().st_ino == info.st_ino
    assert not source.exists()


@pytest.mark.parametrize("boundary", range(1, 17))
def test_reverse_interruption_is_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int) -> None:
    """Every reverse record/rename/fsync/receipt interruption preserves resumable data."""
    root, sessions = tmp_path / "state", tmp_path / "sessions"
    root.mkdir()
    sessions.mkdir()
    source = _legacy(root)
    mirror = sessions / source.relative_to(root)
    mirror.mkdir(parents=True)
    (mirror / "session.bin").write_bytes(b"original session")
    original = (source / upgrade._RECORD_FILENAME).read_bytes()
    plan = upgrade.plan_storage_upgrade(root, sessions)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    calls = 0

    def interrupt() -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            message = "reverse interruption"
            raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "_checkpoint", interrupt)
        with suppress(OSError):
            upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        upgrade.check_storage_upgrade(root, sessions)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert (source / upgrade._RECORD_FILENAME).read_bytes() == original
    assert (mirror / "session.bin").read_bytes() == b"original session"


def test_runtime_resolution_checks_target_without_global_owner_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot resolution checks receipts and its exact legacy candidate, not all users."""
    _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    config = Config(agents={"writer": AgentConfig(display_name="Writer", private=AgentPrivateConfig(per="user"))})
    runtime = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    original = upgrade._legacy_keys
    visited = []

    def record(scope: Path) -> tuple[str, str, str] | None:
        visited.append(scope)
        return original(scope)

    monkeypatch.setattr(upgrade, "_legacy_keys", record)
    for _ in range(3):
        resolve_agent_storage("writer", config, runtime, identity)
    assert visited == []


def test_historical_sqlite_paths_are_retained_and_verified(tmp_path: Path) -> None:
    """Paths persisted in completed sessions/runs remain history, not relocation edits."""
    source = _legacy(tmp_path)
    database = source / "writer/sessions.db"
    storage = SqliteDb(db_file=str(database))
    storage.upsert_session(
        AgentSession(session_id="history", agent_id="writer", session_data={"old_path": str(source)}),
    )
    storage.upsert_run(
        run=RunOutput(run_id="history-run", agent_id="writer", session_id="history", content=str(source)),
        session_id="history",
    )
    storage.db_engine.dispose()
    original = database.read_bytes()
    plan = upgrade.plan_storage_upgrade(tmp_path)
    assert plan.operations[0].moves[0].sessions
    assert database.read_bytes() == original
    assert not database.with_name(database.name + "-wal").exists()
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    moved = source.with_name(plan.operations[0].destination) / "writer/sessions.db"
    assert moved.read_bytes() == original
    upgrade.verify_storage_upgrade(plan)
    assert not moved.with_name(moved.name + "-wal").exists()
    assert not moved.with_name(moved.name + "-shm").exists()


@pytest.mark.parametrize("failure", ["_write_record", "_rename_offline", "fsync_directory_durable"])
def test_reverse_os_failure_can_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """Reverse I/O errors leave the journal fenced and recoverable without lost bytes."""
    source = _legacy(tmp_path)
    original = (source / upgrade._RECORD_FILENAME).read_bytes()
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "reverse I/O failure")

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, failure, fail)
        with pytest.raises(OSError, match="reverse I/O failure"):
            upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        upgrade.check_storage_upgrade(tmp_path)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert (source / upgrade._RECORD_FILENAME).read_bytes() == original


def test_nonempty_concurrent_destination_cannot_be_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a prerequisite-violating writer cannot make POSIX rename replace a nonempty root."""
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    (source / "original").write_text("original")
    info = source.stat()
    original = type(source).rename

    def race(path: Path, target: Path) -> Path:
        target.mkdir()
        (target / "other").write_text("other writer")
        return original(path, target)

    monkeypatch.setattr(type(source), "rename", race)
    with pytest.raises(OSError, match=r"not empty|exists"):
        upgrade._rename_offline(source, destination, device=info.st_dev, inode=info.st_ino)
    assert (source / "original").read_text() == "original"
    assert (destination / "other").read_text() == "other writer"


@pytest.mark.asyncio
async def test_export_pass_shares_one_full_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested cleanup and discovery share preflight while still checking receipts."""
    _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    runtime = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(
        agents={
            name: AgentConfig(display_name=name, private=AgentPrivateConfig(per="user"))
            for name in ("writer", "reader")
        },
    )
    original = upgrade._legacy_keys
    visited = []

    def record(scope: Path) -> tuple[str, str, str] | None:
        visited.append(scope)
        return original(scope)

    monkeypatch.setattr(upgrade, "_legacy_keys", record)
    runner = WorkspaceThreadExportRunner(
        WorkspaceThreadExportDeps(
            runtime_paths=runtime,
            config_provider=lambda: config,
            bot_provider=lambda _name: None,
            response_admission_gate=ResponseAdmissionGate(),
        ),
    )
    await runner._run_pass(config, full_pass=True, room_ids=frozenset())
    assert len(visited) == 1
    checked = upgrade.check_runtime_storage_upgrade(runtime)
    upgrade._publish(plan, "prepared")
    with pytest.raises(upgrade._StorageUpgradeRequiredError):
        _clear_disabled_agent_exports(config, runtime, frozenset(), checked=checked)


def test_only_recognized_worker_runtime_metadata_blocks_relocation(tmp_path: Path) -> None:
    """User-authored JSON remains opaque while actual startup metadata needs recovery."""
    source = _legacy(tmp_path)
    historical = source / "writer/workspace/metadata/worker.json"
    historical.parent.mkdir(parents=True)
    historical.write_text(json.dumps({"old_path": str(source)}))
    upgrade.plan_storage_upgrade(tmp_path)
    live = source / "writer/.runtime/startup_manifest.json"
    live.parent.mkdir()
    live.write_text(json.dumps({"state_root": str(source)}))
    with pytest.raises(upgrade.StorageUpgradeError, match="runtime metadata"):
        upgrade.plan_storage_upgrade(tmp_path)


@pytest.mark.parametrize("valid", [False, True])
def test_relative_session_directory_is_verified_without_rewriting(tmp_path: Path, valid: bool) -> None:
    """Canonical session links remain relative while their actual database is checked."""
    source = _legacy(tmp_path)
    archive = source / "writer/archive"
    archive.mkdir()
    database = archive / "writer.db"
    if valid:
        storage = SqliteDb(db_file=str(database))
        storage.upsert_session(AgentSession(session_id="linked", agent_id="writer"))
        storage.upsert_run(
            run=RunOutput(run_id="linked-run", session_id="linked", content="retained"),
            session_id="linked",
        )
        storage.db_engine.dispose()
    else:
        database.write_bytes(b"unreadable session database")
    (source / "writer/sessions").symlink_to("archive", target_is_directory=True)
    if not valid:
        with pytest.raises(upgrade.StorageUpgradeError, match="Session database"):
            upgrade.plan_storage_upgrade(tmp_path)
        return
    original = database.read_bytes()
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    moved = source.with_name(plan.operations[0].destination)
    assert (moved / "writer/sessions").readlink() == type(source)("archive")
    assert (moved / "writer/sessions/writer.db").read_bytes() == original


@pytest.mark.parametrize("target", ["../../../{scope}/writer/workspace/note.md", "../../../../outside"])
def test_relative_links_cannot_escape_or_name_the_old_scope(tmp_path: Path, target: str) -> None:
    """Links that escape the moved tree cannot preserve their logical target."""
    source = _legacy(tmp_path)
    link = source / "writer/workspace/link"
    link.symlink_to(target.format(scope=source.name))
    with pytest.raises(upgrade.StorageUpgradeError, match="symlink"):
        upgrade.plan_storage_upgrade(tmp_path)


def test_named_session_schema_requires_runtime_columns(tmp_path: Path) -> None:
    """A SQLite-valid two-column table is incompatible with the actual runtime."""
    source = _legacy(tmp_path)
    database = source / "writer/sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE writer_sessions (session_id TEXT, session_type TEXT)")
        connection.execute("INSERT INTO writer_sessions VALUES ('original', 'agent')")
    with pytest.raises(upgrade.StorageUpgradeError, match="schema"):
        upgrade.plan_storage_upgrade(tmp_path)


def test_named_run_rows_are_part_of_semantic_snapshot(tmp_path: Path) -> None:
    """Application-named run tables participate in immutable semantic verification."""
    database = tmp_path / "writer.db"
    storage = SqliteDb(db_file=str(database), session_table="writer_sessions")
    storage.upsert_session(AgentSession(session_id="original", agent_id="writer"))
    storage.upsert_run(run=RunOutput(run_id="run", session_id="original", content="before"), session_id="original")
    storage.db_engine.dispose()
    before = upgrade._session_database_snapshot(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE writer_sessions_runs SET run_data = ?", ('{"content": "after"}',))
    assert upgrade._session_database_snapshot(database) != before


def test_completed_receipts_are_parsed_once_per_stable_participant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated hot checks stat every marker but do not repeatedly parse full plans."""
    state, sessions = tmp_path / "state", tmp_path / "sessions"
    state.mkdir()
    sessions.mkdir()
    _legacy(state)
    plan = upgrade.plan_storage_upgrade(state, sessions)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    original = upgrade._read_journal
    calls = 0

    def counted(path: Path) -> upgrade._Journal | None:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(upgrade, "_read_journal", counted)
    for _ in range(6):
        upgrade.check_storage_upgrade(state, sessions, scan_legacy=False)
    assert calls == 2
    upgrade.check_storage_upgrade(state, sessions)
    assert calls == 4  # Exhaustive operation preflight bypasses the hot cache.
    marker = sessions / upgrade._MARKER
    marker.write_text(marker.read_text().replace('"complete"', '"prepared"'))
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.check_storage_upgrade(state, sessions, scan_legacy=False)


def test_record_temporary_stays_inside_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner replacement has one parent directory to flush and recover."""
    source = _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    original = type(source).replace

    def checked(path: Path, target: Path) -> Path:
        if target.name == upgrade._RECORD_FILENAME:
            assert path.parent == target.parent
        return original(path, target)

    monkeypatch.setattr(type(source), "replace", checked)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)


@pytest.mark.parametrize("change", ["missing", "replacement", "disagreement", "both_missing"])
def test_hot_receipt_cache_detects_participant_changes(tmp_path: Path, change: str) -> None:
    """Stable-identity caching never accepts missing or replaced transaction markers."""
    state, sessions = tmp_path / "state", tmp_path / "sessions"
    state.mkdir()
    sessions.mkdir()
    _legacy(state)
    plan = upgrade.plan_storage_upgrade(state, sessions)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.check_storage_upgrade(state, sessions, scan_legacy=False)
    marker = sessions / upgrade._MARKER
    if change in {"missing", "both_missing"}:
        marker.unlink()
        if change == "both_missing":
            (state / upgrade._MARKER).unlink()
    elif change == "replacement":
        original = marker.stat()
        replacement = sessions / "replacement"
        replacement.write_bytes(marker.read_bytes().replace(b'"complete"', b'"prepared"'))
        replacement.replace(marker)
        os.utime(marker, ns=(original.st_atime_ns, original.st_mtime_ns))
    else:
        payload = json.loads(marker.read_text())
        payload["plan"]["unresolved_worker_files"] += 1
        marker.write_text(json.dumps(payload))
    with pytest.raises(upgrade.StorageUpgradeError):
        upgrade.check_storage_upgrade(state, sessions, scan_legacy=False)


@pytest.mark.parametrize("boundary", [1, 2, 3, 4])
def test_owner_record_crash_resumes_exact_transaction_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    """Crashes before and after owner replacement preserve data and leave recoverable state."""
    source = _legacy(tmp_path)
    original_record = (source / upgrade._RECORD_FILENAME).read_bytes()
    plan = upgrade.plan_storage_upgrade(tmp_path)
    original_write = upgrade._write_record
    original_checkpoint = upgrade._checkpoint

    def crash_write(scope: Path, operation: upgrade._Operation, *, new: bool) -> None:
        count = 0

        def checkpoint() -> None:
            nonlocal count
            count += 1
            if count == boundary:
                message = "simulated owner write crash"
                raise OSError(message)

        monkeypatch.setattr(upgrade, "_checkpoint", checkpoint)
        try:
            original_write(scope, operation, new=new)
        finally:
            monkeypatch.setattr(upgrade, "_checkpoint", original_checkpoint)

    monkeypatch.setattr(upgrade, "_write_record", crash_write)
    with pytest.raises(OSError, match="owner write crash"):
        upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    monkeypatch.setattr(upgrade, "_write_record", original_write)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    upgrade.verify_storage_upgrade(plan)
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert (source / upgrade._RECORD_FILENAME).read_bytes() == original_record
    assert not list(source.glob(".mindroom-private-owner-*"))


@pytest.mark.parametrize("contents", [b"", b"{", b"unrelated payload"])
def test_transaction_orphan_is_bounded_and_unrelated_data_is_preserved(tmp_path: Path, contents: bytes) -> None:
    """Only the receipt-derived name with an expected partial record can be discarded."""
    source = _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    moved = source.with_name(plan.operations[0].destination)
    temporary = upgrade._owner_temporary(moved, plan.operations[0])
    temporary.write_bytes(contents)
    if contents == b"unrelated payload":
        with pytest.raises(upgrade.StorageUpgradeError, match="unrelated"):
            upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
        assert temporary.read_bytes() == contents
    else:
        upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
        assert not (source / temporary.name).exists()


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE writer_sessions DROP COLUMN summary",
        "ALTER TABLE writer_sessions_runs DROP COLUMN run_data",
    ],
)
def test_named_application_tables_require_complete_runtime_schema(tmp_path: Path, statement: str) -> None:
    """Present tables with incomplete schemas fail without source writes."""
    database = tmp_path / "writer.db"
    storage = SqliteDb(db_file=str(database), session_table="writer_sessions")
    storage.upsert_session(AgentSession(session_id="original", agent_id="writer"))
    storage.upsert_run(run=RunOutput(run_id="run", session_id="original", content="retained"), session_id="original")
    storage.db_engine.dispose()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(statement)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(upgrade.StorageUpgradeError, match="schema"):
        upgrade._session_database_snapshot(database)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_unknown_owner_temporary_is_preserved(tmp_path: Path) -> None:
    """A similarly named file outside the exact transaction is never discarded."""
    source = _legacy(tmp_path)
    plan = upgrade.plan_storage_upgrade(tmp_path)
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    unknown = source.with_name(plan.operations[0].destination) / ".mindroom-private-owner-unrelated.tmp"
    unknown.write_text("retain this file")
    with pytest.raises(upgrade.StorageUpgradeError, match="Unrecognized"):
        upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert unknown.read_text() == "retain this file"


@pytest.mark.parametrize("session_table", ["agno_sessions", "writer_sessions"])
def test_real_agno_session_without_lazy_run_table_survives(tmp_path: Path, session_table: str) -> None:
    """Agno session-only databases remain readable without creating a runs table."""
    source = _legacy(tmp_path)
    database = source / "writer/sessions.db"
    storage = SqliteDb(db_file=str(database), session_table=session_table)
    storage.upsert_session(
        AgentSession(session_id="session-only", agent_id="writer", session_data={"note": "retained"}),
    )
    storage.db_engine.dispose()
    runs_table = "agno_runs" if session_table == "agno_sessions" else f"{session_table}_runs"
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert session_table in tables
    assert runs_table not in tables
    reader = SqliteDb(db_file=str(database), session_table=session_table)
    assert reader.get_session("session-only", SessionType.AGENT).session_data["note"] == "retained"
    reader.db_engine.dispose()
    original = database.read_bytes()
    plan = upgrade.plan_storage_upgrade(tmp_path)
    assert database.read_bytes() == original
    upgrade.apply_storage_upgrade(plan, writers_stopped=True, backup_verified=True)
    moved = source.with_name(plan.operations[0].destination) / "writer/sessions.db"
    upgrade.verify_storage_upgrade(plan)
    reader = SqliteDb(db_file=str(moved), session_table=session_table)
    session = reader.get_session("session-only", SessionType.AGENT)
    assert session.session_data["note"] == "retained"
    assert not session.runs
    reader.db_engine.dispose()
    upgrade.rollback_storage_upgrade(plan, writers_stopped=True)
    assert database.read_bytes() == original
    with closing(sqlite3.connect(database)) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name = ?", (runs_table,)).fetchall()


def test_present_non_table_run_schema_is_not_lazy_absence(tmp_path: Path) -> None:
    """A conflicting run view cannot masquerade as a lazily absent run table."""
    database = tmp_path / "writer.db"
    storage = SqliteDb(db_file=str(database), session_table="writer_sessions")
    storage.upsert_session(AgentSession(session_id="session-only", agent_id="writer"))
    storage.db_engine.dispose()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE VIEW writer_sessions_runs AS SELECT session_id FROM writer_sessions")
    with pytest.raises(upgrade.StorageUpgradeError, match="schema"):
        upgrade._session_database_snapshot(database)
