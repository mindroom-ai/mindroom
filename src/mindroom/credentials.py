"""Unified credentials management for MindRoom.

This module provides centralized credential storage and retrieval for all integrations,
used by both agents and the dashboard interface.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mindroom import constants
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    resolve_execution_identity_for_worker_scope,
    resolve_worker_key,
    worker_root_path,
)

_SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]+$")
_WORKER_SHARED_CREDENTIALS_DIRNAME = ".shared_credentials"
SHARED_CREDENTIALS_PATH_ENV = "MINDROOM_SHARED_CREDENTIALS_PATH"
_DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
_DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"
logger = get_logger(__name__)

# Global instance for convenience (lazy initialization)
_credentials_manager: CredentialsManager | None = None
_credentials_manager_signature: tuple[Path, Path, str | None, Path | None] | None = None
_credentials_manager_lock = threading.Lock()
_PRIMARY_CREDENTIALS_STORAGE_PATH: Path | None = None


def validate_service_name(service: str) -> str:
    """Validate and normalize credential service names."""
    normalized = service.strip()
    if not normalized:
        msg = "Service name is required"
        raise ValueError(msg)
    if not _SERVICE_NAME_PATTERN.fullmatch(normalized):
        msg = "Service name can only include letters, numbers, colon, underscore, and hyphen"
        raise ValueError(msg)
    return normalized


class CredentialsManager:
    """Centralized credentials storage and retrieval for MindRoom."""

    def __init__(self, base_path: Path | None = None, *, shared_base_path: Path | None = None) -> None:
        """Initialize the credentials manager.

        Args:
            base_path: Base directory for storing credentials.
                      Defaults to STORAGE_PATH/credentials (usually mindroom_data/credentials)
            shared_base_path: Optional shared credential layer used for inherited or mirrored
                credentials within the current execution context.

        """
        if base_path is None:
            self.base_path = _default_credentials_base_path()
        else:
            self.base_path = Path(base_path)
        if shared_base_path is None:
            self.shared_base_path = _default_shared_credentials_base_path(self.base_path)
        else:
            self.shared_base_path = Path(shared_base_path)

        # Ensure the directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)
        if self.shared_base_path != self.base_path:
            self.shared_base_path.mkdir(parents=True, exist_ok=True)

    @property
    def storage_root(self) -> Path:
        """Return the storage root that owns this credentials directory."""
        return self.base_path.parent

    def for_worker(self, worker_key: str) -> CredentialsManager:
        """Return a credentials manager rooted in one worker's persistent state."""
        worker_root = worker_root_path(self.storage_root, worker_key)
        worker_credentials_path = worker_root / "credentials"
        worker_shared_credentials_path = worker_root / _WORKER_SHARED_CREDENTIALS_DIRNAME
        return CredentialsManager(
            base_path=worker_credentials_path,
            shared_base_path=worker_shared_credentials_path,
        )

    def shared_manager(self) -> CredentialsManager:
        """Return a manager rooted in the shared credential layer for this execution context."""
        return CredentialsManager(
            base_path=self.shared_base_path,
            shared_base_path=self.shared_base_path,
        )

    def get_credentials_path(self, service: str) -> Path:
        """Get the path for a service's credentials file.

        Args:
            service: Name of the service (e.g., 'google', 'homeassistant')

        Returns:
            Path to the credentials file

        """
        normalized_service = validate_service_name(service)
        return self.base_path / f"{normalized_service}_credentials.json"

    def load_credentials(self, service: str) -> dict[str, Any] | None:
        """Load credentials for a service.

        Args:
            service: Name of the service

        Returns:
            Credentials dictionary or None if not found

        """
        credentials_path = self.get_credentials_path(service)
        if credentials_path.exists():
            try:
                with credentials_path.open() as f:
                    data: dict[str, Any] = json.load(f)
                    return data
            except Exception:
                logger.exception(
                    "Failed to load credentials",
                    service=service,
                    path=str(credentials_path),
                )
                return None
        return None

    def save_credentials(self, service: str, credentials: dict[str, Any]) -> None:
        """Save credentials for a service.

        Args:
            service: Name of the service
            credentials: Credentials dictionary to save

        """
        credentials_path = self.get_credentials_path(service)
        with credentials_path.open("w") as f:
            json.dump(credentials, f, indent=2)

    def delete_credentials(self, service: str) -> None:
        """Delete credentials for a service.

        Args:
            service: Name of the service

        """
        credentials_path = self.get_credentials_path(service)
        if credentials_path.exists():
            credentials_path.unlink()

    def list_services(self) -> list[str]:
        """List all services with stored credentials.

        Returns:
            List of service names

        """
        services = []
        if self.base_path.exists():
            for path in self.base_path.glob("*_credentials.json"):
                service = path.stem.replace("_credentials", "")
                if _SERVICE_NAME_PATTERN.fullmatch(service):
                    services.append(service)
        return sorted(services)

    def get_api_key(self, service: str, key_name: str = "api_key") -> str | None:
        """Get an API key for a service.

        Args:
            service: Name of the service (e.g., 'openai', 'anthropic')
            key_name: Name of the key field (default: 'api_key')

        Returns:
            API key string or None if not found

        """
        credentials = self.load_credentials(service)
        if credentials:
            return credentials.get(key_name)
        return None

    def set_api_key(self, service: str, api_key: str, key_name: str = "api_key") -> None:
        """Set an API key for a service.

        Args:
            service: Name of the service
            api_key: The API key to store
            key_name: Name of the key field (default: 'api_key')

        """
        credentials = self.load_credentials(service) or {}
        credentials[key_name] = api_key
        self.save_credentials(service, credentials)


