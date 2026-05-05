"""Pure credential service classification and visibility policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_WorkerScope = Literal["shared", "user", "user_agent"]

OAUTH_CREDENTIAL_FIELDS = frozenset(
    {
        "_id_token",
        "_oauth_claims",
        "_oauth_claims_verified",
        "_oauth_provider",
        "_source",
        "access_token",
        "client_id",
        "client_secret",
        "expires_at",
        "expires_in",
        "id_token",
        "refresh_token",
        "scope",
        "scopes",
        "token",
        "token_type",
        "token_uri",
    },
)

_OAUTH_CLIENT_CONFIG_SERVICE_SUFFIX = "_oauth_client"

_LOCAL_ONLY_SHARED_CREDENTIAL_SERVICES = frozenset(
    {
        "google_calendar",
        "google_calendar_oauth",
        "google_drive",
        "google_drive_oauth",
        "google_gmail",
        "google_gmail_oauth",
        "google_sheets",
        "google_sheets_oauth",
        "gmail",
        "homeassistant",
    },
)

_UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS = frozenset(
    {
        "google_vertex_adc",
        "google_calendar_oauth",
        "google_drive_oauth",
        "google_gmail_oauth",
        "google_sheets_oauth",
    },
)


@dataclass(frozen=True, slots=True)
class _CredentialServicePolicy:
    """Credential placement decisions for one service in one worker scope."""

    service: str
    worker_scope: _WorkerScope | None
    is_local_only_shared_service: bool
    uses_local_shared_credentials: bool
    uses_primary_runtime_global_credentials: bool
    uses_primary_runtime_scoped_credentials: bool
    worker_grantable_supported: bool


def credential_service_policy(service: str, worker_scope: _WorkerScope | None) -> _CredentialServicePolicy:
    """Return credential placement policy for one service in one worker scope."""
    is_local_only = service in _LOCAL_ONLY_SHARED_CREDENTIAL_SERVICES
    is_primary_runtime_global = is_oauth_client_config_service(service)
    return _CredentialServicePolicy(
        service=service,
        worker_scope=worker_scope,
        is_local_only_shared_service=is_local_only,
        uses_local_shared_credentials=worker_scope == "shared" and is_local_only,
        uses_primary_runtime_global_credentials=is_primary_runtime_global,
        uses_primary_runtime_scoped_credentials=(
            worker_scope in {"user", "user_agent"} and is_local_only and not is_primary_runtime_global
        ),
        worker_grantable_supported=not is_primary_runtime_global
        and service not in _UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS,
    )


def is_oauth_client_config_service(service: str) -> bool:
    """Return whether a service name follows the OAuth client config naming contract."""
    return service.endswith(_OAUTH_CLIENT_CONFIG_SERVICE_SUFFIX)


def dashboard_may_edit_oauth_service(*, token_service: bool, tool_config_service: bool) -> bool:
    """Return whether dashboard credential routes may edit one OAuth service role."""
    return tool_config_service and not token_service


def looks_like_oauth_credentials(credentials: dict[str, object]) -> bool:
    """Return whether a credential document appears to contain OAuth token state."""
    return (
        credentials.get("_source") == "oauth"
        or isinstance(credentials.get("_oauth_provider"), str)
        or isinstance(credentials.get("_id_token"), str)
        or isinstance(credentials.get("_oauth_claims"), dict)
    )


def filter_oauth_credential_fields(credentials: dict[str, object]) -> dict[str, object]:
    """Return credentials with OAuth token material and internal fields removed."""
    return {
        key: value
        for key, value in credentials.items()
        if key not in OAUTH_CREDENTIAL_FIELDS and not key.startswith("_")
    }
