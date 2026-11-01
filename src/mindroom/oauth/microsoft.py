"""Built-in Microsoft 365 OAuth providers for delegated Microsoft Graph access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.oauth.providers import OAuthProvider, OAuthProviderError, OAuthRuntimeEndpoints

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths

__all__ = [
    "ACCOUNT_LOOKUP_TIMEOUT_SECONDS",
    "GRAPH_ME_URL",
    "microsoft_oauth_provider",
    "microsoft_oauth_providers",
    "microsoft_token_url",
]

_CLIENT_CONFIG_SERVICE = "microsoft_oauth_client"
_TENANT_ENV = "MICROSOFT_TENANT_ID"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
ACCOUNT_LOOKUP_TIMEOUT_SECONDS = 5.0
_DEFAULT_TENANT = "organizations"
# Every provider labels the account through /me and keeps a refresh token.
_IDENTITY_SCOPES = ("User.Read", "offline_access")
# A tenant is a GUID, a verified domain, or organizations, common, or consumers; it becomes a URL path segment.
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


@dataclass(frozen=True, slots=True)
class _MicrosoftProviderSpec:
    """What sets one Microsoft Graph provider apart; every other field is shared or named after the provider id."""

    display_name: str
    # Requested before `_IDENTITY_SCOPES`.
    service_scopes: tuple[str, ...]
    status_capabilities: tuple[str, ...]


_MICROSOFT_PROVIDERS = {
    "microsoft_files": _MicrosoftProviderSpec(
        display_name="Microsoft OneDrive and SharePoint",
        service_scopes=("Files.ReadWrite.All",),
        status_capabilities=("search, read, and save OneDrive and SharePoint files",),
    ),
    "microsoft_sharepoint": _MicrosoftProviderSpec(
        display_name="Microsoft SharePoint",
        service_scopes=("Sites.ReadWrite.All",),
        status_capabilities=("read SharePoint sites, lists, and pages, and add and change list items",),
    ),
    "outlook_mail": _MicrosoftProviderSpec(
        display_name="Outlook Mail",
        service_scopes=(
            "Mail.ReadWrite",
            "Mail.Send",
            "Mail.ReadWrite.Shared",
            "Mail.Send.Shared",
            "MailboxSettings.ReadWrite",
        ),
        status_capabilities=(
            "search, read, draft, send, and organize Outlook mail, including shared mailboxes, and set automatic replies",
        ),
    ),
    "outlook_calendar": _MicrosoftProviderSpec(
        display_name="Outlook Calendar",
        service_scopes=("Calendars.ReadWrite", "Calendars.ReadWrite.Shared"),
        status_capabilities=("read and change Outlook calendar events",),
    ),
    "microsoft_todo": _MicrosoftProviderSpec(
        display_name="Microsoft To Do",
        service_scopes=("Tasks.ReadWrite",),
        status_capabilities=("list, create, update, and delete Microsoft To Do tasks",),
    ),
    # `ChannelMessage.Read.All` and `User.Read.All`, which finds a person's user id for a presence read, need admin
    # consent.
    "microsoft_teams": _MicrosoftProviderSpec(
        display_name="Microsoft Teams",
        service_scopes=(
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "ChannelMessage.Read.All",
            "ChannelMessage.Send",
            "Chat.ReadWrite",
            "ChatMessage.Send",
            "Presence.Read.All",
            "User.Read.All",
        ),
        status_capabilities=("read and send Teams channel and chat messages, start chats, and read presence",),
    ),
    # `OnlineMeetingTranscript.Read.All` needs admin consent.
    "microsoft_meetings": _MicrosoftProviderSpec(
        display_name="Microsoft Teams Meetings",
        service_scopes=("OnlineMeetings.Read", "OnlineMeetingTranscript.Read.All"),
        status_capabilities=("find Teams meetings and read their transcripts",),
    ),
    "microsoft_onenote": _MicrosoftProviderSpec(
        display_name="Microsoft OneNote",
        service_scopes=("Notes.ReadWrite.All",),
        status_capabilities=("read, create, and append to OneNote pages",),
    ),
    "microsoft_people": _MicrosoftProviderSpec(
        display_name="Microsoft 365 People",
        service_scopes=("User.Read.All",),
        status_capabilities=("look up people, their managers, and their direct reports in the directory",),
    ),
}


def _endpoint(tenant: str, kind: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{kind}"


def _microsoft_tenant(runtime_paths: RuntimePaths) -> str:
    """Return the Microsoft Entra tenant that sign-in and token requests use."""
    tenant = runtime_paths.env_value(_TENANT_ENV, default=_DEFAULT_TENANT).strip()
    if not _TENANT.fullmatch(tenant):
        msg = f"{_TENANT_ENV} must be a tenant ID, a verified domain, organizations, common, or consumers."
        raise OAuthProviderError(msg)
    return tenant


def microsoft_token_url(runtime_paths: RuntimePaths) -> str:
    """Return the token endpoint of the configured tenant, which every stored connection must be bound to."""
    return _endpoint(_microsoft_tenant(runtime_paths), "token")


async def _microsoft_runtime_endpoints(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
    tenant = _microsoft_tenant(runtime_paths)
    return OAuthRuntimeEndpoints(
        authorization_url=_endpoint(tenant, "authorize"),
        token_url=_endpoint(tenant, "token"),
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


def microsoft_oauth_provider(provider_id: str) -> OAuthProvider:
    """Return the requester-scoped Microsoft Graph provider `provider_id`, sharing the `microsoft_oauth_client` app."""
    spec = _MICROSOFT_PROVIDERS[provider_id]
    return OAuthProvider(
        id=provider_id,
        display_name=spec.display_name,
        # The runtime resolves the configured tenant; these defaults name the organizations endpoints.
        authorization_url=_endpoint(_DEFAULT_TENANT, "authorize"),
        token_url=_endpoint(_DEFAULT_TENANT, "token"),
        scopes=(*spec.service_scopes, *_IDENTITY_SCOPES),
        credential_service=f"{provider_id}_oauth",
        tool_config_service=provider_id,
        client_config_services=(f"{provider_id}_oauth_client",),
        shared_client_config_services=(_CLIENT_CONFIG_SERVICE,),
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
        pkce_code_challenge_method="S256",
        requester_scoped_credentials=True,
        status_capabilities=spec.status_capabilities,
        runtime_bootstrapper=_microsoft_runtime_endpoints,
        account_claims_reader=_microsoft_account_claims,
    )


def microsoft_oauth_providers() -> tuple[OAuthProvider, ...]:
    """Return every built-in Microsoft Graph provider."""
    return tuple(microsoft_oauth_provider(provider_id) for provider_id in _MICROSOFT_PROVIDERS)
