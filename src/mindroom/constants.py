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

STORAGE_PATH = os.getenv("STORAGE_PATH", "mindroom_data")
STORAGE_PATH_OBJ = Path(STORAGE_PATH)

# Specific files and directories
MATRIX_STATE_FILE = STORAGE_PATH_OBJ / "matrix_state.yaml"
SESSIONS_DIR = STORAGE_PATH_OBJ / "sessions"
TRACKING_DIR = STORAGE_PATH_OBJ / "tracking"
MEMORY_DIR = STORAGE_PATH_OBJ / "memory"
CREDENTIALS_DIR = STORAGE_PATH_OBJ / "credentials"

# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"
