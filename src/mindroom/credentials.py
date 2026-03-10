"""Unified credentials management for MindRoom.

This module provides centralized credential storage and retrieval for all integrations,
used by both agents and the dashboard interface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mindroom.constants import CREDENTIALS_DIR
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    get_tool_execution_identity,
    resolve_worker_key,
    worker_root_path,
)

_SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]+$")
logger = get_logger(__name__)


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

    def __init__(self, base_path: Path | None = None) -> None:
        """Initialize the credentials manager.

        Args:
            base_path: Base directory for storing credentials.
                      Defaults to STORAGE_PATH/credentials (usually mindroom_data/credentials)

        """
        if base_path is None:
            self.base_path = CREDENTIALS_DIR
        else:
            self.base_path = Path(base_path)

        # Ensure the directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)

    @property
    def storage_root(self) -> Path:
        """Return the storage root that owns this credentials directory."""
        return self.base_path.parent

    def for_worker(self, worker_key: str) -> CredentialsManager:
        """Return a credentials manager rooted in one worker's persistent state."""
        worker_credentials_path = worker_root_path(self.storage_root, worker_key) / "credentials"
        return CredentialsManager(base_path=worker_credentials_path)

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


# Global instance for convenience (lazy initialization)
_credentials_manager: CredentialsManager | None = None


def get_credentials_manager() -> CredentialsManager:
    """Get the global credentials manager instance.

    Returns:
        The global CredentialsManager instance

    """
    global _credentials_manager
    if _credentials_manager is None:
        _credentials_manager = CredentialsManager()
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

    execution_identity = execution_identity or get_tool_execution_identity()
    if execution_identity is None:
        return None

    worker_key = resolve_worker_key(worker_scope, execution_identity, agent_name=routing_agent_name)
    if worker_key is None:
        return None

    return credentials_manager.for_worker(worker_key)


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
    if worker_scope is None:
        return manager.load_credentials(service)

    shared_credentials = manager.load_credentials(service)
    merged_credentials: dict[str, Any] = {}
    if isinstance(shared_credentials, Mapping) and (
        worker_scope == "shared" or shared_credentials.get("_source") == "env"
    ):
        merged_credentials.update(shared_credentials)

    worker_manager = _resolve_worker_credentials_manager(
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        credentials_manager=manager,
        execution_identity=execution_identity,
    )
    if worker_manager is None:
        return merged_credentials or None

    worker_credentials = worker_manager.load_credentials(service)
    if isinstance(worker_credentials, Mapping):
        merged_credentials.update(worker_credentials)
    return merged_credentials or None


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
    worker_manager = _resolve_worker_credentials_manager(
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        credentials_manager=manager,
        execution_identity=execution_identity,
    )
    target_manager = worker_manager or manager
    target_manager.save_credentials(service, credentials)
