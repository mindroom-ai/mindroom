"""Sync shared named-service credentials from runtime env into CredentialsManager."""

from collections import defaultdict

from mindroom.config.main import load_config
from mindroom.connections import canonical_connection_provider
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
) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Return configured shared credential services grouped by auth purpose."""
    try:
        runtime_config = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    except Exception as exc:
        logger.debug(
            "credential_env_sync_config_unavailable",
            config_path=str(runtime_paths.config_path),
            error=str(exc),
        )
        return {}, set(), set()

    api_key_services: dict[str, set[str]] = defaultdict(set)
    google_adc_services: set[str] = set()
    google_oauth_services: set[str] = set()
    for connection in runtime_config.connections.values():
        if connection.service is None:
            continue
        if connection.auth_kind == "api_key":
            api_key_services[canonical_connection_provider(connection.provider)].add(connection.service)
        elif connection.auth_kind == "google_adc":
            google_adc_services.add(connection.service)
        elif connection.auth_kind == "oauth_client" and canonical_connection_provider(connection.provider) == "google":
            google_oauth_services.add(connection.service)
    return api_key_services, google_adc_services, google_oauth_services


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


def _sync_google_vertex_adc_credentials(runtime_paths: RuntimePaths, services: set[str]) -> int:
    """Seed/update configured ADC services from GOOGLE_APPLICATION_CREDENTIALS."""
    adc_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if adc_path is None:
        logger.debug("No GOOGLE_APPLICATION_CREDENTIALS path found for google_vertex_adc")
        return 0

    synced_count = 0
    for service in sorted(services or {"google_vertex_adc"}):
        if _sync_service_credentials(
            service=service,
            credentials={"application_credentials_path": str(adc_path)},
            runtime_paths=runtime_paths,
            env_var="GOOGLE_APPLICATION_CREDENTIALS",
        ):
            synced_count += 1
    return synced_count


def _sync_google_oauth_client_credentials(runtime_paths: RuntimePaths, services: set[str]) -> int:
    """Seed/update configured Google OAuth client services from GOOGLE_CLIENT_ID/SECRET."""
    client_id = get_secret_from_env("GOOGLE_CLIENT_ID", runtime_paths=runtime_paths)
    client_secret = get_secret_from_env("GOOGLE_CLIENT_SECRET", runtime_paths=runtime_paths)
    if not client_id or not client_secret:
        return 0

    synced_count = 0
    for service in sorted(services or {"google_oauth_client"}):
        if _sync_service_credentials(
            service=service,
            credentials={"client_id": client_id, "client_secret": client_secret},
            runtime_paths=runtime_paths,
            env_var="GOOGLE_CLIENT_ID,GOOGLE_CLIENT_SECRET",
        ):
            synced_count += 1
    return synced_count


def sync_env_to_credentials(runtime_paths: RuntimePaths) -> None:
    """Sync supported shared named-service env values into CredentialsManager."""
    synced_count = 0
    api_key_services, google_adc_services, google_oauth_services = _configured_connection_targets(runtime_paths)

    for env_var, service in _ENV_TO_SERVICE_MAP.items():
        if env_var == "GOOGLE_APPLICATION_CREDENTIALS":
            continue
        env_value = get_secret_from_env(env_var, runtime_paths=runtime_paths)

        if not env_value:
            logger.debug("credential_env_value_missing", env_var=env_var)
            continue

        logger.debug("credential_env_value_found", env_var=env_var, value_length=len(env_value))

        target_services = api_key_services.get(_API_KEY_ENV_TO_PROVIDER[env_var]) or {service}
        for target_service in sorted(target_services):
            if _sync_service_credentials(
                service=target_service,
                credentials={"api_key": env_value},
                runtime_paths=runtime_paths,
                env_var=env_var,
            ):
                synced_count += 1

    synced_count += _sync_google_vertex_adc_credentials(runtime_paths=runtime_paths, services=google_adc_services)

    synced_count += _sync_google_oauth_client_credentials(
        runtime_paths=runtime_paths,
        services=google_oauth_services,
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
