"""Microsoft 365 OAuth provider: Microsoft identity platform v2 with requester-scoped Graph access."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

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
MICROSOFT_365_TENANT_ENV = "MICROSOFT_365_TENANT_ID"
_AUTHORITY = "https://login.microsoftonline.com"
# The Excel REST API supports only work or school (business) storage, not consumer OneDrive.
# `organizations` needs a multi-tenant app registration; single-tenant apps set the tenant.
_DEFAULT_TENANT = "organizations"
# Files.ReadWrite reaches only the user's own files; the .All scope also reaches files
# shared with the user and SharePoint document libraries the user can open.
_SCOPES = ("offline_access", "Files.ReadWrite.All")
_GRAPH_SCOPE_PREFIX = "https://graph.microsoft.com/"
_TENANT_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"|organizations",
)


def normalize_tenant_id(value: str) -> str:
    """Return a tenant GUID, verified domain, or ``organizations``, lowercased."""
    tenant = value.strip().lower()
    if not _TENANT_PATTERN.fullmatch(tenant):
        msg = (
            f"{MICROSOFT_365_TENANT_ENV} must be a tenant GUID, a verified domain such as contoso.onmicrosoft.com, "
            "or 'organizations'."
        )
        raise OAuthProviderError(msg)
    return tenant


def _endpoints(tenant: str) -> OAuthRuntimeEndpoints:
    return OAuthRuntimeEndpoints(
        authorization_url=f"{_AUTHORITY}/{tenant}/oauth2/v2.0/authorize",
        token_url=f"{_AUTHORITY}/{tenant}/oauth2/v2.0/token",
    )


def _configured_tenant(runtime_paths: RuntimePaths) -> str:
    """Read the optional tenant from the runtime environment, defaulting to any work or school tenant."""
    tenant = (runtime_paths.env_value(MICROSOFT_365_TENANT_ENV) or "").strip()
    return normalize_tenant_id(tenant) if tenant else _DEFAULT_TENANT


async def _tenant_endpoints(_provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
    """Resolve the configured tenant's endpoints; this bootstrapper registers no client."""
    return _endpoints(_configured_tenant(runtime_paths))


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
