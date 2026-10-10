"""Egress service status and request-log responses shared by the personal and the admin egress APIs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from pydantic import BaseModel

from mindroom.api import config_lifecycle, oauth
from mindroom.egress_broker.secrets import EgressServiceStatus, OAuthStatus, service_status
from mindroom.egress_broker.user_services import effective_config
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.service import oauth_provider_service_account_configured

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from fastapi import Request

    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.egress_broker import EgressService
    from mindroom.config.main import Config
    from mindroom.credentials import CredentialsManager
    from mindroom.egress_broker.audit import AuditRecord
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


type EgressServiceSource = Literal["config", "user"]


def effective_services(
    config: Config,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
) -> dict[str, EgressService]:
    """Return the services one scope uses, by name: the config's first, then the scope's own.

    Both egress APIs look services up here, so what they list and accept is what the broker matches. A cache miss
    reads the credential store, so async callers run this in a thread.
    """
    return effective_config(config.egress_broker, manager, target).services


def service_source(config: Config, name: str) -> EgressServiceSource:
    """Say whether a service of an effective config is the administrator's (`config`) or the scope's own (`user`)."""
    return "config" if name in config.egress_broker.services else "user"


class AuditRecordResponse(BaseModel):
    """One audit log record for API responses."""

    at: str
    kind: str
    scope: str
    agent_name: str | None
    requester_id: str | None
    method: str
    host: str
    path: str
    service: str | None
    status: int
    bytes_up: int
    bytes_down: int
    duration_ms: int


class AuditLogsResponse(BaseModel):
    """Audit log query response."""

    records: list[AuditRecordResponse]


def audit_logs_response(records: Iterable[AuditRecord]) -> AuditLogsResponse:
    """Describe audit records, already in the order the log returned them, for an API response."""
    return AuditLogsResponse(
        records=[
            AuditRecordResponse(
                at=rec.at.isoformat(),
                kind=rec.kind,
                scope=rec.scope,
                agent_name=rec.agent_name,
                requester_id=rec.requester_id,
                method=rec.method,
                host=rec.host,
                path=rec.path,
                service=rec.service,
                status=rec.status,
                bytes_up=rec.bytes_up,
                bytes_down=rec.bytes_down,
                duration_ms=rec.duration_ms,
            )
            for rec in records
        ],
    )


class EgressOAuthStatus(BaseModel):
    """Connection state of the OAuth account a service can use instead of an API key.

    `connected` means the broker would inject a personal access token. `service_account` marks a provider that a
    shared service account serves instead: the broker cannot inject that, and personal accounts are not connectable.
    `unavailable_reason` is `shared_sandbox` where the broker never uses a requester's own account (GitHub,
    Atlassian) because several requesters share the sandbox; nothing is connected or connectable then.
    `shared_worker_opt_in` marks such a sandbox whose service allows it anyway (`oauth_on_shared_workers`), so every
    user of the agent can act with the connected account.
    """

    provider: str
    display_name: str
    connected: bool
    account_label: str | None
    can_connect: bool
    reset_required: bool
    service_account: bool
    unavailable_reason: Literal["shared_sandbox"] | None
    shared_worker_opt_in: bool


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


def service_oauth_provider(snapshot: ApiSnapshot, service: EgressService | None) -> OAuthProvider | None:
    """Return the registry's provider for an egress service, or None without a service or a known provider."""
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
    """Return the OAuth provider of a configured egress service for the admin connect and disconnect routes.

    A service the config does not define is a 404; see `oauth_provider_of_service` for the rest.
    """
    config = config_lifecycle.bind_current_request_snapshot(request).runtime_config
    service = config.egress_broker.services.get(service_name) if config is not None else None
    if service is None:
        raise HTTPException(404, "Service is not configured", headers=headers)
    return oauth_provider_of_service(request, service, connecting=connecting, headers=headers)


def oauth_provider_of_service(
    request: Request,
    service: EgressService,
    *,
    connecting: bool,
    headers: Mapping[str, str] | None = None,
) -> OAuthProvider:
    """Return the OAuth provider of an egress service for the connect and disconnect routes.

    A service without a provider, or whose provider the registry does not know, is a 404. Connecting is a 409 while
    a shared service account is configured for the provider, since personal accounts are not managed then.
    Disconnecting stays possible, so a personal connection stored earlier can still be revoked.
    """
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    provider = service_oauth_provider(snapshot, service)
    if provider is None:
        raise HTTPException(404, "Service has no account connection", headers=headers)
    if connecting and oauth_provider_service_account_configured(provider, snapshot.runtime_paths):
        raise HTTPException(409, "Personal account linking is unavailable for this service", headers=headers)
    return provider
