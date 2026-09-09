"""Startup-only relocation of verified private scopes, with per-scope recovery.

Deployment must stop previous primaries and independent controllers first.
Managed workers must be absent before inspecting or moving any scope contents.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from mindroom.durable_write import fsync_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.private_instance_identity_store import (
    PrivateInstanceIdentity,
    historical_private_instance_worker_key,
    load_private_instance_identity,
    load_private_instance_legacy_alias,
    load_private_instance_record_payload,
    parse_private_instance_identity_payload,
    reconstruct_private_instance_worker_key,
)
from mindroom.tool_system.worker_routing import private_instance_scope_root_path

if TYPE_CHECKING:
    from typing import Any

    from mindroom.constants import RuntimePaths

_RECORD = ".mindroom-private-instance.json"
_INTENT = ".mindroom-private-storage-migration.json"
_LOCK = ".mindroom-storage-upgrade.lock"


@dataclass(frozen=True)
class _Intent:
    version: int
    primary_root: str
    session_root: str
    old_key: str
    new_key: str
    requester_id: str
    primary_inode: int
    session_inode: int | None


def _reject(message: str) -> NoReturn:
    detail = f"Private storage migration: {message}"
    raise ValueError(detail)


def _directory(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        _reject("scope, namespace and volume entries must be real directories")
    return info


def _roots(runtime_paths: RuntimePaths) -> tuple[Path, Path]:
    configured = runtime_paths.env_value("MINDROOM_SESSION_STORAGE_PATH")
    sessions = Path(configured).expanduser() if configured and configured.strip() else runtime_paths.storage_root
    if not sessions.is_absolute():
        sessions = runtime_paths.config_dir / sessions
    roots = runtime_paths.storage_root.absolute(), sessions.absolute()
    for root in set(roots):
        for parent in (*reversed(root.parents), root):
            _directory(parent)
        if (root / ".mindroom-storage-upgrade.json").exists() or (root / ".mindroom-storage-upgrade.json").is_symlink():
            _reject("unrelated recovery record requires operator recovery")
    return roots[0].resolve(), roots[1].resolve()


def _keys(identity: PrivateInstanceIdentity) -> tuple[str, str]:
    current = reconstruct_private_instance_worker_key(identity.worker_key, identity.requester_id)
    return historical_private_instance_worker_key(identity.worker_key, identity.requester_id), current


def _locations(root: Path, intent: _Intent) -> tuple[Path, Path]:
    return private_instance_scope_root_path(root, intent.old_key), private_instance_scope_root_path(
        root,
        intent.new_key,
    )


def _recorded_location(root: Path, intent: _Intent, inode: int | None) -> Path | None:
    old, new = _locations(root, intent)
    alias = old.is_symlink()
    if alias and os.readlink(old) != new.name:  # noqa: PTH115 - Preserve the literal target text.
        _reject("recorded alias does not name its canonical sibling")
    locations = [path for path in (old, new) if not (path == old and alias) and _directory(path) is not None]
    if inode is None:
        if locations or alias:
            _reject("unexpected session data for a scope recorded without a session mirror")
        return None
    if len(locations) != 1 or locations[0].lstat().st_ino != inode or (alias and locations[0] != new):
        _reject("recorded scope must exist at exactly one location with its original inode")
    return locations[0]


def _read_intent(payload: object, roots: tuple[Path, Path], scope: Path) -> _Intent:
    if not isinstance(payload, dict) or set(payload) != set(_Intent.__dataclass_fields__):
        _reject("invalid migration intent schema")
    intent = _Intent(**cast("dict[str, Any]", payload))
    if (
        type(intent.version) is not int
        or intent.version != 1
        or any(
            not isinstance(value, str)
            for value in (
                intent.primary_root,
                intent.session_root,
                intent.old_key,
                intent.new_key,
                intent.requester_id,
            )
        )
        or type(intent.primary_inode) is not int
        or intent.primary_inode <= 0
        or (intent.session_inode is not None and (type(intent.session_inode) is not int or intent.session_inode <= 0))
        or (intent.primary_root, intent.session_root) != tuple(map(str, roots))
    ):
        _reject("invalid intent identity or configured storage roots changed")
    owner = parse_private_instance_identity_payload(_owner_payload(intent.old_key, intent.requester_id))
    if _keys(owner) != (intent.old_key, intent.new_key) or intent.old_key == intent.new_key:
        _reject("intent does not describe a historical owner")
    primary = _recorded_location(roots[0], intent, intent.primary_inode)
    if primary != scope:
        _reject("intent is outside its recorded scope")
    sessions = _recorded_location(roots[1], intent, intent.session_inode) if roots[0] != roots[1] else primary
    new = _locations(roots[0], intent)[1]
    payload = load_private_instance_record_payload(scope / _RECORD)
    parse_private_instance_identity_payload(payload)
    allowed = [_owner_payload(intent.old_key, intent.requester_id)]
    if primary == new:
        allowed.append(_owner_payload(intent.new_key, intent.requester_id))
        if sessions is not None and sessions.name != new.name:
            _reject("primary moved before its session mirror")
    if payload not in allowed or (roots[0] == roots[1] and intent.session_inode is not None):
        _reject("owner record conflicts with migration intent")
    _validate_recorded_aliases(
        roots,
        intent,
        primary == new and payload == _owner_payload(intent.new_key, intent.requester_id),
    )
    return intent


def _validate_recorded_aliases(roots: tuple[Path, Path], intent: _Intent, current_owner: bool) -> None:
    primary_alias = _locations(roots[0], intent)[0].is_symlink()
    session_alias = roots[0] != roots[1] and _locations(roots[1], intent)[0].is_symlink()
    if primary_alias or session_alias:
        if not current_owner:
            _reject("alias publication requires both moves and the current owner")
        if primary_alias and roots[0] != roots[1] and intent.session_inode is not None and not session_alias:
            _reject("primary alias appeared before the recorded session alias")


def _owner_payload(key: str, requester: str) -> dict[str, object]:
    return {"format": "mindroom-private-instance", "version": 1, "worker_key": key, "requester_id": requester}


def _scopes(root: Path) -> list[Path]:
    namespace = root / "private_instances"
    if _directory(namespace) is None:
        return []
    scopes = sorted(namespace.iterdir())
    for scope in scopes:
        if not scope.is_symlink():
            _directory(scope)
    return scopes


def _fresh_intent(scope: Path, owner: PrivateInstanceIdentity, roots: tuple[Path, Path]) -> _Intent:
    primary, sessions = roots
    old, new = _keys(owner)
    target = private_instance_scope_root_path(primary, new)
    if _directory(target) is not None:
        _reject("migration destination already exists")
    mirror = sessions / "private_instances" / scope.name
    mirror_info = _directory(mirror) if sessions != primary else None
    if sessions != primary and _directory(sessions / "private_instances" / target.name) is not None:
        _reject("session migration destination already exists")
    return _Intent(
        1,
        str(primary),
        str(sessions),
        old,
        new,
        owner.requester_id,
        scope.lstat().st_ino,
        mirror_info.st_ino if mirror_info else None,
    )


def _discover(roots: tuple[Path, Path]) -> list[_Intent]:
    primary = roots[0]
    pending = []
    known_names: set[str] = set()
    scopes = _scopes(primary)
    for scope in scopes:
        if scope.is_symlink():
            continue
        payload = load_private_instance_record_payload(scope / _INTENT)
        if payload is not None or (scope / _INTENT).exists():
            intent = _read_intent(payload, roots, scope)
            pending.append(intent)
            known_names.update(path.name for path in _locations(primary, intent))
            continue
        payload = load_private_instance_record_payload(scope / _RECORD)
        if payload is None:
            if any(scope.iterdir()):
                _reject("populated private scope has no authoritative owner")
            continue
        owner = parse_private_instance_identity_payload(payload)
        _old, new = _keys(owner)
        if private_instance_scope_root_path(primary, owner.worker_key) != scope:
            _reject("owner record does not match its directory hash")
        known_names.add(scope.name)
        if owner.worker_key == new:
            continue
        intent = _fresh_intent(scope, owner, roots)
        pending.append(intent)
        known_names.add(_locations(primary, intent)[1].name)
    pending_aliases = {_locations(primary, intent)[0] for intent in pending}
    for alias in scopes:
        if not alias.is_symlink() or alias in pending_aliases:
            continue
        _validate_completed_alias(primary, alias)
        known_names.add(alias.name)
    _validate_batch(roots, pending, known_names)
    return pending


def _validate_completed_alias(primary: Path, alias: Path) -> None:
    target_name = os.readlink(alias)  # noqa: PTH115 - Preserve the literal target text.
    if target_name in {"", ".", ".."} or "/" in target_name:
        _reject("alias must name its canonical sibling")
    owner = load_private_instance_identity(primary, alias.parent / target_name)
    if owner is None or load_private_instance_legacy_alias(primary, owner.worker_key) != alias:
        _reject("alias has no matching current owner")


def _validate_batch(roots: tuple[Path, Path], pending: list[_Intent], known_names: set[str]) -> None:
    primary, sessions = roots
    if sessions != primary:
        _validate_session_scopes(roots, pending, known_names)
    if pending:
        if any(_directory(root) is None for root in roots):
            _reject("configured migration volume is missing")
        if primary != sessions and (primary.is_relative_to(sessions) or sessions.is_relative_to(primary)):
            _reject("migration volumes must not overlap")
        destinations = [intent.new_key for intent in pending]
        if len(set(destinations)) != len(destinations):
            _reject("multiple old scopes claim the same current owner")


def _validate_session_scopes(roots: tuple[Path, Path], pending: list[_Intent], known_names: set[str]) -> None:
    primary, sessions = roots
    pending_aliases = {_locations(sessions, intent)[0] for intent in pending if intent.session_inode is not None}
    for scope in _scopes(sessions):
        if scope.is_symlink():
            if scope in pending_aliases:
                continue  # _read_intent already checked the mirror and publication order.
            counterpart = primary / "private_instances" / scope.name
            if (
                not counterpart.is_symlink()
                or os.readlink(scope) != os.readlink(counterpart)  # noqa: PTH115 - Preserve the literal target text.
                or _directory(scope.parent / os.readlink(scope)) is None  # noqa: PTH115 - Preserve the literal target text.
            ):
                _reject("session alias does not match its verified primary alias")
            continue
        if (scope / _INTENT).exists() or (scope / _INTENT).is_symlink():
            _reject("session mirror contains an unrelated migration intent")
        if scope.name not in known_names and any(scope.iterdir()):
            _reject("session-only private data has no authoritative primary owner")
        counterpart = primary / "private_instances" / scope.name
        if counterpart.is_symlink():
            _reject("session directory conflicts with a completed primary alias")


def _raise_scan_error(error: OSError) -> NoReturn:
    raise error


def _linux_mountpoints() -> list[Path]:
    """Read one complete Linux mount snapshot, rejecting unusable kernel metadata."""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except (OSError, UnicodeError) as error:
        message = "Private storage migration: mount table is unreadable"
        raise ValueError(message) from error
    if not lines:
        _reject("mount table is empty")
    mountpoints = []
    for line in lines:
        fields = line.split()
        if (
            len(fields) < 10
            or not fields[0].isdigit()
            or not fields[1].isdigit()
            or re.fullmatch(r"[0-9]+:[0-9]+", fields[2]) is None
            or not fields[4].startswith("/")
            or fields.count("-") != 1
            or fields.index("-") < 6
            or len(fields) - fields.index("-") != 4
            or re.search(r"\\(?![0-7]{3})", fields[4])
        ):
            _reject("mount table is malformed")
        mountpoint = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        if ".." in mountpoint.parts:
            _reject("mount table contains an unnormalized path")
        mountpoints.append(mountpoint)
    return mountpoints


def _check_tree(scope: Path) -> None:
    """On non-Linux systems inspect mounts without following workspace symlinks."""
    device = scope.lstat().st_dev
    if os.path.ismount(scope) or os.path.ismount(scope.parent):
        _reject("nested scope or namespace mounts cannot be renamed safely")
    for directory, subdirectories, files in os.walk(scope, followlinks=False, onerror=_raise_scan_error):
        for name in (*subdirectories, *files):
            entry = Path(directory) / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if info.st_dev != device or os.path.ismount(entry):
                _reject("nested mounts cannot be renamed safely")


def _check_mounts(roots: tuple[Path, Path], pending: list[_Intent]) -> None:
    """Validate each rename on its own device and reject mounts at either name."""
    locations = tuple(path for root in set(roots) for intent in pending for path in _locations(root, intent))
    if sys.platform == "linux":
        for mountpoint in _linux_mountpoints():
            if any(mountpoint == scope.parent or mountpoint.is_relative_to(scope) for scope in locations):
                _reject("nested mounts cannot be renamed safely")
    for intent in pending:
        for root, inode in ((roots[0], intent.primary_inode), (roots[1], intent.session_inode)):
            if inode is None:
                continue
            scope = _recorded_location(root, intent, inode)
            assert scope is not None
            device = root.lstat().st_dev
            if any(path.lstat().st_dev != device for path in (scope, scope.parent)):
                _reject("nested scope or namespace mounts cannot be renamed safely")
            if sys.platform != "linux":
                _check_tree(scope)


def _publish_alias(root: Path, intent: _Intent) -> None:
    old, new = _locations(root, intent)
    if old.is_symlink():
        if os.readlink(old) != new.name:  # noqa: PTH115 - Preserve the literal target text.
            _reject("alias changed before publication")
    else:
        # symlink creation never overwrites an entry that appeared after discovery.
        old.symlink_to(new.name, target_is_directory=True)
    fsync_directory_durable(old.parent)


def _move(source: Path, destination: Path) -> None:
    if _directory(destination) is not None:
        _reject("destination appeared before rename")
    source.rename(destination)
    fsync_directory_durable(destination.parent)


def _apply(roots: tuple[Path, Path], intent: _Intent) -> None:
    primary, sessions = roots
    source = _recorded_location(primary, intent, intent.primary_inode)
    assert source is not None
    record = source / _INTENT
    if load_private_instance_record_payload(record) is None:
        write_json_file_durable(record, asdict(intent), strict_atomic_replace=True)
    # A failed earlier publication may have left a visible but unsynced intent.
    fsync_directory_durable(source)
    destination = _locations(primary, intent)[1]
    if primary != sessions:
        mirror = _recorded_location(sessions, intent, intent.session_inode)
        if mirror is not None:
            target = _locations(sessions, intent)[1]
            if mirror != target:
                _move(mirror, target)
            fsync_directory_durable(target.parent)
    if source != destination:
        _move(source, destination)
    fsync_directory_durable(destination.parent)
    write_json_file_durable(
        destination / _RECORD,
        _owner_payload(intent.new_key, intent.requester_id),
        strict_atomic_replace=True,
    )
    if primary != sessions and intent.session_inode is not None:
        _publish_alias(sessions, intent)
    _publish_alias(primary, intent)
    (destination / _INTENT).unlink()
    fsync_directory_durable(destination)


def _migrate(runtime_paths: RuntimePaths) -> None:
    roots = _roots(runtime_paths)
    if not _discover(roots):
        return
    with ExitStack() as locks:
        for root in sorted(set(roots)):
            lock = root / _LOCK
            if lock.is_symlink() or (lock.exists() and not lock.is_file()):
                _reject("volume lock must be a regular file")
            locks.enter_context(advisory_file_lock(lock))
        pending = _discover(_roots(runtime_paths))
        if not pending:
            return
        # Keep Docker and Kubernetes dependencies off primary module import paths.
        from mindroom.workers.storage_preflight import check_workers_absent_for_storage_upgrade  # noqa: PLC0415

        check_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=120.0)
        pending = _discover(_roots(runtime_paths))
        _check_mounts(roots, pending)
        for intent in pending:
            _apply(roots, intent)


async def migrate_private_storage(runtime_paths: RuntimePaths) -> None:
    """Migrate every verified old scope before admitting primary runtime work."""
    from mindroom.background_tasks import run_blocking_until_complete  # noqa: PLC0415

    await run_blocking_until_complete(_migrate, runtime_paths)
