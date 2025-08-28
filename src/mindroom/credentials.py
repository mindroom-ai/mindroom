"""Unified credentials management for MindRoom.

This module provides centralized credential storage and retrieval for all integrations,
used by both agents and the widget interface.
"""

import json
from pathlib import Path
from typing import Any

from .constants import CREDENTIALS_DIR


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

    def get_credentials_path(self, service: str) -> Path:
        """Get the path for a service's credentials file.

        Args:
            service: Name of the service (e.g., 'google', 'homeassistant')

        Returns:
            Path to the credentials file

        """
        return self.base_path / f"{service}_credentials.json"

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
