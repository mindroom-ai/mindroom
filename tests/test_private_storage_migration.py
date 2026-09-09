"""Startup relocation preserves exact owners and opaque private state."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentThreadExportConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, resolve_session_state_root
from mindroom.file_locks import file_lock_is_held
from mindroom.private_instance_identity_store import (
    ensure_private_instance_identity,
    load_private_instance_identity,
    reconstruct_private_instance_worker_key,
)
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.thread_export.workspace_sync import _private_targets
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, private_instance_scope_root_path
from mindroom.workers.backends._dedicated_worker_common import plan_scoped_visible_state_roots

if TYPE_CHECKING:
    from collections.abc import Callable

_RECORD = ".mindroom-private-instance.json"
_INTENT = ".mindroom-private-storage-migration.json"


def _paths(tmp_path: Path, *, separate: bool = True) -> RuntimePaths:
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    sessions = tmp_path / "sessions" if separate else root
    sessions.mkdir(exist_ok=True)
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=root,
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(sessions)},
    )


def _seed(paths: RuntimePaths, old_key: str, requester: str) -> Path:
    scope = private_instance_scope_root_path(paths.storage_root, old_key)
    scope.mkdir(parents=True)
    (scope / _RECORD).write_text(
        json.dumps(
            {
                "format": "mindroom-private-instance",
                "version": 1,
                "worker_key": old_key,
                "requester_id": requester,
            },
        ),
    )
    workspace = scope / "writer" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_bytes(b"private workspace\x00retained")
    sessions = resolve_session_state_root(scope, paths) / "writer" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sessions / "writer.db") as connection:
        connection.execute("CREATE TABLE history (message TEXT)")
        connection.execute("INSERT INTO history VALUES ('retained session')")
    (sessions / "writer.db-wal").write_bytes(b"opaque companion")
    (sessions / "credentials.bin").write_bytes(b"opaque credentials")
    return scope


def _files(scope: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(scope)): path.read_bytes()
        for path in scope.rglob("*")
        if path.is_file() and path.name != _RECORD
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True])
async def test_startup_moves_every_owner_and_preserves_contents(tmp_path: Path, separate: bool) -> None:
    """No request is needed to preserve both private scope kinds at current paths."""
    paths = _paths(tmp_path, separate=separate)
    fixtures = [
        ("v1:default:user:@alice:example.org", "@alice:example.org", "v1:default:user:~@alice:example.org"),
        (
            "v1:default:user_agent:@bob:example.org:writer",
            "@bob:example.org",
            "v1:default:user_agent:~@bob:example.org:writer",
        ),
    ]
    sources = [_seed(paths, old, requester) for old, requester, _new in fixtures]
    database_inodes = [
        (resolve_session_state_root(source, paths) / "writer/sessions/writer.db").stat().st_ino for source in sources
    ]
    primary = [_files(source) for source in sources]
    secondary = [_files(resolve_session_state_root(source, paths)) for source in sources]
    migration = importlib.import_module("mindroom.private_storage_migration")
    await migration.migrate_private_storage(paths)
    for index, (_old, requester, current) in enumerate(fixtures):
        target = private_instance_scope_root_path(paths.storage_root, current)
        assert sources[index].is_symlink()
        assert sources[index].samefile(target)
        assert _files(target) == primary[index]
        session_target = resolve_session_state_root(target, paths)
        assert _files(session_target) == secondary[index]
        assert (session_target / "writer/sessions/writer.db").stat().st_ino == database_inodes[index]
        assert load_private_instance_identity(paths.storage_root, target).requester_id == requester
        # Remove the deliberately opaque companion before SQLite opens the database.
        (session_target / "writer/sessions/writer.db-wal").unlink()
        with sqlite3.connect(session_target / "writer/sessions/writer.db") as connection:
            assert connection.execute("SELECT message FROM history").fetchall() == [("retained session",)]


_OLD = "v1:default:user:@alice:example.org"
_NEW = "v1:default:user:~@alice:example.org"
_REQUESTER = "@alice:example.org"


@pytest.mark.asyncio
async def test_every_filesystem_mutation_boundary_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crashes around actual fsync, replace, rename and unlink never lose scope contents."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    observed: list[str] = []
    originals = {name: getattr(os, name) for name in ("fsync", "replace", "rename", "unlink", "symlink")}

    def instrument(name: str, crash_at: int | None, after: bool) -> Callable:
        def call(*args: object, **kwargs: object) -> object:
            observed.append(name)
            crash = len(observed) == crash_at
            message = "injected interruption"
            if crash and not after:
                raise OSError(message)
            result = originals[name](*args, **kwargs)
            if crash:
                raise OSError(message)
            return result

        return call

    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    expected_primary, expected_sessions = _files(source), _files(resolve_session_state_root(source, paths))
    with monkeypatch.context() as faults:
        for name in originals:
            faults.setattr(os, name, instrument(name, None, False))
        await migration.migrate_private_storage(paths)
    checkpoints = len(observed)
    assert {"fsync", "replace", "rename", "unlink", "symlink"} <= set(observed)
    for checkpoint in range(1, checkpoints + 1):
        for after in (False, True):
            base = tmp_path / f"crash-{checkpoint}-{after}"
            base.mkdir()
            paths = _paths(base)
            _seed(paths, _OLD, _REQUESTER)
            observed.clear()
            with monkeypatch.context() as faults:
                for name in originals:
                    faults.setattr(os, name, instrument(name, checkpoint, after))
                with pytest.raises(OSError, match="injected interruption"):
                    await migration.migrate_private_storage(paths)
            await migration.migrate_private_storage(paths)
            target = private_instance_scope_root_path(paths.storage_root, _NEW)
            old = private_instance_scope_root_path(paths.storage_root, _OLD)
            assert old.is_symlink()
            assert old.samefile(target)
            assert resolve_session_state_root(old, paths).samefile(resolve_session_state_root(target, paths))
            assert not (target / _INTENT).exists()
            assert _files(target) == expected_primary
            assert _files(resolve_session_state_root(target, paths)) == expected_sessions
            assert load_private_instance_identity(paths.storage_root, target).worker_key == _NEW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    ["primary", "session", "owner", "recordless", "orphan", "scope_link", "record_link", "intent"],
)
async def test_preflight_rejects_conflicts_before_any_move(
    tmp_path: Path,
    conflict: str,
) -> None:
    """A conflicting later scope must not leave an earlier verified scope partially moved."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    sessions = resolve_session_state_root(source, paths)
    if conflict == "primary":
        target.mkdir()
    elif conflict == "session":
        resolve_session_state_root(target, paths).mkdir()
    elif conflict == "owner":
        payload = json.loads((source / _RECORD).read_text())
        payload["requester_id"] = "@intruder:example.org"
        (source / _RECORD).write_text(json.dumps(payload))
    elif conflict == "recordless":
        (source / _RECORD).unlink()
    elif conflict == "orphan":
        orphan = sessions.parent / "unowned"
        orphan.mkdir()
        (orphan / "private.db").write_bytes(b"orphan session")
    elif conflict == "scope_link":
        (source.parent / "linked").symlink_to(source, target_is_directory=True)
    elif conflict == "record_link":
        saved = source / "saved-owner.json"
        (source / _RECORD).rename(saved)
        (source / _RECORD).symlink_to(saved)
    else:
        (source / _INTENT).write_text('{"unrelated": true}')
    before = _files(source)
    with pytest.raises(ValueError, match=r"[Pp]rivate"):
        await migration.migrate_private_storage(paths)
    assert source.exists()
    assert sessions.exists()
    assert _files(source) == before


