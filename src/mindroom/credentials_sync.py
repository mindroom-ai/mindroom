"""Sync shared named-service credentials from runtime env into CredentialsManager."""

from mindroom.config.main import load_config
from mindroom.connections import canonical_connection_provider, default_connection_id
from mindroom.constants import RuntimePaths, runtime_env_path
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.logging_config import get_logger

logger = get_logger(__name__)

_ENV_TO_SERVICE_MAP = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GOOGLE_API_KEY": "google_gemini",
    "OPENROUTER_API_KEY": "openrouter",
    "DEEPSEEK_API_KEY": "deepseek",
    "CEREBRAS_API_KEY": "cerebras",
    "GROQ_API_KEY": "groq",
    "GOOGLE_APPLICATION_CREDENTIALS": "google_vertex_adc",
}
_API_KEY_ENV_TO_PROVIDER = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GOOGLE_API_KEY": "google",
    "OPENROUTER_API_KEY": "openrouter",
    "DEEPSEEK_API_KEY": "deepseek",
    "CEREBRAS_API_KEY": "cerebras",
    "GROQ_API_KEY": "groq",
}


def _configured_connection_targets(
    runtime_paths: RuntimePaths,
) -> tuple[dict[str, str | None], str | None, str | None]:
    """Return configured shared credential services grouped by auth purpose."""
    try:
        runtime_config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    except Exception as exc:
        logger.debug(
            "credential_env_sync_config_unavailable",
            config_path=str(runtime_paths.config_path),
            error=str(exc),
        )
        return {}, None, None

    def configured_service(
        *,
        provider: str,
        purpose: str,
        expected_auth_kind: str,
    ) -> tuple[bool, str | None]:
        connection_id = default_connection_id(provider=provider, purpose=purpose)
        if connection_id is None:
            return False, None
        connection = runtime_config.connections.get(connection_id)
        if connection is None:
            return False, None
        if (
            canonical_connection_provider(connection.provider) != canonical_connection_provider(provider)
            or connection.auth_kind != expected_auth_kind
        ):
            return False, None
        return True, connection.service

    api_key_services: dict[str, str | None] = {}
    for provider in set(_API_KEY_ENV_TO_PROVIDER.values()):
        configured, service = configured_service(
            provider=provider,
            purpose="chat_model",
            expected_auth_kind="api_key",
        )
        if configured:
            api_key_services[provider] = service
    google_adc_service = configured_service(
        provider="vertexai_claude",
        purpose="chat_model",
        expected_auth_kind="google_adc",
    )[1]
    google_oauth_service = configured_service(
        provider="google",
        purpose="google_oauth_client",
        expected_auth_kind="oauth_client",
    )[1]
    return api_key_services, google_adc_service, google_oauth_service


def get_secret_from_env(name: str, runtime_paths: RuntimePaths) -> str | None:
    """Read a secret from NAME or NAME_FILE.

    If env var `NAME` is set, return it. Otherwise, if `NAME_FILE` points to
    a readable file, return its stripped contents. Else return None.
    """
    val = runtime_paths.env_value(name)
    if val:
        return val
    file_var = f"{name}_FILE"
    file_path = runtime_env_path(runtime_paths, file_var)
    if file_path is not None and file_path.exists():
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except Exception:
            # Avoid noisy logs here; callers can handle None gracefully
            return None
    return None


def _sync_github_private_credentials(runtime_paths: RuntimePaths) -> bool:
    """Seed/update github_private from GITHUB_TOKEN for Git knowledge sync."""
    github_token = get_secret_from_env("GITHUB_TOKEN", runtime_paths=runtime_paths)
    if not github_token:
        logger.debug("No value found for GITHUB_TOKEN or GITHUB_TOKEN_FILE")
        return False

    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    existing = creds_manager.load_credentials("github_private")
    if existing is not None:
        source = existing.get("_source")
        if source != "env":
            # UI-set or legacy (no _source) — don't overwrite
            logger.debug("Credentials for github_private not env-sourced, skipping env sync")
            return False

    creds_manager.save_credentials(
        "github_private",
        {
            "username": "x-access-token",
            "token": github_token,
            "_source": "env",
        },
    )
    if existing is None:
        logger.info("Seeded github_private credentials from GITHUB_TOKEN")
    else:
        logger.info("Updated github_private credentials from GITHUB_TOKEN")
    return True


