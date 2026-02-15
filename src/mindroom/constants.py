"""Shared constants for the mindroom package.

This module contains constants that are used across multiple modules
to avoid circular imports. It does not import anything from the internal
codebase.
"""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Agent names
ROUTER_AGENT_NAME = "router"

# Default path to agents configuration file. Allow overriding via environment
# variable so deployments can place the writable configuration file on a
# persistent volume instead of the package directory (which may be read-only).
_CONFIG_PATH_ENV = os.getenv("MINDROOM_CONFIG_PATH") or os.getenv("CONFIG_PATH")
DEFAULT_AGENTS_CONFIG = (
    Path(_CONFIG_PATH_ENV).expanduser() if _CONFIG_PATH_ENV else Path(__file__).parent.parent.parent / "config.yaml"
)

# Optional template path used to seed the writable config file if it does not
# exist yet. Defaults to the same location as DEFAULT_AGENTS_CONFIG so the
# behaviour is unchanged when no overrides are provided.
_CONFIG_TEMPLATE_ENV = os.getenv("MINDROOM_CONFIG_TEMPLATE") or os.getenv("CONFIG_TEMPLATE_PATH")
DEFAULT_CONFIG_TEMPLATE = Path(_CONFIG_TEMPLATE_ENV).expanduser() if _CONFIG_TEMPLATE_ENV else DEFAULT_AGENTS_CONFIG

STORAGE_PATH = os.getenv("STORAGE_PATH", "mindroom_data")
STORAGE_PATH_OBJ = Path(STORAGE_PATH)

# Specific files and directories
MATRIX_STATE_FILE = STORAGE_PATH_OBJ / "matrix_state.yaml"
TRACKING_DIR = STORAGE_PATH_OBJ / "tracking"
MEMORY_DIR = STORAGE_PATH_OBJ / "memory"
CREDENTIALS_DIR = STORAGE_PATH_OBJ / "credentials"
ENCRYPTION_KEYS_DIR = STORAGE_PATH_OBJ / "encryption_keys"


def env_flag(name: str, *, default: bool = False) -> bool:
    """Read a boolean environment flag."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = env_flag("MINDROOM_ENABLE_STREAMING", default=True)
ENABLE_AI_CACHE = env_flag("ENABLE_AI_CACHE", default=True)

# Matrix
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
# (for federation setups where hostname != server_name)
MATRIX_SERVER_NAME = os.getenv("MATRIX_SERVER_NAME", None)
MATRIX_SSL_VERIFY = env_flag("MATRIX_SSL_VERIFY", default=True)


# Canonical mapping from provider name to the environment variable it requires.
# Other modules derive their own views from this single source of truth.
PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "OLLAMA_HOST",
}


def env_key_for_provider(provider: str) -> str | None:
    """Get the environment variable name for a provider's API key.

    Handles the gemini→google alias so callers don't need to.
    """
    if provider == "gemini":
        return PROVIDER_ENV_KEYS.get("google")
    return PROVIDER_ENV_KEYS.get(provider)


def safe_replace(tmp_path: Path, target_path: Path) -> None:
    """Replace *target_path* with *tmp_path*, with a fallback for bind mounts.

    ``Path.replace`` performs an atomic rename which fails on some filesystems
    (e.g. Docker bind mounts) with ``OSError: [Errno 16] Device or resource
    busy``.  When that happens we fall back to a non-atomic copy.
    """
    try:
        tmp_path.replace(target_path)
    except OSError:
        shutil.copy2(tmp_path, target_path)
        tmp_path.unlink(missing_ok=True)
