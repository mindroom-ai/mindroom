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

# Use storage path if available, otherwise use mindroom_data directory
STORAGE_PATH = os.getenv("STORAGE_PATH", "mindroom_data")

# Common paths for organized storage
STORAGE_PATH_OBJ = Path(STORAGE_PATH)
STATE_DIR = STORAGE_PATH_OBJ / "state"
MATRIX_STATE_DIR = STATE_DIR / "matrix"
AGENTS_STATE_DIR = STATE_DIR / "agents"
AGENTS_SESSIONS_DIR = AGENTS_STATE_DIR / "sessions"
AGENTS_TRACKING_DIR = AGENTS_STATE_DIR / "tracking"
MEMORY_STATE_DIR = STATE_DIR / "memory"
CREDENTIALS_DIR = STORAGE_PATH_OBJ / "credentials"

# Specific files
MATRIX_STATE_FILE = MATRIX_STATE_DIR / "matrix_state.yaml"

# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"
