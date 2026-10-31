"""Launch facts shared by script launchers, the shim that starts a script, and the SDK inside it.

The shim and the SDK run in the script's own process, so this module imports only the standard library.
"""

from __future__ import annotations

from pathlib import Path

GATEWAY_URL_ENV = "MINDROOM_SCRIPT_GATEWAY_URL"
RUN_ID_ENV = "MINDROOM_SCRIPT_RUN_ID"
SOURCE_DIGEST_ENV = "MINDROOM_SCRIPT_SOURCE_DIGEST"
SNAPSHOT_ROOT_ENV = "MINDROOM_SCRIPT_SNAPSHOT_ROOT"
TOKEN_PATH_ENV = "MINDROOM_SCRIPT_TOKEN_PATH"  # noqa: S105 - this names a path, not a token.
WORKSPACE_ROOT_ENV = "MINDROOM_SCRIPT_WORKSPACE_ROOT"
MAX_SOURCE_BYTES = 128 * 1024
MAX_TOKEN_BYTES = 4096
SOURCE_FILE_NAME = "source.py"
CAPABILITY_FILE_NAME = "capability"


def snapshot_dir(run_id: str) -> Path:
    """Return the workspace-relative directory holding one run's source and capability."""
    return Path(".mindroom") / "script-runs" / run_id
