"""Machine-local Matrix desktop command processor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.desktop.accessibility import (
    AccessibilityActionOutcomeUnknownError,
    AccessibilityError,
)
from mindroom.desktop.media import DesktopMediaError, upload_encrypted_screenshot
from mindroom.desktop.playwright_mcp import (
    BrowserImage,
    BrowserProvider,
    PlaywrightActionOutcomeUnknownError,
    PlaywrightBrowserError,
    browser_action_requires_control,
)
from mindroom.desktop.protocol import (
    DESKTOP_APP_ACTIONS,
    DESKTOP_BROWSER_ACTIONS,
    DESKTOP_COMMAND_EVENT_TYPE,
    DESKTOP_CONTROL_ACTIONS,
    DESKTOP_RESPONSE_EVENT_TYPE,
    DESKTOP_SAFE_KEYS,
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
_MAX_PARAMETER_TEXT_LENGTH = 2_000
_MAX_PARAMETER_IDENTIFIER_LENGTH = 256


@dataclass(frozen=True, slots=True)
class DesktopBridgePolicy:
    """Local authority for one running desktop bridge process."""

    controller: PinnedMatrixDevice
    allowed_requester_ids: frozenset[str]
    allowed_agent_names: frozenset[str]
    allowed_app_ids: frozenset[str]
    allow_control: bool = False
    control_lease_expires_at_ms: int | None = None
    browser_enabled: bool = False

    def __post_init__(self) -> None:
        """Require explicit caller, agent, and application allowlists."""
        if not self.allowed_requester_ids:
            msg = "Desktop bridge requires at least one allowed requester Matrix ID."
            raise ValueError(msg)
        if not self.allowed_agent_names:
            msg = "Desktop bridge requires at least one allowed agent name."
            raise ValueError(msg)
        if not self.allowed_app_ids:
            msg = "Desktop bridge requires at least one allowed application ID."
            raise ValueError(msg)
        if any(not value.strip() for value in self.allowed_requester_ids):
            msg = "Desktop bridge requester IDs must not be empty."
            raise ValueError(msg)
        if any(not value.strip() for value in self.allowed_agent_names):
            msg = "Desktop bridge agent names must not be empty."
            raise ValueError(msg)
        if any(not value.strip() for value in self.allowed_app_ids):
            msg = "Desktop bridge application IDs must not be empty."
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


@dataclass(frozen=True, slots=True)
class _Execution:
    result: dict[str, object]
    capture_app: str | None = None
    capture_state_id: str | None = None
    follow_up_app: str | None = None
    browser_image: BrowserImage | None = None


@dataclass(frozen=True, slots=True)
class _CachedDesktopResponse:
    command_fingerprint: str
    response: DesktopResponse


@dataclass
class DesktopBridge:
    """Validate, execute, and answer pinned encrypted desktop commands."""

    client: nio.AsyncClient
    provider: DesktopProvider
    policy: DesktopBridgePolicy
    browser_provider: BrowserProvider | None = None
    clock: Callable[[], float] = time.time
    monotonic_clock: Callable[[], float] = time.monotonic
    _responses: OrderedDict[str, _CachedDesktopResponse] = field(default_factory=OrderedDict, init=False)
    _sequence_high_watermarks: OrderedDict[str, int] = field(default_factory=OrderedDict, init=False)
    _in_flight: set[str] = field(default_factory=set, init=False)
    _execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _control_revoked: bool = field(default=False, init=False)
    _control_lease_deadline: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Convert the wall-clock lease label into a rollback-safe local deadline."""
        if self.policy.browser_enabled != (self.browser_provider is not None):
            msg = "Desktop browser policy and local browser provider must be enabled together."
            raise ValueError(msg)
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
            if cached.command_fingerprint != _command_fingerprint(command):
                logger.warning(
                    "desktop_command_request_id_reused",
                    request_id=command.request_id,
                    action=command.action,
                    requester_id=command.requester_id,
                    agent_name=command.agent_name,
                )
                await self._send_response(
                    self._error_response(command, "Desktop request ID was reused with different command content."),
                )
                return
            logger.info(
                "desktop_command_replayed",
                request_id=command.request_id,
                action=command.action,
                requester_id=command.requester_id,
                agent_name=command.agent_name,
                ok=cached.response.ok,
            )
            await self._send_response(cached.response)
            return
        if command.request_id in self._in_flight:
            return

        self._in_flight.add(command.request_id)
        try:
            async with self._execution_lock:
                response = await self._process(command)
            self._remember_response(command, response)
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

    async def _process(self, command: DesktopCommand) -> DesktopResponse:  # noqa: PLR0911
        policy_error = self._policy_error(command)
        if policy_error is not None:
            return self._error_response(command, policy_error)
        sequence_error = self._record_sequence(command)
        if sequence_error is not None:
            return self._error_response(command, sequence_error)
        execution = await self._execute_safely(command)
        if isinstance(execution, DesktopResponse):
            return execution
        if execution.follow_up_app is not None:
            execution = await self._attach_follow_up_state(command, execution)
            if isinstance(execution, DesktopResponse):
                return execution
        if execution.browser_image is not None:
            return await self._browser_image_response(command, execution)
        if execution.capture_app is None or execution.capture_state_id is None:
            return self._success_response(command, result=execution.result)
        return await self._capture_response(
            command,
            result=execution.result,
            app_id=execution.capture_app,
            state_id=execution.capture_state_id,
        )

    async def _browser_image_response(self, command: DesktopCommand, execution: _Execution) -> DesktopResponse:
        image = execution.browser_image
        if image is None:
            return self._success_response(command, result=execution.result)
        extension = "png" if image.mime_type == "image/png" else "jpg"
        try:
            screenshot = await upload_encrypted_screenshot(
                self.client,
                image.content,
                mime_type=image.mime_type,
                filename=f"browser-{command.request_id}.{extension}",
            )
        except DesktopMediaError as exc:
            return self._capture_error_response(command, result=execution.result, error=str(exc))
        except Exception:
            logger.exception("browser_image_upload_failed", action=command.action)
            return self._capture_error_response(command, result=execution.result, error="Browser image upload failed.")
        return self._success_response(command, result=execution.result, screenshot=screenshot)

    async def _execute_safely(self, command: DesktopCommand) -> _Execution | DesktopResponse:  # noqa: PLR0911
        try:
            return await self._execute(command)
        except DesktopEmergencyStopError as exc:
            self._control_revoked = True
            return self._error_response(command, str(exc))
        except AccessibilityActionOutcomeUnknownError:
            return self._unknown_control_response(command)
        except PlaywrightActionOutcomeUnknownError:
            return self._unknown_control_response(command)
        except (AccessibilityError, DesktopProviderError, DesktopProtocolError, PlaywrightBrowserError) as exc:
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

    async def _attach_follow_up_state(
        self,
        command: DesktopCommand,
        execution: _Execution,
    ) -> _Execution | DesktopResponse:
        app_id = execution.follow_up_app
        if app_id is None:
            return execution
        try:
            state = await asyncio.to_thread(self.provider.get_app_state, app_id)
        except Exception:
            logger.exception(
                "desktop_control_follow_up_state_failed",
                request_id=command.request_id,
                action=command.action,
            )
            warning = (
                "The desktop action completed, but fresh app state could not be read; do not repeat the action "
                "automatically. Request get_app_state before deciding the next step."
            )
            return self._success_response(
                command,
                result={**execution.result, "warning": warning, "follow_up_state": "failed"},
            )
        return _Execution(
            result={**execution.result, "state": state.to_result()},
            capture_app=state.app_id,
            capture_state_id=state.state_id,
        )

    async def _capture_response(
        self,
        command: DesktopCommand,
        *,
        result: dict[str, object],
        app_id: str,
        state_id: str,
    ) -> DesktopResponse:
        try:
            capture = await asyncio.to_thread(
                self.provider.screenshot,
                app_id=app_id,
                state_id=state_id,
            )
            screenshot = await upload_encrypted_screenshot(
                self.client,
                capture.content,
                mime_type=capture.mime_type,
                filename=f"desktop-{command.request_id}.jpg",
            )
        except (AccessibilityError, DesktopProviderError, DesktopMediaError) as exc:
            return self._capture_error_response(command, result=result, error=str(exc))
        except Exception:
            logger.exception("desktop_follow_up_screenshot_failed", action=command.action)
            return self._capture_error_response(command, result=result, error="Local screenshot operation failed.")
        return self._success_response(
            command,
            result={
                **result,
                "screen": {"width": capture.screen_width, "height": capture.screen_height},
                "capture": {
                    "x": capture.capture_x,
                    "y": capture.capture_y,
                    "width": capture.capture_width,
                    "height": capture.capture_height,
                },
                "image": {"width": capture.image_width, "height": capture.image_height},
            },
            screenshot=screenshot,
        )

    def _policy_error(self, command: DesktopCommand) -> str | None:
        now_ms = round(self.clock() * 1000)
        app_id = command.parameters.get("app")
        if command.issued_at_ms > now_ms + _MAX_FUTURE_SKEW_MS:
            error = "Desktop command was issued too far in the future."
        elif command.expires_at_ms <= now_ms:
            error = "Desktop command expired before local execution."
        elif not self.policy.caller_allowed(command):
            error = "Desktop command requester or agent is not allowed by local policy."
        elif command.action in DESKTOP_BROWSER_ACTIONS:
            error = self._browser_policy_error(command)
        elif command.action in DESKTOP_APP_ACTIONS and (
            not isinstance(app_id, str) or app_id not in self.policy.allowed_app_ids
        ):
            error = "Desktop command must target an application in the local allowlist."
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

    def _browser_policy_error(self, command: DesktopCommand) -> str | None:  # noqa: PLR0911
        if not self.policy.browser_enabled or self.browser_provider is None:
            return "Playwright browser extension support is disabled on this desktop bridge."
        try:
            browser_action, browser_parameters = _browser_command_parameters(command.parameters)
            requires_control = browser_action_requires_control(browser_action, browser_parameters)
        except (DesktopProtocolError, PlaywrightBrowserError) as exc:
            return str(exc)
        expected_action = "browser_control" if requires_control else "browser_observe"
        if command.action != expected_action:
            return "Desktop browser command used the wrong observe/control classification."
        if not requires_control:
            return None
        if not self.policy.allow_control:
            return "Desktop control is disabled; this bridge is observe-only."
        if self._control_revoked:
            return "Desktop emergency stop is latched; restart the bridge locally before granting control again."
        if not self._control_available():
            return "Local desktop control lease has expired."
        return None

    def _record_sequence(self, command: DesktopCommand) -> str | None:
        previous = self._sequence_high_watermarks.get(command.session_id)
        if previous is not None and command.sequence <= previous:
            return "Desktop command sequence was already used or arrived out of order."
        self._sequence_high_watermarks[command.session_id] = command.sequence
        self._sequence_high_watermarks.move_to_end(command.session_id)
        while len(self._sequence_high_watermarks) > _MAX_TRACKED_SESSIONS:
            evicted_session_id, _ = self._sequence_high_watermarks.popitem(last=False)
            logger.warning(
                "desktop_sequence_session_evicted",
                session_id=evicted_session_id,
                tracked_session_limit=_MAX_TRACKED_SESSIONS,
            )
        return None

    async def _execute(self, command: DesktopCommand) -> _Execution:
        parameters = command.parameters
        if command.action in DESKTOP_BROWSER_ACTIONS:
            browser_action, browser_parameters = _browser_command_parameters(parameters)
            if self.browser_provider is None:
                msg = "Playwright browser extension support is disabled on this desktop bridge."
                raise DesktopProtocolError(msg)
            if command.action == "browser_control":
                await asyncio.to_thread(self.provider.check_emergency_stop)
            browser_result = await self.browser_provider.execute(browser_action, browser_parameters)
            return _Execution(browser_result.payload, browser_image=browser_result.image)
        if command.action == "status":
            _reject_unexpected_parameters(parameters, allowed=frozenset())
            status = await asyncio.to_thread(self.provider.status)
            return _Execution({**status, "bridge": self._bridge_status()})
        if command.action == "list_apps":
            _reject_unexpected_parameters(parameters, allowed=frozenset())
            apps = await asyncio.to_thread(self.provider.list_apps)
            return _Execution({"apps": [app.to_result() for app in apps]})
        if command.action in {"get_app_state", "screenshot"}:
            _reject_unexpected_parameters(parameters, allowed=frozenset({"app"}))
            state = await asyncio.to_thread(self.provider.get_app_state, _required_str_parameter(parameters, "app"))
            return _Execution(
                {"action": command.action, "state": state.to_result()},
                capture_app=state.app_id,
                capture_state_id=state.state_id,
            )

        app_id = _required_str_parameter(parameters, "app")
        state_id = _required_str_parameter(parameters, "state_id")
        if command.action in {"click_element", "set_value", "scroll_element", "perform_action"}:
            await self._execute_semantic_control(command, app_id=app_id, state_id=state_id)
        else:
            await self._execute_fallback_control(command, app_id=app_id, state_id=state_id)
        return _Execution(
            {"action": command.action, "action_completed": True},
            follow_up_app=app_id,
        )

    async def _execute_semantic_control(
        self,
        command: DesktopCommand,
        *,
        app_id: str,
        state_id: str,
    ) -> None:
        parameters = command.parameters
        if command.action == "click_element":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "element_index"}))
            await asyncio.to_thread(
                self.provider.click_element,
                app_id=app_id,
                state_id=state_id,
                element_index=_required_int_parameter(parameters, "element_index"),
            )
        elif command.action == "set_value":
            _reject_unexpected_parameters(
                parameters,
                allowed=frozenset({"app", "state_id", "element_index", "value"}),
            )
            await asyncio.to_thread(
                self.provider.set_value,
                app_id=app_id,
                state_id=state_id,
                element_index=_required_int_parameter(parameters, "element_index"),
                value=_required_str_parameter(parameters, "value", allow_empty=True),
            )
        elif command.action == "scroll_element":
            _reject_unexpected_parameters(
                parameters,
                allowed=frozenset({"app", "state_id", "element_index", "direction", "pages"}),
            )
            await asyncio.to_thread(
                self.provider.scroll_element,
                app_id=app_id,
                state_id=state_id,
                element_index=_required_int_parameter(parameters, "element_index"),
                direction=_required_str_parameter(parameters, "direction"),
                pages=_required_int_parameter(parameters, "pages"),
            )
        elif command.action == "perform_action":
            _reject_unexpected_parameters(
                parameters,
                allowed=frozenset({"app", "state_id", "element_index", "action_name"}),
            )
            await asyncio.to_thread(
                self.provider.perform_action,
                app_id=app_id,
                state_id=state_id,
                element_index=_required_int_parameter(parameters, "element_index"),
                action_name=_required_str_parameter(parameters, "action_name"),
            )
        else:
            msg = f"Unsupported semantic desktop action: {command.action}."
            raise DesktopProtocolError(msg)

    async def _execute_fallback_control(
        self,
        command: DesktopCommand,
        *,
        app_id: str,
        state_id: str,
    ) -> None:
        parameters = command.parameters
        if command.action == "click":
            _reject_unexpected_parameters(
                parameters,
                allowed=frozenset({"app", "state_id", "x", "y", "button"}),
            )
            await asyncio.to_thread(
                self.provider.click,
                app_id=app_id,
                state_id=state_id,
                x=_required_int_parameter(parameters, "x"),
                y=_required_int_parameter(parameters, "y"),
                button=_optional_str_parameter(parameters, "button", default="left"),
            )
        elif command.action == "type_text":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "text"}))
            await asyncio.to_thread(
                self.provider.type_text,
                app_id=app_id,
                state_id=state_id,
                text=_required_str_parameter(parameters, "text"),
            )
        elif command.action == "scroll":
            _reject_unexpected_parameters(
                parameters,
                allowed=frozenset({"app", "state_id", "direction", "pages", "x", "y"}),
            )
            await asyncio.to_thread(
                self.provider.scroll,
                app_id=app_id,
                state_id=state_id,
                direction=_required_str_parameter(parameters, "direction"),
                pages=_required_int_parameter(parameters, "pages"),
                x=_optional_int_parameter(parameters, "x"),
                y=_optional_int_parameter(parameters, "y"),
            )
        elif command.action == "keypress":
            _reject_unexpected_parameters(parameters, allowed=frozenset({"app", "state_id", "keys"}))
            await asyncio.to_thread(
                self.provider.keypress,
                app_id=app_id,
                state_id=state_id,
                keys=_required_str_list_parameter(parameters, "keys"),
            )
        else:
            msg = f"Unsupported fallback desktop action: {command.action}."
            raise DesktopProtocolError(msg)

    def _bridge_status(self) -> dict[str, object]:
        control_available = self._control_available()
        status: dict[str, object] = {
            "mode": "control" if control_available else "observe_only",
            "control_available": control_available,
            "emergency_stop_latched": self._control_revoked,
            "allowed_app_count": len(self.policy.allowed_app_ids),
            "browser_enabled": self.policy.browser_enabled,
        }
        if self.policy.control_lease_expires_at_ms is not None:
            status["control_lease_expires_at_ms"] = self.policy.control_lease_expires_at_ms
        return status

    def _control_available(self) -> bool:
        lease_expires_at_ms = self.policy.control_lease_expires_at_ms
        return (
            self.policy.allow_control
            and not self._control_revoked
            and lease_expires_at_ms is not None
            and self.clock() * 1000 < lease_expires_at_ms
            and self._control_lease_deadline is not None
            and self.monotonic_clock() < self._control_lease_deadline
        )

    def _capture_error_response(
        self,
        command: DesktopCommand,
        *,
        result: dict[str, object],
        error: str,
    ) -> DesktopResponse:
        if command.action == "get_app_state":
            warning = (
                f"Accessibility state was read, but its app-window screenshot failed: {error} "
                "The state may now be stale; "
                "request get_app_state again before acting."
            )
            return self._success_response(
                command,
                result={**result, "warning": warning, "follow_up_screenshot": "failed"},
            )
        if command.action not in DESKTOP_CONTROL_ACTIONS:
            return self._error_response(command, error)
        warning = (
            f"The desktop action completed and fresh app state was read, but its follow-up screenshot failed: {error} "
            "do not repeat the action automatically. Request get_app_state before the next action."
        )
        logger.warning(
            "desktop_control_follow_up_screenshot_failed",
            request_id=command.request_id,
            action=command.action,
            requester_id=command.requester_id,
            agent_name=command.agent_name,
            error=error,
        )
        return self._success_response(
            command,
            result={**result, "warning": warning, "follow_up_screenshot": "failed"},
        )

    def _unknown_control_response(self, command: DesktopCommand) -> DesktopResponse:
        recovery_action = (
            "browser(action='tabs' or 'snapshot', target='desktop')"
            if command.action in DESKTOP_BROWSER_ACTIONS
            else "get_app_state"
        )
        warning = (
            "The desktop action outcome is unknown and it may have completed; do not repeat the action automatically. "
            f"Request {recovery_action} before deciding the next step."
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

    def _remember_response(self, command: DesktopCommand, response: DesktopResponse) -> None:
        self._responses[response.request_id] = _CachedDesktopResponse(
            command_fingerprint=_command_fingerprint(command),
            response=response,
        )
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


def _command_fingerprint(command: DesktopCommand) -> str:
    encoded = json.dumps(
        command.to_content(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def _required_str_parameter(parameters: dict[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        qualifier = "a string" if allow_empty else "a non-empty string"
        msg = f"Desktop parameter {key} must be {qualifier}."
        raise DesktopProtocolError(msg)
    max_length = _MAX_PARAMETER_TEXT_LENGTH if key in {"text", "value"} else _MAX_PARAMETER_IDENTIFIER_LENGTH
    if len(value) > max_length:
        msg = f"Desktop parameter {key} must not exceed {max_length} characters."
        raise DesktopProtocolError(msg)
    return value


def _required_object_parameter(parameters: dict[str, object], key: str) -> dict[str, object]:
    value = parameters.get(key)
    if not isinstance(value, dict):
        msg = f"Desktop parameter {key} must be an object with string keys."
        raise DesktopProtocolError(msg)
    result: dict[str, object] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            msg = f"Desktop parameter {key} must be an object with string keys."
            raise DesktopProtocolError(msg)
        result[item_key] = item_value
    return result


def _browser_command_parameters(parameters: dict[str, object]) -> tuple[str, dict[str, object]]:
    _reject_unexpected_parameters(
        parameters,
        allowed=frozenset({"browser_action", "browser_parameters"}),
    )
    return (
        _required_str_parameter(parameters, "browser_action"),
        _required_object_parameter(parameters, "browser_parameters"),
    )


def _optional_str_parameter(parameters: dict[str, object], key: str, *, default: str) -> str:
    if key not in parameters:
        return default
    return _required_str_parameter(parameters, key)


def _required_str_list_parameter(parameters: dict[str, object], key: str) -> list[str]:
    value = parameters.get(key)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        msg = f"Desktop parameter {key} must contain exactly one locally safe navigation key."
        raise DesktopProtocolError(msg)
    normalized = value[0].strip().lower()
    if normalized not in DESKTOP_SAFE_KEYS:
        msg = f"Desktop parameter {key} may escape the allowed app."
        raise DesktopProtocolError(msg)
    return [normalized]


__all__ = ["DesktopBridge", "DesktopBridgePolicy"]
