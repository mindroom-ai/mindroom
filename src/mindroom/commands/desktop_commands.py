"""Matrix-native self-service Desktop pairing commands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.credentials import (
    delete_scoped_credentials,
    get_runtime_credentials_manager,
    load_scoped_credentials,
    save_scoped_credentials,
)
from mindroom.desktop.configuration import DesktopConfigurationStatus, desktop_configuration_state
from mindroom.desktop.identity import DesktopIdentityError, controller_identity_for_entity
from mindroom.desktop.pairing import (
    DesktopPairingError,
    complete_desktop_pairing,
    confirm_desktop_pairing,
    create_desktop_pairing,
)
from mindroom.session_ids import create_session_id
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    build_agent_toolkit_worker_target,
    build_tool_execution_identity,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True, slots=True)
class DesktopCommandScope:
    """Trusted command context used to resolve one private Desktop store."""

    config: Config
    runtime_paths: RuntimePaths
    agent_name: str
    requester_id: str
    room_id: str
    thread_id: str | None


def _desktop_worker_target(scope: DesktopCommandScope) -> ResolvedWorkerTarget:
    if scope.agent_name not in scope.config.agents:
        msg = "Run this command while talking directly to a configured agent."
        raise DesktopPairingError(msg)
    entity = scope.config.resolve_entity(scope.agent_name)
    if "desktop" not in entity.available_tools:
        msg = f"Agent '{scope.agent_name}' does not declare the Desktop tool."
        raise DesktopPairingError(msg)
    execution_scope = entity.execution_scope
    agent_config = scope.config.get_agent(scope.agent_name)
    if agent_config.private is None or execution_scope != "user_agent":
        msg = "Chat pairing requires private.per: user_agent."
        raise DesktopPairingError(msg)
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=scope.agent_name,
        runtime_paths=scope.runtime_paths,
        requester_id=scope.requester_id,
        room_id=scope.room_id,
        thread_id=scope.thread_id,
        resolved_thread_id=scope.thread_id,
        session_id=create_session_id(scope.room_id, scope.thread_id),
    )
    return build_agent_toolkit_worker_target(
        execution_scope,
        scope.agent_name,
        is_private=True,
        execution_identity=identity,
        runtime_paths=scope.runtime_paths,
    )


def _load_desktop_credentials(scope: DesktopCommandScope) -> dict[str, object] | None:
    return load_scoped_credentials(
        "desktop",
        credentials_manager=get_runtime_credentials_manager(scope.runtime_paths),
        worker_target=_desktop_worker_target(scope),
        allowed_shared_services=frozenset(),
    )


def _setup_response(scope: DesktopCommandScope) -> str:
    _desktop_worker_target(scope)
    controller = controller_identity_for_entity(scope.agent_name, runtime_paths=scope.runtime_paths)
    pairing = create_desktop_pairing(
        scope.runtime_paths,
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
        room_id=scope.room_id,
        thread_id=scope.thread_id,
    )
    command = " ".join(
        (
            "mindroom desktop pair",
            f"--code {shlex.quote(pairing.token)}",
            f"--controller-user-id {shlex.quote(controller.user_id)}",
            f"--controller-device-id {shlex.quote(controller.device_id)}",
            f"--controller-ed25519 {shlex.quote(controller.ed25519)}",
        ),
    )
    return (
        "🔐 **Desktop pairing started**\n\n"
        "Run this on your computer after `mindroom desktop login`:\n\n"
        f"```bash\n{command}\n```\n\n"
        "Then return here and run the exact `!desktop confirm ...` command printed by the local pairing command.\n\n"
        "Current Desktop target remains unchanged until confirmation."
    )


def _status_response(scope: DesktopCommandScope) -> str:
    state = desktop_configuration_state(_load_desktop_credentials(scope))
    if state.status is DesktopConfigurationStatus.READY:
        return f"✅ Desktop is configured for you and agent `{scope.agent_name}`."
    if state.status is DesktopConfigurationStatus.INVALID:
        return f"⚠️ Desktop configuration is invalid: {state.error} Run `!desktop setup` to replace it."
    return f"Desktop setup is required for you and agent `{scope.agent_name}`. Run `!desktop setup`."


def _confirm_response(scope: DesktopCommandScope, token: str, verification: str) -> str:
    worker_target = _desktop_worker_target(scope)
    pending = confirm_desktop_pairing(
        scope.runtime_paths,
        token=token,
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
        verification=verification,
    )
    assert pending.device_user_id is not None
    assert pending.device_id is not None
    assert pending.device_ed25519 is not None
    credentials: dict[str, object] = {
        "device_user_id": pending.device_user_id,
        "device_id": pending.device_id,
        "device_ed25519": pending.device_ed25519,
    }
    state = desktop_configuration_state(credentials)
    if state.status is not DesktopConfigurationStatus.READY:
        raise DesktopPairingError(state.error or "Claimed Desktop device identity is invalid.")
    save_scoped_credentials(
        "desktop",
        credentials,
        credentials_manager=get_runtime_credentials_manager(scope.runtime_paths),
        worker_target=worker_target,
    )
    complete_desktop_pairing(scope.runtime_paths, token=token)
    return (
        f"✅ Desktop paired for you and agent `{scope.agent_name}`. "
        "Start the local bridge with exact requester, agent, application, and lease allowlists."
    )


def _disconnect_response(scope: DesktopCommandScope, *, confirmed: bool) -> str:
    if not confirmed:
        return (
            f"This removes your Desktop target for agent `{scope.agent_name}`. "
            "Run `!desktop disconnect confirm` to continue."
        )
    delete_scoped_credentials(
        "desktop",
        credentials_manager=get_runtime_credentials_manager(scope.runtime_paths),
        worker_target=_desktop_worker_target(scope),
    )
    return f"✅ Desktop disconnected for you and agent `{scope.agent_name}`."


def handle_desktop_command(args_text: str, *, scope: DesktopCommandScope) -> str:
    """Execute one requester-bound Desktop setup command."""
    parts = args_text.split()
    operation = parts[0].lower() if parts else "status"
    try:
        if operation in {"setup", "rotate"} and len(parts) == 1:
            return _setup_response(scope)
        if operation == "status" and len(parts) == 1:
            return _status_response(scope)
        if operation == "confirm" and len(parts) == 3:
            return _confirm_response(scope, parts[1], parts[2])
        if operation == "disconnect" and len(parts) in {1, 2}:
            return _disconnect_response(scope, confirmed=len(parts) == 2 and parts[1].lower() == "confirm")
    except (DesktopIdentityError, DesktopPairingError, ValueError) as exc:
        return f"❌ Desktop setup failed: {exc}"
    return (
        "Usage: `!desktop setup`, `!desktop status`, `!desktop confirm <code> <verification>`, "
        "`!desktop rotate`, or `!desktop disconnect [confirm]`."
    )


__all__ = ["DesktopCommandScope", "handle_desktop_command"]
