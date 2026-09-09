"""Historical OAuth credential adoption, normalization, binding and file retirement.

Only the SQLite store invokes this boundary, at its initialization and commit milestones.
This module never owns a transaction or imports the live store or lifecycle.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from mindroom.credential_policy import is_oauth_token_service
from mindroom.credentials import scoped_credentials_path
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from mindroom.oauth.credential_store_types import OAuthCredentialStoreContext


logger = get_logger(__name__)
_LEGACY_PUBLICATION_KEY = "_mindroom_oauth_publication"


@dataclass(frozen=True, slots=True)
class _LegacyCredentialPayload:
    """One legacy credential prepared for atomic SQLite adoption."""

    payload: bytes | None
    present: bool
    unreadable: bool


@dataclass(slots=True)
class OAuthCredentialCompatibility:
    """Retain historical adoption and cleanup state across the store's two commits."""

    context: OAuthCredentialStoreContext
    _adoption: _LegacyCredentialPayload | None = None
    _cleanup_deferred: bool = False
    _cleanup_on_commit: bool = False

    def initial_payload(self) -> _LegacyCredentialPayload:
        """Prepare the first credential state and remember any adoption to report."""
        payload = _legacy_credential_payload(self.context)
        if payload.present:
            self._adoption = payload
        return payload

    def adopt_deferred_payload(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        """Retry retained source bytes within the store's initialization transaction."""
        self._adoption = _adopt_deferred_legacy_payload(self.context, connection, row)

    def prepare_initialization_commit(self, connection: sqlite3.Connection) -> None:
        """Capture cleanup eligibility while initialization still holds its lock."""
        self._cleanup_deferred = _legacy_cleanup_must_be_deferred(connection)

    def initialization_committed(self) -> None:
        """Report committed adoption and retire sources whose bytes are now durable."""
        if self._adoption is not None:
            logger.info(
                "oauth_legacy_credentials_adopted",
                provider_id=self.context.provider.id,
                credential_service=self.context.provider.credential_service,
                credential_present=self._adoption.present,
                credential_unreadable=self._adoption.unreadable,
            )
        if not self._cleanup_deferred:
            _cleanup_legacy_files(self.context)

    def credentials_replaced(self) -> None:
        """Schedule retained-source cleanup after publish, reset or normalization."""
        self._cleanup_on_commit = self._cleanup_deferred

    def transaction_committed(self) -> None:
        """Retire a retained source only after the replacement has committed."""
        if self._cleanup_on_commit:
            _cleanup_legacy_files(self.context)


def _adopt_deferred_legacy_payload(
    context: OAuthCredentialStoreContext,
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> _LegacyCredentialPayload | None:
    """Adopt a retained legacy payload once the active codec can represent it."""
    legacy = deferred_legacy_payload(context, row)
    if legacy is None:
        return None
    connection.execute(
        """
        UPDATE oauth_credential_state
        SET credential_payload = ?, credential_unreadable = ?,
            generation = ?, connection_generation = ?
        WHERE singleton = 1
        """,
        (
            legacy.payload,
            int(legacy.unreadable),
            secrets.token_hex(32),
            secrets.token_hex(32),
        ),
    )
    return legacy


def deferred_legacy_payload(
    context: OAuthCredentialStoreContext,
    row: sqlite3.Row,
) -> _LegacyCredentialPayload | None:
    """Return retained legacy bytes that the active codec can now represent."""
    if (
        not bool(row["credential_present"])
        or not bool(row["credential_unreadable"])
        or row["credential_payload"] is not None
    ):
        return None
    legacy = _legacy_credential_payload(context)
    return legacy if legacy.present and legacy.payload is not None else None


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
    credential_path = _legacy_credential_path(context)
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


def _legacy_cleanup_must_be_deferred(connection: sqlite3.Connection) -> bool:
    """Keep the legacy source when its bytes were deliberately not adopted."""
    row = connection.execute(
        """
        SELECT credential_payload, credential_present, credential_unreadable
        FROM oauth_credential_state
        WHERE singleton = 1
        """,
    ).fetchone()
    return (
        row is not None
        and bool(row["credential_present"])
        and bool(row["credential_unreadable"])
        and row["credential_payload"] is None
    )


def _legacy_credential_payload(context: OAuthCredentialStoreContext) -> _LegacyCredentialPayload:
    legacy_path = _legacy_credential_path(context)
    try:
        raw = legacy_path.read_bytes()
    except FileNotFoundError:
        return _LegacyCredentialPayload(payload=None, present=False, unreadable=False)
    manager = context.credentials_manager
    try:
        credentials = manager.decode_credentials(context.provider.credential_service, raw)
    except (OSError, TypeError, ValueError, InvalidTag):
        retain_payload = not manager.credentials_encryption_enabled or manager.payload_is_encrypted(raw)
        return _LegacyCredentialPayload(
            payload=raw if retain_payload else None,
            present=True,
            unreadable=True,
        )
    normalized = without_legacy_publication(credentials)
    return _LegacyCredentialPayload(
        payload=manager.encode_credentials(context.provider.credential_service, normalized),
        present=True,
        unreadable=False,
    )


def _legacy_credential_path(context: OAuthCredentialStoreContext) -> Path:
    return scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )


def _cleanup_legacy_files(context: OAuthCredentialStoreContext) -> None:
    credential_path = _legacy_credential_path(context)
    paths = (
        credential_path,
        credential_path.with_name(f"{credential_path.name}.oauth-generation.json"),
        credential_path.with_name(f"{credential_path.name}.oauth-operation.lock"),
        credential_path.with_name(f"{credential_path.name}.oauth-refresh.lock"),
    )
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "oauth_legacy_credential_cleanup_failed",
                provider_id=context.provider.id,
                credential_service=context.provider.credential_service,
                error_type=type(exc).__name__,
            )


def without_legacy_publication(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the retired publication marker from a credential mapping."""
    result = dict(credentials)
    result.pop(_LEGACY_PUBLICATION_KEY, None)
    return result
