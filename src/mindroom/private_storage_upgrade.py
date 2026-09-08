"""Explicit offline relocation of owner-verified legacy private scopes.

Writers, including old binaries and workers without shared-volume visibility, must
remain stopped throughout planning, apply, and recovery. Worker credentials are
never inferred from private owner records. Journals contain private owner mappings;
keep them with the protected volumes and retain a verified backup.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from mindroom.durable_write import fsync_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.private_instance_identity_store import (
    PrivateInstanceIdentityError,
    load_private_instance_identity,
    load_private_instance_record_payload,
    parse_private_instance_identity_payload,
    reconstruct_private_instance_worker_key,
)
from mindroom.tool_system.worker_routing import normalize_worker_key_part, private_instance_scope_root_path

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_RECORD_FILENAME = ".mindroom-private-instance.json"
_MARKER = ".mindroom-storage-upgrade.json"
_LOCK = ".mindroom-storage-upgrade.lock"


class StorageUpgradeError(ValueError):
    """Private storage cannot safely be relocated from the supplied evidence."""


class _StorageUpgradeRequiredError(StorageUpgradeError):
    """Runtime startup or cleanup must wait for offline storage recovery."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Volume(_FrozenModel):
    path: str
    device: int
    inode: int


class _Move(_FrozenModel):
    volume: int
    inventory: str
    device: int
    inode: int


class _Operation(_FrozenModel):
    source: str
    destination: str
    requester_id: str
    old_key: str
    new_key: str
    record: str
    record_mode: int
    record_uid: int
    record_gid: int
    record_atime: int
    record_mtime: int
    record_xattrs: dict[str, str]
    scope_atime: int
    scope_mtime: int
    moves: tuple[_Move, ...]


class StorageUpgradePlan(_FrozenModel):
    """Protected immutable owner mapping; normal CLI output exposes only counts."""

    version: Literal[1] = 1
    volumes: tuple[_Volume, ...]
    operations: tuple[_Operation, ...]
    unresolved_worker_files: int
    control_state: str


class _Journal(_FrozenModel):
    plan: StorageUpgradePlan
    status: Literal["prepared", "moving", "complete", "reversing", "rolled_back"]


def _fail(message: str) -> None:
    raise StorageUpgradeError(message)


def _directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        _fail("Storage roots and namespaces must be real directories")
    return info


def _root(path: Path) -> Path:
    path = path.expanduser().absolute()
    for ancestor in (*reversed(path.parents), path):
        _directory(ancestor)
    return path.resolve(strict=True)


def _read_journal(path: Path) -> _Journal | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 64 * 1024 * 1024:
        _fail("Invalid storage upgrade participation marker")
    journal = _Journal.model_validate(load_private_instance_record_payload(path, max_bytes=64 * 1024 * 1024))
    if not journal.plan.volumes or len(journal.plan.volumes) > 2:
        _fail("Invalid storage upgrade participant list")
    if path.parent not in {Path(volume.path) for volume in journal.plan.volumes}:
        _fail("Storage upgrade marker is outside its participating volumes")
    return journal


def _legacy_keys(scope: Path) -> tuple[str, str, str] | None:
    """Prove the old encoder and directory hash from one exact raw owner."""
    record = scope / _RECORD_FILENAME
    if record.lstat().st_size > 65536:
        _fail("Private owner record exceeds the size limit")
    identity = parse_private_instance_identity_payload(load_private_instance_record_payload(record))
    current = reconstruct_private_instance_worker_key(identity.worker_key, identity.requester_id)
    if private_instance_scope_root_path(scope.parent.parent, identity.worker_key) != scope:
        _fail("Private owner record does not match its directory hash")
    if identity.worker_key == current:
        return None
    parts = identity.worker_key.split(":")
    tenant, worker_scope = parts[1:3]
    requester = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", identity.requester_id.strip()).strip("_") or "default"
    old = f"v1:{normalize_worker_key_part(tenant)}:{worker_scope}:{requester}"
    if worker_scope == "user_agent":
        old += ":" + normalize_worker_key_part(parts[-1])
    if old != identity.worker_key:
        _fail("Private owner record does not match the legacy encoder")
    return old, current, identity.requester_id


