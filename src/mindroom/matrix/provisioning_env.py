"""Environment readers that decide how hosted installs register Matrix accounts, and their client-credential headers.

This module stays free of Matrix and HTTP client imports because `mindroom service status` checks pairing on every poll.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import runtime_env_path

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def _permanent_startup_error(message: str) -> ValueError:
    """Return the permanent Matrix startup error without importing the Matrix client at module load."""
    from mindroom.matrix.client_session import matrix_startup_error  # noqa: PLC0415

    return matrix_startup_error(message, permanent=True)


def provisioning_url_from_env(runtime_paths: RuntimePaths) -> str | None:
    """Get hosted provisioning API base URL from environment if configured."""
    url = (runtime_paths.env_value("MINDROOM_PROVISIONING_URL") or "").strip()
    return url.rstrip("/") or None


def registration_token_from_env(runtime_paths: RuntimePaths) -> str | None:
    """Get MATRIX_REGISTRATION_TOKEN from environment if configured."""
    token = (runtime_paths.env_value("MATRIX_REGISTRATION_TOKEN") or "").strip()
    return token or None


def registration_shared_secret_from_env(runtime_paths: RuntimePaths) -> str | None:
    """Get Synapse shared-secret registration credentials from env or file."""
    secret = (runtime_paths.env_value("MATRIX_REGISTRATION_SHARED_SECRET") or "").strip()
    if secret:
        return secret

    file_path = runtime_env_path(runtime_paths, "MATRIX_REGISTRATION_SHARED_SECRET_FILE")
    if file_path is None:
        return None
    try:
        return file_path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        msg = f"MATRIX_REGISTRATION_SHARED_SECRET_FILE is not readable: {file_path}"
        raise _permanent_startup_error(msg) from exc


def local_provisioning_client_credentials_from_env(
    runtime_paths: RuntimePaths,
) -> tuple[str, str] | None:
    """Get local provisioning client credentials from environment if configured."""
    client_id = (runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_ID") or "").strip()
    client_secret = (runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_SECRET") or "").strip()
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        msg = (
            "Provisioning credentials are incomplete. "
            "Set both MINDROOM_LOCAL_CLIENT_ID and MINDROOM_LOCAL_CLIENT_SECRET, "
            "or run `mindroom connect` again."
        )
        raise _permanent_startup_error(msg)
    return client_id, client_secret


def local_client_headers(client_id: str, client_secret: str) -> dict[str, str]:
    """Return the headers that authenticate a paired install to the provisioning service."""
    return {
        "X-Local-MindRoom-Client-Id": client_id,
        "X-Local-MindRoom-Client-Secret": client_secret,
    }


def local_pairing_required(runtime_paths: RuntimePaths) -> bool:
    """Return whether hosted registration needs this install to pair before startup."""
    return (
        provisioning_url_from_env(runtime_paths) is not None
        and registration_token_from_env(runtime_paths) is None
        and registration_shared_secret_from_env(runtime_paths) is None
        and local_provisioning_client_credentials_from_env(runtime_paths) is None
    )
