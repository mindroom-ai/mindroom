"""Machine-local Matrix desktop command processor."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.desktop.media import DesktopMediaError, upload_encrypted_screenshot
from mindroom.desktop.protocol import (
    DESKTOP_COMMAND_EVENT_TYPE,
    DESKTOP_CONTROL_ACTIONS,
    DESKTOP_RESPONSE_EVENT_TYPE,
    DesktopCommand,
    DesktopProtocolError,
    DesktopResponse,
    EncryptedDesktopMedia,
    event_content,
)
from mindroom.desktop.provider import DesktopEmergencyStopError, DesktopProvider, DesktopProviderError
from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    PinnedMatrixDevice,
    authenticated_sender_matches,
    send_encrypted_to_device,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.matrix.to_device import AuthenticatedToDeviceEvent

logger = get_logger(__name__)

_MAX_REPLAY_RESPONSES = 1024
_MAX_TRACKED_SESSIONS = 128
_MAX_FUTURE_SKEW_MS = 30_000


@dataclass(frozen=True, slots=True)
class DesktopBridgePolicy:
    """Local authority for one running desktop bridge process."""

    controller: PinnedMatrixDevice
    allowed_requester_ids: frozenset[str]
    allowed_agent_names: frozenset[str]
    allow_control: bool = False
    control_lease_expires_at_ms: int | None = None

    def __post_init__(self) -> None:
        """Require explicit caller and agent allowlists."""
        if not self.allowed_requester_ids:
            msg = "Desktop bridge requires at least one allowed requester Matrix ID."
            raise ValueError(msg)
        if not self.allowed_agent_names:
            msg = "Desktop bridge requires at least one allowed agent name."
            raise ValueError(msg)
        if self.allow_control and self.control_lease_expires_at_ms is None:
            msg = "Control-enabled desktop bridge requires a lease expiry."
            raise ValueError(msg)
        if not self.allow_control and self.control_lease_expires_at_ms is not None:
            msg = "Observe-only desktop bridge cannot carry a control lease expiry."
            raise ValueError(msg)

    def caller_allowed(self, command: DesktopCommand) -> bool:
        """Return whether local static policy admits the human and agent provenance."""
        return command.requester_id in self.allowed_requester_ids and command.agent_name in self.allowed_agent_names


@dataclass
class DesktopBridge:
    """Validate, execute, and answer pinned encrypted desktop commands."""

    client: nio.AsyncClient
    provider: DesktopProvider
    policy: DesktopBridgePolicy
    clock: Callable[[], float] = time.time
    monotonic_clock: Callable[[], float] = time.monotonic
    _responses: OrderedDict[str, DesktopResponse] = field(default_factory=OrderedDict, init=False)
    _sequence_high_watermarks: OrderedDict[str, int] = field(default_factory=OrderedDict, init=False)
    _in_flight: set[str] = field(default_factory=set, init=False)
    _execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _control_revoked: bool = field(default=False, init=False)
    _control_lease_deadline: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Convert the wall-clock lease label into a rollback-safe local deadline."""
        if self.policy.control_lease_expires_at_ms is None:
            return
        remaining_seconds = max(0.0, self.policy.control_lease_expires_at_ms / 1000 - self.clock())
        self._control_lease_deadline = self.monotonic_clock() + remaining_seconds

    async def on_to_device_event(self, event: AuthenticatedToDeviceEvent) -> None:
        """Handle one authenticated custom to-device event without trusting its payload."""
        if event.type != DESKTOP_COMMAND_EVENT_TYPE:
            return
        if not authenticated_sender_matches(self.client, event, self.policy.controller):
            logger.warning(
                "desktop_command_sender_rejected",
                sender=event.sender,
                device_id=event.authenticated_device_id,
            )
            return
        try:
            command = DesktopCommand.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_command_malformed", reason=str(exc))
            return

        cached = self._responses.get(command.request_id)
        if cached is not None:
            logger.info(
                "desktop_command_replayed",
                request_id=command.request_id,
                action=command.action,
                requester_id=command.requester_id,
                agent_name=command.agent_name,
                ok=cached.ok,
            )
            await self._send_response(cached)
            return
        if command.request_id in self._in_flight:
            return

        self._in_flight.add(command.request_id)
        try:
            async with self._execution_lock:
                response = await self._process(command)
            self._remember_response(response)
            logger.info(
                "desktop_command_completed",
                request_id=command.request_id,
                action=command.action,
                requester_id=command.requester_id,
                agent_name=command.agent_name,
                ok=response.ok,
                partial=bool(response.result.get("warning")),
            )
            await self._send_response(response)
        finally:
            self._in_flight.discard(command.request_id)

    async def _process(self, command: DesktopCommand) -> DesktopResponse:
        policy_error = self._policy_error(command)
        if policy_error is not None:
            return self._error_response(command, policy_error)
        sequence_error = self._record_sequence(command)
        if sequence_error is not None:
            return self._error_response(command, sequence_error)
        execution = await self._execute_safely(command)
        if isinstance(execution, DesktopResponse):
            return execution
        result, should_capture = execution
        if not should_capture:
            return self._success_response(command, result=result)
        return await self._capture_response(command, result=result)

    async def _execute_safely(
        self,
        command: DesktopCommand,
    ) -> tuple[dict[str, object], bool] | DesktopResponse:
        try:
            return await self._execute(command)
        except DesktopEmergencyStopError as exc:
            self._control_revoked = True
            return self._error_response(command, str(exc))
        except (DesktopProviderError, DesktopProtocolError) as exc:
            return self._error_response(command, str(exc))
        except Exception:
            logger.exception(
                "desktop_command_execution_failed",
                request_id=command.request_id,
                action=command.action,
            )
            if command.action in DESKTOP_CONTROL_ACTIONS:
                return self._unknown_control_response(command)
            return self._error_response(command, "Local desktop operation failed.")

    async def _capture_response(
        self,
        command: DesktopCommand,
        *,
        result: dict[str, object],
    ) -> DesktopResponse:
        try:
            capture = await asyncio.to_thread(self.provider.screenshot)
            screenshot = await upload_encrypted_screenshot(
                self.client,
                capture.content,
                mime_type=capture.mime_type,
                filename=f"desktop-{command.request_id}.jpg",
            )
        except (DesktopProviderError, DesktopMediaError) as exc:
            return self._capture_error_response(command, result=result, error=str(exc))
        except Exception:
            logger.exception("desktop_follow_up_screenshot_failed", action=command.action)
            return self._capture_error_response(command, result=result, error="Local screenshot operation failed.")
        return self._success_response(
            command,
            result={
                **result,
                "screen": {"width": capture.screen_width, "height": capture.screen_height},
                "image": {"width": capture.image_width, "height": capture.image_height},
            },
            screenshot=screenshot,
        )

    def _policy_error(self, command: DesktopCommand) -> str | None:
        now_ms = round(self.clock() * 1000)
        if command.issued_at_ms > now_ms + _MAX_FUTURE_SKEW_MS:
            error = "Desktop command was issued too far in the future."
        elif command.expires_at_ms < now_ms:
            error = "Desktop command expired before local execution."
        elif not self.policy.caller_allowed(command):
            error = "Desktop command requester or agent is not allowed by local policy."
        elif command.action not in DESKTOP_CONTROL_ACTIONS:
            error = None
        elif not self.policy.allow_control:
            error = "Desktop control is disabled; this bridge is observe-only."
        elif self._control_revoked:
            error = "Desktop emergency stop is latched; restart the bridge locally before granting control again."
        elif not self._control_available():
            error = "Local desktop control lease has expired."
        else:
            error = None
        return error

    def _record_sequence(self, command: DesktopCommand) -> str | None:
        previous = self._sequence_high_watermarks.get(command.session_id)
        if previous is not None and command.sequence <= previous:
            return "Desktop command sequence was already used or arrived out of order."
        self._sequence_high_watermarks[command.session_id] = command.sequence
        self._sequence_high_watermarks.move_to_end(command.session_id)
        while len(self._sequence_high_watermarks) > _MAX_TRACKED_SESSIONS:
            self._sequence_high_watermarks.popitem(last=False)
        return None

    async def _execute(self, command: DesktopCommand) -> tuple[dict[str, object], bool]:
        parameters = command.parameters
        if command.action == "status":
            _reject_unexpected_parameters(parameters, allowed=frozenset())
            status = await asyncio.to_thread(self.provider.status)
            return {**status, "bridge": self._bridge_status()}, False
        if command.action == "screenshot":
            _reject_unexpected_parameters(parameters, allowed=frozenset())
            return {"action": command.action}, True
        if command.action == "click":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"x", "y", "button"}))
            await asyncio.to_thread(
                self.provider.click,
                x=_required_int_parameter(parameters, "x"),
                y=_required_int_parameter(parameters, "y"),
                button=_optional_str_parameter(parameters, "button", default="left"),
            )
        elif command.action == "type_text":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"text"}))
            await asyncio.to_thread(self.provider.type_text, text=_required_str_parameter(parameters, "text"))
        elif command.action == "scroll":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"clicks", "x", "y"}))
            await asyncio.to_thread(
                self.provider.scroll,
                clicks=_required_int_parameter(parameters, "clicks"),
                x=_optional_int_parameter(parameters, "x"),
                y=_optional_int_parameter(parameters, "y"),
            )
        elif command.action == "keypress":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"keys"}))
            await asyncio.to_thread(self.provider.keypress, keys=_required_str_list_parameter(parameters, "keys"))
        else:
            msg = f"Unsupported desktop action: {command.action}."
            raise DesktopProtocolError(msg)

        return {"action": command.action, "action_completed": True}, True

    def _bridge_status(self) -> dict[str, object]:
        control_available = self._control_available()
        status: dict[str, object] = {
            "mode": "control" if control_available else "observe_only",
            "control_available": control_available,
            "emergency_stop_latched": self._control_revoked,
        }
        if self.policy.control_lease_expires_at_ms is not None:
            status["control_lease_expires_at_ms"] = self.policy.control_lease_expires_at_ms
        return status

    def _control_available(self) -> bool:
        return (
            self.policy.allow_control
            and not self._control_revoked
            and self._control_lease_deadline is not None
            and self.monotonic_clock() <= self._control_lease_deadline
        )

    def _capture_error_response(
        self,
        command: DesktopCommand,
        *,
        result: dict[str, object],
        error: str,
    ) -> DesktopResponse:
        if command.action not in DESKTOP_CONTROL_ACTIONS:
            return self._error_response(command, error)
        warning = (
            "The desktop action completed, but its follow-up screenshot failed; do not repeat the action automatically. "
            "Request status or a screenshot before deciding the next step."
        )
        logger.warning(
            "desktop_control_follow_up_screenshot_failed",
            request_id=command.request_id,
            action=command.action,
            requester_id=command.requester_id,
            agent_name=command.agent_name,
        )
        return self._success_response(
            command,
            result={**result, "warning": warning, "follow_up_screenshot": "failed"},
        )

    def _unknown_control_response(self, command: DesktopCommand) -> DesktopResponse:
        warning = (
            "The desktop action outcome is unknown and it may have completed; do not repeat the action automatically. "
            "Request status or a screenshot before deciding the next step."
        )
        logger.warning(
            "desktop_control_outcome_unknown",
            request_id=command.request_id,
            action=command.action,
            requester_id=command.requester_id,
            agent_name=command.agent_name,
        )
        return self._success_response(
            command,
            result={"action": command.action, "action_outcome": "unknown", "warning": warning},
        )

    def _remember_response(self, response: DesktopResponse) -> None:
        self._responses[response.request_id] = response
        self._responses.move_to_end(response.request_id)
        while len(self._responses) > _MAX_REPLAY_RESPONSES:
            self._responses.popitem(last=False)

    async def _send_response(self, response: DesktopResponse) -> None:
        try:
            await send_encrypted_to_device(
                self.client,
                self.policy.controller,
                event_type=DESKTOP_RESPONSE_EVENT_TYPE,
                content=response.to_content(),
            )
        except OlmToDeviceError:
            logger.exception("desktop_response_delivery_failed", request_id=response.request_id)

    @staticmethod
    def _error_response(command: DesktopCommand, error: str) -> DesktopResponse:
        return DesktopResponse(
            request_id=command.request_id,
            session_id=command.session_id,
            ok=False,
            error=error,
        )

    @staticmethod
    def _success_response(
        command: DesktopCommand,
        *,
        result: dict[str, object],
        screenshot: EncryptedDesktopMedia | None = None,
    ) -> DesktopResponse:
        return DesktopResponse(
            request_id=command.request_id,
            session_id=command.session_id,
            ok=True,
            result=result,
            screenshot=screenshot,
        )


