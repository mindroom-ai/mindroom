"""Read-only public contract for private runtime identities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import WorkerScope

from mindroom.private_instance_identity_store import (
    PrivateInstance,
    PrivateInstanceIdentity,
    PrivateInstanceIdentityError,
    load_private_instance_identity,
)
from mindroom.private_instance_identity_store import (
    private_instances_for_agent as _private_instances_for_agent,
)
from mindroom.private_storage_upgrade import check_storage_upgrade


def private_instances_for_agent(
    base_storage_path: Path,
    agent_name: str,
    worker_scope: WorkerScope,
) -> tuple[PrivateInstance, ...]:
    """Discover private owners only after the storage upgrade fence passes."""
    check_storage_upgrade(base_storage_path)
    return _private_instances_for_agent(base_storage_path, agent_name, worker_scope)


__all__ = [
    "PrivateInstance",
    "PrivateInstanceIdentity",
    "PrivateInstanceIdentityError",
    "load_private_instance_identity",
    "private_instances_for_agent",
]
