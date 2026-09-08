"""Startup-only relocation of verified private scopes, with per-scope recovery.

Deployment must stop previous primaries and independent controllers first.
Managed workers must be absent before inspecting or moving any scope contents.
"""

from __future__ import annotations

import os
import re
import stat
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from mindroom.durable_write import fsync_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.private_instance_identity_store import (
    PrivateInstanceIdentity,
    load_private_instance_record_payload,
    parse_private_instance_identity_payload,
    reconstruct_private_instance_worker_key,
)
from mindroom.tool_system.worker_routing import normalize_worker_key_part, private_instance_scope_root_path

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
    parts = current.split(":")
    requester = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", identity.requester_id.strip()).strip("_") or "default"
    historical = f"v1:{normalize_worker_key_part(parts[1])}:{parts[2]}:{requester}"
    if parts[2] == "user_agent":
        historical += ":" + normalize_worker_key_part(parts[-1])
    if identity.worker_key not in {historical, current}:
        _reject("owner does not match the historical or current requester encoding")
    return historical, current


def _locations(root: Path, intent: _Intent) -> tuple[Path, Path]:
    return private_instance_scope_root_path(root, intent.old_key), private_instance_scope_root_path(
        root,
        intent.new_key,
    )


def _recorded_location(root: Path, intent: _Intent, inode: int | None) -> Path | None:
    locations = [path for path in _locations(root, intent) if _directory(path) is not None]
    if inode is None:
        if locations:
            _reject("unexpected session data for a scope recorded without a session mirror")
        return None
    if len(locations) != 1 or locations[0].lstat().st_ino != inode:
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
    return intent


def _owner_payload(key: str, requester: str) -> dict[str, object]:
    return {"format": "mindroom-private-instance", "version": 1, "worker_key": key, "requester_id": requester}


def _scopes(root: Path) -> list[Path]:
    namespace = root / "private_instances"
    if _directory(namespace) is None:
        return []
    scopes = sorted(namespace.iterdir())
    for scope in scopes:
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
    for scope in _scopes(primary):
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
    _validate_batch(roots, pending, known_names)
    return pending


def _validate_batch(roots: tuple[Path, Path], pending: list[_Intent], known_names: set[str]) -> None:
    primary, sessions = roots
    if sessions != primary:
        for scope in _scopes(sessions):
            if (scope / _INTENT).exists() or (scope / _INTENT).is_symlink():
                _reject("session mirror contains an unrelated migration intent")
            if scope.name not in known_names and any(scope.iterdir()):
                _reject("session-only private data has no authoritative primary owner")
    if pending:
        if any(_directory(root) is None for root in roots):
            _reject("configured migration volume is missing")
        if primary != sessions and (primary.is_relative_to(sessions) or sessions.is_relative_to(primary)):
            _reject("migration volumes must not overlap")
        destinations = [intent.new_key for intent in pending]
        if len(set(destinations)) != len(destinations):
            _reject("multiple old scopes claim the same current owner")


def _raise_scan_error(error: OSError) -> NoReturn:
    raise error


def _check_relative_link(entry: Path, scope: Path, target: Path) -> None:
    """Follow each relative component without allowing intermediate links to leave the scope."""
    remaining = deque(target.parts)
    cursor = entry.parent
    links = 0
    while remaining:
        part = remaining.popleft()
        cursor = cursor.parent if part == ".." else cursor / part
        if not cursor.is_relative_to(scope):
            _reject("relative symlink leaves its relocated scope")
        if cursor.is_symlink():
            links += 1
            target = cursor.readlink()
            if target.is_absolute() or links > 40:
                _reject("relative symlink traverses an absolute link or a cyclic chain")
            remaining.extendleft(reversed(target.parts))
            cursor = cursor.parent


def _check_tree(scope: Path, moving: tuple[Path, ...]) -> None:
    """Inspect directory mounts and links without reading opaque file contents."""
    device = scope.parent.parent.lstat().st_dev
    if any(path.lstat().st_dev != device for path in (scope, scope.parent)):
        _reject("nested scope or namespace mounts cannot be renamed safely")
    for directory, subdirectories, files in os.walk(scope, followlinks=False, onerror=_raise_scan_error):
        path = Path(directory)
        for name in (*subdirectories, *files):
            entry = path / name
            info = entry.lstat()
            if info.st_dev != device:
                _reject("nested mounts cannot be renamed safely")
            if not stat.S_ISLNK(info.st_mode):
                continue
            target = entry.readlink()
            if target.is_absolute():
                if any(target.is_relative_to(old) or entry.resolve().is_relative_to(old) for old in moving):
                    _reject("absolute symlink depends on a relocated scope")
            else:
                _check_relative_link(entry, scope, target)


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
        moving = tuple(path for root in set(roots) for intent in pending for path in _locations(root, intent))
        for intent in pending:
            for root, inode in ((roots[0], intent.primary_inode), (roots[1], intent.session_inode)):
                if inode is not None:
                    scope = _recorded_location(root, intent, inode)
                    assert scope is not None
                    _check_tree(scope, moving)
        for intent in pending:
            _apply(roots, intent)


async def migrate_private_storage(runtime_paths: RuntimePaths) -> None:
    """Migrate every verified old scope before admitting primary runtime work."""
    from mindroom.background_tasks import run_blocking_until_complete  # noqa: PLC0415

    await run_blocking_until_complete(_migrate, runtime_paths)