def _inventory(root: Path, old_paths: tuple[str, ...]) -> str:
    """Hash names, bytes, modes, owners and timestamps without following links."""
    digest = hashlib.sha256()
    for path in sorted((root, *root.rglob("*"))):
        relative = path.relative_to(root).as_posix()
        if relative == _RECORD_FILENAME:
            continue
        info = path.lstat()
        # Directory mtime also changes when SQLite creates and removes read-side sidecars.
        # File sizes frame the content stream so adjacent entries cannot be conflated.
        metadata = (
            relative,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            0 if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns,
            info.st_size if not stat.S_ISDIR(info.st_mode) else 0,
        )
        digest.update(json.dumps(metadata).encode())
        if stat.S_ISLNK(info.st_mode):
            target = str(path.readlink())
            if Path(target).is_absolute():
                _fail("Absolute symlink requires an explicit relocation repair")
            digest.update(target.encode())
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                _fail("Hard-linked data requires an explicit relocation repair")
            with path.open("rb") as source:
                tail = b""
                while chunk := source.read(1024 * 1024):
                    if any(old.encode() in tail + chunk for old in old_paths):
                        _fail("Persisted absolute path requires an explicit relocation repair")
                    digest.update(chunk)
                    tail = (tail + chunk)[-max(map(len, old_paths), default=1) :]
        elif not stat.S_ISDIR(info.st_mode):
            _fail("Special files require offline recovery before relocation")
    return digest.hexdigest()


def _volumes(storage: Path, sessions: Path | None) -> tuple[_Volume, ...]:
    roots = dict.fromkeys((_root(storage), _root(sessions or storage)))
    if len(roots) > 1:
        first, second = roots
        if first.is_relative_to(second) or second.is_relative_to(first):
            _fail("Participating storage roots must not overlap")
    return tuple(_Volume(path=str(root), device=root.stat().st_dev, inode=root.stat().st_ino) for root in roots)


def _validate_volumes(plan: StorageUpgradePlan) -> None:
    for volume in plan.volumes:
        info = _root(Path(volume.path)).stat()
        if (info.st_dev, info.st_ino) != (volume.device, volume.inode):
            _fail("A required storage volume is missing or changed")
        namespace = Path(volume.path) / "private_instances"
        if namespace.exists() or namespace.is_symlink():
            _directory(namespace)


def plan_storage_upgrade(
    storage: Path,
    sessions: Path | None = None,
    *,
    control_state: Path | None = None,
) -> StorageUpgradePlan:
    """Inspect existing roots without creating storage or adopting unknown owners."""
    try:
        return _plan_storage_upgrade(storage, sessions, control_state)
    except (OSError, PrivateInstanceIdentityError) as exc:
        message = "Private storage ownership or volume validation failed"
        raise StorageUpgradeError(message) from exc


def _plan_storage_upgrade(  # noqa: C901 - ordered preflight refuses partial ownership evidence
    storage: Path,
    sessions: Path | None,
    control_state: Path | None,
) -> StorageUpgradePlan:
    volumes = _volumes(storage, sessions)
    control_root = (control_state or storage / "control_state").expanduser().absolute()
    _check_active_scripts(control_root)
    for volume in volumes:
        if _read_journal(Path(volume.path) / _MARKER) is not None:
            _fail("Existing transaction requires its original plan and recovery command")
    namespace = Path(volumes[0].path) / "private_instances"
    operations = []
    if namespace.exists() or namespace.is_symlink():
        _directory(namespace)
        for scope in sorted(namespace.iterdir()):
            _directory(scope)
            if not any(scope.iterdir()):
                continue
            keys = _legacy_keys(scope)
            if keys is None:
                continue
            old, new, requester = keys
            destination = private_instance_scope_root_path(namespace.parent, new).name
            moves = []
            old_paths = tuple(str(Path(volume.path) / "private_instances" / scope.name) for volume in volumes)
            for index, volume in enumerate(volumes):
                parent = Path(volume.path) / "private_instances"
                if parent.exists() or parent.is_symlink():
                    _directory(parent)
                target = parent / destination
                if target.exists() or target.is_symlink():
                    _fail("Destination already exists; automatic merge is forbidden")
                source = parent / scope.name
                if source.exists() or source.is_symlink():
                    info = _directory(source)
                    moves.append(
                        _Move(
                            volume=index,
                            inventory=_inventory(source, old_paths),
                            device=info.st_dev,
                            inode=info.st_ino,
                        ),
                    )
            record = scope / _RECORD_FILENAME
            info = record.stat()
            operations.append(
                _Operation(
                    source=scope.name,
                    destination=destination,
                    requester_id=requester,
                    old_key=old,
                    new_key=new,
                    record=base64.b64encode(record.read_bytes()).decode(),
                    record_mode=info.st_mode,
                    record_uid=info.st_uid,
                    record_gid=info.st_gid,
                    record_atime=info.st_atime_ns,
                    record_mtime=info.st_mtime_ns,
                    record_xattrs={
                        name: base64.b64encode(os.getxattr(record, name)).decode() for name in os.listxattr(record)
                    },
                    scope_atime=scope.stat().st_atime_ns,
                    scope_mtime=scope.stat().st_mtime_ns,
                    moves=tuple(moves),
                ),
            )
    workers = Path(volumes[0].path) / "workers"
    unresolved = sum(1 for path in workers.glob("*/credentials/**/*") if path.is_file()) if workers.is_dir() else 0
    return StorageUpgradePlan(
        volumes=volumes,
        operations=tuple(operations),
        unresolved_worker_files=unresolved,
        control_state=str(control_root),
    )


