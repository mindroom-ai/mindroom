"""Shared named credential connection resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.config.connections import ConnectionAuthKind, ConnectionConfig
from mindroom.credentials import get_runtime_shared_credentials_manager

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

ConnectionPurpose = Literal["chat_model", "embedder", "memory_llm", "voice_stt", "google_oauth_client"]


@dataclass(frozen=True)
class ResolvedConnection:
    """Resolved named connection for one runtime consumer."""

    connection_id: str
    provider: str
    auth_kind: ConnectionAuthKind
    service: str | None
    credentials: dict[str, Any] | None


_DEFAULT_API_KEY_CONNECTION_PROVIDERS = frozenset(
    {"openai", "anthropic", "google", "openrouter", "deepseek", "cerebras", "groq"},
)


def connection_api_key(connection: ResolvedConnection) -> str | None:
    """Return the API key payload when present."""
    if connection.credentials is None:
        return None
    api_key = connection.credentials.get("api_key")
    if not isinstance(api_key, str):
        return None
    normalized = api_key.strip()
    return normalized or None


def connection_google_application_credentials_path(connection: ResolvedConnection) -> str | None:
    """Return the ADC path payload when present."""
    if connection.credentials is None:
        return None
    credentials_path = connection.credentials.get("application_credentials_path")
    if not isinstance(credentials_path, str):
        return None
    normalized = credentials_path.strip()
    return normalized or None


def connection_oauth_client(connection: ResolvedConnection) -> tuple[str, str] | None:
    """Return the OAuth client id/secret pair when present."""
    if connection.credentials is None:
        return None
    client_id = connection.credentials.get("client_id")
    client_secret = connection.credentials.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        return None
    normalized_client_id = client_id.strip()
    normalized_client_secret = client_secret.strip()
    if not normalized_client_id or not normalized_client_secret:
        return None
    return normalized_client_id, normalized_client_secret


def canonical_connection_provider(provider: str) -> str:
    """Return the canonical provider key used for connection routing."""
    normalized = provider.strip().lower().replace("-", "_")
    return "google" if normalized == "gemini" else normalized


def default_connection_ids(
    *,
    provider: str,
    purpose: ConnectionPurpose,
) -> tuple[str, ...]:
    """Return ordered default connection ids for one provider/purpose pair."""
    canonical_provider = canonical_connection_provider(provider)
    if purpose == "google_oauth_client":
        connection_ids = ("google/oauth",)
    elif canonical_provider == "vertexai_claude":
        connection_ids = ("vertexai_claude/default",)
    elif purpose == "voice_stt" and canonical_provider in _DEFAULT_API_KEY_CONNECTION_PROVIDERS:
        connection_ids = (f"{canonical_provider}/stt", f"{canonical_provider}/default")
    elif purpose == "embedder" and canonical_provider == "openai":
        connection_ids = ("openai/embeddings",)
    elif canonical_provider in _DEFAULT_API_KEY_CONNECTION_PROVIDERS:
        connection_ids = (f"{canonical_provider}/default",)
    else:
        connection_ids = ()
    return connection_ids


def default_connection_id(
    *,
    provider: str,
    purpose: ConnectionPurpose,
) -> str | None:
    """Return the primary stable default connection id for one provider/purpose pair."""
    connection_ids = default_connection_ids(provider=provider, purpose=purpose)
    return connection_ids[0] if connection_ids else None


def default_connection_config(
    *,
    provider: str,
    purpose: ConnectionPurpose,
) -> ConnectionConfig | None:
    """Return the conventional default connection shape for one provider/purpose pair."""
    connection_id = default_connection_id(provider=provider, purpose=purpose)
    if connection_id is None:
        return None

    canonical_provider = canonical_connection_provider(provider)
    auth_kind = required_connection_auth_kind(provider=provider, purpose=purpose)
    if auth_kind is None:
        return None
    if auth_kind == "oauth_client":
        service = "google_oauth_client"
    elif auth_kind == "google_adc":
        service = "google_vertex_adc"
    elif auth_kind == "api_key":
        service = "google_gemini" if canonical_provider == "google" else canonical_provider
    else:
        service = None

    return ConnectionConfig(
        provider=canonical_provider,
        service=service,
        auth_kind=auth_kind,
    )


def allowed_connection_auth_kinds(
    *,
    provider: str,
    purpose: ConnectionPurpose,
) -> tuple[ConnectionAuthKind, ...]:
    """Return the auth kinds allowed for one provider/purpose pair."""
    expected_auth_kind = required_connection_auth_kind(provider=provider, purpose=purpose)
    if expected_auth_kind is None:
        return ()
    if expected_auth_kind == "api_key" and canonical_connection_provider(provider) == "openai":
        return ("api_key", "none")
    return (expected_auth_kind,)


def required_connection_auth_kind(
    *,
    provider: str,
    purpose: ConnectionPurpose,
) -> ConnectionAuthKind | None:
    """Return the required auth kind for one provider/purpose pair when constrained."""
    canonical_provider = canonical_connection_provider(provider)
    if purpose == "google_oauth_client":
        return "oauth_client"
    if canonical_provider == "vertexai_claude":
        return "google_adc"
    if canonical_provider == "ollama":
        return "none"
    if default_connection_id(provider=provider, purpose=purpose) is not None:
        return "api_key"
    return None


def _validate_connection_credentials(connection: ResolvedConnection) -> None:
    """Reject missing or malformed credential payloads for authenticated connections."""
    if connection.auth_kind in {"none", "google_adc"}:
        return

    if not isinstance(connection.credentials, dict):
        msg = f"Connection '{connection.connection_id}' is missing credentials"
        raise ValueError(msg)  # noqa: TRY004

    if connection.auth_kind == "api_key" and connection_api_key(connection) is None:
        msg = f"Connection '{connection.connection_id}' is missing api_key"
        raise ValueError(msg)
    if connection.auth_kind == "oauth_client" and connection_oauth_client(connection) is None:
        msg = f"Connection '{connection.connection_id}' is missing client_id/client_secret"
        raise ValueError(msg)


def _configured_default_connection_id(config: Config, connection_ids: tuple[str, ...]) -> str | None:
    """Return the first configured default connection id from one ordered candidate list."""
    for connection_id in connection_ids:
        if connection_id in config.connections:
            return connection_id
    return None


def _missing_connection_suggestion(connection_ids: tuple[str, ...]) -> str:
    """Return one shared suggestion string for missing implicit connection lookups."""
    if not connection_ids:
        return " Set an explicit connection name."
    configured_targets = " or ".join(f"connections.{connection_id}" for connection_id in connection_ids)
    return f" Add {configured_targets} or set an explicit connection name."


def resolve_connection(
    config: Config,
    *,
    provider: str,
    purpose: ConnectionPurpose,
    connection_name: str | None = None,
    runtime_paths: RuntimePaths,
) -> ResolvedConnection:
    """Resolve one named connection and load its shared credentials when needed."""
    canonical_provider = canonical_connection_provider(provider)
    expected_auth_kind = required_connection_auth_kind(provider=provider, purpose=purpose)
    conventional_default_ids = default_connection_ids(provider=provider, purpose=purpose)
    resolved_connection_id = connection_name
    if resolved_connection_id is None:
        resolved_connection_id = _configured_default_connection_id(config, conventional_default_ids)

    if resolved_connection_id is None:
        if expected_auth_kind == "none":
            return ResolvedConnection(
                connection_id=canonical_provider,
                provider=canonical_provider,
                auth_kind="none",
                service=None,
                credentials=None,
            )
        suggestion = _missing_connection_suggestion(conventional_default_ids)
        msg = f"Provider '{provider}' used for purpose '{purpose}' requires a configured connection.{suggestion}"
        raise ValueError(msg)

    connection_config = config.connections.get(resolved_connection_id)
    if connection_config is None:
        msg = f"Unknown connection '{resolved_connection_id}'"
        raise ValueError(msg)

    connection_provider = canonical_connection_provider(connection_config.provider)
    if connection_provider != canonical_provider:
        msg = (
            f"Connection '{resolved_connection_id}' is configured for provider "
            f"'{connection_config.provider}', not '{provider}'"
        )
        raise ValueError(msg)

    allowed_auth_kinds = allowed_connection_auth_kinds(provider=provider, purpose=purpose)
    if allowed_auth_kinds and connection_config.auth_kind not in allowed_auth_kinds:
        allowed_auth_kind_text = " or ".join(f"'{auth_kind}'" for auth_kind in allowed_auth_kinds)
        msg = (
            f"Connection '{resolved_connection_id}' has auth_kind '{connection_config.auth_kind}', "
            f"but provider '{provider}' used for purpose '{purpose}' requires {allowed_auth_kind_text}"
        )
        raise ValueError(msg)

    credentials: dict[str, Any] | None = None
    if connection_config.auth_kind != "none":
        if connection_config.service is None:
            msg = (
                f"Connection '{resolved_connection_id}' has auth_kind '{connection_config.auth_kind}' "
                "but no credential service is configured"
            )
            raise ValueError(msg)
        credentials = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(connection_config.service)

    resolved_connection = ResolvedConnection(
        connection_id=resolved_connection_id,
        provider=connection_provider,
        auth_kind=connection_config.auth_kind,
        service=connection_config.service,
        credentials=credentials,
    )
    _validate_connection_credentials(resolved_connection)
    return resolved_connection
