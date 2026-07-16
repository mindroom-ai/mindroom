"""Cloud controller identity lookup for desktop-device pinning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nio.store import SqliteStore

from mindroom.matrix.client_session import olm_store_dir, olm_store_exists
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import matrix_state_for_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


class DesktopIdentityError(RuntimeError):
    """A configured MindRoom entity has no pinnable Matrix device identity."""


@dataclass(frozen=True, slots=True)
class DesktopControllerIdentity:
    """Public Matrix identity fields copied to the local desktop bridge."""

    entity_name: str
    user_id: str
    device_id: str
    ed25519: str


def controller_identity_for_entity(
    entity_name: str,
    *,
    runtime_paths: RuntimePaths,
) -> DesktopControllerIdentity:
    """Read one managed entity's exact device identity from its local Olm store."""
    account = matrix_state_for_runtime(runtime_paths).get_account(managed_account_key(entity_name))
    if account is None:
        msg = f"MindRoom entity {entity_name!r} has no managed Matrix account; start MindRoom once first."
        raise DesktopIdentityError(msg)
    if account.domain is None or account.device_id is None:
        msg = f"MindRoom entity {entity_name!r} has no persisted Matrix device; start MindRoom once first."
        raise DesktopIdentityError(msg)

    user_id = MatrixID.from_username(account.username, account.domain).full_id
    if not olm_store_exists(user_id, account.device_id, runtime_paths):
        msg = f"MindRoom entity {entity_name!r} has no local Olm store for device {account.device_id}."
        raise DesktopIdentityError(msg)

    store = SqliteStore(
        user_id,
        account.device_id,
        str(olm_store_dir(user_id, runtime_paths)),
    )
    try:
        olm_account = store.load_account()
        fingerprint = olm_account.identity_keys.get("ed25519") if olm_account is not None else None
    finally:
        store.database.close()
    if not isinstance(fingerprint, str) or not fingerprint:
        msg = f"MindRoom entity {entity_name!r} has no local Ed25519 device identity."
        raise DesktopIdentityError(msg)
    return DesktopControllerIdentity(
        entity_name=entity_name,
        user_id=user_id,
        device_id=account.device_id,
        ed25519=fingerprint,
    )


__all__ = [
    "DesktopControllerIdentity",
    "DesktopIdentityError",
    "controller_identity_for_entity",
]