def _default_credentials_base_path() -> Path:
    if _PRIMARY_CREDENTIALS_STORAGE_PATH is not None:
        return _PRIMARY_CREDENTIALS_STORAGE_PATH / "credentials"
    return constants.get_runtime_paths().storage_root / "credentials"


def _default_shared_credentials_base_path(base_path: Path) -> Path:
    shared_storage_path = os.getenv(SHARED_CREDENTIALS_PATH_ENV, "").strip()
    if shared_storage_path:
        return Path(shared_storage_path).expanduser().resolve()
    return base_path


def _current_dedicated_worker_key() -> str | None:
    raw_worker_key = os.getenv(_DEDICATED_WORKER_KEY_ENV, "").strip()
    return raw_worker_key or None


def _current_dedicated_worker_root() -> Path | None:
    raw_worker_root = os.getenv(_DEDICATED_WORKER_ROOT_ENV, "").strip()
    if not raw_worker_root:
        return None
    return Path(raw_worker_root).expanduser().resolve()


def set_primary_credentials_storage_path(storage_path: Path | None) -> None:
    """Set the primary runtime storage root used for default credentials access."""
    global _credentials_manager, _credentials_manager_signature, _PRIMARY_CREDENTIALS_STORAGE_PATH
    with _credentials_manager_lock:
        normalized_storage_path = None if storage_path is None else storage_path.expanduser().resolve()
        if normalized_storage_path == _PRIMARY_CREDENTIALS_STORAGE_PATH:
            return

        _PRIMARY_CREDENTIALS_STORAGE_PATH = normalized_storage_path
        _credentials_manager = None
        _credentials_manager_signature = None


def get_credentials_manager() -> CredentialsManager:
    """Get the global credentials manager instance.

    Returns:
        The global CredentialsManager instance

    """
    global _credentials_manager, _credentials_manager_signature

    base_path = _default_credentials_base_path()
    shared_base_path = _default_shared_credentials_base_path(base_path)
    current_signature = (
        base_path,
        shared_base_path,
        _current_dedicated_worker_key(),
        _current_dedicated_worker_root(),
    )

    with _credentials_manager_lock:
        if _credentials_manager is None or _credentials_manager_signature != current_signature:
            _credentials_manager = CredentialsManager()
            _credentials_manager_signature = current_signature
        return _credentials_manager


def _resolve_worker_credentials_manager(
    *,
    worker_scope: WorkerScope | None,
    routing_agent_name: str | None,
    credentials_manager: CredentialsManager,
    execution_identity: ToolExecutionIdentity | None = None,
) -> CredentialsManager | None:
    """Return the worker-scoped credentials manager for the current execution, if any."""
    if worker_scope is None:
        return None

    execution_identity = resolve_execution_identity_for_worker_scope(
        worker_scope,
        agent_name=routing_agent_name,
        execution_identity=execution_identity,
    )
    if execution_identity is None:
        return None

    worker_key = resolve_worker_key(worker_scope, execution_identity, agent_name=routing_agent_name)
    if worker_key is None:
        return None

    current_dedicated_worker_key = _current_dedicated_worker_key()
    current_dedicated_worker_root = _current_dedicated_worker_root()
    current_storage_root = credentials_manager.storage_root.expanduser().resolve()
    if (
        current_dedicated_worker_key == worker_key
        and current_dedicated_worker_root is not None
        and current_storage_root == current_dedicated_worker_root
    ):
        return credentials_manager

    expected_worker_root = worker_root_path(credentials_manager.storage_root, worker_key)
    if current_storage_root == expected_worker_root:
        return credentials_manager

    return credentials_manager.for_worker(worker_key)


