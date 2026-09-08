"""Explicit offline relocation of owner-verified legacy private scopes.

Writers, including old binaries and workers without shared-volume visibility, must
remain stopped throughout planning, apply, and recovery. Worker credentials are
never inferred from private owner records. Journals contain private owner mappings;
keep them with the protected volumes and retain a verified backup.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never

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
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths

_RECORD_FILENAME = ".mindroom-private-instance.json"
_MARKER = ".mindroom-storage-upgrade.json"
_LOCK = ".mindroom-storage-upgrade.lock"
_KNOWN_RECEIPT_ROOTS: set[Path] = set()


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
    sessions: dict[str, str]
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


def read_storage_upgrade_plan(path: Path) -> StorageUpgradePlan:
    """Read an inspected manifest or an automatically persisted recovery receipt."""
    payload = load_private_instance_record_payload(path, max_bytes=64 * 1024 * 1024)
    if isinstance(payload, dict) and "plan" in payload:
        return _Journal.model_validate(payload).plan
    return StorageUpgradePlan.model_validate(payload)


def _fail(message: str) -> Never:
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


def _check_relative_symlink(root: Path, path: Path, target: str) -> None:
    """Prove every path component remains inside the relocated tree."""
    if Path(target).is_absolute():
        _fail("Absolute symlink requires an explicit relocation repair")
    # Never traverse above the moved root, even if the old basename later
    # re-enters it: that route changes meaning after directory relocation.
    depth = len(path.parent.relative_to(root).parts)
    for part in Path(target).parts:
        depth += -1 if part == ".." else 1
        if depth < 0:
            _fail("Relative symlink escapes the private scope during relocation")
    try:
        contained = path.resolve().is_relative_to(root)
    except (OSError, RuntimeError) as error:
        message = "Relative symlink cannot be resolved safely"
        raise StorageUpgradeError(message) from error
    if not contained:
        _fail("Relative symlink leaves the private scope")


def _inventory(root: Path, *, owner_temporary: Path | None = None, exclude_owner_record: bool = False) -> str:
    """Hash names, bytes, modes, owners and timestamps without following links."""
    digest = hashlib.sha256()
    for path in sorted((root, *root.rglob("*"))):
        relative = path.relative_to(root).as_posix()
        if (exclude_owner_record and relative == _RECORD_FILENAME) or path == owner_temporary:
            continue
        if len(path.relative_to(root).parts) == 1 and path.name.startswith(".mindroom-private-owner-"):
            _fail("Unrecognized owner temporary requires explicit recovery")
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
            _check_relative_symlink(root, path, target)
            digest.update(target.encode())
        elif stat.S_ISREG(info.st_mode):
            parts = path.relative_to(root).parts
            runtime_metadata = {(".runtime", "startup_manifest.json"), ("metadata", "worker.json")}
            if parts in runtime_metadata or (len(parts) == 3 and parts[1:] in runtime_metadata):
                _fail("Persisted worker runtime metadata requires explicit offline recovery")
            if info.st_nlink != 1:
                _fail("Hard-linked data requires an explicit relocation repair")
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
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


def _check_secondary_scopes(
    storage: Path,
    sessions: Path,
    operations: tuple[_Operation, ...] = (),
) -> None:
    """Pair every secondary entry with a proven owner or validated recovery operation."""
    if storage == sessions:
        return
    namespace = sessions / "private_instances"
    if not namespace.exists() and not namespace.is_symlink():
        return
    _directory(namespace)
    planned = {name for operation in operations for name in (operation.source, operation.destination)}
    for scope in namespace.iterdir():
        _directory(scope)
        if scope.name in planned:
            continue
        owner = storage / "private_instances" / scope.name
        try:
            _directory(owner)
            _legacy_keys(owner)
        except (OSError, PrivateInstanceIdentityError, StorageUpgradeError) as error:
            message = "Secondary session scope has no verified primary owner"
            raise StorageUpgradeError(message) from error


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
    _check_secondary_scopes(Path(volumes[0].path), Path(volumes[-1].path))
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
                            inventory=_inventory(source, exclude_owner_record=index == 0),
                            sessions=_session_snapshots(source),
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


def _marker_identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        _fail("Invalid storage upgrade participation marker")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _load_receipt(path: Path) -> tuple[_Journal, str]:
    journal = _read_journal(path)
    if journal is None:
        _fail("Storage upgrade participation marker disappeared")
    return journal, hashlib.sha256(journal.model_dump_json().encode()).hexdigest()


@lru_cache(maxsize=32)
def _cached_complete_receipt(path: Path, identity: tuple[int, ...]) -> tuple[_Journal, str]:
    receipt = _load_receipt(path)
    if receipt[0].status != "complete" or _marker_identity(path) != identity:
        _fail("Storage upgrade is incomplete or its marker changed")
    return receipt


def _receipt(path: Path, *, cached: bool) -> tuple[_Journal, str] | None:
    try:
        identity = _marker_identity(path)
    except FileNotFoundError:
        if path.parent in _KNOWN_RECEIPT_ROOTS:
            _fail("Previously verified participation marker disappeared")
        return None
    receipt = _cached_complete_receipt(path, identity) if cached else _load_receipt(path)
    if _marker_identity(path) != identity:
        _fail("Storage upgrade participation marker changed while reading")
    return receipt


def check_storage_upgrade(  # noqa: C901, PLR0912 - inspect every participant before access
    storage: Path,
    sessions: Path | None = None,
    *,
    scan_legacy: bool = True,
) -> None:
    """Stat every receipt on hot calls; fully read and scan at operation boundaries."""
    try:
        roots = list(dict.fromkeys(root.expanduser().absolute() for root in (storage, sessions or storage)))
        receipts: dict[Path, tuple[_Journal, str] | None] = {}
        for root in roots:
            if not root.exists() and not root.is_symlink():
                if root in _KNOWN_RECEIPT_ROOTS:
                    _fail("Previously verified storage root disappeared")
                continue
            _root(root)
            if root not in receipts:
                receipts[root] = _receipt(root / _MARKER, cached=not scan_legacy)
            receipt = receipts[root]
            if receipt is not None:
                journal, digest = receipt
                _validate_volumes(journal.plan)
                if journal.status != "complete":
                    _fail("Storage upgrade is incomplete")
                for volume in journal.plan.volumes:
                    participant = Path(volume.path)
                    if participant not in receipts:
                        receipts[participant] = _receipt(participant / _MARKER, cached=not scan_legacy)
                    other = receipts[participant]
                    if other is None or other[1] != digest:
                        _fail("Storage upgrade participation markers disagree")
                _KNOWN_RECEIPT_ROOTS.update(Path(volume.path) for volume in journal.plan.volumes)
            namespace = root / "private_instances"
            if not namespace.exists() and not namespace.is_symlink():
                continue
            _directory(namespace)
            if not scan_legacy:
                continue
            for scope in namespace.iterdir():
                if not scope.is_dir() or scope.is_symlink():
                    continue
                try:
                    legacy = _legacy_keys(scope)
                except (OSError, PrivateInstanceIdentityError, StorageUpgradeError):
                    continue
                if legacy is not None:
                    _fail("Owner-verified legacy private storage requires offline upgrade")
    except (OSError, ValueError) as exc:
        message = "Private storage requires offline upgrade or recovery before runtime access"
        raise _StorageUpgradeRequiredError(message) from exc


@dataclass(frozen=True)
class StorageUpgradeCheck:
    """Evidence that one outer operation completed its exhaustive legacy preflight."""

    roots: tuple[Path, Path]


def _runtime_roots(runtime_paths: RuntimePaths) -> tuple[Path, Path]:
    configured = runtime_paths.env_value("MINDROOM_SESSION_STORAGE_PATH")
    sessions = Path(configured).expanduser() if configured and configured.strip() else runtime_paths.storage_root
    if not sessions.is_absolute():
        sessions = runtime_paths.config_dir / sessions
    return runtime_paths.storage_root, sessions


@dataclass(frozen=True)
class StorageUpgradeDiscovery:
    """Validated participants and any original transaction awaiting startup."""

    volumes: tuple[_Volume, ...]
    plan: StorageUpgradePlan | None = None
    direction: Literal["apply", "rollback", "stopped"] = "apply"


def discover_runtime_storage_upgrade(runtime_paths: RuntimePaths) -> StorageUpgradeDiscovery | None:
    """Inspect owners and receipts without inventorying files or creating roots."""
    try:
        return _discover_runtime_storage_upgrade(runtime_paths)
    except StorageUpgradeError:
        raise
    except (OSError, ValueError) as error:
        message = "Private storage ownership or required participants could not be verified"
        raise StorageUpgradeError(message) from error


def _discover_runtime_storage_upgrade(  # noqa: C901, PLR0912 - validate all participant states before startup
    runtime_paths: RuntimePaths,
) -> StorageUpgradeDiscovery | None:
    roots = tuple(dict.fromkeys(path.expanduser().absolute() for path in _runtime_roots(runtime_paths)))
    journals = []
    for root in roots:
        if root.exists() or root.is_symlink():
            _root(root)
            journals.append(_read_journal(root / _MARKER))
        else:
            journals.append(None)
    if any(journals):
        volumes = _volumes(roots[0], roots[-1])
        existing = [journal for journal in journals if journal is not None]
        plan = existing[0].plan
        if plan.volumes != volumes or Path(plan.control_state) != runtime_paths.control_state_root:
            _fail("Recorded storage participants do not match the configured runtime")
        if any(journal.plan != plan for journal in existing):
            _fail("Different storage transactions claim the configured volumes")
        if len(existing) != len(journals) and any(journal.status != "prepared" for journal in existing):
            _fail("Storage transaction has an unsafe missing participation marker")
        statuses = {journal.status for journal in existing}
        if statuses == {"complete"}:
            _check_secondary_scopes(roots[0], roots[-1])
            check_runtime_storage_upgrade(runtime_paths)
            return None
        _check_configured_control_root(runtime_paths)
        _validate_plan(plan)
        if statuses == {"rolled_back"}:
            return StorageUpgradeDiscovery(volumes, plan, "stopped")
        direction = "rollback" if statuses & {"reversing", "rolled_back"} else "apply"
        return StorageUpgradeDiscovery(volumes, plan, direction)
    namespace = roots[0] / "private_instances"
    needs_upgrade = False
    if namespace.exists() or namespace.is_symlink():
        _directory(namespace)
        for scope in namespace.iterdir():
            _directory(scope)
            if any(scope.iterdir()) and _legacy_keys(scope) is not None:
                needs_upgrade = True
    if needs_upgrade:
        _check_configured_control_root(runtime_paths)
        _check_secondary_scopes(roots[0], roots[-1])
        return StorageUpgradeDiscovery(_volumes(roots[0], roots[-1]))
    _check_secondary_scopes(roots[0], roots[-1])
    check_runtime_storage_upgrade(runtime_paths)
    return None


def _check_configured_control_root(runtime_paths: RuntimePaths) -> None:
    root = runtime_paths.control_state_root
    if root is None:
        _fail("Private storage migration requires a primary control-state root")
    if root != runtime_paths.storage_root / "control_state":
        _root(root)


@contextmanager
def storage_upgrade_locks(volumes: tuple[_Volume, ...]) -> Iterator[None]:
    """Hold sorted migration locks without creating missing participant roots."""

    def validate_roots() -> None:
        for volume in volumes:
            info = _root(Path(volume.path)).stat()
            if (info.st_dev, info.st_ino) != (volume.device, volume.inode):
                _fail("A required storage volume is missing or changed")

    validate_roots()
    with ExitStack() as locks:
        for volume in sorted(volumes, key=lambda volume: volume.path):
            lock = Path(volume.path) / _LOCK
            if lock.is_symlink():
                _fail("Invalid migration lock")
            locks.enter_context(advisory_file_lock(lock))
        validate_roots()
        yield


def check_runtime_storage_upgrade(
    runtime_paths: RuntimePaths,
    *,
    checked: StorageUpgradeCheck | None = None,
) -> StorageUpgradeCheck:
    """Preflight one startup/export operation, sharing proof with nested helpers."""
    roots = _runtime_roots(runtime_paths)
    check_storage_upgrade(*roots, scan_legacy=checked is None or checked.roots != roots)
    return StorageUpgradeCheck(roots)


def check_runtime_storage_markers(runtime_paths: RuntimePaths) -> None:
    """Check participation fences without scanning every user's owner record."""
    check_storage_upgrade(*_runtime_roots(runtime_paths), scan_legacy=False)


