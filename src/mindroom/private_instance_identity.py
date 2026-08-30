"""Durable ownership records for private runtime instances."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn, cast

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    private_instance_scope_root_path,
    resolve_worker_key,
)

if TYPE_CHECKING:
    from pathlib import Path

_RECORD_FILENAME: Final = ".mindroom-private-instance.json"
_LOCK_FILENAME: Final = ".mindroom-private-instance.lock"
_RECORD_FORMAT: Final = "mindroom-private-instance"
_RECORD_VERSION: Final = 1
_RECORD_FIELDS: Final = frozenset({"format", "version", "worker_key", "requester_id"})

__all__ = [
    "PrivateInstanceIdentity",
    "PrivateInstanceIdentityError",
    "ensure_private_instance_identity",
    "load_private_instance_identity",
]


@dataclass(frozen=True)
class PrivateInstanceIdentity:
    """The requester and worker key that authoritatively own one private scope."""

    worker_key: str
    requester_id: str


class PrivateInstanceIdentityError(ValueError):
    """Raised when a private-instance identity record is invalid or conflicting."""


def load_private_instance_identity(scope_root: Path) -> PrivateInstanceIdentity | None:
    """Load and validate the identity record for one private scope, if it exists."""
    record_path = scope_root / _RECORD_FILENAME
    try:
        raw_payload = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        msg = "Private instance identity record is unreadable"
        raise PrivateInstanceIdentityError(msg) from error

    identity = _parse_identity(raw_payload)
    _validate_identity(identity, scope_root)
    return identity


def ensure_private_instance_identity(
    scope_root: Path,
    *,
    worker_key: str,
    requester_id: str,
) -> PrivateInstanceIdentity:
    """Durably create or return the identity record for one private scope."""
    requested_identity = PrivateInstanceIdentity(worker_key=worker_key, requester_id=requester_id)
    _validate_identity(requested_identity, scope_root)

    with advisory_file_lock(scope_root / _LOCK_FILENAME):
        existing_identity = load_private_instance_identity(scope_root)
        if existing_identity is None:
            write_json_file_durable(
                scope_root / _RECORD_FILENAME,
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
    """Return the exact versioned persistence schema for one identity."""
    return {
        "format": _RECORD_FORMAT,
        "version": _RECORD_VERSION,
        "worker_key": identity.worker_key,
        "requester_id": identity.requester_id,
    }


def _parse_identity(payload: object) -> PrivateInstanceIdentity:
    """Parse one exact identity-record payload or fail closed."""
    if not isinstance(payload, dict):
        _raise_invalid_record("must use the exact schema")
    record = cast("dict[str, object]", payload)
    if set(record) != _RECORD_FIELDS:
        _raise_invalid_record("must use the exact schema")

    format_name = record["format"]
    version = record["version"]
    worker_key = record["worker_key"]
    requester_id = record["requester_id"]
    if format_name != _RECORD_FORMAT or type(version) is not int or version != _RECORD_VERSION:
        _raise_invalid_record("has an unsupported format or version")
    if not isinstance(worker_key, str) or not isinstance(requester_id, str) or not requester_id.strip():
        _raise_invalid_record("has invalid identity fields")
    return PrivateInstanceIdentity(worker_key=worker_key, requester_id=requester_id)


def _validate_identity(identity: PrivateInstanceIdentity, scope_root: Path) -> None:
    """Ensure the stored identity matches worker routing and its containing scope path."""
    reconstructed_worker_key = _reconstruct_worker_key(identity.worker_key, identity.requester_id)
    if reconstructed_worker_key != identity.worker_key:
        _raise_invalid_record("does not match its requester")

    resolved_scope_root = scope_root.expanduser().resolve()
    expected_scope_root = private_instance_scope_root_path(resolved_scope_root, identity.worker_key)
    if expected_scope_root != resolved_scope_root:
        _raise_invalid_record("does not match its scope location")


def _reconstruct_worker_key(worker_key: str, requester_id: str) -> str:
    """Rebuild a private worker key from its stable routing components."""
    parts = worker_key.split(":")
    if len(parts) < 4 or parts[0] != "v1" or not parts[1]:
        _raise_invalid_record("has an invalid worker key")

    tenant_id = parts[1]
    scope = parts[2]
    if scope == "user":
        if len(parts) < 4:
            _raise_invalid_record("has an invalid worker key")
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
    """Raise the contract's single validation error type."""
    msg = f"Private instance identity record {reason}"
    raise PrivateInstanceIdentityError(msg)
