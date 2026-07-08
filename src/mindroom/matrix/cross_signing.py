"""Agent cross-signing bootstrap at Matrix login.

MSC4153-era clients stop sharing room keys with devices that are not
cross-signed, so every bot device must carry a self-signed identity.
mindroom-nio owns the mechanism (key generation, persistence next to the
encryption store, upload with password-based UIA fallback, device
signing); this module decides when to run it and keeps startup resilient
when a homeserver rejects the bootstrap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nio import crypto

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
        # Cross-signing is a trust upgrade, not a startup requirement: a
        # homeserver that rejects the upload must not keep the agent offline.
        logger.warning(
            "matrix_cross_signing_bootstrap_failed",
            agent=agent_user.agent_name,
            user_id=client.user_id,
            device_id=client.device_id,
            error=str(exc),
        )
        return
    if status != "already_signed":
        logger.info(
            "matrix_cross_signing_ready",
            agent=agent_user.agent_name,
            user_id=client.user_id,
            device_id=client.device_id,
            status=status,
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