def _merge_unscoped_credentials(
    service: str,
    *,
    shared_manager: CredentialsManager,
    local_manager: CredentialsManager,
) -> dict[str, Any] | None:
    """Merge mirrored shared credentials with local worker overrides for unscoped workers."""
    merged_credentials: dict[str, Any] = {}

    shared_credentials = shared_manager.load_credentials(service)
    if isinstance(shared_credentials, Mapping):
        merged_credentials.update(shared_credentials)

    local_credentials = local_manager.load_credentials(service)
    if isinstance(local_credentials, Mapping):
        merged_credentials.update(local_credentials)

    return merged_credentials or None


def merge_scoped_credentials(
    service: str,
    *,
    base_manager: CredentialsManager,
    worker_manager: CredentialsManager | None,
) -> dict[str, Any] | None:
    """Merge env-backed shared credentials with worker-scoped overrides."""
    shared_credentials = base_manager.load_credentials(service)
    merged_credentials: dict[str, Any] = {}
    if isinstance(shared_credentials, Mapping) and shared_credentials.get("_source") == "env":
        merged_credentials.update(shared_credentials)

    if worker_manager is not None:
        worker_credentials = worker_manager.load_credentials(service)
        if isinstance(worker_credentials, Mapping):
            merged_credentials.update(worker_credentials)

    return merged_credentials or None


def sync_shared_credentials_to_worker(
    worker_key: str,
    *,
    include_ui_credentials: bool = False,
    credentials_manager: CredentialsManager | None = None,
) -> None:
    """Sync shared credentials into one worker's dedicated shared-credential mirror.

    The worker's override store remains separate. Env-backed shared credentials are always
    copied; UI-backed shared credentials and legacy untagged shared credentials are copied
    only when explicitly requested.
    """
    manager = credentials_manager or get_credentials_manager()
    worker_shared_manager = manager.for_worker(worker_key).shared_manager()
    mirrored_services = set(worker_shared_manager.list_services())
    allowed_services: set[str] = set()

    for service in manager.list_services():
        shared_credentials = manager.load_credentials(service)
        if not isinstance(shared_credentials, Mapping):
            continue
        source = shared_credentials.get("_source")
        if source != "env" and not include_ui_credentials:
            continue
        if source not in {"env", "ui", None}:
            continue

        allowed_services.add(service)
        worker_shared_manager.save_credentials(service, dict(shared_credentials))

    for service in mirrored_services - allowed_services:
        worker_shared_manager.delete_credentials(service)


def _sync_env_credentials_to_worker(
    worker_key: str,
    *,
    credentials_manager: CredentialsManager | None = None,
) -> None:
    """Backward-compatible wrapper for syncing env-backed shared credentials."""
    sync_shared_credentials_to_worker(
        worker_key,
        include_ui_credentials=False,
        credentials_manager=credentials_manager,
    )


def load_scoped_credentials(
    service: str,
    *,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
    credentials_manager: CredentialsManager | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> dict[str, Any] | None:
    """Load credentials for a service, resolving worker-scoped overrides when available."""
    manager = credentials_manager or get_credentials_manager()
    shared_manager = manager.shared_manager()
    if worker_scope is None:
        if manager.shared_base_path != manager.base_path:
            return _merge_unscoped_credentials(
                service,
                shared_manager=shared_manager,
                local_manager=manager,
            )
        return shared_manager.load_credentials(service)

    worker_manager = _resolve_worker_credentials_manager(
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        credentials_manager=manager,
        execution_identity=execution_identity,
    )
    return merge_scoped_credentials(
        service,
        base_manager=shared_manager,
        worker_manager=worker_manager,
    )


def save_scoped_credentials(
    service: str,
    credentials: dict[str, Any],
    *,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
    credentials_manager: CredentialsManager | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Save credentials for a service to the current worker scope when available."""
    manager = credentials_manager or get_credentials_manager()
    if worker_scope is None:
        target_manager = manager if manager.shared_base_path != manager.base_path else manager.shared_manager()
        target_manager.save_credentials(service, credentials)
        return

    worker_manager = _resolve_worker_credentials_manager(
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        credentials_manager=manager,
        execution_identity=execution_identity,
    )
    target_manager = worker_manager or manager.shared_manager()
    target_manager.save_credentials(service, credentials)
