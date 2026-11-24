"""Matrix-native self-service Desktop pairing commands."""

from __future__ import annotations

import json
import shlex
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import runtime_matrix_homeserver
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.desktop.configuration import (
    DesktopConfigurationStatus,
    desktop_configuration_state,
    desktop_disconnect_error,
    desktop_pairing_error,
)
from mindroom.desktop.credentials import (
    delete_desktop_credentials,
    load_desktop_credentials,
    save_desktop_credentials,
)
from mindroom.desktop.identity import DesktopIdentityError
from mindroom.desktop.pairing import (
    DesktopPairingError,
    complete_desktop_pairing,
    confirm_desktop_pairing,
    create_desktop_pairing,
)
from mindroom.desktop.protocol import DesktopSetupDescriptor
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.room_membership import cached_member_ids, room_has_only

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

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
    controller_identity: Callable[[str], DesktopControllerIdentity]


def chat_pairing_desktop_error(config: Config, agent_name: str) -> str | None:
    """Return why an agent cannot own requester-scoped Desktop pairing."""
    return desktop_pairing_error(config, agent_name)


@dataclass(frozen=True, slots=True)
class _ParsedDesktopCommand:
    operation: str
    target_agent: str | None = None
    token: str | None = None
    verification: str | None = None
    confirmed: bool = False


def _parse_desktop_args(args_text: str) -> _ParsedDesktopCommand | None:
    parts = args_text.split()
    if not parts:
        return _ParsedDesktopCommand(operation="status")
    op, *rest = parts
    result: _ParsedDesktopCommand | None = None
    match op.lower(), rest:
        case ("setup" | "rotate" | "status") as action, []:
            result = _ParsedDesktopCommand(operation=action)
        case ("setup" | "rotate" | "status") as action, [agent]:
            result = _ParsedDesktopCommand(operation=action, target_agent=agent)
        case "confirm", [token, verification]:
            result = _ParsedDesktopCommand(operation="confirm", token=token, verification=verification)
        case "confirm", [agent, token, verification]:
            result = _ParsedDesktopCommand(
                operation="confirm",
                target_agent=agent,
                token=token,
                verification=verification,
            )
        case "disconnect", []:
            result = _ParsedDesktopCommand(operation="disconnect", confirmed=False)
        case "disconnect", [arg] if arg.lower() == "confirm":
            result = _ParsedDesktopCommand(operation="disconnect", confirmed=True)
        case "disconnect", [agent]:
            result = _ParsedDesktopCommand(operation="disconnect", target_agent=agent, confirmed=False)
        case "disconnect", [agent, arg] if arg.lower() == "confirm":
            result = _ParsedDesktopCommand(operation="disconnect", target_agent=agent, confirmed=True)
    return result


def desktop_room_agent(
    config: Config,
    runtime_paths: RuntimePaths,
    room: nio.MatrixRoom,
    requester: str,
    *,
    args_text: str = "",
) -> str | None:
    """Return the Desktop agent of a room holding only the requester and MindRoom accounts.

    Without an explicit agent name in ``args_text``, the room must hold exactly one answering entity.
    With an explicit agent name (such as ``setup <agent>``), multiple MindRoom agents may be in the room,
    while any other human account is still refused.
    """
    registry = entity_identity_registry(config, runtime_paths)
    others = cached_member_ids(room) - {requester}
    names = [registry.current_entity_name_for_user_id(user_id) for user_id in others]
    if None in names or not room_has_only(config, room, {requester, *others}):
        return None
    answering = [name for name in names if name in config.agents or name in config.teams]
    parsed = _parse_desktop_args(args_text)
    target_agent = parsed.target_agent if parsed is not None else None
    eligibility_check = (
        desktop_disconnect_error
        if parsed is not None and parsed.operation == "disconnect"
        else chat_pairing_desktop_error
    )
    if target_agent is not None:
        if target_agent not in answering:
            return None
        return target_agent if eligibility_check(config, target_agent) is None else None
    if len(answering) != 1:
        return None
    return answering[0] if eligibility_check(config, answering[0]) is None else None


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


def _setup_response(scope: DesktopCommandScope, *, explicit_agent: bool = False) -> str:
    _validate_desktop_scope(scope)
    controller = scope.controller_identity(scope.agent_name)
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
        f"--allow-requester {shlex.quote(scope.requester_id)}",
        f"--allow-agent {shlex.quote(scope.agent_name)}",
    ]
    if cloudflare_access:
        setup_parts.append("--cloudflare-access")
    setup_command = " ".join(setup_parts)
    descriptor = DesktopSetupDescriptor(
        homeserver=homeserver,
        user_id=scope.requester_id,
        code=pairing.token,
        controller_user_id=controller.user_id,
        controller_device_id=controller.device_id,
        controller_ed25519=controller.ed25519,
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
        cloudflare_access=cloudflare_access,
    )
    return (
        "🔐 **Desktop pairing started**\n\n"
        "In the MindRoom macOS app, open Computer access > Connect and import this setup data. "
        "Review the controller, requester, and agent, then select Save and Connect:\n\n"
        f"```json\n{json.dumps(descriptor.to_content(), indent=2)}\n```\n\n"
        "For terminal setup, run this command on your computer. It logs in if needed, claims the pairing, and saves setup for both the terminal and app:\n\n"
        f"```bash\n{setup_command}\n```\n\n"
        + (
            f"Then return here and run `/desktop confirm {scope.agent_name} <code> <verification>` "
            "using the code and verification it prints.\n\n"
            if explicit_agent
            else "Then return here and run the exact `!desktop confirm ...` command it prints.\n\n"
        )
        + "Current Desktop target remains unchanged until confirmation."
    )


