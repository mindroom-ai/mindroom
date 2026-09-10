"""Historical requester keys and verified private-storage aliases."""

# Legacy format: relocated current private directories without historical sibling aliases.
# Last legacy release: v2026.9.36; verified historical aliases introduced in v2026.9.37.
# Handling: accept only aliases proven by the current owner and exact historical reconstruction.
# Coverage: tests/test_private_storage_migration.py::test_completed_aliases_reject_tampering.

from __future__ import annotations

import os
import re
import stat
from typing import TYPE_CHECKING, NoReturn

from mindroom.private_instance_identity_store import (
    PrivateInstanceIdentityError,
    load_private_instance_identity,
    reconstruct_private_instance_worker_key,
)
from mindroom.tool_system.worker_routing import (
    normalize_worker_key_part,
    private_instance_scope_root_path,
    shared_storage_root,
)

if TYPE_CHECKING:
    from pathlib import Path


def historical_private_instance_worker_key(worker_key: str, requester_id: str) -> str:
    """Reconstruct the historical key after validating the exact private scope shape."""
    current = reconstruct_private_instance_worker_key(worker_key, requester_id)
    parts = current.split(":")
    requester = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", requester_id.strip()).strip("_") or "default"
    historical = f"v1:{normalize_worker_key_part(parts[1])}:{parts[2]}:{requester}"
    if parts[2] == "user_agent":
        historical += ":" + normalize_worker_key_part(parts[-1])
    if worker_key not in {historical, current}:
        _raise_invalid_record("does not match the historical or current requester encoding")
    return historical


def load_private_instance_legacy_alias(base_storage_path: Path, worker_key: str) -> Path | None:
    """Return the verified historical alias owned by this current canonical scope.

    The primary's protected namespace retains migration provenance. Owner records
    alone never authorize aliases, and a historical collision grants no access to
    the other current owner. Invalid records or namespace entries fail closed.
    """
    base = shared_storage_root(base_storage_path)
    canonical = private_instance_scope_root_path(base, worker_key)
    owner = load_private_instance_identity(base, canonical)
    if owner is None:
        if canonical.exists():
            _raise_invalid_record("has no canonical owner for its legacy alias")
        return None
    if owner.worker_key != worker_key:
        _raise_invalid_record("does not match the requested current key")
    historical = historical_private_instance_worker_key(worker_key, owner.requester_id)
    alias = private_instance_scope_root_path(base, historical)
    try:
        info = alias.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(info.st_mode):
        return None  # An unmigrated directory grants no additional mount access.
    if not stat.S_ISLNK(info.st_mode):
        _raise_invalid_record("legacy alias must be a symlink")
    # Read the literal target: Path normalization would accept './name' as 'name'.
    target_name = os.readlink(alias)  # noqa: PTH115 - Preserve the literal target text.
    if target_name in {"", ".", ".."} or "/" in target_name:
        _raise_invalid_record("legacy alias must name its canonical sibling")
    target = alias.parent / target_name
    target_owner = load_private_instance_identity(base, target)
    if target_owner is None:
        _raise_invalid_record("legacy alias has no current target owner")
    target_historical = historical_private_instance_worker_key(target_owner.worker_key, target_owner.requester_id)
    if private_instance_scope_root_path(base, target_historical) != alias:
        _raise_invalid_record("legacy alias does not match its current target owner")
    return alias if target == canonical else None


def _raise_invalid_record(reason: str) -> NoReturn:
    msg = f"Private instance identity record {reason}"
    raise PrivateInstanceIdentityError(msg)
