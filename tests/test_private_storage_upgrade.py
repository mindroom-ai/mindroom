"""Offline private-state continuity, ownership, and crash recovery."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import suppress
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
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.thread_export.workspace_sync import _clear_disabled_agent_exports
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


@pytest.mark.parametrize("kind", ["symlink", "document"])
def test_absolute_references_block_without_rewriting(tmp_path: Path, kind: str) -> None:
    """Absolute paths need an explicit repair; documents and history remain intact."""
    source = _legacy(tmp_path)
    reference = source / "writer/workspace/reference"
    if kind == "symlink":
        reference.symlink_to(source / "writer/workspace/note.md")
    else:
        reference.write_text(str(source / "writer/workspace/note.md"))
    with pytest.raises(upgrade.StorageUpgradeError, match=r"[Aa]bsolute"):
        upgrade.plan_storage_upgrade(tmp_path)
    assert reference.exists()


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

    def fail(*_args: object) -> None:
        if failure == "EEXIST":
            destination.mkdir()
            (destination / "unrelated").write_text("preserve competing destination")
            raise FileExistsError(errno.EEXIST, "existing destination")
        raise OSError(errno.EXDEV if failure == "EXDEV" else errno.EIO, "simulated OS failure")

    with monkeypatch.context() as fault:
        fault.setattr(upgrade, "fsync_directory_durable" if failure == "fsync" else "_rename_no_replace", fail)
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
