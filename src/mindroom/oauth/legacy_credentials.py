"""Historical OAuth publication fields and lossless requester bindings.

Only the SQLite store invokes this boundary when validating or normalizing stored state.
This module never owns a transaction or imports the live store or lifecycle.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.credential_policy import is_oauth_token_service
from mindroom.credentials import scoped_credentials_path
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from mindroom.oauth.credential_store_types import OAuthCredentialStoreContext


_LEGACY_PUBLICATION_KEY = "_mindroom_oauth_publication"


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


def without_legacy_publication(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the retired publication marker from a credential mapping."""
    result = dict(credentials)
    result.pop(_LEGACY_PUBLICATION_KEY, None)
    return result
