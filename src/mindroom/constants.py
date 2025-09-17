"""Shared constants for the mindroom package.

This module contains constants that are used across multiple modules
to avoid circular imports. It does not import anything from the internal
codebase.
"""

import os
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
SESSIONS_DIR = STORAGE_PATH_OBJ / "sessions"
TRACKING_DIR = STORAGE_PATH_OBJ / "tracking"
MEMORY_DIR = STORAGE_PATH_OBJ / "memory"
CREDENTIALS_DIR = STORAGE_PATH_OBJ / "credentials"
ENCRYPTION_KEYS_DIR = STORAGE_PATH_OBJ / "encryption_keys"

# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() != "false"
ENABLE_AI_CACHE = os.getenv("ENABLE_AI_CACHE", "true").lower() != "false"

# Matrix
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
# (for federation setups where hostname != server_name)
MATRIX_SERVER_NAME = os.getenv("MATRIX_SERVER_NAME", None)
MATRIX_SSL_VERIFY = os.getenv("MATRIX_SSL_VERIFY", "true").lower() != "false"
