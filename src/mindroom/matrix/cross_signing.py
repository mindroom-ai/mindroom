"""Agent cross-signing bootstrap at Matrix login.

MSC4153-era clients stop sharing room keys with devices that are not
cross-signed, so every bot device must carry a self-signed identity.
mindroom-nio owns the mechanism (key generation, persistence next to the
encryption store, upload with password-based UIA fallback, device
signing); this module decides when to run it, recovers when the
homeserver has lost the uploaded identity, and keeps startup resilient
when a homeserver rejects the bootstrap.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nio import crypto
from nio.crypto.cross_signing import cross_signing_sidecar_path

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.users import AgentMatrixUser

logger = get_logger(__name__)


async def ensure_agent_cross_signing(client: nio.AsyncClient, agent_user: AgentMatrixUser) -> None:
    """Bootstrap or refresh the agent's cross-signing identity, without failing startup."""
    if not crypto.ENCRYPTION_ENABLED or client.olm is None:
        return
    try:
        status = await client.ensure_cross_signing(password=agent_user.password)
    except Exception as exc:
        status = await _recover_from_server_identity_loss(client, agent_user, exc)
        if status is None:
            return
    if status != "already_signed":
        logger.info(
            "matrix_cross_signing_ready",
            agent=agent_user.agent_name,
            user_id=client.user_id,
            device_id=client.device_id,
            status=status,
        )


async def _server_master_public_key(client: nio.AsyncClient) -> str | None:
    """The account's master cross-signing public key as the server knows it, or None."""
    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "Content-Type": "application/json",
    }
    response = await client.send(
        "POST",
        "/_matrix/client/v3/keys/query",
        json.dumps({"device_keys": {client.user_id: []}}),
        headers,
    )
    if response.status != 200:
        msg = f"keys/query failed with status {response.status}"
        raise RuntimeError(msg)
    body = await response.json(content_type=None)
    master = body.get("master_keys", {}).get(client.user_id) if isinstance(body, dict) else None
    keys = master.get("keys") if isinstance(master, dict) else None
    if not isinstance(keys, dict) or not keys:
        return None
    key = next(iter(keys.values()))
    return key if isinstance(key, str) else None


async def _recover_from_server_identity_loss(
    client: nio.AsyncClient,
    agent_user: AgentMatrixUser,
    exc: Exception,
) -> str | None:
    """Re-upload the persisted identity once when the server no longer has it.

    The sidecar's ``uploaded`` flag is local state: when the homeserver loses
    the account's cross-signing keys (e.g. a dev-server reset that wiped
    accounts while ``encryption_keys/`` survived), the flag would otherwise
    skip the only key-upload path forever and wedge the bootstrap on every
    startup. Detect the divergence via the server's own view and retry once.
    """
    identity = client.cross_signing_identity
    if identity is None or not identity.uploaded:
        _log_bootstrap_failed(client, agent_user, str(exc))
        return None
    try:
        server_key = await _server_master_public_key(client)
    except Exception as query_exc:
        _log_bootstrap_failed(client, agent_user, f"{exc}; server key check failed: {query_exc}")
        return None
    if server_key == identity.master_public_key:
        # The server still has our identity, so the failure is something else
        # (e.g. a transient signature-upload error); the next startup retries.
        _log_bootstrap_failed(client, agent_user, str(exc))
        return None
    assert client.store_path
    assert client.user_id
    identity.uploaded = False
    identity.signed_devices = []
    identity.save(cross_signing_sidecar_path(str(client.store_path), client.user_id))
    try:
        status = await client.ensure_cross_signing(password=agent_user.password)
    except Exception as retry_exc:
        _log_bootstrap_failed(
            client,
            agent_user,
            f"{exc}; re-upload after server identity loss failed: {retry_exc}",
        )
        return None
    logger.info(
        "matrix_cross_signing_reuploaded_after_server_loss",
        agent=agent_user.agent_name,
        user_id=client.user_id,
        device_id=client.device_id,
        server_master_key=server_key,
        original_error=str(exc),
    )
    return status


def _log_bootstrap_failed(client: nio.AsyncClient, agent_user: AgentMatrixUser, error: str) -> None:
    # Cross-signing is a trust upgrade, not a startup requirement: a
    # homeserver that rejects the upload must not keep the agent offline.
    logger.warning(
        "matrix_cross_signing_bootstrap_failed",
        agent=agent_user.agent_name,
        user_id=client.user_id,
        device_id=client.device_id,
        error=error,
    )


def cross_signing_status_line(client: nio.AsyncClient) -> str:
    """One human-readable cross-signing status line for diagnostics."""
    identity = client.cross_signing_identity
    if identity is None:
        return "not bootstrapped (bot device shows as unverified)"
    if client.device_id in identity.signed_devices:
        return f"active (master key `ed25519:{identity.master_public_key}`)"
    return "keys present, but this device is not yet self-signed"


__all__ = ["cross_signing_status_line", "ensure_agent_cross_signing"]
