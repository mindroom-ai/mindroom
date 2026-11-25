"""Machine-local Matrix desktop command processor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast

from mindroom.desktop.accessibility import (
    PRIMARY_SCREEN_APP_ID,
    AccessibilityActionOutcomeUnknownError,
    AccessibilityError,
    app_targetable,
)
from mindroom.desktop.command_journal import DesktopCommandJournal, controller_journal_key
from mindroom.desktop.command_parameters import (
    reject_unexpected_parameters,
    required_object_parameter,
    required_str_parameter,
)
from mindroom.desktop.file_actions import execute_file
from mindroom.desktop.filesystem import DesktopFilesystem, DesktopFilesystemError
from mindroom.desktop.gui_actions import execute_fallback_control, execute_semantic_control
from mindroom.desktop.media import MEDIA_UPLOAD_TIMEOUT_SECONDS, DesktopMediaError, upload_encrypted_media
from mindroom.desktop.observations import DesktopObservations
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
    DESKTOP_FILE_ACTIONS,
    DESKTOP_RESPONSE_EVENT_TYPE,
    DESKTOP_SHELL_ACTIONS,
    DesktopCommand,
    DesktopObservationMode,
    DesktopProtocolError,
    DesktopResponse,
    desktop_observation_mode,
    event_content,
)
from mindroom.desktop.provider import (
    DesktopProvider,
    DesktopProviderError,
    missing_macos_desktop_permissions,
)
from mindroom.desktop.reply_fitting import fits_inline, leftmost_fitting, response_metrics, success_response
from mindroom.desktop.shell import DesktopShell, DesktopShellError
from mindroom.desktop.shell_actions import caller_shell_status, execute_shell, shell_status
from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import (
    OlmToDeviceError,
    PinnedMatrixDevice,
    authenticated_sender_matches,
    resolve_pinned_device,
    send_encrypted_to_device,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    import nio
    from nio import AuthenticatedToDeviceEvent

    from mindroom.desktop.accessibility import AccessibilityState
    from mindroom.desktop.protocol import EncryptedDesktopMedia

logger = get_logger(__name__)

_MAX_FUTURE_SKEW_MS = 30_000
# Checking a handle is an observation; starting or killing a command has effects that must not repeat.
_EFFECTFUL_SHELL_ACTIONS = frozenset({"run_shell", "kill_shell"})
_STOP_DELIVERY_TIMEOUT_SECONDS = 2.0
_MAX_ERROR_CHARS = 2_048
_OMITTED_STATE_WARNING = "Desktop state exceeded the inline reply size limit and was omitted."


async def _run_macos_application_events() -> None:
    """Refresh AppKit's application cache while asyncio owns the main thread."""
    if sys.platform != "darwin":
        return
    if threading.current_thread() is not threading.main_thread():
        msg = "The macOS desktop bridge must run on the main thread."
        raise RuntimeError(msg)

    from CoreFoundation import (  # noqa: PLC0415  # ty: ignore[import-not-found]
        CFRunLoopRunInMode,
        kCFRunLoopDefaultMode,
    )
    from objc import autorelease_pool  # noqa: PLC0415

    while True:
        # NSWorkspace and NSRunningApplication update only on the main Cocoa
        # run loop. Keep pumping while native worker threads wait for launches
        # and activation; a refresh only before dispatch cannot observe either.
        with autorelease_pool():
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.0, False)
        await asyncio.sleep(0.05)


class _DesktopBridgeStoppedError(RuntimeError):
    """Admission is fenced while the local bridge is stopping."""


@dataclass(frozen=True, slots=True)
class DesktopControllerPolicy:
    """Pinned controller identity and its allowed human/agent callers."""

    controller: PinnedMatrixDevice
    allowed_requester_ids: frozenset[str]
    allowed_agent_names: frozenset[str]

    def __post_init__(self) -> None:
        """Require explicit non-empty requester and agent allowlists."""
        if not self.allowed_requester_ids:
            msg = "Desktop bridge requires at least one allowed requester Matrix ID."
            raise ValueError(msg)
        if not self.allowed_agent_names:
            msg = "Desktop bridge requires at least one allowed agent name."
            raise ValueError(msg)
        if any(not value.strip() for value in self.allowed_requester_ids):
            msg = "Desktop bridge requester IDs must not be empty."
            raise ValueError(msg)
        if any(not value.strip() for value in self.allowed_agent_names):
            msg = "Desktop bridge agent names must not be empty."
            raise ValueError(msg)

    def caller_allowed(self, command: DesktopCommand) -> bool:
        """Return whether this controller's allowlist admits the human and agent provenance."""
        return command.requester_id in self.allowed_requester_ids and command.agent_name in self.allowed_agent_names


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
    allowed_file_roots: tuple[Path, ...] = ()
    shell_enabled: bool = False
    controllers: tuple[DesktopControllerPolicy, ...] = ()

    def __post_init__(self) -> None:
        """Require explicit caller and agent allowlists, unique controllers, and a lease expiry only with control."""
        primary = DesktopControllerPolicy(self.controller, self.allowed_requester_ids, self.allowed_agent_names)
        if any(not value.strip() for value in self.allowed_app_ids):
            msg = "Desktop bridge application IDs must not be empty."
            raise ValueError(msg)
        if not self.allow_control and self.control_lease_expires_at_ms is not None:
            msg = "Observe-only desktop bridge cannot carry a control lease expiry."
            raise ValueError(msg)
        if self.controllers and self.controllers[0] != primary:
            msg = "Desktop bridge primary controller fields must match controllers[0]."
            raise ValueError(msg)
        seen_devices: set[tuple[str, str]] = set()
        for entry in self.effective_controllers:
            device_key = (entry.controller.user_id, entry.controller.device_id)
            if device_key in seen_devices:
                msg = "Desktop bridge controllers must not contain duplicate devices."
                raise ValueError(msg)
            seen_devices.add(device_key)

    @property
    def effective_controllers(self) -> tuple[DesktopControllerPolicy, ...]:
        """Return all configured controllers, defaulting to the top-level primary controller."""
        return self.controllers or (
            DesktopControllerPolicy(self.controller, self.allowed_requester_ids, self.allowed_agent_names),
        )

    def caller_allowed(self, command: DesktopCommand, controller_key: str | None = None) -> bool:
        """Return whether local static policy admits the human and agent provenance for one controller."""
        if controller_key is None:
            return command.requester_id in self.allowed_requester_ids and command.agent_name in self.allowed_agent_names
        for entry in self.effective_controllers:
            if controller_journal_key(entry.controller) == controller_key:
                return entry.caller_allowed(command)
        return False


@dataclass(frozen=True, slots=True)
class _ControlGrant:
    deadline: float
    expires_at_ms: int | None
    computer: bool


