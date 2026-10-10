"""Egress service status shared by the personal and the admin egress APIs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from pydantic import BaseModel

from mindroom.api import config_lifecycle, oauth
from mindroom.egress_broker.secrets import EgressServiceStatus, OAuthStatus, service_status
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fastapi import Request

    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.egress_broker import EgressService
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class EgressOAuthStatus(BaseModel):
    """Connection state of the OAuth account a service can use instead of an API key.

    `connected` means the broker would inject a personal access token. `service_account` marks a provider that a
    shared service account serves instead: the broker cannot inject that, and personal accounts are not connectable.
    """

    provider: str
    display_name: str
    connected: bool
    account_label: str | None
    can_connect: bool
    reset_required: bool
    service_account: bool


class EgressSourceStatus(BaseModel):
    """Which secret sources a service has in one scope.

    `configured` is true when either source is available and `updated_at` is the API key's timestamp, both kept for
    older clients. `active_source` says which source the broker uses: an explicit key wins over OAuth.
    """

    configured: bool
    updated_at: str | None
    active_source: Literal["key", "oauth"] | None
    key_configured: bool
    key_updated_at: str | None
    oauth: EgressOAuthStatus | None

    @classmethod
    def from_status(cls, status: EgressServiceStatus) -> EgressSourceStatus:
        """Describe the sources of one service for an API response."""
        return cls(
            configured=status.configured,
            updated_at=status.key_updated_at,
            active_source=status.active_source,
            key_configured=status.key_configured,
            key_updated_at=status.key_updated_at,
            oauth=EgressOAuthStatus(**asdict(status.oauth)) if status.oauth is not None else None,
        )


async def egress_oauth_status(
    result: oauth.OAuthStatusResponse,
    *,
    can_manage: bool,
    stored_connection: Callable[[], OAuthStatus | None],
) -> OAuthStatus:
    """Return a service's OAuth status as the broker sees it.

    Without a service account this is what the Connections portal shows the same viewer. A service account is not
    a token the broker can inject, so with one `connected` only reflects a stored personal connection that the
    broker would still use (`stored_connection` reads it like the broker does), and nothing is connectable.
    """
    if not result.has_service_account_config:
        view: oauth.PersonalConnectionView = oauth.personal_connection_view(result, can_manage=can_manage)
        return OAuthStatus(
            provider=result.provider,
            display_name=result.display_name,
            connected=view.connected,
            account_label=view.account_label,
            can_connect=view.can_connect,
            reset_required=view.reset_required,
        )
    stored = await asyncio.to_thread(stored_connection)
    return OAuthStatus(
        provider=result.provider,
        display_name=result.display_name,
        connected=stored is not None and stored.connected,
        account_label=None,
        can_connect=False,
        reset_required=result.reset_required,
        service_account=True,
    )


def unavailable_egress_oauth_status(provider: OAuthProvider) -> OAuthStatus:
    """Return the status shown while a provider's connection state cannot be loaded, so one failure spares the page."""
    return OAuthStatus(
        provider=provider.id,
        display_name=provider.display_name,
        connected=False,
        account_label=None,
        can_connect=False,
        reset_required=False,
    )


async def egress_service_status(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
    service: EgressService,
    name: str,
    oauth_part: OAuthStatus | None,
) -> EgressSourceStatus:
    """Combine a service's key status with its already loaded OAuth status; an explicit key wins.

    The key status reads and decrypts a credential file, so it runs in a thread.
    """
    status = await asyncio.to_thread(
        service_status,
        manager,
        target,
        service,
        name,
        oauth_status=lambda _provider_id, _target: oauth_part,
    )
    return EgressSourceStatus.from_status(status)


def service_oauth_provider(snapshot: ApiSnapshot, service_name: str) -> OAuthProvider | None:
    """Return the registry's provider for a configured egress service, or None without a known one."""
    config = snapshot.runtime_config
    service = config.egress_broker.services.get(service_name) if config is not None else None
    if service is None or service.oauth_provider is None:
        return None
    return load_oauth_providers_for_snapshot(snapshot).get(service.oauth_provider)


def egress_oauth_provider(
    request: Request,
    service_name: str,
    *,
    connecting: bool,
    headers: Mapping[str, str] | None = None,
) -> OAuthProvider:
    """Return the OAuth provider of a configured egress service for the connect and disconnect routes.

    A service without a provider, or whose provider the registry does not know, is a 404. Connecting is a 409 while
    a shared service account is configured for the provider, since personal accounts are not managed then.
    Disconnecting stays possible, so a personal connection stored earlier can still be revoked.
    """
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None or service_name not in config.egress_broker.services:
        raise HTTPException(404, "Service is not configured", headers=headers)
    provider = service_oauth_provider(snapshot, service_name)
    if provider is None:
        raise HTTPException(404, "Service has no account connection", headers=headers)
    if connecting and oauth_provider_service_account_configured(provider, snapshot.runtime_paths):
        raise HTTPException(409, "Personal account linking is unavailable for this service", headers=headers)
    return provider