def check_legacy_private_scope(scope: Path) -> None:
    """Refuse discovery/cleanup of an exact legacy owner without authorizing it."""
    try:
        legacy = _legacy_keys(scope)
    except (OSError, PrivateInstanceIdentityError, StorageUpgradeError):
        return
    if legacy is not None:
        message = "Owner-verified legacy private storage requires offline upgrade"
        raise _StorageUpgradeRequiredError(message)


def check_private_storage_target(storage: Path, worker_key: str, requester_id: str) -> None:
    """Check only the legacy candidate derived from this exact requester."""
    parts = worker_key.split(":")
    requester = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", requester_id.strip()).strip("_") or "default"
    old_key = f"v1:{parts[1]}:{parts[2]}:{requester}"
    if parts[2] == "user_agent":
        old_key += ":" + parts[-1]
    scope = private_instance_scope_root_path(storage, old_key)
    if not scope.exists() and not scope.is_symlink():
        return
    try:
        legacy = _legacy_keys(scope)
    except (OSError, PrivateInstanceIdentityError, StorageUpgradeError):
        return
    if legacy is not None and legacy[1:] == (worker_key, requester_id):
        message = "This exact requester has private storage awaiting offline upgrade"
        raise _StorageUpgradeRequiredError(message)


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


def _rename_offline(source: Path, destination: Path, *, device: int, inode: int) -> None:
    """Move within a locked volume only while every writer is stopped.

    POSIX rename works on filesystems without RENAME_NOREPLACE. The destination
    absence check is safe only under the explicit offline-writer prerequisite;
    it is not an atomic exclusion guarantee against uncooperative writers.
    """
    info = _directory(source)
    if (info.st_dev, info.st_ino) != (device, inode):
        _fail("Transaction directory identity changed before rename")
    if destination.exists() or destination.is_symlink():
        _fail("Destination already exists; offline replacement is forbidden")
    source.rename(destination)
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


