"""Pure credential service classification and visibility policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WorkerScope = Literal["shared", "user", "user_agent"]

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

LOCAL_ONLY_SHARED_CREDENTIAL_SERVICES = frozenset(
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

UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS = frozenset(
    {
        "google_vertex_adc",
        "google_oauth_client",
        "google_calendar_oauth",
        "google_drive_oauth",
        "google_gmail_oauth",
        "google_sheets_oauth",
    },
)


@dataclass(frozen=True, slots=True)
class CredentialServicePolicy:
    """Credential placement decisions for one service in one worker scope."""

    service: str
    worker_scope: WorkerScope | None
    is_local_only_shared_service: bool
    uses_local_shared_credentials: bool
    uses_primary_runtime_scoped_credentials: bool
    worker_grantable_supported: bool


def credential_service_policy(service: str, worker_scope: WorkerScope | None) -> CredentialServicePolicy:
    """Return credential placement policy for one service in one worker scope."""
    is_local_only = service in LOCAL_ONLY_SHARED_CREDENTIAL_SERVICES
    return CredentialServicePolicy(
        service=service,
        worker_scope=worker_scope,
        is_local_only_shared_service=is_local_only,
        uses_local_shared_credentials=worker_scope == "shared" and is_local_only,
        uses_primary_runtime_scoped_credentials=worker_scope in {"user", "user_agent"} and is_local_only,
        worker_grantable_supported=service not in UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS,
    )


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
