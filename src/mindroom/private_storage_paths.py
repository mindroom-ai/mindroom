"""Resolve the verified additional mount spellings of private scopes at the storage boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.legacy_private_storage_aliases import load_private_instance_legacy_alias
from mindroom.private_instance_identity_store import load_private_instance_identity
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
