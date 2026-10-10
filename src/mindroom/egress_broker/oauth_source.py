"""MindRoom's OAuth connections as an egress broker secret source; access tokens never leave the primary.

Tokens come only from `mindroom.oauth.credential_lifecycle`, which refreshes a token near expiry under the
same serialized transaction the tools sharing that connection use. Credential scope follows each provider's own
policy, so a requester-scoped provider such as GitHub uses the requester from the verified proxy token even on a
shared agent. Logs carry the service, the provider id, and error types, never tokens or connect links.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.egress_broker.secrets import OAuthStatus
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    load_oauth_credentials_snapshot_sync,
    oauth_credentials_usable,
    oauth_verified_claim,
    refresh_oauth_credentials_blocking,
    resolve_oauth_credential_context,
)
from mindroom.oauth.providers import OAuthProviderError, OAuthRefreshRejectedError
from mindroom.oauth.registry import load_oauth_providers
from mindroom.oauth.service import (
    OAUTH_REFRESH_REJECTED_REASON,
    oauth_connection_required,
    oauth_provider_service_account_configured,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = ["Missing", "NeedsReconnect", "OAuthTokenResult", "Token", "oauth_status", "resolve_oauth_token"]

logger = get_logger(__name__)

_warned_unknown_providers: set[tuple[str, str]] = set()
_warned_unknown_providers_lock = threading.Lock()
# Each connect link stores a one-time token in the OAuth state file, so a worker retrying in a loop would grow it
# without bound; one scope reuses its link for this long, well inside the token's 10-minute lifetime.
_CONNECT_URL_REUSE_SECONDS = 60.0
_connect_urls: dict[tuple[object, ...], tuple[float, str | None]] = {}
_connect_urls_lock = threading.Lock()


@dataclass(frozen=True)
class Token:
    """A usable access token for the scope's connection; its value never appears in a repr."""

    value: str = field(repr=False)


@dataclass(frozen=True)
class Missing:
    """The scope has no usable connection; `connect_url` is set when the user can connect one."""

    connect_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class NeedsReconnect:
    """The scope's connection cannot supply a token: the grant was revoked, refresh failed, or it is unreadable."""

    connect_url: str | None = field(default=None, repr=False)
    reset_required: bool = False


type OAuthTokenResult = Token | Missing | NeedsReconnect


def resolve_oauth_token(
    *,
    service: str,
    provider_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget,
) -> OAuthTokenResult:
    """Return a fresh access token from the scope's connection to `provider_id`, refreshing it when near expiry.

    `service` names the egress service in the one warning logged for a provider id the registry does not know.
    Blocks on the OAuth transaction owner, so callers on an event loop run it in a thread.
    """
    provider = _registered_provider(service, provider_id, config, runtime_paths)
    if provider is None:
        return Missing()
    context = _credential_context(provider, config, runtime_paths, credentials_manager, worker_target)
    if provider.requester_scoped_credentials and context.worker_target is None:
        # No requester to bind the connection to, so there is no stored token to use.
        return Missing(_connect_url(context))
    try:
        credentials = refresh_oauth_credentials_blocking(context)
    except OAuthProviderError as exc:
        logger.warning(
            "egress_broker_oauth_token_unavailable",
            service=service,
            provider_id=provider.id,
            error_type=type(exc).__name__,
        )
        if isinstance(exc, OAuthCredentialUnreadableError):
            # Unreadable state must be reset from the dashboard before a new connection can be stored.
            return NeedsReconnect(reset_required=True)
        reason = OAUTH_REFRESH_REJECTED_REASON if isinstance(exc, OAuthRefreshRejectedError) else None
        return NeedsReconnect(_connect_url(context, reason))
    token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
    if not oauth_credentials_usable(provider, runtime_paths, credentials) or not isinstance(token, str) or not token:
        return Missing(_connect_url(context))
    return Token(token)


def oauth_status(
    provider_id: str,
    worker_target: ResolvedWorkerTarget | None,
    *,
    service: str,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
) -> OAuthStatus | None:
    """Return the scope's connection state for `provider_id` without refreshing; None for an unknown provider.

    A stored connection whose access token expired still counts as connected while it can be refreshed.
    Binding the keyword arguments gives `secrets.service_status` its `oauth_status` reader.
    """
    provider = _registered_provider(service, provider_id, config, runtime_paths)
    if provider is None:
        return None
    context = _credential_context(provider, config, runtime_paths, credentials_manager, worker_target)
    credentials = None
    reset_required = False
    if context.worker_target is not None or not provider.requester_scoped_credentials:
        try:
            credentials = load_oauth_credentials_snapshot_sync(context).credentials
        except OAuthCredentialUnreadableError:
            reset_required = True
    connected = oauth_credentials_usable(provider, runtime_paths, credentials)
    return OAuthStatus(
        provider=provider.id,
        display_name=provider.display_name,
        connected=connected,
        account_label=oauth_verified_claim(credentials, "email") if connected and credentials else None,
        can_connect=_connectable(provider, runtime_paths),
        reset_required=reset_required,
        service_account=oauth_provider_service_account_configured(provider, runtime_paths),
    )


def _registered_provider(
    service: str,
    provider_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> OAuthProvider | None:
    provider = load_oauth_providers(config, runtime_paths).get(provider_id)
    if provider is None:
        with _warned_unknown_providers_lock:
            first = (service, provider_id) not in _warned_unknown_providers
            _warned_unknown_providers.add((service, provider_id))
        if first:
            logger.warning("egress_broker_oauth_provider_unknown", service=service, provider_id=provider_id)
    return provider


def _credential_context(
    provider: OAuthProvider,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> OAuthCredentialContext:
    """Resolve the credential scope the provider's tools use for this worker target."""
    if worker_target is not None and worker_target.worker_scope is None and worker_target.worker_key is not None:
        # An unscoped worker's routing key is not part of its credential scope: tools on unscoped agents have none.
        worker_target = replace(worker_target, worker_key=None)
    return resolve_oauth_credential_context(
        provider,
        runtime_paths,
        credentials_manager,
        worker_target,
        config=config,
    )


def _connectable(provider: OAuthProvider, runtime_paths: RuntimePaths) -> bool:
    """Return whether a user can connect a personal account: the client is configured and no service account is."""
    return provider.client_config(runtime_paths) is not None and not oauth_provider_service_account_configured(
        provider,
        runtime_paths,
    )


def _connect_url(context: OAuthCredentialContext, reason: str | None = None) -> str | None:
    """Return the scope's connect link, minting at most one per scope and reason every reuse period."""
    if not _connectable(context.provider, context.runtime_paths):
        return None
    key = (context.runtime_paths.storage_root, context.provider.id, context.worker_target, reason)
    now = time.monotonic()
    with _connect_urls_lock:
        expired = [old for old, (minted, _url) in _connect_urls.items() if now - minted >= _CONNECT_URL_REUSE_SECONDS]
        for old in expired:
            del _connect_urls[old]
        if (cached := _connect_urls.get(key)) is not None:
            return cached[1]
    url = oauth_connection_required(context, reason=reason).connect_url
    with _connect_urls_lock:
        _connect_urls[key] = (now, url)
    return url
