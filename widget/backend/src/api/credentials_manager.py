"""Unified credentials management for all integrations."""

import json
from pathlib import Path
from typing import Any


class CredentialsManager:
    """Centralized credentials storage and retrieval."""

    def __init__(self, base_path: Path | None = None) -> None:
        """Initialize the credentials manager.

        Args:
            base_path: Base directory for storing credentials.
                      Defaults to ~/.mindroom/credentials/

        """
        if base_path is None:
            # Use a dedicated credentials directory in the user's home
            self.base_path = Path.home() / ".mindroom" / "credentials"
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
                    return json.load(f)
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


# Global instance for convenience
_credentials_manager = CredentialsManager()


def get_credentials(service: str) -> dict[str, Any] | None:
    """Get credentials for a service using the global manager.

    Args:
        service: Name of the service

    Returns:
        Credentials dictionary or None if not found

    """
    return _credentials_manager.load_credentials(service)


def save_credentials(service: str, credentials: dict[str, Any]) -> None:
    """Save credentials for a service using the global manager.

    Args:
        service: Name of the service
        credentials: Credentials dictionary to save

    """
    _credentials_manager.save_credentials(service, credentials)


def delete_credentials(service: str) -> None:
    """Delete credentials for a service using the global manager.

    Args:
        service: Name of the service

    """
    _credentials_manager.delete_credentials(service)


def list_configured_services() -> list[str]:
    """List all services with stored credentials.

    Returns:
        List of service names

    """
    return _credentials_manager.list_services()
