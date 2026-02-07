"""Sync API keys from environment variables to CredentialsManager.

On first run, API keys from .env are seeded into the CredentialsManager.
On subsequent runs, env-sourced credentials (_source=env) are updated from
.env, but UI-sourced credentials (_source=ui) are never overwritten.
This lets users change keys via the UI without losing them on restart,
while still picking up .env changes for keys that were never manually set.
"""

import os
from pathlib import Path

from .credentials import get_credentials_manager
from .logging_config import get_logger

logger = get_logger(__name__)

# Mapping of environment variables to service names
ENV_TO_SERVICE_MAP = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GOOGLE_API_KEY": "google",  # Also used for Gemini
    "OPENROUTER_API_KEY": "openrouter",
    "DEEPSEEK_API_KEY": "deepseek",
    "CEREBRAS_API_KEY": "cerebras",
    "GROQ_API_KEY": "groq",
    "OLLAMA_HOST": "ollama",  # Special case: host instead of API key
}


def _get_secret(name: str) -> str | None:
    """Read a secret from NAME or NAME_FILE.

    If env var `NAME` is set, return it. Otherwise, if `NAME_FILE` points to
    a readable file, return its stripped contents. Else return None.
    """
    val = os.getenv(name)
    if val:
        return val
    file_var = f"{name}_FILE"
    file_path = os.getenv(file_var)
    if file_path and Path(file_path).exists():
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except Exception:
            # Avoid noisy logs here; callers can handle None gracefully
            return None
    return None


def sync_env_to_credentials() -> None:
    """Sync API keys from environment variables into CredentialsManager.

    - If no credential file exists for a service, seed it from .env.
    - If the existing credential has ``_source=env``, update it from .env
      (the user never touched it via UI, so .env should still win).
    - If the existing credential has ``_source=ui`` (or no ``_source``,
      for legacy files), skip it to protect the user's manual override.

    Environment variables are always exported to ``os.environ`` so that
    libraries like mem0 can pick them up regardless.
    """
    creds_manager = get_credentials_manager()
    synced_count = 0

    for env_var, service in ENV_TO_SERVICE_MAP.items():
        env_value = _get_secret(env_var)

        if not env_value:
            logger.debug(f"No value found for {env_var} or {env_var}_FILE")
            continue

        logger.debug(f"Found value for {env_var}: length={len(env_value)}")

        # Always export to os.environ so libraries (mem0, etc.) can use it
        if service != "ollama":
            os.environ[env_var] = env_value

        # Check existing credentials and their source
        existing = creds_manager.load_credentials(service)
        if existing is not None:
            source = existing.get("_source")
            if source != "env":
                # UI-set or legacy (no _source) — don't overwrite
                logger.debug(f"Credentials for {service} set via UI, skipping env sync")
                continue

        if service == "ollama":
            new_creds = {"host": env_value, "_source": "env"}
        else:
            new_creds = {"api_key": env_value, "_source": "env"}

        creds_manager.save_credentials(service, new_creds)
        if existing is None:
            logger.info(f"Seeded {service} credentials from environment")
        else:
            logger.info(f"Updated {service} credentials from environment")
        synced_count += 1

    if synced_count > 0:
        logger.info(f"Synced {synced_count} credentials from environment")
    else:
        logger.debug("No credentials to sync from environment")


def get_api_key_for_provider(provider: str) -> str | None:
    """Get API key for a provider, checking CredentialsManager first.

    Since we sync from .env to CredentialsManager on startup,
    CredentialsManager will always have the latest keys from .env.

    Args:
        provider: The provider name (e.g., 'openai', 'anthropic')

    Returns:
        The API key if found, None otherwise

    """
    creds_manager = get_credentials_manager()

    # Special case for Ollama - return None as it doesn't use API keys
    if provider == "ollama":
        return None

    # For Google/Gemini, both use the same key
    if provider == "gemini":
        provider = "google"

    return creds_manager.get_api_key(provider)


def get_ollama_host() -> str | None:
    """Get Ollama host configuration.

    Returns:
        The Ollama host URL if configured, None otherwise

    """
    creds_manager = get_credentials_manager()
    ollama_creds = creds_manager.load_credentials("ollama")
    if ollama_creds:
        return ollama_creds.get("host")
    return None