def _owner_temporary(scope: Path, operation: _Operation) -> Path:
    digest = hashlib.sha256(operation.model_dump_json().encode()).hexdigest()
    return scope / f".mindroom-private-owner-{digest}.tmp"


def _check_owner_temporary(scope: Path, operation: _Operation) -> Path | None:
    """Recognize only this exact operation's partial durable owner write."""
    temporary = _owner_temporary(scope, operation)
    if not temporary.exists() and not temporary.is_symlink():
        return None
    info = temporary.lstat()
    expected = (_record_bytes(operation, new=False), _record_bytes(operation, new=True))
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {os.geteuid(), operation.record_uid}
        or info.st_size > max(map(len, expected))
    ):
        _fail("Owner temporary does not match this transaction")
    data = temporary.read_bytes()
    if not any(value.startswith(data) for value in expected):
        _fail("Owner temporary contains unrelated data")
    return temporary


def _write_record(scope: Path, operation: _Operation, *, new: bool) -> None:
    record = scope / _RECORD_FILENAME
    orphan = _check_owner_temporary(scope, operation)
    if orphan is not None:
        orphan.unlink()
        fsync_directory_durable(scope)
    temp = _owner_temporary(scope, operation)
    # A crash can leave this exact partial write. Recovery validates it against
    # the protected operation, ignores it only in that scope's fingerprint, and
    # removes it under the volume locks before retrying the owner replacement.
    with temp.open("xb") as temporary:
        os.fchmod(temporary.fileno(), 0o600)
        _checkpoint()
        temporary.write(_record_bytes(operation, new=new))
        temporary.flush()
        os.fchown(temporary.fileno(), operation.record_uid, operation.record_gid)
        os.fchmod(temporary.fileno(), stat.S_IMODE(operation.record_mode))
        for name, value in operation.record_xattrs.items():
            os.setxattr(temporary.fileno(), name, base64.b64decode(value, validate=True))
        os.utime(temp, ns=(operation.record_atime, operation.record_mtime))
        os.fsync(temporary.fileno())
        _checkpoint()
        temp.replace(record)
        os.utime(scope, ns=(operation.scope_atime, operation.scope_mtime))
        _checkpoint()
        fsync_directory_durable(scope)
        _checkpoint()


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
    if _session_snapshots(path) != move.sessions:
        _fail("Session database schema or rows changed")
    temporary = _check_owner_temporary(path, operation) if move.volume == 0 else None
    if _inventory(path, owner_temporary=temporary, exclude_owner_record=move.volume == 0) != move.inventory:
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
            if move.volume == 0 and _check_owner_temporary(path, operation) is not None:
                _fail("Owner temporary requires recovery before verification")
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
        _validate_record_attributes(operation)
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
    _check_secondary_scopes(Path(plan.volumes[0].path), Path(plan.volumes[-1].path), plan.operations)