@pytest.mark.asyncio
async def test_current_and_fresh_startup_skip_workers_and_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legitimate new traffic never causes a completed scope to be migrated again."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    preflight = importlib.import_module("mindroom.workers.storage_preflight")
    paths = _paths(tmp_path)
    _seed(paths, _OLD, _REQUESTER)
    await migration.migrate_private_storage(paths)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    (target / "new-traffic.txt").write_text("legitimate new traffic")
    inode = target.stat().st_ino
    monkeypatch.setattr(
        preflight,
        "check_workers_absent_for_storage_upgrade",
        Mock(side_effect=AssertionError("unexpected stop")),
    )
    monkeypatch.setattr(os, "walk", Mock(side_effect=AssertionError("unexpected contents traversal")))
    await migration.migrate_private_storage(paths)
    assert target.stat().st_ino == inode
    assert (target / "new-traffic.txt").read_text() == "legitimate new traffic"
    fresh = replace(paths, storage_root=tmp_path / "fresh", process_env={})
    await migration.migrate_private_storage(fresh)
    assert not fresh.storage_root.exists()


async def _interrupt_after_session_move(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> Path:
    migration = importlib.import_module("mindroom.private_storage_migration")
    rename = Path.rename

    def interrupt(source: Path, target: Path) -> Path:
        rename(source, target)
        message = "session moved"
        raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "rename", interrupt)
        with pytest.raises(OSError, match="session moved"):
            await migration.migrate_private_storage(paths)
    return private_instance_scope_root_path(paths.storage_root, _OLD)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["missing_session", "duplicate_session", "copied_scope", "changed_root", "boolean_inode", "wrong_owner"],
)
async def test_recovery_rejects_missing_or_unrelated_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    """An intent authorizes only its exact roots, original directories and saved owner."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    _seed(paths, _OLD, _REQUESTER)
    source = await _interrupt_after_session_move(paths, monkeypatch)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    session_target = resolve_session_state_root(target, paths)
    if damage == "missing_session":
        shutil.rmtree(session_target)
    elif damage == "duplicate_session":
        resolve_session_state_root(source, paths).mkdir()
    elif damage == "copied_scope":
        original = source.with_name("saved")
        source.rename(original)
        shutil.copytree(original, source)
        shutil.rmtree(original)
    elif damage == "changed_root":
        wrong = tmp_path / "wrong-sessions"
        wrong.mkdir()
        paths = replace(paths, process_env={"MINDROOM_SESSION_STORAGE_PATH": str(wrong)})
    elif damage == "boolean_inode":
        payload = json.loads((source / _INTENT).read_text())
        payload["primary_inode"] = True
        (source / _INTENT).write_text(json.dumps(payload))
    else:
        payload = json.loads((source / _RECORD).read_text())
        payload["requester_id"] = "@intruder:example.org"
        (source / _RECORD).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=r"[Pp]rivate"):
        await migration.migrate_private_storage(paths)
    assert source.exists()
    assert not target.exists()


@pytest.mark.asyncio
async def test_recovery_accepts_remount_with_same_inodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kernel device identifiers may change across mounts without invalidating durable intent."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    _seed(paths, _OLD, _REQUESTER)
    source = await _interrupt_after_session_move(paths, monkeypatch)

    # Shift both lstat and fstat device numbers as a remount would do.
    def remounted(call: Callable) -> Callable:
        def invoke(*args: object, **kwargs: object) -> os.stat_result:
            original = call(*args, **kwargs)
            fields = list(original)
            fields[2] += 1000
            return os.stat_result(fields)

        return invoke

    monkeypatch.setattr(os, "stat", remounted(os.stat))
    monkeypatch.setattr(os, "fstat", remounted(os.fstat))
    await migration.migrate_private_storage(paths)
    assert source.is_symlink()
    assert load_private_instance_identity(
        paths.storage_root,
        private_instance_scope_root_path(paths.storage_root, _NEW),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["absolute_old", "absolute_new", "relative_escape", "safe_absolute", "safe_relative"])
async def test_relocation_checks_links_without_rewriting_them(tmp_path: Path, kind: str) -> None:
    """Historical aliases keep opaque link targets unchanged, including absolute references."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    link = source / "link"
    targets = {
        "absolute_old": str(source / "writer/workspace/notes.txt"),
        "absolute_new": str(private_instance_scope_root_path(paths.storage_root, _NEW) / "writer/workspace/notes.txt"),
        "relative_escape": f"../{source.name}/writer/workspace/notes.txt",
        "safe_absolute": str(tmp_path / "shared-knowledge"),
        "safe_relative": "writer/workspace/notes.txt",
    }
    link.symlink_to(targets[kind])
    await migration.migrate_private_storage(paths)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert str((target / "link").readlink()) == targets[kind]


