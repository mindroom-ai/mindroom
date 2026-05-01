"""Sync shared provider/bootstrap credentials from runtime env into CredentialsManager.

On first run, supported provider/bootstrap env values from the config-adjacent
`.env` or exported process env are seeded into the shared credentials store.
On subsequent runs, env-sourced shared credentials (`_source=env`) are updated,
but UI-sourced credentials (`_source=ui`) are never overwritten.

This is intentionally limited to supported shared credentials such as model
provider API keys, Ollama host settings, and `GITHUB_TOKEN` mirroring for
private Git knowledge sync.
It is not a generic bridge for tool-specific env var configuration.
"""

from mindroom.constants import PROVIDER_ENV_KEYS, RuntimePaths, runtime_env_path
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.logging_config import get_logger

logger = get_logger(__name__)

# Reverse view: env-var → provider (derived from the canonical mapping).
_ENV_TO_SERVICE_MAP = {v: k for k, v in PROVIDER_ENV_KEYS.items()}


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


def sync_env_to_credentials(runtime_paths: RuntimePaths) -> None:
    """Sync supported shared provider/bootstrap env values into CredentialsManager.

    - If no shared credential file exists for a supported service, seed it from runtime env.
    - If the existing credential has ``_source=env``, update it from runtime env
      (the user never touched it via UI, so runtime env should still win).
    - If the existing credential has ``_source=ui`` (or no ``_source``,
      for legacy files), skip it to protect the user's manual override.

    This keeps conventional provider/bootstrap `.env` support without treating
    arbitrary tool-specific env vars as a supported tool configuration path.
    """
    synced_count = 0

    for env_var, service in _ENV_TO_SERVICE_MAP.items():
        env_value = get_secret_from_env(env_var, runtime_paths=runtime_paths)

        if not env_value:
            logger.debug("credential_env_value_missing", env_var=env_var)
            continue

        logger.debug("credential_env_value_found", env_var=env_var, value_length=len(env_value))

        credentials = {"host": env_value} if service == "ollama" else {"api_key": env_value}
        if _sync_service_credentials(
            service=service,
            credentials=credentials,
            runtime_paths=runtime_paths,
            env_var=env_var,
        ):
            synced_count += 1

    adc_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if adc_path is not None:
        if _sync_service_credentials(
            service="google_vertex_adc",
            credentials={"application_credentials_path": str(adc_path)},
            runtime_paths=runtime_paths,
            env_var="GOOGLE_APPLICATION_CREDENTIALS",
        ):
            synced_count += 1
    else:
        logger.debug("No GOOGLE_APPLICATION_CREDENTIALS path found for google_vertex_adc")

    if _sync_github_private_credentials(runtime_paths=runtime_paths):
        synced_count += 1

    if synced_count > 0:
        logger.info("credentials_synced_from_env", synced_count=synced_count)
    else:
        logger.debug("No credentials to sync from environment")


def get_api_key_for_provider(provider: str, runtime_paths: RuntimePaths) -> str | None:
    """Get API key for a provider, checking CredentialsManager first.

    Supported provider env values are mirrored into the shared credentials store
    during startup, so model creation reads from one explicit source of truth.

    Args:
        provider: The provider name (e.g., 'openai', 'anthropic')
        runtime_paths: Explicit runtime context for credential lookup.

    Returns:
        The API key if found, None otherwise

    """
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)

    # Special case for Ollama - return None as it doesn't use API keys
    if provider == "ollama":
        return None

    # For Google/Gemini, both use the same key
    if provider == "gemini":
        provider = "google"

    return creds_manager.get_api_key(provider)


def get_ollama_host(runtime_paths: RuntimePaths) -> str | None:
    """Get Ollama host configuration.

    Returns:
        The Ollama host URL if configured, None otherwise

    """
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    ollama_creds = creds_manager.load_credentials("ollama")
    if ollama_creds:
        return ollama_creds.get("host")
    return None