def _reject_unexpected_parameters(parameters: dict[str, object], *, allowed: frozenset[str]) -> None:
    unexpected = sorted(set(parameters) - allowed)
    if unexpected:
        msg = f"Unexpected desktop parameters: {', '.join(unexpected)}."
        raise DesktopProtocolError(msg)


def _required_int_parameter(parameters: dict[str, object], key: str) -> int:
    value = parameters.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Desktop parameter {key} must be an integer."
        raise DesktopProtocolError(msg)
    return value


def _optional_int_parameter(parameters: dict[str, object], key: str) -> int | None:
    if key not in parameters:
        return None
    return _required_int_parameter(parameters, key)


def _required_str_parameter(parameters: dict[str, object], key: str) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or not value:
        msg = f"Desktop parameter {key} must be a non-empty string."
        raise DesktopProtocolError(msg)
    return value


def _optional_str_parameter(parameters: dict[str, object], key: str, *, default: str) -> str:
    if key not in parameters:
        return default
    return _required_str_parameter(parameters, key)


def _required_str_list_parameter(parameters: dict[str, object], key: str) -> list[str]:
    value = parameters.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        msg = f"Desktop parameter {key} must be a list of strings."
        raise DesktopProtocolError(msg)
    return [item for item in value if isinstance(item, str)]


__all__ = ["DesktopBridge", "DesktopBridgePolicy"]
