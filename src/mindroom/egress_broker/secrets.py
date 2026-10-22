"""Egress broker secret storage and resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from mindroom.credentials.manager import delete_scoped_credentials, load_scoped_credentials, save_scoped_credentials
from mindroom.credentials.policy import egress_credential_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.egress_broker import EgressService
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = [
    "PERSONAL_WORKER_SCOPES",
    "EgressServiceStatus",
    "OAuthStatus",
    "OAuthStatusReader",
    "Secret",
    "SecretMissing",
    "SecretNeedsReconnect",
    "SecretResult",
    "SecretServiceChanged",
    "SecretUnavailable",
    "delete_egress_document",
    "delete_secret",
    "is_personal_target",
    "load_egress_document",
    "load_secret",
    "save_egress_document",
    "save_secret",
    "service_status",
]

_MAX_SECRET_SIZE = 16 * 1024  # 16 KiB
# Worker scopes whose sandbox and egress store belong to one requester.
PERSONAL_WORKER_SCOPES: frozenset[str] = frozenset({"user", "user_agent"})


def is_personal_target(target: ResolvedWorkerTarget | None) -> bool:
    """Return whether `target` has a personal worker scope; None is the global store, which is not personal."""
    return target is not None and target.worker_scope in PERSONAL_WORKER_SCOPES


def load_egress_document(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    credential_service: str,
) -> dict[str, Any] | None:
    """Load one egress credential document from the primary-only store of a worker target or the global store.

    `target=None` is the global (unscoped) store. Requester-scoped targets (user, user_agent) never fall back to
    shared or global documents. Worker stores are never read: workers can write them.
    """
    # For global (unscoped) documents, load directly from the base manager
    if target is None:
        return manager.load_credentials(credential_service)

    # For requester-scoped targets, disable shared fallback
    allowed_shared = frozenset() if is_personal_target(target) else None

    return load_scoped_credentials(
        credential_service,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
        allowed_shared_services=allowed_shared,
    )


def save_egress_document(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    credential_service: str,
    document: dict[str, Any],
) -> None:
    """Save one egress credential document to the store `load_egress_document` reads for the same target."""
    # For global (unscoped) documents, save directly to the base manager
    if target is None:
        manager.save_credentials(credential_service, document)
        return

    save_scoped_credentials(
        credential_service,
        document,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )


def delete_egress_document(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    credential_service: str,
) -> None:
    """Delete one egress credential document from the store `load_egress_document` reads for the same target."""
    # For global (unscoped) documents, delete directly from the base manager
    if target is None:
        manager.delete_credentials(credential_service)
        return

    delete_scoped_credentials(
        credential_service,
        credentials_manager=manager,
        worker_target=target,
        primary_built_tool=True,
    )


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
    credentials = load_egress_document(manager, target, egress_credential_service(name))
    if credentials is None:
        return None
    return credentials.get("secret")


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

    # Build credential document with secret and timestamp
    credentials = {
        "secret": secret,
        "_updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    save_egress_document(manager, target, egress_credential_service(name), credentials)


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
    delete_egress_document(manager, target, egress_credential_service(name))


@dataclass(frozen=True)
class Secret:
    """The secret the broker injects for one request; its value never appears in a repr."""

    value: str = field(repr=False)


@dataclass(frozen=True)
class SecretMissing:
    """No secret in scope: no stored key and no usable OAuth connection.

    `provider` and `connect_url` name the OAuth account the user can connect instead, when the service has a
    connectable provider. The URL carries a one-time connect token, so it stays out of reprs and logs.
    `reuse_off` means nobody allowed Agent Keys to use the `provider` account for this service, so it was not read
    and there is no link; `requester_owned` says the account is the requester's own, which only they can allow.
    """

    provider: str | None = None
    connect_url: str | None = field(default=None, repr=False)
    reuse_off: bool = False
    requester_owned: bool = False


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


@dataclass(frozen=True)
class SecretServiceChanged:
    """The service no longer has the definition whose rule routed the request, so its credential may be for other hosts.

    Nothing is resolved; a retry is routed by the current definition.
    """


type SecretResult = Secret | SecretMissing | SecretNeedsReconnect | SecretUnavailable | SecretServiceChanged


@dataclass(frozen=True)
class OAuthStatus:
    """Connection state of a service's OAuth provider for one scope; never holds a token.

    `connected` means the broker would inject a personal access token. A configured service account
    (`service_account`) is not a token the broker can inject, so it never makes a scope connected by itself
    and its provider is never connectable. `unavailable_reason` says why the broker never uses this provider's
    accounts in the scope (`shared_sandbox`: a requester's own account in a sandbox that several requesters share).
    `shared_worker_opt_in` marks such a sandbox where the service allows it anyway, so every user of the agent can
    act with the connected account. `reuse_enabled` says whether Agent Keys may use the connection for this service;
    without it a connected account serves only its tools.
    """

    provider: str
    display_name: str
    connected: bool
    account_label: str | None
    can_connect: bool
    reset_required: bool
    service_account: bool = False
    unavailable_reason: Literal["shared_sandbox"] | None = None
    shared_worker_opt_in: bool = False
    reuse_enabled: bool = False


type OAuthStatusReader = Callable[[str, ResolvedWorkerTarget | None], OAuthStatus | None]


@dataclass(frozen=True)
class EgressServiceStatus:
    """Which secret source a service has in one scope; an explicit key wins over an OAuth connection it may use."""

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
    key = load_egress_document(manager, target, egress_credential_service(name))
    oauth = oauth_status(service.oauth_provider, target) if service.oauth_provider is not None else None
    active_source: Literal["key", "oauth"] | None = None
    if key is not None:
        active_source = "key"
    elif oauth is not None and oauth.connected and oauth.reuse_enabled:
        active_source = "oauth"
    return EgressServiceStatus(
        configured=active_source is not None,
        active_source=active_source,
        key_configured=key is not None,
        key_updated_at=None if key is None else key.get("_updated_at"),
        oauth=oauth,
    )
