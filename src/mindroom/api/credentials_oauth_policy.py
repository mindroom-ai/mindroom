"""Dashboard OAuth credential service policy.

Classifies credentials API service names against registered OAuth providers and
enforces which OAuth credential material the dashboard may read, write, or copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from mindroom.api import config_lifecycle
from mindroom.credential_policy import (
    OAUTH_CREDENTIAL_FIELDS,
    dashboard_may_edit_oauth_service,
    filter_oauth_credential_fields,
    is_oauth_client_config_service,
    looks_like_oauth_credentials,
)
from mindroom.oauth.registry import load_oauth_providers_for_snapshot

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_OAUTH_TOKEN_CREDENTIALS_ERROR = "OAuth token credentials must be managed through the OAuth connect flow."  # noqa: S105
_OAUTH_CLIENT_CONFIG_FIELDS = frozenset({"client_id", "client_secret", "redirect_uri"})
OAUTH_CLIENT_CONFIG_RESPONSE_FIELDS = _OAUTH_CLIENT_CONFIG_FIELDS - {"client_secret"}
_PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD = "none"  # noqa: S105


@dataclass(frozen=True)
class OAuthCredentialServiceMatch:
    """OAuth provider service role for one credentials API service name."""

    provider: OAuthProvider
    token_service: bool
    tool_config_service: bool
    client_config_service: bool


@dataclass(frozen=True)
class OAuthCredentialServices:
    """Classify dashboard credential services registered by OAuth providers."""

    providers: dict[str, OAuthProvider]

    def match(self, service: str) -> OAuthCredentialServiceMatch | None:
        """Return the OAuth role for one credential service, if registered."""
        for provider in self.providers.values():
            token_service = provider.credential_service == service
            tool_config_service = provider.tool_config_service == service
            client_config_service = service in provider.all_client_config_services
            if token_service or tool_config_service or client_config_service:
                return OAuthCredentialServiceMatch(
                    provider=provider,
                    token_service=token_service,
                    tool_config_service=tool_config_service,
                    client_config_service=client_config_service,
                )
        return None

    def reject_non_editable_services(self, services: tuple[str, ...]) -> None:
        """Reject direct dashboard access to non-editable OAuth credential services."""
        for service in services:
            reject_oauth_token_service(self.match(service))

    def dashboard_may_show_service(self, service: str) -> bool:
        """Return whether a service may appear in dashboard credential listings."""
        match = self.match(service)
        return match is None or dashboard_may_edit_oauth_match(match)


def oauth_services_for_request(request: Request) -> OAuthCredentialServices:
    """Return the OAuth credential service classifier for one request snapshot."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    return OAuthCredentialServices(providers=load_oauth_providers_for_snapshot(snapshot))


def oauth_service_match(request: Request, service: str) -> OAuthCredentialServiceMatch | None:
    """Return the OAuth role for one credential service on one request."""
    return oauth_services_for_request(request).match(service)


def reject_oauth_token_service(
    oauth_service_match: OAuthCredentialServiceMatch | None,
) -> None:
    """Reject direct dashboard access to OAuth token credential services."""
    if oauth_service_match is None or dashboard_may_edit_oauth_match(oauth_service_match):
        return
    raise HTTPException(status_code=400, detail=_OAUTH_TOKEN_CREDENTIALS_ERROR)


def dashboard_may_edit_oauth_match(oauth_service_match: OAuthCredentialServiceMatch | None) -> bool:
    """Return whether the dashboard may edit one matched OAuth credential service."""
    if oauth_service_match is None:
        return False
    if oauth_service_match.client_config_service:
        return True
    return dashboard_may_edit_oauth_service(
        token_service=oauth_service_match.token_service,
        tool_config_service=oauth_service_match.tool_config_service,
    )


def _is_oauth_client_config_match(oauth_service_match: OAuthCredentialServiceMatch | None) -> bool:
    return oauth_service_match is not None and oauth_service_match.client_config_service


def is_client_config_service(
    service: str,
    oauth_service_match: OAuthCredentialServiceMatch | None,
) -> bool:
    """Return whether one service holds OAuth app client config."""
    return _is_oauth_client_config_match(oauth_service_match) or is_oauth_client_config_service(service)


def reject_oauth_credentials_document(credentials: dict[str, Any]) -> None:
    """Reject OAuth token documents on generic credential routes."""
    if not looks_like_oauth_credentials(credentials):
        return
    raise HTTPException(status_code=400, detail=_OAUTH_TOKEN_CREDENTIALS_ERROR)


def reject_oauth_api_key_read_field(
    service: str,
    oauth_service_match: OAuthCredentialServiceMatch | None,
    *,
    key_name: str,
) -> None:
    """Reject API-key route reads of protected OAuth credential fields."""
    if is_client_config_service(service, oauth_service_match):
        if key_name == "client_secret":
            raise HTTPException(
                status_code=400,
                detail="OAuth client secret must be managed through the credentials document route.",
            )
        if key_name not in OAUTH_CLIENT_CONFIG_RESPONSE_FIELDS:
            raise HTTPException(
                status_code=400,
                detail=f"OAuth client config field '{key_name}' is not readable through the API key route.",
            )
        return
    if not dashboard_may_edit_oauth_match(oauth_service_match):
        return
    if key_name not in OAUTH_CREDENTIAL_FIELDS:
        return
    raise HTTPException(
        status_code=400,
        detail=f"OAuth field '{key_name}' must be managed through the OAuth connect flow.",
    )


