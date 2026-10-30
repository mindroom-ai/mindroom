"""Requester-agent credential storage for Matrix Desktop devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.credentials.manager import credential_layers
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from mindroom.credentials.manager import CredentialsManager

_DESKTOP_SERVICE = "desktop"


def _desktop_credentials_manager(
    credentials_manager: CredentialsManager,
    *,
    requester_id: str,
    agent_name: str,
) -> CredentialsManager:
    """Return the store the credential policy names for one requester's Desktop identity with one agent."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    target = resolve_worker_target(None, agent_name, execution_identity=identity)
    layers = credential_layers(_DESKTOP_SERVICE, credentials_manager=credentials_manager, worker_target=target)
    return layers.writable_store()


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
