"""Microsoft 365 OAuth provider: Microsoft identity platform v2 with requester-scoped Graph access."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.providers import (
    OAuthProvider,
    OAuthProviderError,
    OAuthRuntimeEndpoints,
    default_oauth_token_parser,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthClientConfig, OAuthTokenResult

MICROSOFT_365_PROVIDER_ID = "microsoft_365"
MICROSOFT_365_CLIENT_CONFIG_SERVICE = "microsoft_365_oauth_client"
_AUTHORITY = "https://login.microsoftonline.com"
# Workbook sessions and several workbook features support only work or school accounts.
_DEFAULT_TENANT = "organizations"
# Files.ReadWrite reaches only the user's own files; the .All scope also reaches files
# shared with the user and SharePoint document libraries the user can open.
_SCOPES = ("offline_access", "Files.ReadWrite.All")
_GRAPH_SCOPE_PREFIX = "https://graph.microsoft.com/"
_TENANT_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"|organizations|common",
)


def normalize_tenant_id(value: str) -> str:
    """Return a tenant GUID, verified domain, ``organizations``, or ``common``, lowercased."""
    tenant = value.strip().lower()
    if not _TENANT_PATTERN.fullmatch(tenant):
        msg = (
            "Microsoft 365 tenant_id must be a tenant GUID, a verified domain such as contoso.onmicrosoft.com, "
            "'organizations', or 'common'."
        )
        raise OAuthProviderError(msg)
    return tenant


def _endpoints(tenant: str) -> OAuthRuntimeEndpoints:
    return OAuthRuntimeEndpoints(
        authorization_url=f"{_AUTHORITY}/{tenant}/oauth2/v2.0/authorize",
        token_url=f"{_AUTHORITY}/{tenant}/oauth2/v2.0/token",
    )


def _configured_tenant(runtime_paths: RuntimePaths) -> str:
    """Read the optional tenant from the stored app registration, defaulting to work or school accounts."""
    credentials = get_runtime_credentials_manager(runtime_paths).load_credentials(MICROSOFT_365_CLIENT_CONFIG_SERVICE)
    tenant = (credentials or {}).get("tenant_id")
    if tenant is None or (isinstance(tenant, str) and not tenant.strip()):
        return _DEFAULT_TENANT
    if not isinstance(tenant, str):
        msg = "Microsoft 365 tenant_id must be a string."
        raise OAuthProviderError(msg)
    return normalize_tenant_id(tenant)


async def _tenant_endpoints(_provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
    """Resolve the configured tenant's endpoints; this bootstrapper registers no client."""
    return _endpoints(await asyncio.to_thread(_configured_tenant, runtime_paths))


def _canonical_scope(scope: str) -> str:
    """Map Graph's ``https://graph.microsoft.com/Files.ReadWrite.All`` or any casing to the requested name."""
    bare = scope[len(_GRAPH_SCOPE_PREFIX) :] if scope.lower().startswith(_GRAPH_SCOPE_PREFIX) else scope
    return next((requested for requested in _SCOPES if requested.lower() == bare.lower()), scope)


def _microsoft_token_parser(
    provider: OAuthProvider,
    token_response: Mapping[str, Any],
    client_config: OAuthClientConfig,
    runtime_paths: RuntimePaths,
) -> OAuthTokenResult:
    """Parse a token response after normalizing Graph's granted-scope spelling."""
    response = dict(token_response)
    scope = response.get("scope")
    if isinstance(scope, str):
        response["scope"] = " ".join(_canonical_scope(item) for item in scope.split())
    return default_oauth_token_parser(provider, response, client_config, runtime_paths)


def microsoft_365_oauth_provider() -> OAuthProvider:
    """Return the Microsoft 365 provider; tokens always belong to the requesting user."""
    default_endpoints = _endpoints(_DEFAULT_TENANT)
    return OAuthProvider(
        id=MICROSOFT_365_PROVIDER_ID,
        display_name="Microsoft 365",
        authorization_url=default_endpoints.authorization_url,
        token_url=default_endpoints.token_url,
        scopes=_SCOPES,
        credential_service=f"{MICROSOFT_365_PROVIDER_ID}_oauth",
        tool_config_service=MICROSOFT_365_PROVIDER_ID,
        client_config_services=(MICROSOFT_365_CLIENT_CONFIG_SERVICE,),
        requester_scoped_credentials=True,
        status_capabilities=("OneDrive and SharePoint Excel workbooks",),
        token_parser=_microsoft_token_parser,
        runtime_bootstrapper=_tenant_endpoints,
    )