@dataclass(frozen=True, slots=True)
class _Execution:
    result: dict[str, object]
    observed_state: AccessibilityState | None = None
    capture_app: str | None = None
    capture_state_id: str | None = None
    follow_up_app: str | None = None
    browser_image: BrowserImage | None = None


@dataclass
class DesktopBridge:
    """Validate, execute, and answer pinned encrypted desktop commands."""

    client: nio.AsyncClient
    provider: DesktopProvider | None
    policy: DesktopBridgePolicy
    browser_provider: BrowserProvider | None = None
    filesystem: DesktopFilesystem | None = None
    shell: DesktopShell | None = None
    clock: Callable[[], float] = time.time
    monotonic_clock: Callable[[], float] = time.monotonic
    journal_path: Path | None = None
    legacy_journal_path: Path | None = None
    _journal: DesktopCommandJournal = field(init=False)
    _observations: DesktopObservations = field(init=False)
    _controllers: dict[str, DesktopControllerPolicy] = field(default_factory=dict, init=False)
    _in_flight: set[tuple[str, str]] = field(default_factory=set, init=False)
    _execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _shell_start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _work_available: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _shell_work_available: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _response_available: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _capacity_available: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _stopped: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _accepting: bool = field(default=True, init=False)
    _active_action: str | None = field(default=None, init=False)
    _control_grants: dict[str, _ControlGrant] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Pair each enabled capability with its provider and convert the lease label to a local deadline."""
        if self.policy.browser_enabled != (self.browser_provider is not None):
            msg = "Desktop browser policy and local browser provider must be enabled together."
            raise ValueError(msg)
        if bool(self.policy.allowed_app_ids) != (self.provider is not None):
            msg = "Desktop app policy and local GUI provider must be enabled together."
            raise ValueError(msg)
        if bool(self.policy.allowed_file_roots) != (self.filesystem is not None):
            msg = "Desktop file policy and local filesystem provider must be enabled together."
            raise ValueError(msg)
        if self.policy.shell_enabled != (self.shell is not None):
            msg = "Desktop shell policy and local shell provider must be enabled together."
            raise ValueError(msg)
        self._controllers = {
            controller_journal_key(entry.controller): entry for entry in self.policy.effective_controllers
        }
        self._observations = DesktopObservations(clock=self.monotonic_clock)
        self._journal = DesktopCommandJournal.load(
            self.journal_path,
            controller_key=self._primary_controller_key(),
            legacy_path=self.legacy_journal_path,
        )
        if self.policy.control_lease_expires_at_ms is not None:
            remaining_seconds = max(0.0, self.policy.control_lease_expires_at_ms / 1000 - self.clock())
            grant = _ControlGrant(
                deadline=self.monotonic_clock() + remaining_seconds,
                expires_at_ms=self.policy.control_lease_expires_at_ms,
                computer=False,
            )
            self._control_grants = dict.fromkeys(self._controllers, grant)
        elif self.policy.allow_control:
            expired = _ControlGrant(deadline=-math.inf, expires_at_ms=None, computer=False)
            self._control_grants = dict.fromkeys(self._controllers, expired)

    def _match_controller(self, event: AuthenticatedToDeviceEvent) -> str | None:
        for controller_key, entry in self._controllers.items():
            if authenticated_sender_matches(self.client, event, entry.controller):
                return controller_key
        return None

    async def on_to_device_event(self, event: AuthenticatedToDeviceEvent) -> None:
        """Persist authenticated admission before the durable sync consumer acknowledges."""
        if not self._accepting:
            raise _DesktopBridgeStoppedError
        if event.type != DESKTOP_COMMAND_EVENT_TYPE:
            return
        controller_key = self._match_controller(event)
        if controller_key is None:
            logger.warning("desktop_command_sender_rejected", sender=event.sender)
            return
        try:
            command = DesktopCommand.from_content(event_content(event.source))
        except DesktopProtocolError as exc:
            logger.warning("desktop_command_malformed", reason=str(exc))
            return
        fingerprint = _command_fingerprint(command)
        if self._replay_cached(command, fingerprint, controller_key):
            return
        error = self._policy_error(command, controller_key) or self._journal.sequence_error(command, controller_key)
        if error is not None:
            self._queue_response(self._error_response(command, error), controller_key)
            return
        if command.action == "request_status":
            self._queue_response(self._request_status_response(command, controller_key), controller_key)
            return
        self._journal.admit(command, fingerprint, controller_key)
        (self._shell_work_available if command.action == "run_shell" else self._work_available).set()

    def _replay_cached(self, command: DesktopCommand, fingerprint: str, controller_key: str) -> bool:
        cached = self._journal.get(command.request_id, controller_key)
        if cached is None:
            return False
        if cached.command_fingerprint != fingerprint:
            self._queue_response(
                self._error_response(command, "Desktop request ID was reused with different command content."),
                controller_key,
            )
        elif cached.response is not None:
            self._queue_response(cached.response, controller_key)
        elif cached.state == "started" and (controller_key, command.request_id) not in self._in_flight:
            self._journal.remember_response(command, fingerprint, self._interrupted_response(command), controller_key)
            self._response_available.set()
        return True

    def _request_status_response(self, command: DesktopCommand, controller_key: str) -> DesktopResponse:
        try:
            return success_response(command, result=self._request_status(command, controller_key))
        except DesktopProtocolError as exc:
            return self._error_response(command, str(exc))

    def _request_status(self, command: DesktopCommand, controller_key: str) -> dict[str, object]:
        """Read a caller's retained receipt without waiting for the action executor."""
        reject_unexpected_parameters(command.parameters, allowed=frozenset({"request_id"}))
        request_id = required_str_parameter(command.parameters, "request_id")
        entry = self._journal.get(request_id, controller_key)
        result: dict[str, object] = {"request_id": request_id, "state": "not_found"}
        if (
            entry is None
            or entry.command is None
            or entry.command.requester_id != command.requester_id
            or entry.command.agent_name != command.agent_name
            or entry.command.action == "request_status"
        ):
            return result
        if entry.state == "started":
            result["state"] = "running" if (controller_key, request_id) in self._in_flight else "unknown"
        else:
            result["state"] = entry.state
        if entry.response is not None:
            result["response"] = entry.response.to_content()
            if entry.response.result.get("action_outcome") == "unknown":
                result["state"] = "unknown"
        return result

    def _queue_response(self, response: DesktopResponse, controller_key: str) -> None:
        self._journal.queue_response(response, controller_key)
        self._response_available.set()

    def recover_interrupted(self) -> None:
        """Record uncertain outcomes for started work without an active executor."""
        for entry in self._journal.started(all_controllers=True):
            command = entry.command
            if command is not None and (entry.controller_key, command.request_id) not in self._in_flight:
                self._journal.remember_response(
                    command,
                    entry.command_fingerprint,
                    self._interrupted_response(command),
                    entry.controller_key,
                )
                self._response_available.set()

    async def execute_pending(self, *, shell_starts: bool) -> None:
        """Execute one lane's admitted work serially while Matrix polling remains independent.

        Shell starts have their own lane: approval can take the whole command lifetime, and status,
        handle checks, and every other action must keep answering meanwhile.
        """
        lock = self._shell_start_lock if shell_starts else self._execution_lock
        if lock.locked():
            return
        async with lock:
            for entry in self._journal.queued(all_controllers=True):
                if not self._accepting:
                    break
                command = entry.command
                assert command is not None
                if (command.action == "run_shell") != shell_starts:
                    continue
                controller_key = entry.controller_key
                in_flight_key = (controller_key, command.request_id)
                self._in_flight.add(in_flight_key)
                if not shell_starts:
                    self._active_action = command.action
                started_at = self.monotonic_clock()
                try:
                    response = await self._process(command, entry.command_fingerprint, controller_key)
                    response = replace(
                        response,
                        result={
                            **response.result,
                            "metrics": response_metrics(
                                elapsed_ms=max(0, round((self.monotonic_clock() - started_at) * 1000)),
                                structured_bytes=len(json.dumps(response.result, ensure_ascii=False).encode()),
                                screenshot_bytes=response.screenshot.size if response.screenshot is not None else 0,
                            ),
                        },
                    )
                    self._journal.remember_response(command, entry.command_fingerprint, response, controller_key)
                    self._response_available.set()
                    logger.info(
                        "desktop_command_completed",
                        request_id=command.request_id,
                        action=command.action,
                        ok=response.ok,
                    )
                finally:
                    self._in_flight.discard(in_flight_key)
                    if not shell_starts:
                        self._active_action = None

    async def deliver_pending(self) -> bool:
        """Retry exact recorded results without running desktop work again."""
        progressed = False
        async with self._delivery_lock:
            failed_controllers: set[str] = set()
            for delivery_id, controller_key, response in self._journal.pending_responses_with_controller():
                if controller_key in failed_controllers:
                    continue
                entry = self._controllers.get(controller_key)
                if entry is None:
                    continue
                if not await self._send_response(response, entry.controller):
                    failed_controllers.add(controller_key)
                    continue
                self._journal.mark_delivered(delivery_id)
                self._capacity_available.set()
                progressed = True
        return progressed

    async def wait_for_capacity(self) -> None:
        """Wait until response delivery frees admission capacity."""
        await self._capacity_available.wait()
        self._capacity_available.clear()

    async def run(self) -> None:
        """Run independent action and response workers until locally stopped."""
        self.recover_interrupted()
        self._work_available.set()
        self._shell_work_available.set()
        self._response_available.set()
        async with asyncio.TaskGroup() as group:
            application_events = (
                group.create_task(_run_macos_application_events(), name="desktop_application_events")
                if self.provider is not None
                else None
            )
            executor = group.create_task(self._execute_loop(shell_starts=False), name="desktop_executor")
            shell_executor = group.create_task(self._execute_loop(shell_starts=True), name="desktop_shell_executor")
            sender = group.create_task(self._deliver_loop(), name="desktop_responses")
            await self._stopped.wait()
            executor.cancel()
            shell_executor.cancel()
            sender.cancel()
            if application_events is not None:
                application_events.cancel()

    async def _execute_loop(self, *, shell_starts: bool) -> None:
        work_available = self._shell_work_available if shell_starts else self._work_available
        while True:
            await work_available.wait()
            work_available.clear()
            await self.execute_pending(shell_starts=shell_starts)

    async def _deliver_loop(self) -> None:
        while True:
            await self._response_available.wait()
            self._response_available.clear()
            await self.deliver_pending()
            if any(
                controller_key in self._controllers
                for _, controller_key, _ in self._journal.pending_responses_with_controller()
            ):
                await asyncio.sleep(1)
                self._response_available.set()

    def local_status(self) -> dict[str, object]:
        """Report local authority, including pending shell details only for the local approver.

        ``file_roots`` is reported in full here: the native NDJSON channel the Mac app reads over
        has no to-device size limit, unlike the remote ``status`` action reply.
        """
        status = self._bridge_status()
        remaining, expires_at_ms = self._top_level_lease_summary()
        return {
            **status,
            "file_roots": self.filesystem.list_folders()["folders"] if self.filesystem is not None else [],
            "gui_mode": status["gui_mode"] if self._accepting else "stopped",
            "lease_expires_at_ms": expires_at_ms,
            "lease_remaining_seconds": remaining,
            "active_action": self._active_action,
            "shell": shell_status(self.shell),
            "controllers": {key: self._controller_local_status(key) for key in self._controllers},
        }

    def decide_local_shell(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int,
        auto_approve_until_revoked: bool = False,
    ) -> dict[str, object]:
        """Settle the exact pending shell request; call on the event loop that runs the bridge."""
        self._running_shell().decide(
            command_id,
            approved=approved,
            auto_approve_seconds=auto_approve_seconds,
            auto_approve_until_revoked=auto_approve_until_revoked,
        )
        return self.local_status()

    def grant_local_shell(
        self,
        duration_seconds: int | None = None,
        *,
        until_revoked: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]:
        """Auto-approve shell requests for one controller (or the primary controller when omitted)."""
        shell = self._running_shell()
        target = self._primary_controller_key() if controller_key is None else self._require_controller(controller_key)
        shell.grant(duration_seconds, until_revoked=until_revoked, controller_key=target)
        return self.local_status()

    def revoke_local_shell(self, controller_key: str | None = None) -> dict[str, object]:
        """End auto-approval, reject pending approval, and kill handles for one controller or all controllers."""
        shell = self._enabled_shell()
        if controller_key is not None:
            self._require_controller(controller_key)
        shell.revoke(controller_key=controller_key)
        return self.local_status()

    def kill_local_shell_handle(self, handle: str) -> dict[str, object]:
        """Kill any caller's handle from the local management channel; its owner's check reports it killed."""
        self._enabled_shell().kill_handle(handle)
        return self.local_status()

    def _enabled_shell(self) -> DesktopShell:
        if self.shell is None:
            msg = "Local shell access is disabled."
            raise ValueError(msg)
        return self.shell

    def _running_shell(self) -> DesktopShell:
        shell = self._enabled_shell()
        if not self._accepting:
            msg = "The desktop bridge is stopping."
            raise ValueError(msg)
        return shell

    def _primary_controller_key(self) -> str:
        return controller_journal_key(self.policy.controller)

    def _require_controller(self, controller_key: str) -> str:
        if controller_key not in self._controllers:
            msg = "Unknown desktop controller."
            raise ValueError(msg)
        return controller_key

    def grant_local_control(
        self,
        duration_seconds: int | None = None,
        *,
        computer: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]:
        """Grant control only through the local helper's private management channel; it is never saved.

        A duration grants a bounded lease over the saved apps.
        ``computer`` instead grants every app but MindRoom's own and the primary screen until revoked or stopped.
        """
        timed = computer is False and type(duration_seconds) is int and 60 <= duration_seconds <= 3600
        if not timed and not (computer is True and duration_seconds is None):
            msg = "Local control must last 60 to 3600 seconds over the saved apps, or cover the computer until revoked."
            raise ValueError(msg)
        if not self._accepting:
            msg = "Restart the stopped bridge before granting control."
            raise ValueError(msg)
        if self.provider is None:
            msg = "Desktop GUI is unavailable."
            raise ValueError(msg)
        target = self._primary_controller_key() if controller_key is None else self._require_controller(controller_key)
        self._control_grants[target] = _ControlGrant(
            deadline=math.inf if duration_seconds is None else self.monotonic_clock() + duration_seconds,
            expires_at_ms=None if duration_seconds is None else round((self.clock() + duration_seconds) * 1000),
            computer=computer,
        )
        return self.local_status()

    def grant_control(
        self,
        duration_seconds: int | None = None,
        *,
        computer: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]:
        """Grant live GUI control for one controller (or the primary controller when omitted)."""
        return self.grant_local_control(duration_seconds, computer=computer, controller_key=controller_key)

    def revoke_local_control(self, controller_key: str | None = None) -> dict[str, object]:
        """Revoke new input and its computer scope immediately without interrupting active actions."""
        if controller_key is None:
            self._control_grants.clear()
        else:
            self._require_controller(controller_key)
            self._control_grants.pop(controller_key, None)
        return self.local_status()

    def revoke_control(self, controller_key: str | None = None) -> dict[str, object]:
        """Revoke live GUI control for one controller or all configured controllers."""
        return self.revoke_local_control(controller_key=controller_key)

    def update_controllers(self, controllers: tuple[DesktopControllerPolicy, ...]) -> dict[str, object]:
        """Hot-reload configured controllers without dropping the Matrix session or surviving handles."""
        if not self._accepting:
            msg = "The desktop bridge is stopping."
            raise ValueError(msg)
        if not controllers:
            msg = "Desktop bridge requires at least one controller."
            raise ValueError(msg)
        next_policy = replace(
            self.policy,
            controller=controllers[0].controller,
            allowed_requester_ids=controllers[0].allowed_requester_ids,
            allowed_agent_names=controllers[0].allowed_agent_names,
            controllers=controllers,
        )
        new_map = {controller_journal_key(entry.controller): entry for entry in next_policy.effective_controllers}
        removed_keys = [key for key in self._controllers if key not in new_map]
        self._controllers = new_map
        self.policy = next_policy
        for removed_key in removed_keys:
            self._control_grants.pop(removed_key, None)
            if self.shell is not None:
                self.shell.revoke(controller_key=removed_key)
        return self.local_status()

    async def stop(self) -> None:
        """Fence admission, settle active work, and give recorded replies a bounded final delivery."""
        self._accepting = False
        self.revoke_local_control()
        if self.shell is not None:
            # Pending approval can otherwise hold the shell lane until the command expires.
            await self.shell.close()
        async with self._execution_lock, self._shell_start_lock:
            try:
                # Native/CLI shutdown closes transport immediately after this method returns.
                # Include an already-busy sender in the bound; undelivered receipts stay durable.
                async with asyncio.timeout(_STOP_DELIVERY_TIMEOUT_SECONDS):
                    await self.deliver_pending()
            except TimeoutError:
                logger.warning("desktop_stop_response_delivery_timed_out")
            except Exception:
                # Delivery is best effort here; its failure must not prevent callers releasing resources.
                logger.exception("desktop_stop_response_delivery_failed")
            finally:
                self._stopped.set()

    def close(self) -> None:
        """Close durable command storage and pinned folders after workers have stopped."""
        self._journal.close()
        if self.filesystem is not None:
            self.filesystem.close()

    async def _process(
        self,
        command: DesktopCommand,
        command_fingerprint: str,
        controller_key: str | None = None,
    ) -> DesktopResponse:
        key = controller_journal_key(self.policy.controller) if controller_key is None else controller_key
        controller_key = key
        policy_error = self._policy_error(command, controller_key)
        if policy_error is not None:
            return self._error_response(command, policy_error)
        try:
            await resolve_pinned_device(self.client, self._controllers[controller_key].controller)
        except OlmToDeviceError as exc:
            return self._error_response(command, str(exc))
        policy_error = self._policy_error(command, controller_key)
        if policy_error is not None:
            return self._error_response(command, policy_error)
        if command.action in DESKTOP_FILE_ACTIONS | DESKTOP_SHELL_ACTIONS and "observation" in command.parameters:
            return self._error_response(command, "Unexpected desktop parameters: observation.")
        try:
            mode = desktop_observation_mode(command.action, command.parameters.get("observation", "both"))
            if "state_id" in command.parameters:
                resolved = self._observations.resolve(command, controller_key=controller_key)
                allow_window_fallback = self._observations.allows_window_fallback(resolved.parameters["state_id"])
            else:
                resolved = command
                allow_window_fallback = False
        except DesktopProtocolError as exc:
            return self._error_response(command, str(exc))
        self._journal.remember_started(command, command_fingerprint, controller_key)
        execution_command = replace(
            resolved,
            parameters={key: value for key, value in resolved.parameters.items() if key != "observation"},
        )
        execution = await self._execute_safely(
            execution_command,
            controller_key,
            mode=mode,
            allow_window_fallback=allow_window_fallback,
        )
        return await self._execution_response(command, execution, mode, controller_key)

    async def _execution_response(
        self,
        command: DesktopCommand,
        execution: _Execution | DesktopResponse,
        mode: DesktopObservationMode,
        controller_key: str,
    ) -> DesktopResponse:
        """Collect the requested fresh observation after an action."""
        if isinstance(execution, DesktopResponse):
            return execution
        if execution.follow_up_app is not None:
            execution = await self._attach_follow_up_state(command, execution, controller_key, mode=mode)
            if isinstance(execution, DesktopResponse):
                return execution
        if command.action in DESKTOP_APP_ACTIONS:
            execution = replace(execution, result={**execution.result, "observation": {"mode": mode}})
            if mode == "tree":
                return self._finalize_app_response(
                    command,
                    execution.result,
                    observed_state=execution.observed_state,
                    mode=mode,
                    controller_key=controller_key,
                )
        if execution.browser_image is not None:
            return await self._browser_image_response(command, execution, controller_key=controller_key)
        if execution.capture_app is None or execution.capture_state_id is None:
            return success_response(command, result=execution.result)
        return self._observation_refused(
            command,
            execution,
            execution.capture_app,
            controller_key,
            mode=mode,
        ) or await self._capture_response(
            command,
            result=execution.result,
            observed_state=execution.observed_state,
            mode=mode,
            controller_key=controller_key,
            app_id=execution.capture_app,
            state_id=execution.capture_state_id,
        )

    async def _browser_image_response(
        self,
        command: DesktopCommand,
        execution: _Execution,
        *,
        controller_key: str,
    ) -> DesktopResponse:
        image = execution.browser_image
        if image is None:
            return success_response(command, result=execution.result)
        extension = "png" if image.mime_type == "image/png" else "jpg"
        try:
            screenshot = await upload_encrypted_media(
                self.client,
                image.content,
                mime_type=image.mime_type,
                filename=f"browser-{command.request_id}.{extension}",
                timeout_seconds=MEDIA_UPLOAD_TIMEOUT_SECONDS,
            )
        except DesktopMediaError as exc:
            return self._capture_error_response(
                command,
                result=execution.result,
                error=str(exc),
                controller_key=controller_key,
            )
        except Exception:
            logger.exception("browser_image_upload_failed", action=command.action)
            return self._capture_error_response(
                command,
                result=execution.result,
                error="Browser image upload failed.",
                controller_key=controller_key,
            )
        return success_response(command, result=execution.result, screenshot=screenshot)

    async def _execute_safely(
        self,
        command: DesktopCommand,
        controller_key: str,
        *,
        mode: DesktopObservationMode,
        allow_window_fallback: bool = False,
    ) -> _Execution | DesktopResponse:
        try:
            return await self._execute(
                command,
                controller_key,
                mode=mode,
                allow_window_fallback=allow_window_fallback,
            )
        except (AccessibilityActionOutcomeUnknownError, PlaywrightActionOutcomeUnknownError) as exc:
            return self._unknown_control_response(command, reason=str(exc))
        except (
            AccessibilityError,
            DesktopProviderError,
            DesktopProtocolError,
            PlaywrightBrowserError,
            DesktopFilesystemError,
            DesktopShellError,
        ) as exc:
            return self._error_response(command, self._with_shell_hint(str(exc)))
        except Exception:
            logger.exception(
                "desktop_command_execution_failed",
                request_id=command.request_id,
                action=command.action,
            )
            if command.action in DESKTOP_CONTROL_ACTIONS | _EFFECTFUL_SHELL_ACTIONS:
                return self._unknown_control_response(command)
            return self._error_response(command, "Local desktop operation failed.")

    async def _attach_follow_up_state(
        self,
        command: DesktopCommand,
        execution: _Execution,
        controller_key: str,
        *,
        mode: DesktopObservationMode,
    ) -> _Execution | DesktopResponse:
        app_id = execution.follow_up_app
        if app_id is None:
            return execution
        refused = self._observation_refused(command, execution, app_id, controller_key, mode=mode)
        if refused is not None:
            return refused
        try:
            state = await asyncio.to_thread(
                self._required_gui_provider().get_app_state,
                app_id,
                include_elements=mode != "screenshot",
            )
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
            return success_response(
                command,
                result={**execution.result, "warning": warning, "follow_up_state": "failed"},
            )
        return _Execution(
            result=execution.result,
            observed_state=state,
            capture_app=state.app_id,
            capture_state_id=state.state_id,
        )

    async def _capture_response(
        self,
        command: DesktopCommand,
        *,
        result: dict[str, object],
        observed_state: AccessibilityState | None,
        mode: DesktopObservationMode,
        controller_key: str,
        app_id: str,
        state_id: str,
    ) -> DesktopResponse:
        try:
            capture = await asyncio.to_thread(
                self._required_gui_provider().screenshot,
                app_id=app_id,
                state_id=state_id,
            )
            screenshot = await upload_encrypted_media(
                self.client,
                capture.content,
                mime_type=capture.mime_type,
                filename=f"desktop-{command.request_id}.jpg",
                timeout_seconds=MEDIA_UPLOAD_TIMEOUT_SECONDS,
            )
        except (AccessibilityError, DesktopProviderError, DesktopMediaError) as exc:
            return self._capture_error_response(
                command,
                result=result,
                error=self._with_shell_hint(str(exc)),
                observed_state=observed_state,
                mode=mode,
                controller_key=controller_key,
            )
        except Exception:
            logger.exception("desktop_follow_up_screenshot_failed", action=command.action)
            return self._capture_error_response(
                command,
                result=result,
                error="Local screenshot operation failed.",
                observed_state=observed_state,
                mode=mode,
                controller_key=controller_key,
            )
        return self._finalize_app_response(
            command,
            {
                **result,
                "screen": {"width": capture.screen_width, "height": capture.screen_height},
                "capture": {
                    "x": capture.capture_x,
                    "y": capture.capture_y,
                    "width": capture.capture_width,
                    "height": capture.capture_height,
                },
                "image": {"width": capture.image_width, "height": capture.image_height},
                **({"coordinates": capture.coordinates} if capture.coordinates is not None else {}),
            },
            observed_state=observed_state,
            mode=mode,
            controller_key=controller_key,
            screenshot=screenshot,
        )

    def _policy_error(self, command: DesktopCommand, controller_key: str) -> str | None:
        now_ms = round(self.clock() * 1000)
        app_id = command.parameters.get("app")
        controller_entry = self._controllers.get(controller_key)
        if command.issued_at_ms > now_ms + _MAX_FUTURE_SKEW_MS:
            error = "Desktop command was issued too far in the future."
        elif command.expires_at_ms <= now_ms:
            error = "Desktop command expired before local execution."
        elif controller_entry is None or not controller_entry.caller_allowed(command):
            error = "Desktop command requester or agent is not allowed by local policy."
        elif command.action in DESKTOP_FILE_ACTIONS | DESKTOP_SHELL_ACTIONS:
            error = self._local_capability_error(command.action)
        elif command.action in DESKTOP_BROWSER_ACTIONS:
            error = self._browser_policy_error(command, controller_key)
        elif command.action in DESKTOP_APP_ACTIONS and not (
            isinstance(app_id, str)
            and app_targetable(app_id, self.policy.allowed_app_ids, all_apps=self._computer_scope(controller_key))
        ):
            error = "Desktop command must target an application in the local allowlist."
        elif command.action in DESKTOP_CONTROL_ACTIONS:
            error = self._control_policy_error(controller_key)
        else:
            error = None
        return error

    def _control_policy_error(self, controller_key: str | None = None) -> str | None:
        key = self._primary_controller_key() if controller_key is None else controller_key
        grant = self._control_grants.get(key)
        if grant is None:
            return "Desktop control is disabled; this bridge is observe-only."
        if not self._grant_active(grant):
            return "Local desktop control lease has expired."
        return None

    def _local_capability_error(self, action: str) -> str | None:
        if action in DESKTOP_FILE_ACTIONS and not self.policy.allowed_file_roots:
            return "Local file access is disabled."
        if action in DESKTOP_SHELL_ACTIONS and not self.policy.shell_enabled:
            return "Local shell access is disabled."
        return None

    def _browser_policy_error(self, command: DesktopCommand, controller_key: str) -> str | None:
        if not self.policy.browser_enabled or self.browser_provider is None:
            return "Playwright browser extension support is disabled on this desktop bridge."
        try:
            browser_action, _browser_parameters = _browser_command_parameters(command.parameters)
            requires_control = browser_action_requires_control(browser_action)
        except (DesktopProtocolError, PlaywrightBrowserError) as exc:
            return str(exc)
        expected_action = "browser_control" if requires_control else "browser_observe"
        if command.action != expected_action:
            return "Desktop browser command used the wrong observe/control classification."
        if not requires_control:
            return None
        return self._control_policy_error(controller_key)

    async def _execute(
        self,
        command: DesktopCommand,
        controller_key: str,
        *,
        mode: DesktopObservationMode,
        allow_window_fallback: bool = False,
    ) -> _Execution:
        if command.action in DESKTOP_FILE_ACTIONS:
            return _Execution(await execute_file(self.filesystem, command))
        if command.action in DESKTOP_SHELL_ACTIONS:
            return _Execution(await execute_shell(self.client, self.shell, command, controller_key=controller_key))
        self._live_scope(controller_key)
        return await self._execute_desktop(
            command,
            controller_key,
            mode=mode,
            allow_window_fallback=allow_window_fallback,
        )

    def _gui_permission_error(self, command: DesktopCommand, *, mode: DesktopObservationMode) -> str | None:
        missing = missing_macos_desktop_permissions()
        if not missing:
            return None
        app_id = command.parameters.get("app")
        required: list[str] = []
        if command.action == "screenshot":
            required = [name for name in ("Screen Recording",) if name in missing]
        elif command.action == "get_app_state":
            needed = (
                ("Accessibility", "Screen Recording")
                if app_id == PRIMARY_SCREEN_APP_ID and mode == "tree"
                else (("Screen Recording",) if app_id == PRIMARY_SCREEN_APP_ID else ("Accessibility",))
            )
            required = [name for name in needed if name in missing]
        elif command.action in DESKTOP_CONTROL_ACTIONS and command.action != "launch_app":
            required = [name for name in ("Accessibility",) if name in missing]
        if not required:
            return None
        names = " and ".join(required)
        suffix = " Shell commands remain available." if self.policy.shell_enabled else ""
        return f"macOS {names} permission is not granted yet.{suffix}"

    def _with_shell_hint(self, error: str) -> str:
        if (
            self.policy.shell_enabled
            and ("Accessibility" in error or "Screen Recording" in error)
            and "shell commands remain available" not in error.lower()
        ):
            return f"{error} Shell commands remain available."
        return error

    async def _execute_desktop(
        self,
        command: DesktopCommand,
        controller_key: str,
        *,
        mode: DesktopObservationMode,
        allow_window_fallback: bool = False,
    ) -> _Execution:
        parameters = command.parameters
        if command.action in DESKTOP_BROWSER_ACTIONS:
            browser_action, browser_parameters = _browser_command_parameters(parameters)
            if self.browser_provider is None:
                msg = "Playwright browser extension support is disabled on this desktop bridge."
                raise DesktopProtocolError(msg)
            if command.action == "browser_control" and self.provider is None:
                msg = "Desktop GUI is unavailable for browser control."
                raise DesktopProtocolError(msg)
            browser_result = await self.browser_provider.execute(browser_action, browser_parameters)
            return _Execution(browser_result.payload, browser_image=browser_result.image)
        if command.action == "status":
            reject_unexpected_parameters(parameters, allowed=frozenset())
            status: dict[str, object] = (
                await asyncio.to_thread(self.provider.status) if self.provider is not None else {"gui_available": False}
            )
            folders = self.filesystem.list_folders()["folders"] if self.filesystem is not None else []
            return _Execution(
                self._fit_status(
                    command,
                    status,
                    caller_shell_status(self.shell, command, controller_key=controller_key),
                    cast("list[dict[str, str]]", folders),
                    controller_key=controller_key,
                ),
            )
        if command.action == "list_apps":
            reject_unexpected_parameters(parameters, allowed=frozenset())
            apps = await asyncio.to_thread(self.provider.list_apps) if self.provider is not None else []
            # Computer scope lists every running Dock app, so the reply keeps the prefix that fits, primary screen first.
            listed = sorted((app.to_result() for app in apps), key=lambda app: app["id"] != PRIMARY_SCREEN_APP_ID)

            def reply(dropped: int) -> dict[str, object]:
                return {"apps": listed[: len(listed) - dropped], "apps_truncated": dropped > 0}

            return _Execution(reply(leftmost_fitting(command, 0, len(listed), reply)))
        provider = self._gui_provider_for_action(command, mode=mode)
        if command.action == "launch_app":
            reject_unexpected_parameters(parameters, allowed=frozenset({"app"}))
            app_id = required_str_parameter(parameters, "app")
            await asyncio.to_thread(provider.launch_app, app_id)
            return _Execution(
                {"action": command.action, "action_completed": True},
                follow_up_app=app_id,
            )
        if command.action in {"get_app_state", "screenshot"}:
            reject_unexpected_parameters(parameters, allowed=frozenset({"app"}))
            state = await asyncio.to_thread(
                provider.get_app_state,
                required_str_parameter(parameters, "app"),
                include_elements=mode != "screenshot",
            )
            return _Execution(
                {"action": command.action},
                observed_state=state,
                capture_app=state.app_id,
                capture_state_id=state.state_id,
            )

        app_id = required_str_parameter(parameters, "app")
        state_id = required_str_parameter(parameters, "state_id")
        with self._agent_input():
            if command.action in {"click_element", "set_value", "scroll_element", "perform_action"}:
                await execute_semantic_control(provider, command, app_id=app_id, state_id=state_id)
            else:
                await execute_fallback_control(
                    provider,
                    command,
                    app_id=app_id,
                    state_id=state_id,
                    allow_window_fallback=allow_window_fallback,
                )
        return _Execution(
            {"action": command.action, "action_completed": True},
            follow_up_app=app_id,
        )

    def _agent_input(self) -> AbstractContextManager[None]:
        """Keep app input and a shell approval card or prompt from ever being live at the same time."""
        return self.shell.agent_input() if self.shell is not None else nullcontext()

    def _required_gui_provider(self) -> DesktopProvider:
        provider = self.provider
        if provider is None:
            msg = "Desktop GUI is unavailable."
            raise DesktopProtocolError(msg)
        return provider

    def _gui_provider_for_action(
        self,
        command: DesktopCommand,
        *,
        mode: DesktopObservationMode,
    ) -> DesktopProvider:
        provider = self._required_gui_provider()
        permission_error = self._gui_permission_error(command, mode=mode)
        if permission_error is not None:
            raise DesktopProviderError(permission_error)
        return provider

    def _fit_status(
        self,
        command: DesktopCommand,
        provider_status: dict[str, object],
        caller_shell: dict[str, object],
        folders: list[dict[str, str]],
        *,
        controller_key: str,
    ) -> dict[str, object]:
        """Keep the fitting prefix of ``folders`` in the remote reply's ``bridge.file_roots``.

        ``local_status()`` reports every folder unconditionally: only this remote, to-device reply
        is bounded by the inline budget.
        """
        total = len(folders)

        def reply(dropped: int) -> dict[str, object]:
            count = total - dropped
            return {
                **provider_status,
                "bridge": {
                    **self._bridge_status(controller_key),
                    "file_roots": folders[:count],
                    "file_roots_truncated": count < total,
                    "shell": caller_shell,
                },
            }

        dropped = leftmost_fitting(command, 0, total, reply)
        return reply(dropped)

    def _bridge_status(self, controller_key: str | None = None) -> dict[str, object]:
        """Build the shared status fields; callers attach ``file_roots`` themselves (trimmed or not).

        ``gui_mode`` describes only application control; folders and shell access are reported separately.
        """
        control_available = self._control_available(controller_key)
        gui_available = self.provider is not None
        status: dict[str, object] = {
            "gui_mode": "control" if control_available else "observe_only",
            "control_available": control_available,
            "control_scope": "computer" if self._computer_scope(controller_key) else "saved_apps",
            "allowed_app_count": len(self.policy.allowed_app_ids),
            "browser_enabled": self.policy.browser_enabled,
            "gui_available": gui_available,
            "gui_permissions_ready": gui_available and not missing_macos_desktop_permissions(),
            "observation_modes": ["tree", "screenshot", "both"],
            "durable_commands": True,
        }
        if controller_key is not None:
            grant = self._control_grants.get(controller_key)
            expires_at_ms = grant.expires_at_ms if grant is not None else None
        else:
            _, expires_at_ms = self._top_level_lease_summary()
        if expires_at_ms is not None:
            status["control_lease_expires_at_ms"] = expires_at_ms
        return status

    def _controller_local_status(self, controller_key: str) -> dict[str, object]:
        grant = self._control_grants.get(controller_key)
        available = grant is not None and self._grant_active(grant)
        computer = available and grant.computer
        remaining = 0.0
        if available and grant is not None and grant.deadline != math.inf:
            remaining = max(0.0, grant.deadline - self.monotonic_clock())
        shell_entry = (
            self.shell.controller_status(controller_key)
            if self.shell is not None
            else {"auto_approve_remaining_seconds": 0, "auto_approve_until_revoked": False}
        )
        return {
            "gui_mode": ("control" if available else "observe_only") if self._accepting else "stopped",
            "control_available": available,
            "control_scope": "computer" if computer else "saved_apps",
            "lease_expires_at_ms": grant.expires_at_ms if grant is not None else None,
            "lease_remaining_seconds": remaining,
            "shell": shell_entry,
        }

    def _top_level_lease_summary(self) -> tuple[float, int | None]:
        active = [
            grant
            for key, grant in self._control_grants.items()
            if key in self._controllers and self._grant_active(grant)
        ]
        if not active:
            expired_ms = [
                grant.expires_at_ms
                for key, grant in self._control_grants.items()
                if key in self._controllers and grant.expires_at_ms is not None
            ]
            return 0.0, max(expired_ms) if expired_ms else None
        if any(grant.deadline == math.inf for grant in active):
            return 0.0, None
        longest = max(active, key=lambda grant: grant.deadline)
        return max(0.0, longest.deadline - self.monotonic_clock()), longest.expires_at_ms

    def _grant_active(self, grant: _ControlGrant) -> bool:
        return (
            grant.expires_at_ms is None or self.clock() * 1000 < grant.expires_at_ms
        ) and self.monotonic_clock() < grant.deadline

    def _control_available(self, controller_key: str | None = None) -> bool:
        """Report live control; only a local grant sets the deadline of control that has no expiry."""
        if controller_key is not None:
            grant = self._control_grants.get(controller_key)
            return grant is not None and self._grant_active(grant)
        return any(self._grant_active(grant) for key, grant in self._control_grants.items() if key in self._controllers)

    def _computer_scope(self, controller_key: str | None = None) -> bool:
        """Report whether live control reaches every app; expiry, Revoke, and stop all return to the saved apps."""
        if controller_key is not None:
            grant = self._control_grants.get(controller_key)
            return grant is not None and grant.computer and self._grant_active(grant)
        return any(
            grant.computer and self._grant_active(grant)
            for key, grant in self._control_grants.items()
            if key in self._controllers
        )

    def _live_scope(self, controller_key: str) -> bool:
        """Give the provider, which checks every target itself, the scope live control has right now."""
        all_apps = self._computer_scope(controller_key)
        if self.provider is not None:
            self.provider.set_all_apps(all_apps)
        return all_apps

    def _finalize_app_response(
        self,
        command: DesktopCommand,
        result: dict[str, object],
        *,
        observed_state: AccessibilityState | None,
        mode: DesktopObservationMode,
        controller_key: str,
        screenshot: EncryptedDesktopMedia | None = None,
    ) -> DesktopResponse:
        """Fit an app reply within the inline to-device budget and remember only the state sent."""
        if observed_state is None:
            if fits_inline(command, result, screenshot=screenshot):
                return success_response(command, result=result, screenshot=screenshot)
            return self._bounded_fallback_response(command, result, screenshot=screenshot)

        fitted = self._fit_observed_state(
            command,
            result,
            observed_state=observed_state,
            mode=mode,
            screenshot=screenshot,
        )
        if fitted is None:
            return self._bounded_fallback_response(command, result, screenshot=screenshot)
        fitted_state, candidate = fitted
        self._observations.remember(
            fitted_state,
            command,
            controller_key=controller_key,
            screenshot_captured=screenshot is not None,
            tree_captured=mode != "screenshot",
        )
        return success_response(command, result=candidate, screenshot=screenshot)

    @staticmethod
    def _fit_observed_state(
        command: DesktopCommand,
        result: dict[str, object],
        *,
        observed_state: AccessibilityState,
        mode: DesktopObservationMode,
        screenshot: EncryptedDesktopMedia | None,
    ) -> tuple[AccessibilityState, dict[str, object]] | None:
        if mode == "screenshot":
            candidate = {**result, "state": observed_state.to_result(include_elements=False)}
            return (
                (replace(observed_state, elements=()), candidate)
                if fits_inline(command, candidate, screenshot=screenshot)
                else None
            )

        full_candidate = {**result, "state": observed_state.to_result()}
        if fits_inline(command, full_candidate, screenshot=screenshot):
            return observed_state, full_candidate

        elements = observed_state.elements
        total = len(elements)
        if total == 0:
            return None

        def state_with_dropped(dropped: int) -> AccessibilityState:
            return replace(
                observed_state,
                elements=elements[: total - dropped],
                truncated=True,
                stability="truncated",
            )

        def build(dropped: int) -> dict[str, object]:
            return {**result, "state": state_with_dropped(dropped).to_result()}

        dropped = leftmost_fitting(command, 1, total, build, screenshot=screenshot)
        fitted_state = state_with_dropped(dropped)
        candidate = {**result, "state": fitted_state.to_result()}
        return (fitted_state, candidate) if fits_inline(command, candidate, screenshot=screenshot) else None

    @staticmethod
    def _bounded_fallback_response(
        command: DesktopCommand,
        result: dict[str, object],
        *,
        screenshot: EncryptedDesktopMedia | None = None,
    ) -> DesktopResponse:
        preserved: dict[str, object] = {
            key: result[key]
            for key in (
                "action",
                "action_completed",
                "action_outcome",
                "observation",
                "follow_up_screenshot",
                "follow_up_state",
            )
            if key in result
        }
        bounded = {**preserved, "warning": _OMITTED_STATE_WARNING}
        attached_screenshot = (
            screenshot if screenshot is not None and fits_inline(command, bounded, screenshot=screenshot) else None
        )
        return success_response(command, result=bounded, screenshot=attached_screenshot)

    def _observation_refused(
        self,
        command: DesktopCommand,
        execution: _Execution,
        app_id: str,
        controller_key: str,
        *,
        mode: DesktopObservationMode,
    ) -> DesktopResponse | None:
        """Return the receipt alone when the app left the live scope before a new observation of it began."""
        if app_targetable(app_id, self.policy.allowed_app_ids, all_apps=self._live_scope(controller_key)):
            return None
        warning = "Access to this application ended before a new observation; none was taken."
        return self._finalize_app_response(
            command,
            {**execution.result, "warning": warning},
            observed_state=execution.observed_state,
            mode=mode,
            controller_key=controller_key,
        )

    def _capture_error_response(
        self,
        command: DesktopCommand,
        *,
        result: dict[str, object],
        error: str,
        observed_state: AccessibilityState | None = None,
        mode: DesktopObservationMode = "both",
        controller_key: str = "",
    ) -> DesktopResponse:
        if command.action == "get_app_state":
            warning = (
                f"Accessibility state was read, but its app-window screenshot failed: {error} "
                "The state may now be stale; "
                "request get_app_state again before acting."
            )
            return self._finalize_app_response(
                command,
                {**result, "warning": warning, "follow_up_screenshot": "failed"},
                observed_state=observed_state,
                mode=mode,
                controller_key=controller_key,
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
        return self._finalize_app_response(
            command,
            {**result, "warning": warning, "follow_up_screenshot": "failed"},
            observed_state=observed_state,
            mode=mode,
            controller_key=controller_key,
        )

    def _unknown_control_response(self, command: DesktopCommand, *, reason: str | None = None) -> DesktopResponse:
        if command.action in DESKTOP_SHELL_ACTIONS:
            warning = (
                "The shell command outcome is unknown and it may have completed; do not repeat it automatically. "
                "Inspect its effects before deciding the next step."
            )
        else:
            recovery_action = (
                "browser(action='tabs' or 'snapshot', target='desktop')"
                if command.action in DESKTOP_BROWSER_ACTIONS
                else "get_app_state"
            )
            warning = (
                "The desktop action outcome is unknown and it may have completed; "
                "do not repeat the action automatically. "
                f"Request {recovery_action} before deciding the next step."
            )
        if reason is not None:
            warning = f"{reason} {warning}"
        logger.warning(
            "desktop_control_outcome_unknown",
            request_id=command.request_id,
            action=command.action,
            requester_id=command.requester_id,
            agent_name=command.agent_name,
            error=reason,
        )
        result: dict[str, object] = {"action": command.action, "action_outcome": "unknown", "warning": warning}
        if fits_inline(command, result):
            return success_response(command, result=result)
        return self._bounded_fallback_response(command, result)

    def _interrupted_response(self, command: DesktopCommand) -> DesktopResponse:
        if command.action in DESKTOP_CONTROL_ACTIONS | _EFFECTFUL_SHELL_ACTIONS:
            return self._unknown_control_response(command)
        return self._error_response(
            command,
            "Desktop observation was interrupted before its outcome was recorded; retry with a new request.",
        )

    async def _send_response(self, response: DesktopResponse, controller: PinnedMatrixDevice) -> bool:
        try:
            await send_encrypted_to_device(
                self.client,
                controller,
                event_type=DESKTOP_RESPONSE_EVENT_TYPE,
                content=response.to_content(),
            )
        except OlmToDeviceError:
            logger.exception("desktop_response_delivery_failed", request_id=response.request_id)
            return False
        return True

    @staticmethod
    def _error_response(command: DesktopCommand, error: str) -> DesktopResponse:
        if len(error) > _MAX_ERROR_CHARS:
            error = f"{error[: _MAX_ERROR_CHARS - 3]}..."
        return DesktopResponse(
            request_id=command.request_id,
            session_id=command.session_id,
            ok=False,
            error=error,
        )


def _command_fingerprint(command: DesktopCommand) -> str:
    encoded = json.dumps(
        command.to_content(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _browser_command_parameters(parameters: dict[str, object]) -> tuple[str, dict[str, object]]:
    reject_unexpected_parameters(
        parameters,
        allowed=frozenset({"browser_action", "browser_parameters"}),
    )
    return (
        required_str_parameter(parameters, "browser_action"),
        required_object_parameter(parameters, "browser_parameters"),
    )


__all__ = ["DesktopBridge", "DesktopBridgePolicy", "DesktopControllerPolicy"]
