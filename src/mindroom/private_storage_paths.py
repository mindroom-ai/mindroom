"""Resolve private-storage paths and additional mounts at the storage boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.private_instance_identity_store import load_private_instance_identity
from mindroom.private_storage_compat import (
    historical_private_instance_worker_key,
    load_private_instance_legacy_alias,
)
from mindroom.tool_system.worker_routing import private_instance_scope_root_path

if TYPE_CHECKING:
    from pathlib import Path


def private_scope_alias_paths(base_storage_path: Path, worker_key: str) -> tuple[Path, ...]:
    """Return verified additional mount paths for an owned canonical scope."""
    canonical = private_instance_scope_root_path(base_storage_path, worker_key)
    if load_private_instance_identity(base_storage_path, canonical) is None:
        return ()
    alias = load_private_instance_legacy_alias(base_storage_path, worker_key)
    return () if alias is None else (alias,)


def resolve_private_scope_path(base_storage_path: Path, worker_key: str, candidate: Path) -> Path:
    """Resolve a retained scope spelling before the caller checks its allowed roots."""
    canonical = private_instance_scope_root_path(base_storage_path, worker_key)
    if not candidate.is_relative_to(canonical.parent):
        return candidate
    owner = load_private_instance_identity(base_storage_path, canonical)
    if owner is None or owner.worker_key != worker_key:
        return candidate
    historical = private_instance_scope_root_path(
        base_storage_path,
        historical_private_instance_worker_key(worker_key, owner.requester_id),
    )
    try:
        if candidate.is_relative_to(historical) and historical.samefile(canonical):
            return (canonical / candidate.relative_to(historical)).resolve()
    except FileNotFoundError:
        pass
    return candidate
