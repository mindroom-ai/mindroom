"""MindRoom's OAuth connections as an egress broker secret source; access tokens never leave the primary.

Tokens come only from `mindroom.oauth.credential_lifecycle`, which refreshes a token near expiry under the
same serialized transaction the tools sharing that connection use. Credential scope follows each provider's own
policy, so a requester-scoped provider such as GitHub uses the requester from the verified proxy token. Such a
requester's own account is used only on a worker that belongs to that requester; see `shared_worker_oauth`.
Logs carry the service, the provider id, and error types, never tokens or connect links.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

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

__all__ = [
    "Missing",
    "NeedsReconnect",
    "OAuthTokenResult",
    "SharedWorkerOAuth",
    "Token",
    "Unavailable",
    "oauth_status",
    "resolve_oauth_token",
    "shared_worker_oauth",
    "shared_worker_unavailable_status",
]

logger = get_logger(__name__)

_warned_unknown_providers: set[tuple[str, str]] = set()
_warned_unknown_providers_lock = threading.Lock()
# Each connect link stores a one-time token in the OAuth state file, so a worker retrying in a loop would grow it
# without bound. A link is reused for this long per provider, reason, and resolved OAuth target; the target carries
# the caller's identity, so reuse is per caller, finer than the credential scope. 60 s is well inside the token's
# 10-minute lifetime.
_CONNECT_URL_REUSE_SECONDS = 60.0
_connect_urls: dict[tuple[object, ...], tuple[float, str | None]] = {}
_connect_urls_lock = threading.Lock()
# After a refresh fails for a reason that may pass (a provider outage, a timeout, a network error), lookups for
# that credential scope answer at once for this long instead of running the grant again.
_TRANSIENT_FAILURE_BACKOFF_SECONDS = 30.0
# Worker scopes whose sandbox belongs to one requester.
_REQUESTER_WORKER_SCOPES = frozenset({"user", "user_agent"})


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
    """The scope's connection cannot supply a token until the user acts: the grant was revoked or is unreadable."""

    connect_url: str | None = field(default=None, repr=False)
    reset_required: bool = False


@dataclass(frozen=True)
class Unavailable:
    """Refreshing the scope's token failed for a reason that may pass, such as a provider outage; retry later."""


type OAuthTokenResult = Token | Missing | NeedsReconnect | Unavailable
type SharedWorkerOAuth = Literal["refused", "allowed"]