@pytest.mark.asyncio
async def test_cancellation_keeps_locks_until_blocking_migration_drains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled admission cannot release volume locks while the worker thread still moves state."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    preflight = importlib.import_module("mindroom.workers.storage_preflight")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    stopped, release = threading.Event(), threading.Event()

    def pause(*_args: object, **_kwargs: object) -> None:
        stopped.set()
        assert release.wait(10)

    monkeypatch.setattr(preflight, "check_workers_absent_for_storage_upgrade", pause)
    task = asyncio.create_task(migration.migrate_private_storage(paths))
    assert await asyncio.to_thread(stopped.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert file_lock_is_held(paths.storage_root / ".mindroom-storage-upgrade.lock")
        assert source.exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.is_symlink()
    assert not file_lock_is_held(paths.storage_root / ".mindroom-storage-upgrade.lock")


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["api", "orchestrator"])
async def test_primary_admission_fails_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Both primary entry points must reject unowned storage before starting runtime work."""
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    (source / _RECORD).unlink()
    module = importlib.import_module(f"mindroom.{'api.main' if entrypoint == 'api' else 'orchestrator'}")
    monkeypatch.setattr(module, "sync_env_to_credentials", Mock(side_effect=AssertionError("credentials started")))
    if entrypoint == "api":
        monkeypatch.setattr(module, "_app_runtime_paths", lambda _app: paths)
        with pytest.raises(ValueError, match="authoritative owner"):
            async with module._lifespan(module.app):
                pytest.fail("API admitted runtime work")
    else:
        with pytest.raises(ValueError, match="authoritative owner"):
            await module.main("ERROR", paths, api=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["null", "false", "[]", '{"version": 1}'])
async def test_malformed_intent_never_becomes_fresh_migration(tmp_path: Path, payload: str) -> None:
    """A present but invalid intent must not be replaced with newly inferred evidence."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    (source / _INTENT).write_text(payload)
    with pytest.raises(ValueError, match="intent"):
        await migration.migrate_private_storage(paths)
    assert (source / _INTENT).read_text() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["scope", "nested", "namespace", "file"])