def reject_oauth_api_key_write_field(
    service: str,
    oauth_service_match: OAuthCredentialServiceMatch | None,
    *,
    key_name: str,
) -> None:
    """Reject API-key route writes of protected OAuth credential fields."""
    if is_client_config_service(service, oauth_service_match):
        raise HTTPException(
            status_code=400,
            detail="OAuth client config credentials must be managed through the credentials document route.",
        )
    reject_oauth_api_key_read_field(service, oauth_service_match, key_name=key_name)


def reject_oauth_client_config_copy(
    source_service: str,
    source_match: OAuthCredentialServiceMatch | None,
    destination_service: str,
    destination_match: OAuthCredentialServiceMatch | None,
) -> None:
    """Reject copying OAuth client config credentials between services."""
    if not is_client_config_service(
        source_service,
        source_match,
    ) and not is_client_config_service(destination_service, destination_match):
        return
    raise HTTPException(status_code=400, detail="OAuth client config credentials cannot be copied.")


def dashboard_credentials_for_save(
    config_values: dict[str, Any],
    *,
    strip_oauth_fields: bool,
) -> dict[str, Any]:
    """Return user-submitted credentials normalized for dashboard storage."""
    credentials = dict(config_values)
    if strip_oauth_fields:
        credentials = filter_oauth_credential_fields(credentials)
    credentials["_source"] = "ui"
    return credentials


def validate_oauth_client_config_fields(credentials: dict[str, Any]) -> None:
    """Validate submitted OAuth client config fields before storage."""
    _reject_non_client_config_fields(credentials)
    _reject_invalid_client_config_field_values(credentials)


def preserve_oauth_client_config_from_existing(
    credentials: dict[str, Any],
    existing_credentials: dict[str, Any],
    oauth_service_match: OAuthCredentialServiceMatch | None,
) -> None:
    """Require or preserve OAuth client config identity fields from stored credentials."""
    _require_or_preserve_oauth_client_config_field(credentials, existing_credentials, "client_id")
    if _oauth_client_config_secret_required(oauth_service_match):
        _require_or_preserve_oauth_client_config_secret(credentials, existing_credentials)
    else:
        _preserve_optional_oauth_client_config_secret(credentials, existing_credentials)


def _reject_non_client_config_fields(credentials: dict[str, Any]) -> None:
    invalid_fields = sorted(
        key for key in credentials if not key.startswith("_") and key not in _OAUTH_CLIENT_CONFIG_FIELDS
    )
    if invalid_fields:
        raise HTTPException(
            status_code=400,
            detail=f"OAuth client config does not support fields: {', '.join(invalid_fields)}.",
        )


def _reject_invalid_client_config_field_values(credentials: dict[str, Any]) -> None:
    redirect_uri = credentials.get("redirect_uri")
    if redirect_uri is not None and not isinstance(redirect_uri, str):
        raise HTTPException(status_code=400, detail="redirect_uri must be a string for OAuth client config.")


def _require_or_preserve_oauth_client_config_field(
    credentials: dict[str, Any],
    existing_credentials: dict[str, Any],
    field_name: str,
) -> None:
    submitted_value = credentials.get(field_name)
    if isinstance(submitted_value, str) and submitted_value.strip():
        return
    existing_value = existing_credentials.get(field_name)
    if isinstance(existing_value, str) and existing_value.strip():
        credentials[field_name] = existing_value
        return
    raise HTTPException(status_code=400, detail=f"{field_name} is required for OAuth client config.")


def _require_or_preserve_oauth_client_config_secret(
    credentials: dict[str, Any],
    existing_credentials: dict[str, Any],
) -> None:
    submitted_secret = credentials.get("client_secret")
    if isinstance(submitted_secret, str) and submitted_secret.strip():
        return
    submitted_client_id = credentials.get("client_id")
    existing_client_id = existing_credentials.get("client_id")
    client_id_changed = (
        isinstance(submitted_client_id, str)
        and isinstance(existing_client_id, str)
        and submitted_client_id.strip()
        and existing_client_id.strip()
        and submitted_client_id.strip() != existing_client_id.strip()
    )
    if client_id_changed:
        raise HTTPException(status_code=400, detail="client_secret is required when client_id changes.")
    _require_or_preserve_oauth_client_config_field(credentials, existing_credentials, "client_secret")


def _preserve_optional_oauth_client_config_secret(
    credentials: dict[str, Any],
    existing_credentials: dict[str, Any],
) -> None:
    submitted_secret = credentials.get("client_secret")
    if isinstance(submitted_secret, str) and submitted_secret.strip():
        credentials["client_secret"] = submitted_secret.strip()
        return
    credentials.pop("client_secret", None)
    submitted_client_id = credentials.get("client_id")
    existing_client_id = existing_credentials.get("client_id")
    if (
        isinstance(submitted_client_id, str)
        and isinstance(existing_client_id, str)
        and submitted_client_id.strip() == existing_client_id.strip()
    ):
        existing_secret = existing_credentials.get("client_secret")
        if isinstance(existing_secret, str) and existing_secret.strip():
            credentials["client_secret"] = existing_secret


def _oauth_client_config_secret_required(oauth_service_match: OAuthCredentialServiceMatch | None) -> bool:
    return (
        oauth_service_match is None
        or oauth_service_match.provider.token_endpoint_auth_method != _PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD
    )