def _status_response(scope: DesktopCommandScope) -> str:
    state = desktop_configuration_state(_load_desktop_credentials(scope))
    if state.status is DesktopConfigurationStatus.READY:
        return f"✅ Desktop is configured for you and agent `{scope.agent_name}`."
    if state.status is DesktopConfigurationStatus.INVALID:
        return f"⚠️ Desktop configuration is invalid: {state.error} Run `/desktop setup` to replace it."
    return f"Desktop setup is required for you and agent `{scope.agent_name}`. Run `/desktop setup`."


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
    controller = scope.controller_identity(scope.agent_name)
    run_command = _run_command(scope, controller)
    save_desktop_credentials(
        get_runtime_credentials_manager(scope.runtime_paths),
        credentials,
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
    )
    complete_desktop_pairing(scope.runtime_paths, token=token)
    return (
        f"✅ Desktop paired for you and agent `{scope.agent_name}`.\n\n"
        "If you connected in the macOS app, select I've Confirmed in Chat. "
        "Choose and save apps in MindRoom > Computer access > Apps, complete Permissions, then select Start Observe Only or run "
        "`mindroom desktop run`. Both use the same saved setup. Stop one before starting the other.\n\n"
        "To override app access for one terminal run, use:\n\n"
        f"```bash\n{run_command}\n```\n\n"
        "Replace `APPLICATION_ID` with one exact local application ID and repeat `--allow-app` as needed. "
        "Add `--allow-control` for a short local control lease; otherwise the bridge is observe-only. "
        "If setup used `--config` or `--storage-path`, add the same option to this command."
    )


def _disconnect_response(
    scope: DesktopCommandScope,
    *,
    confirmed: bool,
    explicit_agent: bool = False,
) -> str:
    if not confirmed:
        confirm_cmd = (
            f"/desktop disconnect {scope.agent_name} confirm" if explicit_agent else "/desktop disconnect confirm"
        )
        return f"This removes your Desktop target for agent `{scope.agent_name}`. Run `{confirm_cmd}` to continue."
    eligibility_error = desktop_disconnect_error(scope.config, scope.agent_name)
    if eligibility_error is not None:
        raise DesktopPairingError(eligibility_error)
    delete_desktop_credentials(
        get_runtime_credentials_manager(scope.runtime_paths),
        requester_id=scope.requester_id,
        agent_name=scope.agent_name,
    )
    return f"✅ Desktop disconnected for you and agent `{scope.agent_name}`."


def handle_desktop_command(args_text: str, *, scope: DesktopCommandScope) -> str:
    """Execute one requester-bound Desktop setup command."""
    parsed = _parse_desktop_args(args_text)
    if parsed is None:
        return (
            "Usage: `/desktop setup [agent]`, `/desktop status [agent]`, "
            "`/desktop confirm [agent] <code> <verification>`, "
            "`/desktop rotate [agent]`, or `/desktop disconnect [agent] [confirm]`."
        )
    if parsed.target_agent is not None and parsed.target_agent != scope.agent_name:
        return (
            f"❌ Desktop setup failed: Desktop command targets agent '{parsed.target_agent}', not '{scope.agent_name}'."
        )
    try:
        if parsed.operation in {"setup", "rotate"}:
            response = _setup_response(scope, explicit_agent=parsed.target_agent is not None)
        elif parsed.operation == "status":
            response = _status_response(scope)
        elif parsed.operation == "confirm":
            assert parsed.token is not None
            assert parsed.verification is not None
            response = _confirm_response(scope, parsed.token, parsed.verification)
        else:
            response = _disconnect_response(
                scope,
                confirmed=parsed.confirmed,
                explicit_agent=parsed.target_agent is not None,
            )
    except sqlite3.Error:
        return "❌ Desktop setup is temporarily unavailable. Please try again."
    except (DesktopIdentityError, DesktopPairingError, ValueError) as exc:
        return f"❌ Desktop setup failed: {exc}"
    return response


__all__ = ["DesktopCommandScope", "chat_pairing_desktop_error", "desktop_room_agent", "handle_desktop_command"]