@dataclass
class _RefreshGate:
    """Serializes the broker's refreshes of one credential scope and remembers its last transient failure."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    failed_at: float | None = None

    def backing_off(self) -> bool:
        return self.failed_at is not None and time.monotonic() - self.failed_at < _TRANSIENT_FAILURE_BACKOFF_SECONDS


_refresh_gates: dict[tuple[object, ...], _RefreshGate] = {}
_refresh_gates_lock = threading.Lock()


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

    Only a rejected grant or an unreadable credential asks the user to act (`NeedsReconnect`); any other refresh
    failure is `Unavailable`, as the tools treat it, and the scope then answers `Unavailable` without another grant
    for `_TRANSIENT_FAILURE_BACKOFF_SECONDS`. `service` names the egress service in the one warning logged for a
    provider id the registry does not know. Blocks on the OAuth transaction owner, and on another lookup refreshing
    the same scope, so callers on an event loop run it in a thread.
    """
    provider = _registered_provider(service, provider_id, config, runtime_paths)
    if provider is None:
        return Missing()
    if shared_worker_oauth(provider, worker_target, opted_in=_oauth_on_shared_workers(config, service)) == "refused":
        # Connecting an account would not change that, so there is no link either.
        return Missing()
    context = _credential_context(provider, config, runtime_paths, credentials_manager, worker_target)
    if provider.requester_scoped_credentials and context.worker_target is None:
        # No requester to bind the connection to, so there is no stored token to use.
        return Missing(_connect_url(context))
    credentials = _refreshed_credentials(service, context)
    if isinstance(credentials, NeedsReconnect | Unavailable):
        return credentials
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

    A stored connection whose access token expired still counts as connected while it can be refreshed. A None
    `worker_target` is the global store that agents without a worker scope read. Binding the keyword arguments
    gives `secrets.service_status` its `oauth_status` reader.
    """
    provider = _registered_provider(service, provider_id, config, runtime_paths)
    if provider is None:
        return None
    access = shared_worker_oauth(provider, worker_target, opted_in=_oauth_on_shared_workers(config, service))
    if access == "refused":
        return shared_worker_unavailable_status(provider, runtime_paths)
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
        shared_worker_opt_in=access == "allowed",
    )


def shared_worker_oauth(
    provider: OAuthProvider,
    worker_target: ResolvedWorkerTarget | None,
    *,
    opted_in: bool,
) -> SharedWorkerOAuth | None:
    """Return whether the broker uses a requester's own account on a worker that several requesters share.

    A requester-scoped provider (GitHub, Atlassian) supplies the calling requester's own token. On a `shared`
    worker, or one without a worker scope (a None target is the global store such agents read), every requester's
    commands run in one sandbox, so another user's later command can read an earlier caller's proxy token, for
    example from a background process's environment, and act with that caller's account until the token expires.
    Such accounts are "refused" there unless the service sets `oauth_on_shared_workers`, which makes them
    "allowed". None means the rule does not apply: the provider follows the worker scope, or the worker belongs to
    one requester. Injection, placeholders, the status APIs, and the agent tool all decide through this.
    """
    if not provider.requester_scoped_credentials:
        return None
    if worker_target is not None and worker_target.worker_scope in _REQUESTER_WORKER_SCOPES:
        return None
    return "allowed" if opted_in else "refused"


def shared_worker_unavailable_status(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthStatus:
    """Return the status of a provider that `shared_worker_oauth` refuses: nothing to use, connect, or reset."""
    return OAuthStatus(
        provider=provider.id,
        display_name=provider.display_name,
        connected=False,
        account_label=None,
        can_connect=False,
        reset_required=False,
        service_account=oauth_provider_service_account_configured(provider, runtime_paths),
        unavailable_reason="shared_worker",
    )


def _oauth_on_shared_workers(config: Config, service: str) -> bool:
    egress_service = config.egress_broker.services.get(service)
    return egress_service is not None and egress_service.oauth_on_shared_workers


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


def _refreshed_credentials(
    service: str,
    context: OAuthCredentialContext,
) -> dict[str, Any] | NeedsReconnect | Unavailable | None:
    """Refresh the scope's credentials through its gate; a failed refresh becomes the result to return."""
    gate = _refresh_gate(context)
    with gate.lock:
        # Lookups queued behind a failed refresh answer here instead of each repeating the grant.
        if gate.backing_off():
            return Unavailable()
        try:
            credentials = refresh_oauth_credentials_blocking(context)
        except (OAuthCredentialUnreadableError, OAuthRefreshRejectedError) as exc:
            terminal = exc
        except OAuthProviderError as exc:
            gate.failed_at = time.monotonic()
            _log_token_unavailable(service, context.provider, exc)
            return Unavailable()
        else:
            gate.failed_at = None
            return credentials
    _log_token_unavailable(service, context.provider, terminal)
    if isinstance(terminal, OAuthCredentialUnreadableError):
        # Unreadable state must be reset from the dashboard before a new connection can be stored.
        return NeedsReconnect(reset_required=True)
    return NeedsReconnect(_connect_url(context, OAUTH_REFRESH_REJECTED_REASON))


def _log_token_unavailable(service: str, provider: OAuthProvider, exc: OAuthProviderError) -> None:
    logger.warning(
        "egress_broker_oauth_token_unavailable",
        service=service,
        provider_id=provider.id,
        error_type=type(exc).__name__,
    )


def _refresh_gate(context: OAuthCredentialContext) -> _RefreshGate:
    """Return the gate of the context's credential store: one per provider, worker scope, and worker key."""
    target = context.worker_target
    key = (
        context.runtime_paths.storage_root,
        context.provider.id,
        None if target is None else (target.worker_scope, target.worker_key),
    )
    with _refresh_gates_lock:
        return _refresh_gates.setdefault(key, _RefreshGate())


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
    """Return the caller's connect link, minting at most one per caller, provider, and reason every reuse period.

    There is none for a shared scope: its link skips the browser sign-in, and the broker hands links to worker code,
    where any process in the shared worker could read it.
    """
    shared = context.worker_target is not None and context.worker_target.worker_scope == "shared"
    if shared or not _connectable(context.provider, context.runtime_paths):
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
