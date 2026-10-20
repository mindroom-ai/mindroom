"""Built-in Microsoft 365 OAuth providers for delegated Microsoft Graph access."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.oauth.providers import OAuthProvider, OAuthProviderError, OAuthRuntimeEndpoints

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths

__all__ = [
    "ACCOUNT_LOOKUP_TIMEOUT_SECONDS",
    "GRAPH_ME_URL",
    "MICROSOFT_CLIENT_CONFIG_SERVICE",
    "microsoft_files_oauth_provider",
    "microsoft_oauth_provider",
    "microsoft_tenant",
    "microsoft_token_url",
]

MICROSOFT_CLIENT_CONFIG_SERVICE = "microsoft_oauth_client"
_TENANT_ENV = "MICROSOFT_TENANT_ID"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
ACCOUNT_LOOKUP_TIMEOUT_SECONDS = 5.0
_DEFAULT_TENANT = "organizations"
# Every provider labels the account through /me and keeps a refresh token.
_IDENTITY_SCOPES = ("User.Read", "offline_access")
# A tenant is a GUID, a verified domain, or organizations, common, or consumers; it becomes a URL path segment.
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


def _endpoint(tenant: str, kind: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{kind}"


def microsoft_tenant(runtime_paths: RuntimePaths) -> str:
    """Return the Microsoft Entra tenant that sign-in and token requests use."""
    tenant = (runtime_paths.env_value(_TENANT_ENV) or _DEFAULT_TENANT).strip()
    if not _TENANT.fullmatch(tenant):
        msg = f"{_TENANT_ENV} must be a tenant ID, a verified domain, organizations, common, or consumers."
        raise OAuthProviderError(msg)
    return tenant


def microsoft_token_url(runtime_paths: RuntimePaths) -> str:
    """Return the token endpoint of the configured tenant, which every stored connection must be bound to."""
    return _endpoint(microsoft_tenant(runtime_paths), "token")


async def _microsoft_runtime_endpoints(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
    tenant = microsoft_tenant(runtime_paths)
    return OAuthRuntimeEndpoints(
        authorization_url=_endpoint(tenant, "authorize"),
        token_url=microsoft_token_url(runtime_paths),
        token_endpoint_auth_method=provider.token_endpoint_auth_method,
    )


def _account_lookup_client() -> httpx.Client:
    """Build the client for the account lookup; it never follows a redirect, so the token goes to one URL only."""
    return httpx.Client(timeout=ACCOUNT_LOOKUP_TIMEOUT_SECONDS, follow_redirects=False)


def _microsoft_account_claims(token_data: Mapping[str, Any]) -> dict[str, Any]:
    """Read the account's email with the token a code exchange just returned, as `{"email": ...}`.

    Microsoft's token response names no account the provider verifies, so the connection would show as just
    "Connected". The provider runs this in a worker thread and is the error boundary: a Graph failure or an
    unusable profile raises here, the provider logs only the error type, and the account still connects unlabeled.
    """
    token = token_data.get("token")
    if not isinstance(token, str) or not token:
        return {}
    with _account_lookup_client() as client:
        response = client.get(
            GRAPH_ME_URL,
            params={"$select": "mail,userPrincipalName"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        profile = response.json()
    if isinstance(profile, dict):
        for field in ("mail", "userPrincipalName"):
            value = profile.get(field)
            if isinstance(value, str) and _EMAIL.fullmatch(value.strip()):
                return {"email": value.strip()}
    msg = "Microsoft Graph returned no usable account email"
    raise ValueError(msg)


def microsoft_oauth_provider(
    *,
    provider_id: str,
    display_name: str,
    scopes: tuple[str, ...],
    status_capabilities: tuple[str, ...] = (),
) -> OAuthProvider:
    """Return one requester-scoped Microsoft Graph provider that shares the `microsoft_oauth_client` app."""
    return OAuthProvider(
        id=provider_id,
        display_name=display_name,
        # The runtime resolves the configured tenant; these defaults name the organizations endpoints.
        authorization_url=_endpoint(_DEFAULT_TENANT, "authorize"),
        token_url=_endpoint(_DEFAULT_TENANT, "token"),
        scopes=(*scopes, *_IDENTITY_SCOPES),
        credential_service=f"{provider_id}_oauth",
        tool_config_service=provider_id,
        client_config_services=(f"{provider_id}_oauth_client",),
        shared_client_config_services=(MICROSOFT_CLIENT_CONFIG_SERVICE,),
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
        pkce_code_challenge_method="S256",
        requester_scoped_credentials=True,
        status_capabilities=status_capabilities,
        runtime_bootstrapper=_microsoft_runtime_endpoints,
        account_claims_reader=_microsoft_account_claims,
    )


def microsoft_files_oauth_provider() -> OAuthProvider:
    """Return the OneDrive and SharePoint files provider."""
    return microsoft_oauth_provider(
        provider_id="microsoft_files",
        display_name="Microsoft OneDrive and SharePoint",
        scopes=("Files.ReadWrite.All",),
        status_capabilities=("search, read, and save OneDrive and SharePoint files",),
    )
