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

# Storage path for all persistent data (defaults to mindroom_data)
STORAGE_PATH = os.getenv("STORAGE_PATH", "mindroom_data")

# Common paths for organized storage
STORAGE_PATH_OBJ = Path(STORAGE_PATH)
STATE_DIR = STORAGE_PATH_OBJ / "state"
MATRIX_STATE_DIR = STATE_DIR / "matrix"

# Specific files
MATRIX_STATE_FILE = MATRIX_STATE_DIR / "matrix_state.yaml"

# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"