def _validate_record_attributes(operation: _Operation) -> None:
    """Reject unusable attribute names and encodings before any lock or journal write."""
    try:
        for name, encoded in operation.record_xattrs.items():
            if not name or "\0" in name or len(os.fsencode(name)) > 255:
                _fail("Invalid owner attribute name")
            base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as error:
        message = "Invalid owner attribute name or value encoding"
        raise StorageUpgradeError(message) from error


def apply_storage_upgrade(
    plan: StorageUpgradePlan,
    *,
    writers_stopped: bool,
    backup_verified: bool,
) -> None:
    """Apply or resume an inspected plan with stopped writers and verified backups."""
    if not writers_stopped or not backup_verified:
        _fail("Stopped writers and verified volume backups are required")
    _validate_plan(plan)
    _check_active_scripts(Path(plan.control_state))
    with storage_upgrade_locks(plan.volumes):
        apply_storage_upgrade_locked(plan)


def apply_storage_upgrade_locked(  # noqa: C901 - preserve durable transaction ordering
    plan: StorageUpgradePlan,
) -> None:
    """Apply or resume while the caller holds all migration locks and writers remain stopped."""
    _validate_plan(plan)
    _check_active_scripts(Path(plan.control_state))
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
            path, _ = _inspect_move(plan, operation, move)
            # A prior crash may have renamed this entry without syncing it.
            # Persist the observed namespace before publishing any new receipt.
            fsync_directory_durable(path.parent)
    _publish(plan, "prepared")
    _publish(plan, "moving")
    for operation in plan.operations:
        for move in operation.moves:
            path, moved = _inspect_move(plan, operation, move)
            if not moved:
                _rename_offline(path, path.with_name(operation.destination), device=move.device, inode=move.inode)
        destination = Path(plan.volumes[0].path) / "private_instances" / operation.destination
        _write_record(destination, operation, new=True)
    verify_storage_upgrade(plan)
    _publish(plan, "complete")


