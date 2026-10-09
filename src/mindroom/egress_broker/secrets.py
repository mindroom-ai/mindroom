"""Egress broker secret storage and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.credentials import delete_scoped_credentials, load_scoped_credentials, save_scoped_credentials

if TYPE_CHECKING:
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = [
    "SecretStatus",
    "delete_secret",
    "egress_credential_service",
    "load_secret",
    "save_secret",
    "secret_status",
]

_MAX_SECRET_SIZE = 16 * 1024  # 16 KiB


def egress_credential_service(name: str) -> str:
    """Return the credential service name for an egress service."""
    return f"egress_{name}"


def load_secret(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    name: str,
) -> str | None:
    """Load an egress secret for a worker target.

    Returns the secret string, or None if not configured.
    Requester-scoped targets (user, user_agent) do not fall back to shared/global.
    """
    service = egress_credential_service(name)

    # For requester-scoped targets, disable shared fallback
    allowed_shared = frozenset() if target.worker_scope in ("user", "user_agent") else None

    credentials = load_scoped_credentials(
        service,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
        allowed_shared_services=allowed_shared,
    )

    if credentials is None:
        return None

    return credentials.get("secret")  # type: ignore[return-value]


def save_secret(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    name: str,
    secret: str,
) -> None:
    """Save an egress secret for a worker target.

    Raises ValueError if the secret is empty/whitespace, over 16 KiB,
    or contains ASCII control characters.
    """
    # Validate secret
    if not secret or not secret.strip():
        msg = "Secret cannot be empty or whitespace"
        raise ValueError(msg)

    # Check for ASCII control characters (ord < 0x20 or 0x7f)
    for char in secret:
        char_ord = ord(char)
        if char_ord < 0x20 or char_ord == 0x7F:
            msg = f"Secret contains invalid control character (ord {char_ord})"
            raise ValueError(msg)

    if len(secret.encode("utf-8")) > _MAX_SECRET_SIZE:
        msg = f"Secret size exceeds maximum of 16 KiB ({len(secret.encode('utf-8'))} bytes)"
        raise ValueError(msg)

    service = egress_credential_service(name)

    # Build credential document with secret and timestamp
    credentials = {
        "secret": secret,
        "_updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    save_scoped_credentials(
        service,
        credentials,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )


def delete_secret(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    name: str,
) -> None:
    """Delete an egress secret for a worker target."""
    service = egress_credential_service(name)

    delete_scoped_credentials(
        service,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )


@dataclass(frozen=True)
class SecretStatus:
    """Status of an egress secret."""

    configured: bool
    updated_at: str | None


def secret_status(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    name: str,
) -> SecretStatus:
    """Return the status of an egress secret.

    Returns whether it is configured and when it was last updated.
    Never returns the secret value.
    """
    service = egress_credential_service(name)

    # For requester-scoped targets, disable shared fallback
    allowed_shared = frozenset() if target.worker_scope in ("user", "user_agent") else None

    credentials = load_scoped_credentials(
        service,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
        allowed_shared_services=allowed_shared,
    )

    if credentials is None:
        return SecretStatus(configured=False, updated_at=None)

    return SecretStatus(
        configured=True,
        updated_at=credentials.get("_updated_at"),  # type: ignore[arg-type]
    )