def check_storage_upgrade(  # noqa: C901, PLR0912 - inspect every participating volume before access
    storage: Path,
    sessions: Path | None = None,
) -> None:
    """Fail closed before startup, owner discovery, export, or cleanup."""
    try:
        roots = [storage.expanduser().absolute()]
        if sessions is not None and sessions.expanduser().absolute() not in roots:
            roots.append(sessions.expanduser().absolute())
        for root in roots:
            if not root.exists() and not root.is_symlink():
                continue  # A clean installation may create its main storage root.
            _root(root)
            journal = _read_journal(root / _MARKER)
            if journal is not None:
                _validate_volumes(journal.plan)
                if journal.status != "complete":
                    _fail("Storage upgrade is incomplete")
                for volume in journal.plan.volumes:
                    if _read_journal(Path(volume.path) / _MARKER) != journal:
                        _fail("Storage upgrade participation markers disagree")
            namespace = root / "private_instances"
            if not namespace.exists() and not namespace.is_symlink():
                continue
            _directory(namespace)
            for scope in namespace.iterdir():
                if not scope.is_dir() or scope.is_symlink():
                    continue
                try:
                    legacy = _legacy_keys(scope)
                except (OSError, PrivateInstanceIdentityError, StorageUpgradeError):
                    continue  # Preserve existing corrupt/ownerless cleanup semantics.
                if legacy is not None:
                    _fail("Owner-verified legacy private storage requires offline upgrade")
    except (OSError, ValueError) as exc:
        message = "Private storage requires offline upgrade or recovery before runtime access"
        raise _StorageUpgradeRequiredError(message) from exc


def check_runtime_storage_upgrade(runtime_paths: RuntimePaths) -> None:
    """Check main and configured session storage before any runtime side effects."""
    configured = runtime_paths.env_value("MINDROOM_SESSION_STORAGE_PATH")
    sessions = Path(configured).expanduser() if configured and configured.strip() else runtime_paths.storage_root
    if not sessions.is_absolute():
        sessions = runtime_paths.config_dir / sessions
    check_storage_upgrade(runtime_paths.storage_root, sessions)


def _checkpoint() -> None:
    """Fault-injection seam after each durable transition."""