async def test_nested_mounts_fail_before_any_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Scope, nested-directory and namespace mount boundaries cannot be moved by startup."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    mounted = {
        "scope": source,
        "nested": source / "writer/workspace",
        "namespace": source.parent,
        "file": source / "writer/workspace/notes.txt",
    }[location]
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    original = os.stat

    def mounted_stat(path: str | Path, *args: object, **kwargs: object) -> os.stat_result:
        info = original(path, *args, **kwargs)
        if Path(path).is_relative_to(mounted) and (stat.S_ISDIR(info.st_mode) or Path(path) == mounted):
            fields = list(info)
            fields[2] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "stat", mounted_stat)
    with pytest.raises(ValueError, match="mount"):
        await migration.migrate_private_storage(paths)
    assert source.exists()


@pytest.mark.asyncio
async def test_previously_colliding_requesters_keep_distinct_state(tmp_path: Path) -> None:
    """A colon-bearing owner and its underscore lookalike remain isolated after startup."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    first = _seed(paths, _OLD, _REQUESTER)
    second = _seed(paths, "v1:default:user:@alice_example.org", "@alice_example.org")
    (first / "owner.txt").write_text("colon owner")
    (second / "owner.txt").write_text("underscore owner")
    await migration.migrate_private_storage(paths)
    targets = [
        private_instance_scope_root_path(paths.storage_root, key)
        for key in (_NEW, "v1:default:user:~@alice_example.org")
    ]
    assert targets[0] != targets[1]
    assert [(target / "owner.txt").read_text() for target in targets] == ["colon owner", "underscore owner"]


@pytest.mark.asyncio
async def test_missing_configured_session_volume_blocks_startup(tmp_path: Path) -> None:
    """A typo or missing mounted session volume must not silently discard the mirror."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    paths = replace(paths, process_env={"MINDROOM_SESSION_STORAGE_PATH": str(tmp_path / "missing")})
    with pytest.raises(ValueError, match="volume is missing"):
        await migration.migrate_private_storage(paths)
    assert source.exists()


@pytest.mark.asyncio
async def test_absent_optional_mirror_stays_absent_on_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly absent mirror cannot acquire unrelated data during recovery."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    shutil.rmtree(resolve_session_state_root(source, paths))
    await _interrupt_after_session_move(paths, monkeypatch)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert json.loads((target / _INTENT).read_text())["session_inode"] is None
    resolve_session_state_root(target, paths).mkdir()
    with pytest.raises(ValueError, match="unexpected session"):
        await migration.migrate_private_storage(paths)


@pytest.mark.asyncio
async def test_worker_preflight_failure_preserves_original_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage is untouched when managed workers cannot be proven absent."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    preflight = importlib.import_module("mindroom.workers.storage_preflight")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    before = _files(source)
    monkeypatch.setattr(
        preflight,
        "check_workers_absent_for_storage_upgrade",
        Mock(side_effect=RuntimeError("preflight failed")),
    )
    with pytest.raises(RuntimeError, match="preflight failed"):
        await migration.migrate_private_storage(paths)
    assert _files(source) == before
    assert not (source / _INTENT).exists()
    assert not private_instance_scope_root_path(paths.storage_root, _NEW).exists()
    assert resolve_session_state_root(source, paths).exists()


@pytest.mark.asyncio
async def test_recovery_rejects_boolean_owner_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Intent recovery must retain the strict owner schema used for fresh discovery."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    _seed(paths, _OLD, _REQUESTER)
    source = await _interrupt_after_session_move(paths, monkeypatch)
    payload = json.loads((source / _RECORD).read_text())
    payload["version"] = True
    (source / _RECORD).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="version"):
        await migration.migrate_private_storage(paths)
    assert source.exists()


@pytest.mark.asyncio
async def test_unreadable_pending_tree_stops_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-Linux mount scan fails closed when directory metadata is unreadable."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    original = os.scandir

    def unreadable(path: Path | str) -> object:
        if Path(path) == source / "writer/workspace":
            message = "unreadable pending directory"
            raise PermissionError(message)
        return original(path)

    monkeypatch.setattr(os, "scandir", unreadable)
    with pytest.raises(PermissionError, match="unreadable pending"):
        await migration.migrate_private_storage(paths)
    assert source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [_INTENT, _RECORD])
