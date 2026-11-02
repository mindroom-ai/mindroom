"""Legacy OAuth requester bindings without the lossless prefix.

Only the SQLite store invokes this boundary when validating a stored scope binding.
This module never owns a transaction or imports the live store or lifecycle.
"""

# LEGACY_COMPAT: OAuth requester bindings without the lossless prefix.
# Legacy format: OAuth scope bindings used requester keys without the lossless `~` prefix.
# Last legacy release: v2026.9.32; prefixed lossless encoding introduced in v2026.9.33.
# Handling: compatibility added in v2026.9.34 retains verified bindings at their raw-identity paths.
# Coverage: tests/test_oauth_credential_store.py::test_tagged_oauth_scope_binding_reads_literal_v1_database.

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.credentials.manager import scoped_credentials_path
from mindroom.credentials.policy import is_oauth_token_service
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    import sqlite3

    from mindroom.oauth.credential_store_types import OAuthCredentialStoreContext


def compatible_legacy_worker_key(
    context: OAuthCredentialStoreContext,
    connection: sqlite3.Connection,
) -> str | None:
    """Read legacy requester bindings at stable raw-identity paths without migrating them.

    The requester encoding upgrade changed worker keys but left these stores in place.
    Accept only lossless legacy spellings and retain the stored binding for rollback.
    """
    target = context.worker_target
    manager = context.credentials_manager
    if (
        target is None
        or target.worker_scope not in {"user", "user_agent"}
        or not is_oauth_token_service(context.provider.credential_service)
        or manager.current_worker_key is not None
        or manager.storage_root != context.runtime_paths.storage_root
    ):
        return None
    identity = target.execution_identity
    if identity is None or not identity.requester_id:
        return None
    requester = identity.requester_id
    legacy_requester = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", requester.strip()).strip("_") or "default"
    if legacy_requester != requester:
        return None
    canonical_target = resolve_worker_target(
        target.worker_scope,
        target.routing_agent_name,
        execution_identity=identity,
        private_agent_names=target.private_agent_names,
    )
    if canonical_target != target:
        return None
    # Raw requester/agent hashes own legacy stores; never search or adopt worker directories.
    # Old bindings cannot distinguish a misfiled database from a colliding legacy identity.
    credential_path = scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    canonical_path = credential_path.with_name(f"{credential_path.stem}.sqlite3")
    database_path = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
    if Path(database_path).absolute() != canonical_path.absolute():
        return None
    assert canonical_target.worker_key is not None
    return canonical_target.worker_key.replace(
        f":{target.worker_scope}:~{requester}",
        f":{target.worker_scope}:{requester}",
        1,
    )