def _publish(
    plan: StorageUpgradePlan,
    status: Literal["prepared", "moving", "complete", "reversing", "rolled_back"],
) -> None:
    journal = _Journal(plan=plan, status=status)
    for volume in plan.volumes:
        write_json_file_durable(
            Path(volume.path) / _MARKER,
            journal.model_dump(mode="json"),
            strict_atomic_replace=True,
        )
        _checkpoint()


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Linux atomic directory rename that cannot overwrite any destination."""
    if sys.platform != "linux":
        _fail("Offline directory relocation requires Linux renameat2 support")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    _checkpoint()
    fsync_directory_durable(source.parent)
    _checkpoint()


def _record_bytes(operation: _Operation, *, new: bool) -> bytes:
    original = base64.b64decode(operation.record, validate=True)
    if not new:
        return original
    payload = json.loads(original)
    payload["worker_key"] = operation.new_key
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _write_record(scope: Path, operation: _Operation, *, new: bool) -> None:
    record = scope / _RECORD_FILENAME
    with NamedTemporaryFile(dir=scope.parent, prefix=".private-owner-", delete=False) as temporary:
        temp = Path(temporary.name)
        try:
            temporary.write(_record_bytes(operation, new=new))
            temporary.flush()
            os.fchown(temporary.fileno(), operation.record_uid, operation.record_gid)
            os.fchmod(temporary.fileno(), stat.S_IMODE(operation.record_mode))
            for name, value in operation.record_xattrs.items():
                os.setxattr(temporary.fileno(), name, base64.b64decode(value, validate=True))
            os.utime(temp, ns=(operation.record_atime, operation.record_mtime))
            os.fsync(temporary.fileno())
            temp.replace(record)
            os.utime(scope, ns=(operation.scope_atime, operation.scope_mtime))
            _checkpoint()
            fsync_directory_durable(scope)
            _checkpoint()
        finally:
            temp.unlink(missing_ok=True)


def _inspect_move(plan: StorageUpgradePlan, operation: _Operation, move: _Move) -> tuple[Path, bool]:
    parent = Path(plan.volumes[move.volume].path) / "private_instances"
    source, target = parent / operation.source, parent / operation.destination
    present = [p for p in (source, target) if p.exists() or p.is_symlink()]
    if len(present) != 1:
        _fail("Transaction source/destination conflict requires manual recovery")
    path = present[0]
    info = _directory(path)
    if (info.st_dev, info.st_ino) != (move.device, move.inode):
        _fail("Transaction directory identity changed")
    old_paths = tuple(str(Path(v.path) / "private_instances" / operation.source) for v in plan.volumes)
    if _inventory(path, old_paths) != move.inventory:
        _fail("Private data changed; refuse relocation or stale rollback")
    if move.volume == 0:
        record = path / _RECORD_FILENAME
        info = record.lstat()
        expected = (operation.record_mode, operation.record_uid, operation.record_gid, operation.record_mtime)
        if (info.st_mode, info.st_uid, info.st_gid, info.st_mtime_ns) != expected:
            _fail("Transaction owner record metadata changed")
        xattrs = {name: base64.b64encode(os.getxattr(record, name)).decode() for name in os.listxattr(record)}
        if xattrs != operation.record_xattrs:
            _fail("Transaction owner record attributes changed")
        parse_private_instance_identity_payload(load_private_instance_record_payload(record))
        if record.read_bytes() not in (_record_bytes(operation, new=False), _record_bytes(operation, new=True)):
            _fail("Transaction owner record changed")
    return path, path == target


def verify_storage_upgrade(plan: StorageUpgradePlan) -> None:
    """Verify exact relocated data and current owner resolution while offline."""
    _validate_plan(plan)
    for operation in plan.operations:
        for move in operation.moves:
            path, moved = _inspect_move(plan, operation, move)
            if not moved:
                _fail("Private scope has not been relocated")
            if move.volume == 0:
                identity = load_private_instance_identity(Path(plan.volumes[0].path), path)
                if identity is None or identity.requester_id != operation.requester_id:
                    _fail("Relocated owner failed current runtime validation")


def _validate_plan(plan: StorageUpgradePlan) -> None:  # noqa: C901 - validate every untrusted manifest field
    _validate_volumes(plan)
    if not plan.volumes or len(plan.volumes) > 2:
        _fail("Invalid participating volumes")
    if len({(v.device, v.inode) for v in plan.volumes}) != len(plan.volumes):
        _fail("Duplicate participating volume identity")
    if len({operation.source for operation in plan.operations}) != len(plan.operations):
        _fail("Duplicate private scope operation")
    if len({operation.destination for operation in plan.operations}) != len(plan.operations):
        _fail("Conflicting private scope destinations")
    for operation in plan.operations:
        for key, dirname in ((operation.old_key, operation.source), (operation.new_key, operation.destination)):
            if private_instance_scope_root_path(Path(plan.volumes[0].path), key).name != dirname:
                _fail("Invalid transaction scope path")
        identity = parse_private_instance_identity_payload(json.loads(_record_bytes(operation, new=False)))
        if identity.worker_key != operation.old_key or identity.requester_id != operation.requester_id:
            _fail("Invalid transaction owner mapping")
        if reconstruct_private_instance_worker_key(operation.old_key, operation.requester_id) != operation.new_key:
            _fail("Invalid transaction destination owner")
        if not operation.moves or operation.moves[0].volume != 0:
            _fail("Transaction must include the owner volume")
        if len({move.volume for move in operation.moves}) != len(operation.moves):
            _fail("Duplicate transaction volume move")
        if any(move.volume < 0 or move.volume >= len(plan.volumes) for move in operation.moves):
            _fail("Invalid transaction volume")
        _validate_unplanned_mirrors(plan, operation)


def apply_storage_upgrade(  # noqa: C901, PLR0912 - keep durable transaction order explicit
    plan: StorageUpgradePlan,
    *,
    writers_stopped: bool,
    backup_verified: bool,
) -> None:
    """Apply or resume an inspected plan with stopped writers and verified backups."""
    if not writers_stopped or not backup_verified:
        _fail("Stopped writers and verified volume backups are required")
    if sys.platform != "linux":
        _fail("Offline directory relocation requires Linux renameat2 support")
    _validate_plan(plan)
    _check_active_scripts(Path(plan.control_state))
    with ExitStack() as locks:
        for volume in sorted(plan.volumes, key=lambda volume: volume.path):
            lock = Path(volume.path) / _LOCK
            if lock.is_symlink():
                _fail("Invalid migration lock")
            locks.enter_context(advisory_file_lock(lock))
        journals = [_read_journal(Path(v.path) / _MARKER) for v in plan.volumes]
        if any(journal is not None and journal.plan != plan for journal in journals):
            _fail("A different transaction owns these volumes")
        if any(journal is not None and journal.status == "reversing" for journal in journals):
            _fail("Reverse recovery must finish before another upgrade")
        if any(j is not None and j.status == "rolled_back" for j in journals) and not all(
            j is not None and j.status == "rolled_back" for j in journals
        ):
            _fail("Reverse recovery must finish on every volume")
        if not any(journals):
            current = plan_storage_upgrade(
                Path(plan.volumes[0].path),
                Path(plan.volumes[-1].path),
                control_state=Path(plan.control_state),
            )
            if current != plan:
                _fail("Storage changed after planning")
        for operation in plan.operations:
            for move in operation.moves:
                _inspect_move(plan, operation, move)
        _publish(plan, "prepared")
        for operation in plan.operations:
            for move in operation.moves:
                path, moved = _inspect_move(plan, operation, move)
                if not moved:
                    _rename_no_replace(path, path.with_name(operation.destination))
                _publish(plan, "moving")
            destination = Path(plan.volumes[0].path) / "private_instances" / operation.destination
            _write_record(destination, operation, new=True)
            _publish(plan, "moving")
        verify_storage_upgrade(plan)
        _publish(plan, "complete")


def rollback_storage_upgrade(  # noqa: C901 - reverse the journaled transaction in strict order
    plan: StorageUpgradePlan,
    *,
    writers_stopped: bool,
) -> None:
    """Reverse only unchanged pre-traffic data; resume an interrupted reversal."""
    if not writers_stopped:
        _fail("Stopped writers are required for reverse recovery")
    if sys.platform != "linux":
        _fail("Offline directory relocation requires Linux renameat2 support")
    _validate_plan(plan)
    _check_active_scripts(Path(plan.control_state))
    with ExitStack() as locks:
        for volume in sorted(plan.volumes, key=lambda volume: volume.path):
            lock = Path(volume.path) / _LOCK
            if lock.is_symlink():
                _fail("Invalid migration lock")
            locks.enter_context(advisory_file_lock(lock))
        journals = [_read_journal(Path(v.path) / _MARKER) for v in plan.volumes]
        if not any(journals) or any(j is not None and j.plan != plan for j in journals):
            _fail("Rollback requires the original transaction receipt")
        for operation in plan.operations:
            for move in operation.moves:
                _inspect_move(plan, operation, move)
        _publish(plan, "reversing")
        for operation in reversed(plan.operations):
            owner, _ = _inspect_move(plan, operation, operation.moves[0])
            _write_record(owner, operation, new=False)
            for move in reversed(operation.moves):
                path, moved = _inspect_move(plan, operation, move)
                if moved:
                    _rename_no_replace(path, path.with_name(operation.source))
                _publish(plan, "reversing")
        _publish(plan, "rolled_back")


def _check_active_scripts(control_root: Path) -> None:
    """Read closed control databases without instantiating a mutating store."""
    database = control_root / "script_runs" / "script_runs.sqlite3"
    if not database.exists() and not database.is_symlink():
        return
    _root(database.parent)
    if not stat.S_ISREG(database.lstat().st_mode):
        _fail("Script control database must be a regular file")
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        _fail("Script control database must be checkpointed with writers stopped")
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if connection.execute(
                "SELECT 1 FROM script_runs WHERE state IN ('starting', 'running') LIMIT 1",
            ).fetchone():
                _fail("Active script handles must settle before private storage relocation")
        finally:
            connection.close()
    except sqlite3.Error as error:
        message = "Script control state cannot be verified offline"
        raise StorageUpgradeError(message) from error


def _validate_unplanned_mirrors(plan: StorageUpgradePlan, operation: _Operation) -> None:
    """A session tree absent from the plan must remain absent during recovery."""
    planned = {move.volume for move in operation.moves}
    for index, volume in enumerate(plan.volumes):
        if index in planned:
            continue
        namespace = Path(volume.path) / "private_instances"
        for name in (operation.source, operation.destination):
            path = namespace / name
            if path.exists() or path.is_symlink():
                _fail("An unplanned session mirror appeared during the transaction")
