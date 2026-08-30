"""Internal durable storage for private runtime identities."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn, cast

from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    private_instance_scope_root_path,
    resolve_worker_key,
    shared_storage_root,
)

if TYPE_CHECKING:
    from pathlib import Path

_RECORD_FILENAME: Final = ".mindroom-private-instance.json"
_LOCK_FILENAME: Final = ".mindroom-private-instance.lock"
_RECORD_FORMAT: Final = "mindroom-private-instance"
_RECORD_VERSION: Final = 1
_RECORD_FIELDS: Final = frozenset({"format", "version", "worker_key", "requester_id"})


@dataclass(frozen=True)
class PrivateInstanceIdentity:
    """The requester and worker key that authoritatively own one private scope."""

    worker_key: str
    requester_id: str


class PrivateInstanceIdentityError(ValueError):
    """Raised when a private-instance identity record is invalid or conflicting."""


def load_private_instance_identity(base_storage_path: Path, scope_root: Path) -> PrivateInstanceIdentity | None:
    """Load and validate the identity record for one private scope, if it exists."""
    trusted_base_path = shared_storage_root(base_storage_path)
    trusted_scope_root = _trusted_scope_root(trusted_base_path, scope_root, create=False)
    payload = _load_record_payload(trusted_scope_root / _RECORD_FILENAME)
    if payload is None:
        return None
    identity = _parse_identity(payload)
    _validate_identity(identity, trusted_base_path, trusted_scope_root)
    return identity


def ensure_private_instance_identity(
    base_storage_path: Path,
    *,
    worker_key: str,
    requester_id: str,
) -> PrivateInstanceIdentity:
    """Durably create or return the identity record for one private scope."""
    requested_identity = _parse_identity(
        {"format": _RECORD_FORMAT, "version": _RECORD_VERSION, "worker_key": worker_key, "requester_id": requester_id},
    )
    trusted_base_path = shared_storage_root(base_storage_path)
    scope_root = private_instance_scope_root_path(trusted_base_path, worker_key)
    _validate_identity(requested_identity, trusted_base_path, scope_root)
    trusted_scope_root = _trusted_scope_root(trusted_base_path, scope_root, create=True)
    with advisory_file_lock(trusted_scope_root / _LOCK_FILENAME):
        existing_identity = load_private_instance_identity(trusted_base_path, trusted_scope_root)
        if existing_identity is None:
            write_json_file_durable(
                trusted_scope_root / _RECORD_FILENAME,
                _identity_payload(requested_identity),
                indent=2,
                sort_keys=True,
                trailing_newline=True,
            )
            return requested_identity
        if existing_identity != requested_identity:
            msg = "Private instance identity conflicts with the existing scope record"
            raise PrivateInstanceIdentityError(msg)
        return existing_identity


def _identity_payload(identity: PrivateInstanceIdentity) -> dict[str, object]:
    return {
        "format": _RECORD_FORMAT,
        "version": _RECORD_VERSION,
        "worker_key": identity.worker_key,
        "requester_id": identity.requester_id,
    }


def _parse_identity(payload: object) -> PrivateInstanceIdentity:
    if not isinstance(payload, dict):
        _raise_invalid_record("must use the exact schema")
    record = cast("dict[str, object]", payload)
    if set(record) != _RECORD_FIELDS:
        _raise_invalid_record("must use the exact schema")
    format_name, version = record["format"], record["version"]
    worker_key, requester_id = record["worker_key"], record["requester_id"]
    if format_name != _RECORD_FORMAT or type(version) is not int or version != _RECORD_VERSION:
        _raise_invalid_record("has an unsupported format or version")
    if not isinstance(worker_key, str) or not isinstance(requester_id, str) or not requester_id.strip():
        _raise_invalid_record("has invalid identity fields")
    return PrivateInstanceIdentity(worker_key, requester_id)


def _load_record_payload(record_path: Path) -> object | None:
    try:
        record_stat = record_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        _raise_unreadable_record(error)
    if not stat.S_ISREG(record_stat.st_mode):
        _raise_invalid_record("must be a regular file")
    try:
        descriptor = os.open(record_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        _raise_unreadable_record(error)
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode) or (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            record_stat.st_dev,
            record_stat.st_ino,
        ):
            _raise_invalid_record("changed while being opened")
        with os.fdopen(descriptor, encoding="utf-8") as record_file:
            descriptor = -1
            raw_payload = record_file.read()
    except (OSError, UnicodeDecodeError) as error:
        _raise_unreadable_record(error)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    try:
        return json.loads(raw_payload, object_pairs_hook=_object_with_unique_keys)
    except json.JSONDecodeError as error:
        _raise_unreadable_record(error)


def _object_with_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            _raise_invalid_record("contains duplicate JSON fields")
        payload[key] = value
    return payload


def _trusted_scope_root(base_storage_path: Path, scope_root: Path, *, create: bool) -> Path:
    namespace_path = base_storage_path / "private_instances"
    candidate_scope_root = scope_root.expanduser().absolute()
    if candidate_scope_root.parent != namespace_path:
        _raise_invalid_record("is outside the trusted private-instance namespace")
    if not _validate_directory_entry(namespace_path, "private-instance namespace"):
        if not create:
            return candidate_scope_root
        create_directory_durable(namespace_path, mode=0o700)
        _validate_directory_entry(namespace_path, "private-instance namespace")
    if not _validate_directory_entry(candidate_scope_root, "private-instance scope root") and create:
        create_directory_durable(candidate_scope_root, mode=0o700)
        _validate_directory_entry(candidate_scope_root, "private-instance scope root")
    return candidate_scope_root


def _validate_directory_entry(path: Path, name: str) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        _raise_unreadable_record(error)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        _raise_invalid_record(f"has an invalid {name}")
    return True


def _validate_identity(identity: PrivateInstanceIdentity, base_storage_path: Path, scope_root: Path) -> None:
    if _reconstruct_worker_key(identity.worker_key, identity.requester_id) != identity.worker_key:
        _raise_invalid_record("does not match its requester")
    if scope_root.expanduser().absolute() != private_instance_scope_root_path(base_storage_path, identity.worker_key):
        _raise_invalid_record("does not match its scope location")


def _reconstruct_worker_key(worker_key: str, requester_id: str) -> str:
    parts = worker_key.split(":")
    if len(parts) < 4 or parts[0] != "v1" or not parts[1]:
        _raise_invalid_record("has an invalid worker key")
    tenant_id, scope = parts[1], parts[2]
    if scope == "user":
        worker_scope: WorkerScope = "user"
        agent_name = "private-instance"
    elif scope == "user_agent":
        if len(parts) < 5 or not parts[-1]:
            _raise_invalid_record("has an invalid worker key")
        worker_scope = "user_agent"
        agent_name = parts[-1]
    else:
        _raise_invalid_record("does not use a private worker scope")
    reconstructed_worker_key = resolve_worker_key(
        worker_scope,
        ToolExecutionIdentity(
            channel="matrix",
            agent_name=agent_name,
            requester_id=requester_id,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id=tenant_id,
        ),
        agent_name=agent_name,
    )
    if reconstructed_worker_key is None:
        _raise_invalid_record("has an invalid requester")
    return reconstructed_worker_key


def _raise_invalid_record(reason: str) -> NoReturn:
    msg = f"Private instance identity record {reason}"
    raise PrivateInstanceIdentityError(msg)


def _raise_unreadable_record(error: BaseException) -> NoReturn:
    msg = "Private instance identity record is unreadable"
    raise PrivateInstanceIdentityError(msg) from error
