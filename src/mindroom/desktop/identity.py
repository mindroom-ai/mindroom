"""Cloud controller identity lookup for desktop-device pinning."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nio.crypto import OlmAccount

from mindroom.matrix.client_session import matrix_client_config, olm_store_dir, olm_store_exists
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import matrix_state_for_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


_SUPPORTED_STORE_VERSION = 10
_STORE_VERSION_QUERY = "SELECT version FROM storeversion"
_ACCOUNT_QUERY = "SELECT account, user_id, device_id, shared FROM accounts"


class DesktopIdentityError(RuntimeError):
    """A configured MindRoom entity has no pinnable Matrix device identity."""


@dataclass(frozen=True, slots=True)
class DesktopControllerIdentity:
    """Public Matrix identity fields copied to the local desktop bridge."""

    entity_name: str
    user_id: str
    device_id: str
    ed25519: str


def _read_olm_account(database_path: Path, user_id: str, device_id: str) -> OlmAccount:
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        version_rows = connection.execute(_STORE_VERSION_QUERY).fetchall()
        if (
            len(version_rows) != 1
            or type(version_rows[0][0]) is not int
            or version_rows[0][0] != _SUPPORTED_STORE_VERSION
        ):
            msg = "local Olm store version is missing or unsupported"
            raise ValueError(msg)

        account_rows = connection.execute(_ACCOUNT_QUERY).fetchall()
        if len(account_rows) != 1:
            msg = "local Olm store account row cardinality is not one"
            raise ValueError(msg)
        account_pickle, stored_user_id, stored_device_id, shared = account_rows[0]
        if stored_user_id != user_id or stored_device_id != device_id:
            msg = "local Olm store account identity does not match"
            raise ValueError(msg)
        if type(account_pickle) is not bytes:
            msg = "local Olm store account pickle is not bytes"
            raise TypeError(msg)
        if type(shared) is not int or shared not in (0, 1):
            msg = "local Olm store account shared flag is invalid"
            raise ValueError(msg)
    finally:
        connection.close()

    return OlmAccount.from_pickle(
        account_pickle,
        matrix_client_config().pickle_key,
        bool(shared),
    )


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

    try:
        database_path = olm_store_dir(user_id, runtime_paths) / f"{user_id}_{account.device_id}.db"
        olm_account = _read_olm_account(database_path, user_id, account.device_id)
    except Exception as exc:
        msg = f"MindRoom entity {entity_name!r} has an unreadable local Olm identity store."
        raise DesktopIdentityError(msg) from exc
    fingerprint = olm_account.identity_keys.get("ed25519")
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