def rollback_storage_upgrade(
    plan: StorageUpgradePlan,
    *,
    writers_stopped: bool,
) -> None:
    """Reverse only unchanged pre-traffic data; resume an interrupted reversal."""
    if not writers_stopped:
        _fail("Stopped writers are required for reverse recovery")
    _validate_plan(plan)
    _check_active_scripts(Path(plan.control_state))
    with storage_upgrade_locks(plan.volumes):
        rollback_storage_upgrade_locked(plan)


def rollback_storage_upgrade_locked(plan: StorageUpgradePlan) -> None:
    """Reverse while the caller holds all migration locks and writers remain stopped."""
    _validate_plan(plan)
    _check_active_scripts(Path(plan.control_state))
    journals = [_read_journal(Path(v.path) / _MARKER) for v in plan.volumes]
    if not any(journals) or any(j is not None and j.plan != plan for j in journals):
        _fail("Rollback requires the original transaction receipt")
    for operation in plan.operations:
        for move in operation.moves:
            path, _ = _inspect_move(plan, operation, move)
            # A prior crash may have renamed this entry without syncing it.
            # Persist the observed namespace before publishing any new receipt.
            fsync_directory_durable(path.parent)
    if any(journal is None for journal in journals) and all(
        journal is None or journal.status == "prepared" for journal in journals
    ):
        # Finish initial participation before recording any reversal intent.
        # Otherwise a crash could leave reversing beside a missing marker.
        _publish(plan, "prepared")
    _publish(plan, "reversing")
    for operation in reversed(plan.operations):
        owner, _ = _inspect_move(plan, operation, operation.moves[0])
        _write_record(owner, operation, new=False)
        for move in reversed(operation.moves):
            path, moved = _inspect_move(plan, operation, move)
            if moved:
                _rename_offline(path, path.with_name(operation.source), device=move.device, inode=move.inode)
    _publish(plan, "rolled_back")


