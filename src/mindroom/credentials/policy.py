"""Pure credential service classification and visibility policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

_WorkerScope = Literal["shared", "user", "user_agent"]

RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY = "_oauth_client_runtime_bootstrap"
OAUTH_DYNAMIC_CLIENT_REGISTRATION_SOURCE = "oauth_dynamic_client_registration"
OAUTH_DYNAMIC_CLIENT_REGISTERED_REDIRECT_URI_KEY = "_oauth_dynamic_client_registered_redirect_uri"
OAUTH_DYNAMIC_CLIENT_REGISTERED_TOKEN_URL_KEY = "_oauth_dynamic_client_registered_token_url"  # noqa: S105

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
_OAUTH_TOKEN_SERVICE_SUFFIX = "_oauth"  # noqa: S105
# OAuth provider token services use the *_oauth naming contract. The separate
# *_oauth_client app-client config contract intentionally does not match it.

_EGRESS_BROKER_SERVICE_PREFIX = "egress_"

_LOCAL_ONLY_SHARED_CREDENTIAL_SERVICES = frozenset(
    {
        "google_calendar",
        "google_docs",
        "google_drive",
        "google_gmail",
        "google_sheets",
        "google_tasks",
        "gmail",
        "homeassistant",
    },
)
# A Desktop pairing belongs to one requester with one agent, whatever the agent's worker scope.
_REQUESTER_AGENT_CREDENTIAL_SERVICES = frozenset({"desktop"})

_UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS = frozenset(
    {
        "desktop",
        "google_vertex_adc",
        # The Slack app's bot token, signing secret and app token stay in the primary runtime.
        "slack_app",
    },
)


def is_oauth_token_service(service: str) -> bool:
    """Return whether a service name follows the OAuth token naming contract."""
    return service.endswith(_OAUTH_TOKEN_SERVICE_SUFFIX)


def is_egress_broker_service(service: str) -> bool:
    """Return whether a service name is an egress broker service."""
    return service.startswith(_EGRESS_BROKER_SERVICE_PREFIX)


def egress_credential_service(name: str) -> str:
    """Return the credential service name that stores the key of the egress service `name`."""
    return f"{_EGRESS_BROKER_SERVICE_PREFIX}{name}"


class CredentialPlacement(Enum):
    """The store that holds one service's credentials for one worker scope."""

    # The runtime's own store; every service when unscoped, and OAuth app client configs in every scope.
    GLOBAL = "global"
    # The runtime's shared store; local-only services on shared workers.
    SHARED = "shared"
    # The primary's store of one agent; OAuth tokens and primary-owned settings on shared workers.
    AGENT = "agent"
    # The primary's store of one requester, of one requester with one agent on `user_agent` workers.
    REQUESTER = "requester"
    # The primary's store of one requester with one agent, whatever the worker scope.
    REQUESTER_AGENT = "requester_agent"
    # The worker's own store, which worker code can write.
    WORKER = "worker"


@dataclass(frozen=True, slots=True)
class CredentialServicePolicy:
    """Where one service's credentials live in one worker scope, and where else they may go."""

    placement: CredentialPlacement
    # The placement's store is read over the shared layer: a worker's own store, and the settings the primary owns.
    reads_shared: bool
    # The service may be mirrored into workers through `worker_grantable_credentials`.
    grantable: bool
    # The service may be sent to a worker in a per-call lease.
    leasable: bool


def credential_service_policy(
    service: str,
    worker_scope: _WorkerScope | None,
    *,
    primary_built_tool: bool = False,
) -> CredentialServicePolicy:
    """Return where one service's credentials live in one worker scope.

    ``primary_built_tool`` marks the settings of a tool the primary process builds for this agent.
    Worker code can write the worker store, so those settings never come from it.
    """
    # Egress broker services are primary-only like primary-built tool settings, and never reach a worker.
    # The prefix wins over the OAuth suffixes, so `egress_x_oauth_client` is never an OAuth client config.
    egress_service = is_egress_broker_service(service)
    oauth_token_service = not egress_service and is_oauth_token_service(service)
    oauth_client_service = is_global_credential_service(service)
    local_only_service = service in _LOCAL_ONLY_SHARED_CREDENTIAL_SERVICES or oauth_token_service
    primary_owned = (primary_built_tool or egress_service) and not local_only_service and not oauth_client_service
    if service in _REQUESTER_AGENT_CREDENTIAL_SERVICES:
        placement = CredentialPlacement.REQUESTER_AGENT
    elif worker_scope is None or oauth_client_service:
        placement = CredentialPlacement.GLOBAL
    elif worker_scope == "shared":
        # Scoped OAuth tokens and primary-owned settings stay bound to their agent even on a shared worker.
        if oauth_token_service or primary_owned:
            placement = CredentialPlacement.AGENT
        elif local_only_service:
            placement = CredentialPlacement.SHARED
        else:
            placement = CredentialPlacement.WORKER
    elif local_only_service or primary_owned:
        placement = CredentialPlacement.REQUESTER
    else:
        placement = CredentialPlacement.WORKER
    return CredentialServicePolicy(
        placement=placement,
        reads_shared=placement is CredentialPlacement.WORKER
        or (primary_owned and placement in {CredentialPlacement.AGENT, CredentialPlacement.REQUESTER}),
        grantable=not (
            oauth_client_service
            or oauth_token_service
            or egress_service
            or service in _UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS
        ),
        leasable=not egress_service,
    )


def is_global_credential_service(service: str) -> bool:
    """Return whether a service's credentials live in the global store whatever the worker scope: OAuth app configs."""
    return not is_egress_broker_service(service) and is_oauth_client_config_service(service)


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
