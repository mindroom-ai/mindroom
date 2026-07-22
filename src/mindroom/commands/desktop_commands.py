"""Matrix-native self-service Desktop pairing commands."""

from __future__ import annotations

import shlex
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import runtime_matrix_homeserver
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.desktop.configuration import DesktopConfigurationStatus, desktop_configuration_state
from mindroom.desktop.credentials import (
    delete_desktop_credentials,
    load_desktop_credentials,
    save_desktop_credentials,
)
from mindroom.desktop.identity import DesktopIdentityError, controller_identity_for_entity
from mindroom.desktop.pairing import (
    DesktopPairingError,
    complete_desktop_pairing,
    confirm_desktop_pairing,
    create_desktop_pairing,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.desktop.identity import DesktopControllerIdentity


@dataclass(frozen=True, slots=True)
class DesktopCommandScope:
    """Trusted command context used to resolve one requester-agent Desktop store."""

    config: Config
    runtime_paths: RuntimePaths
    agent_name: str
    requester_id: str


def chat_pairing_desktop_error(config: Config, agent_name: str) -> str | None:
    """Return why an agent cannot own requester-scoped Desktop pairing."""
    if agent_name not in config.agents:
        return "Run this command while talking directly to a configured agent."
    if "desktop" not in config.resolve_entity(agent_name).available_tools:
        return f"Agent '{agent_name}' does not declare the Desktop tool."
    return None


def _validate_desktop_scope(scope: DesktopCommandScope) -> None:
    eligibility_error = chat_pairing_desktop_error(scope.config, scope.agent_name)
    if eligibility_error is not None:
        raise DesktopPairingError(eligibility_error)


def _load_desktop_credentials(scope: DesktopCommandScope) -> dict[str, object] | None:
    _validate_desktop_scope(scope)
    return load_desktop_credentials(
        get_runtime_credentials_manager(scope.runtime_paths),
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
    )


def _setup_response(scope: DesktopCommandScope) -> str:
    _validate_desktop_scope(scope)
    controller = controller_identity_for_entity(scope.agent_name, runtime_paths=scope.runtime_paths)
    pairing = create_desktop_pairing(
        scope.runtime_paths,
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
    )
    homeserver = scope.runtime_paths.env_value("MINDROOM_DESKTOP_MATRIX_HOMESERVER") or runtime_matrix_homeserver(
        scope.runtime_paths,
    )
    cloudflare_access = scope.runtime_paths.env_flag("MINDROOM_DESKTOP_CLOUDFLARE_ACCESS")
    setup_parts = [
        "mindroom desktop setup",
        f"--user-id {shlex.quote(scope.requester_id)}",
        f"--homeserver {shlex.quote(homeserver)}",
        f"--code {shlex.quote(pairing.token)}",
        f"--controller-user-id {shlex.quote(controller.user_id)}",
        f"--controller-device-id {shlex.quote(controller.device_id)}",
        f"--controller-ed25519 {shlex.quote(controller.ed25519)}",
    ]
    if cloudflare_access:
        setup_parts.append("--cloudflare-access")
    setup_command = " ".join(setup_parts)
    return (
        "🔐 **Desktop pairing started**\n\n"
        "On your computer, run this command. It logs in if needed, then claims the pairing:\n\n"
        f"```bash\n{setup_command}\n```\n\n"
        "Then return here and run the exact `!desktop confirm ...` command it prints.\n\n"
        "Current Desktop target remains unchanged until confirmation."
    )


def _status_response(scope: DesktopCommandScope) -> str:
    state = desktop_configuration_state(_load_desktop_credentials(scope))
    if state.status is DesktopConfigurationStatus.READY:
        return f"✅ Desktop is configured for you and agent `{scope.agent_name}`."
    if state.status is DesktopConfigurationStatus.INVALID:
        return f"⚠️ Desktop configuration is invalid: {state.error} Run `!desktop setup` to replace it."
    return f"Desktop setup is required for you and agent `{scope.agent_name}`. Run `!desktop setup`."


def _run_command(scope: DesktopCommandScope, controller: DesktopControllerIdentity) -> str:
    """Return the current local bridge command with only the app choice left open."""
    parts = [
        "mindroom desktop run",
        f"--controller-user-id {shlex.quote(controller.user_id)}",
        f"--controller-device-id {shlex.quote(controller.device_id)}",
        f"--controller-ed25519 {shlex.quote(controller.ed25519)}",
        f"--allow-requester {shlex.quote(scope.requester_id)}",
        f"--allow-agent {shlex.quote(scope.agent_name)}",
        "--allow-app APPLICATION_ID",
    ]
    return " \\\n  ".join(parts)


def _confirm_response(scope: DesktopCommandScope, token: str, verification: str) -> str:
    _validate_desktop_scope(scope)
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
    save_desktop_credentials(
        get_runtime_credentials_manager(scope.runtime_paths),
        credentials,
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
    )
    complete_desktop_pairing(scope.runtime_paths, token=token)
    controller = controller_identity_for_entity(scope.agent_name, runtime_paths=scope.runtime_paths)
    run_command = _run_command(scope, controller)
    return (
        f"✅ Desktop paired for you and agent `{scope.agent_name}`.\n\n"
        "Start the local bridge with:\n\n"
        f"```bash\n{run_command}\n```\n\n"
        "Replace `APPLICATION_ID` with one exact local application ID and repeat `--allow-app` as needed. "
        "Add `--allow-control` for a short local control lease; otherwise the bridge is observe-only."
    )


def _disconnect_response(scope: DesktopCommandScope, *, confirmed: bool) -> str:
    if not confirmed:
        return (
            f"This removes your Desktop target for agent `{scope.agent_name}`. "
            "Run `!desktop disconnect confirm` to continue."
        )
    _validate_desktop_scope(scope)
    delete_desktop_credentials(
        get_runtime_credentials_manager(scope.runtime_paths),
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
    )
    return f"✅ Desktop disconnected for you and agent `{scope.agent_name}`."


def handle_desktop_command(args_text: str, *, scope: DesktopCommandScope) -> str:
    """Execute one requester-bound Desktop setup command."""
    parts = args_text.split()
    operation = parts[0].lower() if parts else "status"
    try:
        if operation in {"setup", "rotate"} and len(parts) == 1:
            response = _setup_response(scope)
        elif operation == "status" and len(parts) <= 1:
            response = _status_response(scope)
        elif operation == "confirm" and len(parts) == 3:
            response = _confirm_response(scope, parts[1], parts[2])
        elif operation == "disconnect" and len(parts) in {1, 2}:
            response = _disconnect_response(scope, confirmed=len(parts) == 2 and parts[1].lower() == "confirm")
        else:
            response = (
                "Usage: `!desktop setup`, `!desktop status`, `!desktop confirm <code> <verification>`, "
                "`!desktop rotate`, or `!desktop disconnect [confirm]`."
            )
    except sqlite3.Error:
        return "❌ Desktop setup is temporarily unavailable. Please try again."
    except (DesktopIdentityError, DesktopPairingError, ValueError) as exc:
        return f"❌ Desktop setup failed: {exc}"
    return response


__all__ = ["DesktopCommandScope", "chat_pairing_desktop_error", "handle_desktop_command"]
