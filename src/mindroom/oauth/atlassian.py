"""Atlassian Cloud OAuth 2.0 (3LO) provider, the scopes each Jira and Confluence function calls, and site pins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from mindroom.oauth.providers import OAuthProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

AtlassianProduct = Literal["jira", "confluence"]
ATLASSIAN_PRODUCTS: tuple[AtlassianProduct, ...] = ("jira", "confluence")
_ATLASSIAN_CLIENT_CONFIG_SERVICE = "atlassian_oauth_client"
_CLOUD_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class _AtlassianFunction:
    """One model-visible function and the OAuth scopes its API calls require."""

    name: str
    product: AtlassianProduct
    writes: bool
    scopes: tuple[str, ...]


# Classic scopes wherever an API accepts them.
# Confluence page and comment writes exist only in the v2 API, which accepts granular scopes only.
_ATLASSIAN_FUNCTIONS = (
    _AtlassianFunction("jira_search_issues", "jira", writes=False, scopes=("read:jira-work",)),
    _AtlassianFunction("jira_get_issue", "jira", writes=False, scopes=("read:jira-work",)),
    _AtlassianFunction("jira_create_issue", "jira", writes=True, scopes=("write:jira-work",)),
    _AtlassianFunction("jira_update_issue", "jira", writes=True, scopes=("write:jira-work",)),
    _AtlassianFunction("jira_add_comment", "jira", writes=True, scopes=("write:jira-work",)),
    _AtlassianFunction("jira_transition_issue", "jira", writes=True, scopes=("read:jira-work", "write:jira-work")),
    _AtlassianFunction("confluence_search", "confluence", writes=False, scopes=("search:confluence",)),
    _AtlassianFunction(
        "confluence_get_page",
        "confluence",
        writes=False,
        scopes=("search:confluence", "read:confluence-content.all"),
    ),
    _AtlassianFunction(
        "confluence_list_attachments",
        "confluence",
        writes=False,
        scopes=("search:confluence", "read:confluence-content.all"),
    ),
    _AtlassianFunction(
        "confluence_download_attachment",
        "confluence",
        writes=False,
        scopes=("readonly:content.attachment:confluence",),
    ),
    _AtlassianFunction(
        "confluence_create_page",
        "confluence",
        writes=True,
        scopes=("read:space:confluence", "write:page:confluence"),
    ),
    _AtlassianFunction("confluence_update_page", "confluence", writes=True, scopes=("write:page:confluence",)),
    _AtlassianFunction("confluence_add_comment", "confluence", writes=True, scopes=("write:comment:confluence",)),
)


def _atlassian_functions(
    products: Sequence[AtlassianProduct] = ATLASSIAN_PRODUCTS,
    *,
    write: bool = True,
) -> tuple[_AtlassianFunction, ...]:
    """Return the functions one connection exposes, in a stable order."""
    return tuple(
        function for function in _ATLASSIAN_FUNCTIONS if function.product in products and (write or not function.writes)
    )


def atlassian_function_names(
    products: Sequence[AtlassianProduct] = ATLASSIAN_PRODUCTS,
    *,
    write: bool = True,
    prefix: str = "",
) -> tuple[str, ...]:
    """Return the model-visible function names one connection exposes."""
    return tuple(f"{prefix}{function.name}" for function in _atlassian_functions(products, write=write))


def _atlassian_oauth_scopes(products: Sequence[AtlassianProduct], *, write: bool) -> tuple[str, ...]:
    """Return exactly the scopes the enabled functions call, plus offline access for refresh tokens."""
    scopes = ["offline_access"]
    for function in _atlassian_functions(products, write=write):
        scopes.extend(scope for scope in function.scopes if scope not in scopes)
    return tuple(scopes)


def atlassian_product_scopes(product: AtlassianProduct) -> frozenset[str]:
    """Return every scope that marks a site as reachable for one product."""
    return frozenset(
        scope for function in _ATLASSIAN_FUNCTIONS if function.product == product for scope in function.scopes
    )


def normalize_site_url(value: str) -> str:
    """Return the HTTPS origin of an Atlassian site URL such as https://example.atlassian.net/wiki."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        msg = "site_url must be an https:// URL"
        raise ValueError(msg) from None
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        msg = "site_url must be an https:// URL without credentials, query, or fragment"
        raise ValueError(msg)
    host = parts.hostname.lower()
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def normalize_cloud_id(value: str) -> str:
    """Return a canonical Atlassian cloud ID, which is a UUID."""
    cloud_id = value.strip().lower()
    if not _CLOUD_ID_PATTERN.fullmatch(cloud_id):
        msg = "cloud_id must be an Atlassian cloud ID (a UUID)"
        raise ValueError(msg)
    return cloud_id


def atlassian_oauth_provider(
    *,
    provider_id: str = "atlassian",
    display_name: str = "Atlassian",
    products: Sequence[AtlassianProduct] = ATLASSIAN_PRODUCTS,
    write: bool = True,
    client_config_service: str = _ATLASSIAN_CLIENT_CONFIG_SERVICE,
) -> OAuthProvider:
    """Return one Atlassian connection's provider; tokens always belong to the requesting user."""
    return OAuthProvider(
        id=provider_id,
        display_name=display_name,
        authorization_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",  # noqa: S106
        scopes=_atlassian_oauth_scopes(products, write=write),
        credential_service=f"{provider_id}_oauth",
        tool_config_service=provider_id,
        # Shared services derive each provider's own callback instead of reusing a stored redirect URI.
        shared_client_config_services=(client_config_service,),
        extra_auth_params={"audience": "api.atlassian.com", "prompt": "consent"},
        requester_scoped_credentials=True,
        status_capabilities=tuple(
            {"jira": "Jira issues", "confluence": "Confluence pages and attachments"}[product]
            for product in ATLASSIAN_PRODUCTS
            if product in products
        ),
    )
