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
# Organize state files under a state subdirectory
MATRIX_STATE_FILE = Path(STORAGE_PATH) / "state" / "matrix" / "sync_state.yaml"

# Other constants
VOICE_PREFIX = "🎤 "
ENABLE_STREAMING = os.getenv("MINDROOM_ENABLE_STREAMING", "true").lower() == "true"
