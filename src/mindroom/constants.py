"""Shared constants for the mindroom package.

This module contains constants that are used across multiple modules
to avoid circular imports. It does not import anything from the internal
codebase.
"""

import os
from pathlib import Path

# Agent names
ROUTER_AGENT_NAME = "router"

# Default path to agents configuration file
DEFAULT_AGENTS_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

# Use storage path if available, otherwise current directory
STORAGE_PATH = os.getenv("STORAGE_PATH", ".")
MATRIX_STATE_FILE = Path(STORAGE_PATH) / "matrix_state.yaml"

# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"

# Matrix
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
# (for federation setups where hostname != server_name)
MATRIX_SERVER_NAME = os.getenv("MATRIX_SERVER_NAME", None)