async def test_abrupt_process_exit_leaves_harmless_partial_temporary(
    tmp_path: Path,
    reserved: str,
) -> None:
    """Real process death bypasses durable-writer cleanup; partial temporaries stay opaque on retry."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    program = textwrap.dedent("""
        import asyncio
        import json
        import os
        import sys
        from pathlib import Path
        from mindroom.constants import resolve_runtime_paths
        from mindroom.private_storage_migration import migrate_private_storage
        paths = resolve_runtime_paths(
            config_path=Path(sys.argv[1]), storage_path=Path(sys.argv[2]),
            process_env={"MINDROOM_SESSION_STORAGE_PATH": sys.argv[3]},
        )
        original = json.dump
        def interrupted(payload, stream, **kwargs):
            if Path(stream.name).name.startswith(sys.argv[4] + "."):
                stream.write('{"partial":')
                stream.flush()
                os.fsync(stream.fileno())
                os._exit(17)
            return original(payload, stream, **kwargs)
        json.dump = interrupted
        asyncio.run(migrate_private_storage(paths))
    """)
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            program,
            str(paths.config_path),
            str(paths.storage_root),
            str(resolve_session_state_root(paths.storage_root, paths)),
            reserved,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 17, result.stderr
    temporaries = list(paths.storage_root.glob(f"private_instances/*/{reserved}.*.tmp"))
    assert len(temporaries) == 1
    temporary = temporaries[0]
    assert temporary.read_bytes() == b'{"partial":'
    await migration.migrate_private_storage(paths)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert source.is_symlink()
    assert (target / temporary.name).read_bytes() == b'{"partial":'
    assert load_private_instance_identity(paths.storage_root, target).worker_key == _NEW


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["user", "user_agent"])
async def test_current_runtime_export_and_mounts_find_migrated_contents(tmp_path: Path, scope: str) -> None:
    """Normal consumers resolve moved owner data through unchanged current-key paths."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    old_key = _OLD if scope == "user" else "v1:default:user_agent:@alice:example.org:writer"
    source = _seed(paths, old_key, _REQUESTER)
    await migration.migrate_private_storage(paths)
    config = Config(
        agents={"writer": AgentConfig(display_name="Writer", private=AgentPrivateConfig(per=scope, root="workspace"))},
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id=_REQUESTER,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    runtime = resolve_agent_runtime("writer", config, paths, identity)
    assert (runtime.state_root / "workspace/notes.txt").read_bytes() == b"private workspace\x00retained"
    assert (runtime.session_state_root / "sessions/credentials.bin").read_bytes() == b"opaque credentials"
    targets = _private_targets(config, paths, "writer", "@writer:example.org", AgentThreadExportConfig())
    assert len(targets) == 1
    assert targets[0].output_dir == runtime.state_root / "workspace/thread_exports"
    assert _REQUESTER in targets[0].required_member_user_ids
    mounts = plan_scoped_visible_state_roots(
        worker_key=runtime.execution.worker_key,
        local_shared_storage_root=paths.storage_root,
        worker_visible_shared_storage_root=Path("/app/worker"),
        private_agent_names=frozenset({"writer"}),
        allow_unknown_worker_key=False,
    )
    private_mounts = [mount for mount in mounts if mount.local_path == runtime.state_root.parent]
    assert {(mount.local_path, mount.worker_visible_path) for mount in private_mounts} == {
        (runtime.state_root.parent, Path("/app/worker/private_instances") / runtime.state_root.parent.name),
        (runtime.state_root.parent, Path("/app/worker/private_instances") / source.name),
    }
    assert (
        private_mounts[0].local_path / "writer/workspace/notes.txt"
    ).read_bytes() == b"private workspace\x00retained"
    assert source.is_symlink()


@pytest.mark.parametrize("state", ["fresh", "ownerless", "owned_without_alias", "foreign_collision"])
def test_worker_mount_plan_never_infers_historical_access(tmp_path: Path, state: str) -> None:
    """Canonical-only scopes remain usable without granting unverified historical destinations."""
    worker_key = "v1:default:user:~alice_bob"
    canonical = private_instance_scope_root_path(tmp_path, worker_key)
    if state == "ownerless":
        canonical.mkdir(parents=True)
        (canonical / "notes.txt").write_text("existing unowned workspace")
    elif state in {"owned_without_alias", "foreign_collision"}:
        ensure_private_instance_identity(tmp_path, worker_key=worker_key, requester_id="alice_bob")
    if state == "foreign_collision":
        historical_key = "v1:default:user:alice_bob"
        foreign_key = reconstruct_private_instance_worker_key(historical_key, "alice/bob")
        ensure_private_instance_identity(tmp_path, worker_key=foreign_key, requester_id="alice/bob")
        foreign = private_instance_scope_root_path(tmp_path, foreign_key)
        private_instance_scope_root_path(tmp_path, historical_key).symlink_to(foreign.name)

    mounts = plan_scoped_visible_state_roots(
        worker_key=worker_key,
        local_shared_storage_root=tmp_path,
        worker_visible_shared_storage_root=Path("/app/worker"),
        private_agent_names=frozenset(),
        allow_unknown_worker_key=False,
    )

    assert {(mount.local_path, mount.worker_visible_path) for mount in mounts} == {
        (tmp_path / "agents", Path("/app/worker/agents")),
        (canonical, Path("/app/worker/private_instances") / canonical.name),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_sync_fails", [False, True])
async def test_retry_durably_publishes_existing_intent_before_any_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_sync_fails: bool,
) -> None:
    """A visible intent left by failed directory fsync must become durable before recovery renames."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    mirror = resolve_session_state_root(source, paths)
    inode = source.stat().st_ino
    fsync, rename = os.fsync, Path.rename
    events: list[str] = []

    def fail_intent_sync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == inode and (source / _INTENT).exists():
            message = "intent directory sync failed"
            raise OSError(message)
        fsync(descriptor)

    with monkeypatch.context() as first:
        first.setattr(os, "fsync", fail_intent_sync)
        with pytest.raises(OSError, match="intent directory sync failed"):
            await migration.migrate_private_storage(paths)
    assert (source / _INTENT).is_file()
    assert mirror.is_dir()

    def retry_sync(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == inode:
            if retry_sync_fails:
                message = "retry directory sync failed"
                raise OSError(message)
            fsync(descriptor)
            events.append("intent directory synced")
        else:
            fsync(descriptor)

    def record_rename(origin: Path, destination: Path) -> Path:
        events.append("rename")
        return rename(origin, destination)

    with monkeypatch.context() as retry:
        retry.setattr(os, "fsync", retry_sync)
        retry.setattr(Path, "rename", record_rename)
        if retry_sync_fails:
            with pytest.raises(OSError, match="retry directory sync failed"):
                await migration.migrate_private_storage(paths)
        else:
            await migration.migrate_private_storage(paths)
    if retry_sync_fails:
        assert events == []
        assert source.is_dir()
        assert mirror.is_dir()
    else:
        assert events.index("intent directory synced") < events.index("rename")
    await migration.migrate_private_storage(paths)
    assert source.is_symlink()
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert (target / "writer/workspace/notes.txt").read_bytes() == b"private workspace\x00retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("indirect", [False, True])
async def test_relative_link_chain_keeps_old_absolute_paths(tmp_path: Path, indirect: bool) -> None:
    """Links through external directories can still return to retained historical names."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    other = _seed(paths, "v1:default:user:@bob:example.org", "@bob:example.org")
    shared = tmp_path / "shared"
    shared.mkdir()
    (source / "shared").symlink_to(shared, target_is_directory=True)
    target = f"shared/../state/private_instances/{source.name}/writer/workspace/notes.txt"
    if indirect:
        (source / "intermediate").symlink_to(target)
        target = "intermediate"
    (source / "link").symlink_to(target)
    assert (source / "link").read_bytes() == b"private workspace\x00retained"
    await migration.migrate_private_storage(paths)
    for original in (source, other):
        assert original.is_symlink()
        assert resolve_session_state_root(original, paths).is_dir()
        assert not (original / _INTENT).exists()
    assert (source / "link").read_bytes() == b"private workspace\x00retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["contained_chain", "dangling", "cycle"])
async def test_relative_link_chain_policy(tmp_path: Path, kind: str) -> None:
    """Contained, dangling and cyclic workspace links remain opaque and unchanged."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    targets = {"contained_chain": "writer/workspace", "dangling": "missing", "cycle": "link"}
    (source / "intermediate").symlink_to(targets[kind], target_is_directory=True)
    (source / "link").symlink_to("intermediate" if kind == "cycle" else "intermediate/notes.txt")
    await migration.migrate_private_storage(paths)
    target = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert str((target / "intermediate").readlink()) == targets[kind]
    assert str((target / "link").readlink()) == ("intermediate" if kind == "cycle" else "intermediate/notes.txt")
    if kind == "contained_chain":
        assert (target / "link").read_bytes() == b"private workspace\x00retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True])
async def test_historical_absolute_shebang_survives_migration(tmp_path: Path, separate: bool) -> None:
    """Executable bytes and both absolute names remain usable after relocation."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path, separate=separate)
    old = _seed(paths, _OLD, _REQUESTER)
    command = old / "writer/workspace/example-cli"
    interpreter = old / "writer/workspace/python"
    interpreter.symlink_to(sys.executable)
    command.write_text(f'#!{interpreter}\nprint("preserved")\n')
    command.chmod(0o700)
    before = command.read_bytes()
    inode = old.stat().st_ino
    old_sessions = resolve_session_state_root(old, paths)
    assert (await asyncio.to_thread(subprocess.check_output, [str(command)], text=True)).strip() == "preserved"
    await migration.migrate_private_storage(paths)
    new = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert old.is_symlink()
    assert old.readlink() == Path(new.name)
    assert old.samefile(new)
    assert new.stat().st_ino == inode
    assert (new / "writer/workspace/example-cli").read_bytes() == before
    assert (await asyncio.to_thread(subprocess.check_output, [str(command)], text=True)).strip() == "preserved"
    assert (
        await asyncio.to_thread(subprocess.check_output, [str(new / "writer/workspace/example-cli")], text=True)
    ).strip() == "preserved"
    new_sessions = resolve_session_state_root(new, paths)
    assert old_sessions.samefile(new_sessions)
    assert old_sessions.readlink() == Path(new_sessions.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["scope", "nested", "namespace", "file", "session", "destination"])
@pytest.mark.parametrize("escaped", [False, True])
async def test_linux_bind_mounts_fail_before_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    escaped: bool,
) -> None:
    """The mount table detects same-device binds, including kernel-escaped mountpoints."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    base = tmp_path / "space tab\t slash\\" if escaped else tmp_path
    base.mkdir(exist_ok=True)
    paths = _paths(base)
    source = _seed(paths, _OLD, _REQUESTER)
    mounted = {
        "scope": source,
        "nested": source / "writer/workspace",
        "namespace": source.parent,
        "file": source / "writer/workspace/notes.txt",
        "session": resolve_session_state_root(source, paths),
        "destination": private_instance_scope_root_path(paths.storage_root, _NEW),
    }[location]
    mount_text = str(mounted).replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")
    table = f"1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 8:1 / {mount_text} rw shared:1 - ext4 /dev/root rw\n"
    original = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        return table if str(path) == "/proc/self/mountinfo" else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(migration.sys, "platform", "linux")
    with pytest.raises(ValueError, match="mount"):
        await migration.migrate_private_storage(paths)
    assert not source.is_symlink()
    assert not (source / _INTENT).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    ["", "garbage", "1 0 8:1 / relative rw - ext4 root rw", "1 0 8:1 / /bad\\999 rw - ext4 root rw", None],
)
async def test_linux_unavailable_mount_table_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str | None,
) -> None:
    """Missing or malformed Linux mount evidence cannot authorize a rename."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    original = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) != "/proc/self/mountinfo":
            return original(path, *args, **kwargs)
        if table is None:
            message = "mount table unavailable"
            raise PermissionError(message)
        return table

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(migration.sys, "platform", "linux")
    with pytest.raises(ValueError, match="mount"):
        await migration.migrate_private_storage(paths)
    assert not source.is_symlink()
    assert not (source / _INTENT).exists()


