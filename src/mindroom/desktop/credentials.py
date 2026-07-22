"""Requester-agent credential storage for Matrix Desktop devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.credentials import CredentialsManager

_DESKTOP_SERVICE = "desktop"


def _desktop_credentials_manager(
    credentials_manager: CredentialsManager,
    *,
    requester_id: str,
    agent_name: str,
) -> CredentialsManager:
    """Return the exact requester-agent store for one Desktop identity."""
    return credentials_manager.for_primary_runtime_scope(requester_id, agent_name)


def load_desktop_credentials(
    credentials_manager: CredentialsManager,
    *,
    requester_id: str,
    agent_name: str,
) -> dict[str, Any] | None:
    """Load one requester's Desktop identity for one agent."""
    return _desktop_credentials_manager(
        credentials_manager,
        requester_id=requester_id,
        agent_name=agent_name,
    ).load_credentials(_DESKTOP_SERVICE)


def save_desktop_credentials(
    credentials_manager: CredentialsManager,
    credentials: dict[str, Any],
    *,
    requester_id: str,
    agent_name: str,
) -> None:
    """Save one requester's Desktop identity for one agent."""
    _desktop_credentials_manager(
        credentials_manager,
        requester_id=requester_id,
        agent_name=agent_name,
    ).save_credentials(_DESKTOP_SERVICE, credentials)


def delete_desktop_credentials(
    credentials_manager: CredentialsManager,
    *,
    requester_id: str,
    agent_name: str,
) -> None:
    """Delete one requester's Desktop identity for one agent."""
    _desktop_credentials_manager(
        credentials_manager,
        requester_id=requester_id,
        agent_name=agent_name,
    ).delete_credentials(_DESKTOP_SERVICE)


__all__ = ["delete_desktop_credentials", "load_desktop_credentials", "save_desktop_credentials"]
