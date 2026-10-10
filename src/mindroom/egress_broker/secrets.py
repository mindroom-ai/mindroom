"""Egress broker secret storage and resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.credentials import delete_scoped_credentials, load_scoped_credentials, save_scoped_credentials

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.egress_broker import EgressService
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = [
    "EgressServiceStatus",
    "OAuthStatus",
    "OAuthStatusReader",
    "Secret",
    "SecretMissing",
    "SecretNeedsReconnect",
    "SecretResult",
    "SecretStatus",
    "SecretUnavailable",
    "delete_secret",
    "egress_credential_service",
    "load_secret",
    "save_secret",
    "secret_status",
    "service_status",
]

_MAX_SECRET_SIZE = 16 * 1024  # 16 KiB


def egress_credential_service(name: str) -> str:
    """Return the credential service name for an egress service."""
    return f"egress_{name}"


def load_secret(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    name: str,
) -> str | None:
    """Load an egress secret for a worker target or the global store.

    Args:
        manager: Credentials manager
        target: Worker target, or None for the global (unscoped) store
        name: Service name

    Returns the secret string, or None if not configured.
    Requester-scoped targets (user, user_agent) do not fall back to shared/global.

    """
    service = egress_credential_service(name)

    # For global (unscoped) secrets, load directly from the base manager
    if target is None:
        credentials = manager.load_credentials(service)
        if credentials is None:
            return None
        return credentials.get("secret")  # type: ignore[return-value]

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
    target: ResolvedWorkerTarget | None,
    name: str,
    secret: str,
) -> None:
    """Save an egress secret for a worker target or the global store.

    Args:
        manager: Credentials manager
        target: Worker target, or None for the global (unscoped) store
        name: Service name
        secret: Secret value to store

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

    # For global (unscoped) secrets, save directly to the base manager
    if target is None:
        manager.save_credentials(service, credentials)
        return

    save_scoped_credentials(
        service,
        credentials,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )


def delete_secret(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    name: str,
) -> None:
    """Delete an egress secret for a worker target or the global store.

    Args:
        manager: Credentials manager
        target: Worker target, or None for the global (unscoped) store
        name: Service name

    """
    service = egress_credential_service(name)

    # For global (unscoped) secrets, delete directly from the base manager
    if target is None:
        manager.delete_credentials(service)
        return

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
    target: ResolvedWorkerTarget | None,
    name: str,
) -> SecretStatus:
    """Return the status of an egress secret.

    Args:
        manager: Credentials manager
        target: Worker target, or None for the global (unscoped) store
        name: Service name

    Returns whether it is configured and when it was last updated.
    Never returns the secret value.

    """
    service = egress_credential_service(name)

    # For global (unscoped) secrets, check the base manager directly
    if target is None:
        credentials = manager.load_credentials(service)
        if credentials is None:
            return SecretStatus(configured=False, updated_at=None)
        return SecretStatus(
            configured=True,
            updated_at=credentials.get("_updated_at"),  # type: ignore[arg-type]
        )

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


@dataclass(frozen=True)
class Secret:
    """The secret the broker injects for one request; its value never appears in a repr."""

    value: str = field(repr=False)


@dataclass(frozen=True)
class SecretMissing:
    """No secret in scope: no stored key and no usable OAuth connection.

    `provider` and `connect_url` name the OAuth account the user can connect instead, when the service has a
    connectable provider. The URL carries a one-time connect token, so it stays out of reprs and logs.
    """

    provider: str | None = None
    connect_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SecretNeedsReconnect:
    """The scope's OAuth connection cannot supply a token until the user reconnects: its grant was revoked.

    `reset_required` means the stored credential is unreadable and must be reset before reconnecting.
    """

    provider: str
    connect_url: str | None = field(default=None, repr=False)
    reset_required: bool = False


@dataclass(frozen=True)
class SecretUnavailable:
    """Refreshing the scope's OAuth token failed for a reason that may pass, such as a provider outage."""

    provider: str


type SecretResult = Secret | SecretMissing | SecretNeedsReconnect | SecretUnavailable


@dataclass(frozen=True)
class OAuthStatus:
    """Connection state of a service's OAuth provider for one scope; never holds a token.

    `connected` means the broker would inject a personal access token. A configured service account
    (`service_account`) is not a token the broker can inject, so it never makes a scope connected by itself
    and its provider is never connectable. `unavailable_reason` says why the broker never uses this provider's
    accounts in the scope (`shared_worker`: a requester's own account on a worker that several requesters share).
    `shared_worker_opt_in` marks such a worker where the service allows it anyway, so every user of the agent can
    act with the connected account.
    """

    provider: str
    display_name: str
    connected: bool
    account_label: str | None
    can_connect: bool
    reset_required: bool
    service_account: bool = False
    unavailable_reason: Literal["shared_worker"] | None = None
    shared_worker_opt_in: bool = False


type OAuthStatusReader = Callable[[str, ResolvedWorkerTarget | None], OAuthStatus | None]


@dataclass(frozen=True)
class EgressServiceStatus:
    """Which secret source a service has in one scope; an explicit key wins over OAuth."""

    configured: bool
    active_source: Literal["key", "oauth"] | None
    key_configured: bool
    key_updated_at: str | None
    oauth: OAuthStatus | None


def service_status(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    service: EgressService,
    name: str,
    *,
    oauth_status: OAuthStatusReader,
) -> EgressServiceStatus:
    """Return both secret sources of an egress service for a worker target or the global store.

    `oauth_status(provider_id, target)` reports the service's OAuth provider for the same target; it is not
    called for services without one. Never returns a secret or token.
    """
    key = secret_status(manager, target, name)
    oauth = oauth_status(service.oauth_provider, target) if service.oauth_provider is not None else None
    active_source: Literal["key", "oauth"] | None = None
    if key.configured:
        active_source = "key"
    elif oauth is not None and oauth.connected:
        active_source = "oauth"
    return EgressServiceStatus(
        configured=active_source is not None,
        active_source=active_source,
        key_configured=key.configured,
        key_updated_at=key.updated_at,
        oauth=oauth,
    )