def _check_active_scripts(control_root: Path) -> None:
    """Read closed control databases without instantiating a mutating store."""
    database = control_root / "script_runs" / "script_runs.sqlite3"
    if not database.exists() and not database.is_symlink():
        return
    _root(database.parent)
    _settled_database(database, label="Script control")
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
    _settled_database(database, label="Script control")


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


def _session_snapshots(root: Path) -> dict[str, str]:
    """Read known session and learning paths, including contained relative links."""
    snapshots = {}
    for agent in sorted(root.iterdir()):
        if not agent.is_dir():
            continue
        candidates = [
            agent / "session.db",
            agent / "sessions.db",
            *(agent / "sessions").glob("*.db"),
            *(agent / "learning").glob("*.db"),
        ]
        for database in sorted(candidates):
            if not database.exists() and not database.is_symlink():
                continue
            target = database.resolve(strict=True)
            if not target.is_relative_to(root):
                _fail("Session database link leaves the private scope")
            snapshots[database.relative_to(root).as_posix()] = _session_database_snapshot(
                target,
                include_learning=database.parent == agent / "learning",
            )
    return snapshots


def _settled_database(database: Path, *, label: str = "Session") -> None:
    """Immutable SQLite reads are safe only when no pending journal is ignored."""
    if not stat.S_ISREG(database.lstat().st_mode):
        _fail(f"{label} database must be a regular file")
    for suffix in ("-wal", "-journal"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
            _fail(f"{label} database sidecar must be settled with writers stopped")


def _session_database_snapshot(database: Path, *, include_learning: bool = False) -> str:
    """Validate schema/integrity and fingerprint actual session and run rows."""
    from agno.db.sqlite.schemas import get_table_schema_definition  # noqa: PLC0415

    _settled_database(database)
    digest = hashlib.sha256()
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                _fail("Session database integrity validation failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                _fail("Session database contains broken row references")
            tables = connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name",
            ).fetchall()
            sessions = [name for name, _ in tables if name.endswith("sessions")]
            learning_tables = (
                {
                    "agno_learnings": "learnings",
                    "agno_memories": "memories",
                }
                if include_learning
                else {}
            )
            learning_tables = {
                name: kind
                for name, kind in learning_tables.items()
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
            }
            if not sessions and not learning_tables:
                _fail("Session database has no recognized session schema")
            digest.update(repr(tables).encode())
            expected_tables = dict.fromkeys(sessions, "sessions")
            expected_tables.update(
                {"agno_runs" if name == "agno_sessions" else f"{name}_runs": "runs" for name in sessions},
            )
            expected_tables.update(learning_tables)
            table_names = {name for name, _ in tables}
            for name, table_type in sorted(expected_tables.items()):
                # Agno creates matching run tables lazily, including for named
                # session tables. Absence is valid; malformed present objects are not.
                if name not in table_names:
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
                        _fail("Session database has an incompatible run schema")
                    continue
                columns = {row[1] for row in connection.execute("SELECT * FROM pragma_table_info(?)", (name,))}
                required = {column for column in get_table_schema_definition(table_type) if not column.startswith("_")}
                if not required <= columns:
                    _fail("Session database has an incompatible session or run schema")
                identifier = '"' + name.replace('"', '""') + '"'
                order = {
                    "sessions": "session_id",
                    "runs": "run_id",
                    "learnings": "learning_id",
                    "memories": "memory_id",
                }[table_type]
                # Identifiers come from SQLite metadata and are quoted; order is a fixed literal.
                rows = connection.execute(f"SELECT * FROM {identifier} ORDER BY {order}")  # noqa: S608
                digest.update(name.encode())
                for row in rows:
                    digest.update(repr(row).encode())
    except sqlite3.Error as error:
        message = "Session database cannot be read safely offline"
        raise StorageUpgradeError(message) from error
    _settled_database(database)
    return digest.hexdigest()