@pytest.mark.asyncio
async def test_linux_reads_mount_table_once_without_walking_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured volume mounts are allowed and preflight work is bounded by mount count."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    source = _seed(paths, _OLD, _REQUESTER)
    roots = [paths.storage_root, resolve_session_state_root(paths.storage_root, paths)]
    table = "1 0 8:1 / / rw - ext4 /dev/root rw\n9 1 0:4 net:[123] /run/netns/test rw - nsfs nsfs rw\n" + "".join(
        f"{index} 1 8:{index} / {root} rw - ext4 /dev/volume rw\n" for index, root in enumerate(roots, 2)
    )
    original = Path.read_text
    reads = []

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/self/mountinfo":
            reads.append(path)
            assert len(reads) == 1
            return table
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(migration.sys, "platform", "linux")
    monkeypatch.setattr(os, "walk", Mock(side_effect=AssertionError("workspace traversal")))
    await migration.migrate_private_storage(paths)
    assert source.is_symlink()
    assert len(reads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["foreign", "missing", "chain", "absolute", "owner", "mirror", "orphan_mirror"])
async def test_completed_aliases_reject_tampering(tmp_path: Path, damage: str) -> None:
    """Completed aliases are accepted only at their exact owner-bound primary and mirror names."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    old = _seed(paths, _OLD, _REQUESTER)
    old_mirror = resolve_session_state_root(old, paths)
    await migration.migrate_private_storage(paths)
    new = private_instance_scope_root_path(paths.storage_root, _NEW)
    if damage == "foreign":
        old.rename(old.with_name("foreign"))
    elif damage == "missing":
        shutil.rmtree(new)
    elif damage == "chain":
        old.unlink()
        (old.parent / "chain").symlink_to(new.name)
        old.symlink_to("chain")
    elif damage == "absolute":
        old.unlink()
        old.symlink_to(new)
    elif damage == "owner":
        payload = json.loads((new / _RECORD).read_text())
        payload["requester_id"] = "@other:example.org"
        (new / _RECORD).write_text(json.dumps(payload))
    elif damage == "mirror":
        old_mirror.unlink()
        old_mirror.symlink_to("different")
    else:
        old.unlink()
    with pytest.raises(ValueError, match=r"[Pp]rivate"):
        await migration.migrate_private_storage(paths)


@pytest.mark.asyncio
async def test_current_scope_without_alias_is_not_given_historical_provenance(tmp_path: Path) -> None:
    """A current owner record alone cannot authorize creating an old path."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    current = _seed(paths, _NEW, _REQUESTER)
    await migration.migrate_private_storage(paths)
    assert current.is_dir()
    assert not current.is_symlink()
    assert not private_instance_scope_root_path(paths.storage_root, _OLD).exists()


