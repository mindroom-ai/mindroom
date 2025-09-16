"""Sync API keys from environment variables to CredentialsManager.

This module ensures that API keys defined in .env are automatically
synchronized to the CredentialsManager, making .env the source of truth
while allowing the frontend to read the current state.
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
    """Sync API keys from environment variables to CredentialsManager.

    This function reads API keys from environment variables and updates
    the CredentialsManager with any new or changed values. This ensures
    that .env remains the source of truth while making keys available
    to the frontend.
    """
    creds_manager = get_credentials_manager()
    updated_count = 0

    for env_var, service in ENV_TO_SERVICE_MAP.items():
        # Prefer file-based secrets if provided (NAME_FILE), fallback to NAME
        env_value = _get_secret(env_var)

        if not env_value:
            logger.debug(f"No value found for {env_var} or {env_var}_FILE")
            continue

        logger.debug(f"Found value for {env_var}: length={len(env_value)}")

        # Special handling for Ollama (it's a host, not an API key)
        if service == "ollama":
            current_creds = creds_manager.load_credentials(service) or {}
            if current_creds.get("host") != env_value:
                current_creds["host"] = env_value
                creds_manager.save_credentials(service, current_creds)
                logger.info(f"Updated {service} host from environment")
                updated_count += 1
        else:
            # Regular API key handling
            current_key = creds_manager.get_api_key(service)
            if env_value and current_key != env_value:
                creds_manager.set_api_key(service, env_value)
                logger.info(f"Updated {service} API key from environment")
                updated_count += 1

            # Also set the environment variable directly for libraries that need it
            # This ensures mem0 and other libraries can access the keys
            if env_value:
                os.environ[env_var] = env_value

    if updated_count > 0:
        logger.info(f"Synchronized {updated_count} credentials from environment")
    else:
        logger.debug("All credentials already in sync with environment")


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
