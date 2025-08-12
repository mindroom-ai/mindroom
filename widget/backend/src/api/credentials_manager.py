"""Unified credentials management for all integrations."""

import json
from pathlib import Path
from typing import Any


class CredentialsManager:
    """Centralized credentials storage and retrieval."""

    def __init__(self, base_path: Path | None = None):
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

        # Also maintain backward compatibility paths
        self.legacy_paths = {
            "google": Path(__file__).parent.parent.parent.parent.parent / "google_token.json",
            "gmail": Path(__file__).parent.parent.parent.parent.parent / "token.json",
            "homeassistant": Path(__file__).parent.parent.parent.parent.parent / "homeassistant_token.json",
        }

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
        # First check the unified location
        unified_path = self.get_credentials_path(service)
        if unified_path.exists():
            try:
                with unified_path.open() as f:
                    return json.load(f)
            except Exception:
                pass

        # Check legacy paths for backward compatibility
        if service in self.legacy_paths:
            legacy_path = self.legacy_paths[service]
            if legacy_path.exists():
                try:
                    with legacy_path.open() as f:
                        data = json.load(f)
                    # Migrate to unified location
                    self.save_credentials(service, data)
                    # Optionally remove the legacy file (commented out for safety)
                    # legacy_path.unlink()
                    return data
                except Exception:
                    pass

        # Check root directory with pattern {service}_credentials.json
        root_path = Path(__file__).parent.parent.parent.parent.parent / f"{service}_credentials.json"
        if root_path.exists():
            try:
                with root_path.open() as f:
                    data = json.load(f)
                # Migrate to unified location
                self.save_credentials(service, data)
                return data
            except Exception:
                pass

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

        # Also update legacy paths for backward compatibility if they exist
        # This ensures tools that still look in old locations continue to work
        if service == "google" and "token" in credentials:
            # Save Google token to legacy location
            legacy_google = self.legacy_paths.get("google")
            if legacy_google and legacy_google.parent.exists():
                with legacy_google.open("w") as f:
                    json.dump(credentials, f, indent=2)

            # Also save Gmail-specific token for agno compatibility
            legacy_gmail = self.legacy_paths.get("gmail")
            if legacy_gmail and legacy_gmail.parent.exists():
                gmail_creds = {
                    "token": credentials.get("token"),
                    "refresh_token": credentials.get("refresh_token"),
                    "token_uri": credentials.get("token_uri"),
                    "client_id": credentials.get("client_id"),
                    "client_secret": credentials.get("client_secret"),
                    "scopes": [
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.modify",
                        "https://www.googleapis.com/auth/gmail.compose",
                    ],
                }
                with legacy_gmail.open("w") as f:
                    json.dump(gmail_creds, f, indent=2)

    def delete_credentials(self, service: str) -> None:
        """Delete credentials for a service.

        Args:
            service: Name of the service

        """
        # Delete from unified location
        credentials_path = self.get_credentials_path(service)
        if credentials_path.exists():
            credentials_path.unlink()

        # Also delete from legacy locations if they exist
        if service in self.legacy_paths:
            legacy_path = self.legacy_paths[service]
            if legacy_path.exists():
                legacy_path.unlink()

        # Delete from root directory pattern
        root_path = Path(__file__).parent.parent.parent.parent.parent / f"{service}_credentials.json"
        if root_path.exists():
            root_path.unlink()

    def list_services(self) -> list[str]:
        """List all services with stored credentials.

        Returns:
            List of service names

        """
        services = set()

        # Check unified location
        if self.base_path.exists():
            for path in self.base_path.glob("*_credentials.json"):
                service = path.stem.replace("_credentials", "")
                services.add(service)

        # Check legacy locations
        for service, path in self.legacy_paths.items():
            if path.exists():
                services.add(service)

        return sorted(list(services))


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