def _sync_service_credentials(
    *,
    service: str,
    credentials: dict[str, str],
    runtime_paths: RuntimePaths,
    env_var: str | None = None,
) -> bool:
    """Seed or update one env-backed named service."""
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    existing = creds_manager.load_credentials(service)
    if existing is not None:
        source = existing.get("_source")
        if source != "env":
            logger.debug("credential_env_sync_skipped", service=service, source=source)
            return False

    creds_manager.save_credentials(service, {**credentials, "_source": "env"})
    log_context = {"service": service}
    if env_var is not None:
        log_context["env_var"] = env_var
    if existing is None:
        logger.info("credential_seeded_from_env", **log_context)
    else:
        logger.info("credential_updated_from_env", **log_context)
    return True


def _sync_google_vertex_adc_credentials(runtime_paths: RuntimePaths, service: str | None) -> int:
    """Seed/update configured ADC services from GOOGLE_APPLICATION_CREDENTIALS."""
    adc_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if adc_path is None:
        logger.debug("No GOOGLE_APPLICATION_CREDENTIALS path found for google_vertex_adc")
        return 0

    target_service = service or "google_vertex_adc"
    if not target_service:
        return 0
    if _sync_service_credentials(
        service=target_service,
        credentials={"application_credentials_path": str(adc_path)},
        runtime_paths=runtime_paths,
        env_var="GOOGLE_APPLICATION_CREDENTIALS",
    ):
        return 1
    return 0


def _sync_google_oauth_client_credentials(runtime_paths: RuntimePaths, service: str | None) -> int:
    """Seed/update configured Google OAuth client services from GOOGLE_CLIENT_ID/SECRET."""
    client_id = get_secret_from_env("GOOGLE_CLIENT_ID", runtime_paths=runtime_paths)
    client_secret = get_secret_from_env("GOOGLE_CLIENT_SECRET", runtime_paths=runtime_paths)
    if not client_id or not client_secret:
        return 0

    target_service = service or "google_oauth_client"
    if not target_service:
        return 0
    if _sync_service_credentials(
        service=target_service,
        credentials={"client_id": client_id, "client_secret": client_secret},
        runtime_paths=runtime_paths,
        env_var="GOOGLE_CLIENT_ID,GOOGLE_CLIENT_SECRET",
    ):
        return 1
    return 0


def sync_env_to_credentials(runtime_paths: RuntimePaths) -> None:
    """Sync supported shared named-service env values into CredentialsManager."""
    synced_count = 0
    api_key_services, google_adc_service, google_oauth_service = _configured_connection_targets(runtime_paths)

    for env_var, service in _ENV_TO_SERVICE_MAP.items():
        if env_var == "GOOGLE_APPLICATION_CREDENTIALS":
            continue
        env_value = get_secret_from_env(env_var, runtime_paths=runtime_paths)

        if not env_value:
            logger.debug("credential_env_value_missing", env_var=env_var)
            continue

        logger.debug("credential_env_value_found", env_var=env_var, value_length=len(env_value))

        target_service = api_key_services.get(_API_KEY_ENV_TO_PROVIDER[env_var], service)
        if target_service is None:
            continue
        if _sync_service_credentials(
            service=target_service,
            credentials={"api_key": env_value},
            runtime_paths=runtime_paths,
            env_var=env_var,
        ):
            synced_count += 1

    synced_count += _sync_google_vertex_adc_credentials(runtime_paths=runtime_paths, service=google_adc_service)

    synced_count += _sync_google_oauth_client_credentials(
        runtime_paths=runtime_paths,
        service=google_oauth_service,
    )

    if _sync_github_private_credentials(runtime_paths=runtime_paths):
        synced_count += 1

    if synced_count > 0:
        logger.info("credentials_synced_from_env", synced_count=synced_count)
    else:
        logger.debug("No credentials to sync from environment")


def get_ollama_host(runtime_paths: RuntimePaths) -> str | None:
    """Get Ollama host configuration.

    Returns:
        The Ollama host URL if configured, None otherwise

    """
    value = runtime_paths.env_value("OLLAMA_HOST")
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