@pytest.mark.asyncio
async def test_empty_orphan_session_placeholder_remains_tolerated(tmp_path: Path) -> None:
    """Empty real placeholders keep prior startup tolerance without gaining alias authority."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    old = _seed(paths, _OLD, _REQUESTER)
    mirror = resolve_session_state_root(old, paths)
    placeholder = mirror.parent / "unowned"
    placeholder.mkdir()
    await migration.migrate_private_storage(paths)
    await migration.migrate_private_storage(paths)
    assert old.is_symlink()
    assert placeholder.is_dir()
    assert not placeholder.is_symlink()
    assert list(placeholder.iterdir()) == []
    assert not (old / _INTENT).exists()


@pytest.mark.asyncio
async def test_later_session_data_gets_no_guessed_alias(tmp_path: Path) -> None:
    """A mirror absent at migration may later hold legitimate current-key data."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    old = _seed(paths, _OLD, _REQUESTER)
    old_mirror = resolve_session_state_root(old, paths)
    shutil.rmtree(old_mirror)
    await migration.migrate_private_storage(paths)
    current = private_instance_scope_root_path(paths.storage_root, _NEW)
    current_mirror = resolve_session_state_root(current, paths)
    current_mirror.mkdir()
    (current_mirror / "session.db").write_bytes(b"later session")
    await migration.migrate_private_storage(paths)
    assert old.is_symlink()
    assert not old_mirror.exists()
    assert not old_mirror.is_symlink()
    assert (current_mirror / "session.db").read_bytes() == b"later session"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["early_session_alias", "absolute_session_alias", "primary_before_session", "changed_owner"],
)
async def test_recovery_aliases_require_recorded_publication_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    """An exact link target cannot excuse a wrong owner or impossible publication order."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    old = _seed(paths, _OLD, _REQUESTER)
    old_mirror = resolve_session_state_root(old, paths)
    new = private_instance_scope_root_path(paths.storage_root, _NEW)
    new_mirror = resolve_session_state_root(new, paths)
    if damage in {"early_session_alias", "absolute_session_alias"}:
        await _interrupt_after_session_move(paths, monkeypatch)
        old_mirror.symlink_to(new_mirror if damage == "absolute_session_alias" else new_mirror.name)
    else:
        original = os.unlink

        def interrupt(path: str | Path, *args: object, **kwargs: object) -> None:
            if Path(path).name == _INTENT:
                message = "retain intent"
                raise OSError(message)
            original(path, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "unlink", interrupt)
            with pytest.raises(OSError, match="retain intent"):
                await migration.migrate_private_storage(paths)
        if damage == "primary_before_session":
            old_mirror.unlink()
        else:
            payload = json.loads((new / _RECORD).read_text())
            payload["worker_key"] = _OLD
            (new / _RECORD).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=r"[Pp]rivate"):
        await migration.migrate_private_storage(paths)
    assert (new if new.exists() else old).joinpath(_INTENT).is_file()


@pytest.mark.asyncio
async def test_alias_publication_does_not_overwrite_new_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A namespace conflict appearing after preflight remains intact with recovery evidence."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path, separate=False)
    old = _seed(paths, _OLD, _REQUESTER)
    original = os.symlink

    def conflict(target: str, link: str | Path, *args: object, **kwargs: object) -> None:
        Path(link).mkdir()
        (Path(link) / "foreign.txt").write_text("keep me")
        original(target, link, *args, **kwargs)

    monkeypatch.setattr(os, "symlink", conflict)
    with pytest.raises(FileExistsError):
        await migration.migrate_private_storage(paths)
    assert (old / "foreign.txt").read_text() == "keep me"
    new = private_instance_scope_root_path(paths.storage_root, _NEW)
    assert (new / _INTENT).is_file()
    assert (new / "writer/workspace/notes.txt").read_bytes() == b"private workspace\x00retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_separate_volumes_may_use_distinct_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    """Each scope rename stays on its own device; workspace symlinks are never traversed."""
    migration = importlib.import_module("mindroom.private_storage_migration")
    paths = _paths(tmp_path)
    old = _seed(paths, _OLD, _REQUESTER)
    old_mirror = resolve_session_state_root(old, paths)
    sessions = old_mirror.parent.parent
    (old / "external").symlink_to(sys.executable)
    (old / "dangling").symlink_to("missing")
    original = os.stat

    def separate_device(path: str | Path, *args: object, **kwargs: object) -> os.stat_result:
        info = original(path, *args, **kwargs)
        if Path(path).is_relative_to(sessions):
            fields = list(info)
            fields[2] += 1000
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "stat", separate_device)
    monkeypatch.setattr(migration.sys, "platform", platform)
    await migration.migrate_private_storage(paths)
    assert old.is_symlink()
    assert old_mirror.is_symlink()
    assert os.readlink(old / "external") == sys.executable  # noqa: PTH115 - Compare literal link bytes.
    assert os.readlink(old / "dangling") == "missing"  # noqa: PTH115 - Compare literal link bytes.
