"""Security filtering of credential payloads for dashboard API responses."""

from __future__ import annotations

from typing import Any

from mindroom.api.credentials_oauth_policy import OAUTH_CLIENT_CONFIG_RESPONSE_FIELDS
from mindroom.credential_policy import filter_oauth_credential_fields, looks_like_oauth_credentials


def _filter_internal_keys(credentials: dict[str, Any]) -> dict[str, Any]:
    """Remove internal metadata keys (prefixed with _) from credentials."""
    return {k: v for k, v in credentials.items() if not k.startswith("_")}


def filter_credentials_for_response(credentials: dict[str, Any], *, is_oauth_service: bool) -> dict[str, Any]:
    """Return credentials safe for dashboard config responses."""
    filtered = _filter_internal_keys(credentials)
    if not is_oauth_service and not looks_like_oauth_credentials(credentials):
        return filtered
    return filter_oauth_credential_fields(credentials)


def filter_oauth_client_config_for_response(credentials: dict[str, Any]) -> dict[str, Any]:
    """Return OAuth app client config without client secret material."""
    filtered = _filter_internal_keys(credentials)
    return {key: value for key, value in filtered.items() if key in OAUTH_CLIENT_CONFIG_RESPONSE_FIELDS}
