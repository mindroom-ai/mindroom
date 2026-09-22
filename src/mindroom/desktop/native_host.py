"""App-owned native desktop helper lifecycle and stdio protocol."""

# Platform and optional runtime imports stay lazy so status works before desktop
# extras are installed. The action dispatcher mirrors the finite wire action set.
# ruff: noqa: ANN401, C901, EM101, PLC0415, PLR0911, PLR0912, PLR0915, SIM105, TRY003

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

from mindroom.desktop.command_journal import DesktopCommandJournalError, check_controller_binding
from mindroom.desktop.native_config import (
    NativeConfigError,
    NativeDesktopConfig,
    load_native_config,
    native_config_path,
    save_native_config,
)
from mindroom.desktop.native_protocol import (
    MAX_NATIVE_INPUT_BYTES,
    NATIVE_PROTOCOL_VERSION,
    NativeProtocolError,
    NativeRequest,
    encode_native_message,
    parse_native_request,
)
from mindroom.desktop.protocol import DesktopSetupDescriptor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths


_MAX_REGULAR_NATIVE_REQUESTS = 4
_MAX_STOP_NATIVE_REQUESTS = 1
_IMMEDIATE_NATIVE_ACTIONS = frozenset({"status", "revoke_control", "reset_emergency_stop"})


class _NativeBridgeRuntimeProtocol(Protocol):
    """Bridge operations exposed to the local coordinator."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def status(self) -> dict[str, object]: ...
    def grant_control(self, duration_seconds: int) -> dict[str, object]: ...
    def revoke_control(self) -> dict[str, object]: ...
    def reset_emergency_stop(self) -> dict[str, object]: ...
    async def connect_browser(self) -> None: ...
    async def disconnect_browser(self) -> None: ...


type _LoginHandler = Callable[[RuntimePaths, dict[str, object]], Awaitable[dict[str, object]]]
type _PairHandler = Callable[[RuntimePaths, NativeDesktopConfig, dict[str, object]], Awaitable[dict[str, object]]]
type _RuntimeFactory = Callable[[RuntimePaths, NativeDesktopConfig], _NativeBridgeRuntimeProtocol]


@dataclass(frozen=True, slots=True)
class NativeHostDependencies:
    """Replaceable effects used by portable lifecycle tests."""

    runtime_factory: _RuntimeFactory | None = None
    login: _LoginHandler | None = None
    pair: _PairHandler | None = None


class NativeDesktopHost:
    """Serialize setup and local bridge authority operations."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        helper_version: str,
        dependencies: NativeHostDependencies | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._helper_version = helper_version
        self._dependencies = dependencies or NativeHostDependencies()
        self._lock = asyncio.Lock()
        self._runtime: _NativeBridgeRuntimeProtocol | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._helper_state = "running"
        self._last_error: dict[str, object] | None = None
        self._pairing_state = "unpaired"
        self._config: NativeDesktopConfig | None = None
        self._config_error_revision = 0
        try:
            self._config = load_native_config(native_config_path(runtime_paths.storage_root))
        except NativeConfigError as exc:
            self._config_error_revision = exc.revision
            if exc.code != "configuration_missing":
                self._last_error = _error_payload(exc.code, str(exc))

    def hello(self) -> dict[str, object]:
        """Return the first process record."""
        return {
            "v": NATIVE_PROTOCOL_VERSION,
            "type": "hello",
            "protocol_version": NATIVE_PROTOCOL_VERSION,
            "helper_version": self._helper_version,
            "capabilities": ["observe", "control", "browser"],
        }

    def status(self) -> dict[str, object]:
        """Return complete redacted process state."""
        config = self._config
        session_state, session_identity = _saved_session_identity(self._runtime_paths)
        runtime_status = self._runtime.status() if self._runtime is not None else {}
        mode = str(runtime_status.get("mode", "stopped"))
        bridge_state = mode if mode in {"stopped", "observe_only", "control", "stopping", "faulted"} else "faulted"
        if self._helper_state == "stopping":
            bridge_state = "stopping"
        browser_configured = bool(config and config.browser.enabled)
        return {
            "config": {
                "state": "ready" if config is not None else ("invalid" if self._last_error else "missing"),
                "revision": config.revision if config is not None else self._config_error_revision,
                "enabled": config.enabled if config is not None else False,
                "controller_user_id": config.controller.user_id if config is not None else None,
                "controller_device_id": config.controller.device_id if config is not None else None,
                "allowed_requester_ids": list(config.allowed_requester_ids) if config is not None else [],
                "allowed_agent_names": list(config.allowed_agent_names) if config is not None else [],
                "allowed_app_ids": list(config.allowed_app_ids) if config is not None else [],
            },
            "pairing": {
                "state": self._pairing_state,
                "session_state": session_state,
                "homeserver": session_identity.get("homeserver"),
                "user_id": session_identity.get("user_id"),
                "device_id": session_identity.get("device_id"),
                "controller_fingerprint": config.controller.ed25519 if config is not None else None,
            },
            "helper": {"state": self._helper_state, "version": self._helper_version},
            "bridge": {
                "state": bridge_state,
                "active_action": runtime_status.get("active_action"),
                "last_error": runtime_status.get("last_error", self._last_error),
            },
            "authority": {
                "control_available": bool(runtime_status.get("control_available", False)),
                "lease_remaining_seconds": runtime_status.get("lease_remaining_seconds", 0),
                "lease_expires_at_ms": runtime_status.get("lease_expires_at_ms"),
                "emergency_stop_latched": bool(runtime_status.get("emergency_stop_latched", False)),
            },
            "permissions": _permission_status(),
            "browser": {
                "configured": browser_configured,
                "executable_path": str(config.browser.executable_path)
                if config and config.browser.executable_path
                else None,
                "user_data_dir": str(config.browser.user_data_dir) if config and config.browser.user_data_dir else None,
                "runtime": "available" if shutil.which("npx") else "missing",
                "extension": (
                    "connected"
                    if runtime_status.get("browser_connected")
                    else ("disconnected" if browser_configured else "disabled")
                ),
                "reconnect_token_configured": bool(self._runtime_paths.env_value("PLAYWRIGHT_MCP_EXTENSION_TOKEN")),
                "last_error": None,
            },
            "apps": [
                {"id": app_id, "name": app_id, "installed": None, "running": None}
                for app_id in (config.allowed_app_ids if config is not None else ())
            ],
            "capabilities": ["observe", "control", "browser"],
        }

    async def handle(self, request: NativeRequest) -> dict[str, object]:
        """Execute one request and return a redacted result."""
        if request.action in {"status", "stop", "revoke_control", "reset_emergency_stop"}:
            return await self._handle_guarded(request)
        async with self._lock:
            return await self._handle_guarded(request)

    async def _handle_guarded(self, request: NativeRequest) -> dict[str, object]:
        try:
            return await self._handle_locked(request)
        except NativeProtocolError:
            raise
        except NativeConfigError as exc:
            raise NativeProtocolError(exc.code, str(exc)) from exc
        except ValueError as exc:
            raise NativeProtocolError("invalid_request", str(exc)) from exc
        except Exception as exc:
            message = str(exc) or "Native desktop operation failed."
            self._last_error = _error_payload(
                "internal_error",
                message,
                recovery="Retry the operation or restart the helper.",
                retryable=True,
            )
            raise NativeProtocolError(
                "internal_error",
                message,
                recovery="Retry the operation or restart the helper.",
                retryable=True,
            ) from exc

    async def _handle_locked(self, request: NativeRequest) -> dict[str, object]:
        action, parameters = request.action, request.parameters
        if action == "status":
            _expect_keys(parameters, set())
            return {"status": self.status()}
        if action in {"configure", "set_allowed_apps"}:
            config_key = "config" if action == "configure" else "allowed_app_ids"
            _expect_keys(parameters, {"expected_revision", config_key})
            if self._runtime is not None:
                raise NativeProtocolError("busy", "Stop the desktop bridge before changing its configuration.")
            if action == "set_allowed_apps":
                if self._config is None:
                    raise NativeProtocolError("invalid_request", "Complete desktop setup before saving app access.")
                config = self._config.with_allowed_apps(parameters.get("allowed_app_ids"))
            else:
                config = NativeDesktopConfig.from_payload(parameters.get("config"))
            try:
                check_controller_binding(
                    self._runtime_paths.storage_root / "desktop_bridge" / "commands.sqlite3",
                    json.dumps([config.controller.user_id, config.controller.device_id, config.controller.ed25519]),
                )
            except DesktopCommandJournalError as exc:
                raise NativeProtocolError("invalid_request", str(exc)) from exc
            self._config = save_native_config(
                native_config_path(self._runtime_paths.storage_root),
                config,
                expected_revision=_required_int(parameters, "expected_revision", minimum=0),
            )
            self._last_error = None
            return {"status": self.status()}
        if action == "import_setup":
            _expect_keys(parameters, {"descriptor"})
            descriptor = DesktopSetupDescriptor.from_content(parameters.get("descriptor"))
            return descriptor.to_content()
        if action == "login":
            if self._runtime is not None:
                raise NativeProtocolError("busy", "Stop the desktop bridge before replacing its Matrix session.")
            try:
                details = await (self._dependencies.login or _login)(self._runtime_paths, parameters)
            except NativeProtocolError:
                raise
            except Exception as exc:
                raise NativeProtocolError(
                    "login_failed",
                    str(exc) or "Desktop Matrix login failed.",
                    recovery="Check the account, homeserver, and login method, then retry.",
                    retryable=True,
                ) from exc
            self._pairing_state = "unpaired"
            return {**_redacted_identity(details), "status": self.status()}
        if action == "pair":
            if self._runtime is not None:
                raise NativeProtocolError("busy", "Stop the desktop bridge before pairing.")
            self._pairing_state = "claiming"
            try:
                result = await (self._dependencies.pair or _pair)(
                    self._runtime_paths,
                    self._require_config(),
                    parameters,
                )
            except Exception as exc:
                self._pairing_state = "invalid"
                if isinstance(exc, NativeProtocolError):
                    raise
                raise NativeProtocolError(
                    "pairing_failed",
                    str(exc) or "Desktop pairing failed.",
                    recovery="Generate a fresh pairing code in the same agent chat and retry.",
                    retryable=True,
                ) from exc
            self._pairing_state = "awaiting_chat_confirmation"
            return {**result, "status": self.status()}
        if action == "start":
            _expect_keys(parameters, set())
            config = self._require_config()
            if not config.enabled:
                raise NativeProtocolError("configuration_missing", "Enable Desktop Control before starting.")
            if not config.allowed_app_ids:
                raise NativeProtocolError("configuration_missing", "Select and save at least one app before starting.")
            if self._runtime is not None:
                raise NativeProtocolError("already_running", "The desktop bridge is already running.")
            runtime = (self._dependencies.runtime_factory or NativeBridgeRuntime)(self._runtime_paths, config)
            self._helper_state = "starting"
            self._startup_task = asyncio.create_task(runtime.start())
            try:
                await self._startup_task
            except asyncio.CancelledError as exc:
                self._helper_state = "running"
                raise NativeProtocolError("not_running", "Desktop bridge startup was stopped.") from exc
            except Exception as exc:
                self._helper_state = "faulted"
                message = str(exc) or "Desktop bridge start failed."
                code = "session_missing" if "session" in message.lower() else "internal_error"
                raise NativeProtocolError(
                    code,
                    message,
                    recovery="Complete sign-in and pairing, then retry.",
                    retryable=True,
                ) from exc
            finally:
                self._startup_task = None
            self._runtime = runtime
            self._helper_state = "running"
            return {"status": self.status()}
        if action == "stop":
            _expect_keys(parameters, set())
            if self._startup_task is not None:
                self._helper_state = "stopping"
                self._startup_task.cancel()
                # Startup owns cleanup; wait until its handler has released
                # the lifecycle lock before deciding whether it published a runtime.
                async with self._lock:
                    pass
                if self._runtime is None:
                    self._helper_state = "running"
                    return {"status": self.status()}
            runtime = self._require_runtime()
            self._helper_state = "stopping"
            await runtime.stop()
            if self._runtime is runtime:
                self._runtime = None
            self._helper_state = "running"
            return {"status": self.status()}
        if action == "grant_control":
            _expect_keys(parameters, {"duration_seconds"})
            try:
                self._require_runtime().grant_control(
                    _required_int(parameters, "duration_seconds", minimum=60, maximum=3600),
                )
            except ValueError as exc:
                raise NativeProtocolError("control_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "revoke_control":
            _expect_keys(parameters, set())
            self._require_runtime().revoke_control()
            return {"status": self.status()}
        if action == "reset_emergency_stop":
            _expect_keys(parameters, set())
            try:
                self._require_runtime().reset_emergency_stop()
            except ValueError as exc:
                raise NativeProtocolError("control_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "request_permission":
            _expect_keys(parameters, {"permission"})
            permission = parameters.get("permission")
            if permission not in {"accessibility", "screen_recording"}:
                raise NativeProtocolError("invalid_request", "Permission must be accessibility or screen_recording.")
            try:
                _request_permission(str(permission))
            except NativeProtocolError:
                raise
            except Exception as exc:
                raise NativeProtocolError(
                    "permission_missing",
                    str(exc) or "macOS could not request the desktop permission.",
                    recovery="Open System Settings > Privacy & Security and grant the permission.",
                ) from exc
            return {"status": self.status()}
        if action == "browser_connect":
            _expect_keys(parameters, set())
            config = self._require_config()
            if not config.browser.enabled:
                raise NativeProtocolError(
                    "browser_unavailable",
                    "Browser integration is disabled.",
                    recovery="Enable browser integration in Desktop Control.",
                )
            if shutil.which("npx") is None:
                raise NativeProtocolError(
                    "browser_unavailable",
                    "The browser automation runtime is missing.",
                    recovery="Install Node.js, then reopen Desktop Control.",
                )
            runtime = self._require_runtime()
            try:
                await runtime.connect_browser()
            except Exception as exc:
                raise NativeProtocolError(
                    "browser_unavailable",
                    str(exc) or "Browser extension connection failed.",
                    recovery="Check the extension, selected profile, and reconnect token, then retry.",
                    retryable=True,
                ) from exc
            return {"status": self.status()}
        if action == "browser_disconnect":
            _expect_keys(parameters, set())
            await self._require_runtime().disconnect_browser()
            return {"status": self.status()}
        raise NativeProtocolError("invalid_request", "Native desktop action is unsupported.")

    async def shutdown(self) -> None:
        """Clear local authority and close owned runtime resources."""
        async with self._lock:
            self._helper_state = "stopping"
            if self._runtime is not None:
                runtime, self._runtime = self._runtime, None
                await runtime.stop()
            self._helper_state = "stopped"

    def _require_config(self) -> NativeDesktopConfig:
        if self._config is None:
            raise NativeProtocolError(
                "configuration_missing",
                "Native desktop configuration is missing.",
                recovery="Complete Desktop Control setup first.",
            )
        return self._config

    def _require_runtime(self) -> _NativeBridgeRuntimeProtocol:
        if self._runtime is None:
            raise NativeProtocolError("not_running", "The desktop bridge is not running.")
        return self._runtime


class NativeBridgeRuntime:
    """Own the existing Python bridge, transport, provider, browser, and session."""

    def __init__(self, runtime_paths: RuntimePaths, config: NativeDesktopConfig) -> None:
        self._runtime_paths, self._config = runtime_paths, config
        self._owner: Any = None
        self._bridge: Any = None
        self._browser: Any = None
        self._registration: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False
        self._fault: str | None = None
        self._browser_connected = False

    async def start(self) -> None:
        """Open one observe-only bridge session."""
        from nio import AuthenticatedToDeviceEvent

        from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
        from mindroom.desktop.cloudflare_access import cloudflare_access_headers
        from mindroom.desktop.playwright_mcp import PlaywrightMCPBrowserProvider
        from mindroom.desktop.provider import PyAutoGuiDesktopProvider
        from mindroom.desktop.session import (
            desktop_session_path,
            load_desktop_http_headers,
            load_desktop_session,
            open_desktop_client,
            prepare_desktop_client,
        )
        from mindroom.desktop.transport import DesktopTransport
        from mindroom.matrix.olm_to_device import resolve_pinned_device

        session = load_desktop_session(desktop_session_path(self._runtime_paths))
        headers_path = _optional_env_path(self._runtime_paths, "MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE")
        http_headers = load_desktop_http_headers(headers_path)
        if session.cloudflare_access:
            http_headers = cloudflare_access_headers(session.homeserver, http_headers)
        try:
            if self._config.browser.enabled:
                self._browser = PlaywrightMCPBrowserProvider(
                    output_dir=self._runtime_paths.storage_root / "desktop-browser",
                    executable_path=self._config.browser.executable_path,
                    user_data_dir=self._config.browser.user_data_dir,
                    call_timeout_seconds=self._config.browser.timeout_seconds,
                    extension_token=self._runtime_paths.env_value("PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
                )
            self._owner = await open_desktop_client(
                session,
                runtime_paths=self._runtime_paths,
                http_headers=http_headers,
            )
            provider = PyAutoGuiDesktopProvider(
                allowed_app_ids=frozenset(self._config.allowed_app_ids),
                max_screenshot_width=self._config.capture.max_screenshot_width,
                jpeg_quality=self._config.capture.jpeg_quality,
            )
            self._bridge = DesktopBridge(
                client=self._owner.client,
                provider=provider,
                policy=DesktopBridgePolicy(
                    controller=self._config.controller,
                    allowed_requester_ids=frozenset(self._config.allowed_requester_ids),
                    allowed_agent_names=frozenset(self._config.allowed_agent_names),
                    allowed_app_ids=frozenset(self._config.allowed_app_ids),
                    allow_control=False,
                    control_lease_expires_at_ms=None,
                    browser_enabled=self._config.browser.enabled,
                ),
                browser_provider=self._browser,
                journal_path=self._runtime_paths.storage_root / "desktop_bridge" / "commands.sqlite3",
                legacy_journal_path=self._runtime_paths.storage_root / "desktop_bridge" / "command_journal.json",
            )
            self._owner.client.add_to_device_callback(self._bridge.on_to_device_event, AuthenticatedToDeviceEvent)
            self._registration = self._owner.client.to_device_callbacks[-1]
            await resolve_pinned_device(self._owner.client, self._config.controller)
            await prepare_desktop_client(self._owner.client)
            transport = DesktopTransport(self._owner.source, wait_for_capacity=self._bridge.wait_for_capacity)
            self._tasks = {
                asyncio.create_task(self._bridge.run(), name="native_desktop_workers"),
                asyncio.create_task(transport.run(), name="native_desktop_transport"),
            }
            self._supervisor = asyncio.create_task(self._supervise(), name="native_desktop_supervisor")
        except BaseException:
            await self._cleanup()
            raise

    async def stop(self) -> None:
        """Fence admission, drain the active action, and close resources."""
        self._stopping = True
        if self._bridge is not None:
            await self._bridge.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._supervisor is not None and self._supervisor is not asyncio.current_task():
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        await self._cleanup()

    def status(self) -> dict[str, object]:
        """Return current local bridge authority."""
        if self._fault is not None:
            return {
                "mode": "faulted",
                "control_available": False,
                "lease_remaining_seconds": 0,
                "lease_expires_at_ms": None,
                "emergency_stop_latched": False,
                "active_action": None,
                "last_error": _error_payload(
                    "internal_error",
                    self._fault,
                    recovery="Stop and restart the desktop bridge.",
                    retryable=True,
                ),
                "browser_connected": False,
            }
        if self._bridge is None:
            return {"mode": "stopped", "control_available": False, "browser_connected": False}
        return {**self._bridge.local_status(), "browser_connected": self._browser_connected}

    def grant_control(self, duration_seconds: int) -> dict[str, object]:
        """Grant a bounded local control lease."""
        return self._required_bridge().grant_local_control(duration_seconds)

    def revoke_control(self) -> dict[str, object]:
        """Revoke new local control actions."""
        return self._required_bridge().revoke_local_control()

    def reset_emergency_stop(self) -> dict[str, object]:
        """Reset the local emergency latch while idle."""
        return self._required_bridge().reset_local_emergency_stop()

    async def connect_browser(self) -> None:
        """Connect the configured installed-profile extension."""
        if self._browser is None:
            raise ValueError("Browser integration is not configured.")
        await self._browser.execute("start", {})
        self._browser_connected = True

    async def disconnect_browser(self) -> None:
        """Disconnect the installed-profile extension without stopping Matrix."""
        if self._browser is None:
            raise ValueError("Browser integration is not configured.")
        await self._browser.execute("stop", {})
        self._browser_connected = False

    def _required_bridge(self) -> Any:
        if self._bridge is None:
            raise ValueError("The desktop bridge is not running.")
        return self._bridge

    async def _supervise(self) -> None:
        assert self._bridge is not None
        failure = await supervise_native_tasks(self._tasks, self._bridge.stop)
        if not self._stopping:
            self._fault = str(failure)
            await self._cleanup()

    async def _cleanup(self) -> None:
        self._tasks.clear()
        if self._registration is not None and self._owner is not None:
            try:
                self._owner.client.to_device_callbacks.remove(self._registration)
            except ValueError:
                pass
        self._registration = None
        if self._bridge is not None:
            self._bridge.close()
        self._bridge = None
        if self._browser is not None:
            await self._browser.close()
        self._browser = None
        self._browser_connected = False
        if self._owner is not None:
            await self._owner.close()
        self._owner = None
        if self._supervisor is asyncio.current_task():
            self._supervisor = None


async def supervise_native_tasks(
    tasks: set[asyncio.Task[None]],
    fence: Callable[[], Awaitable[None]],
) -> BaseException:
    """Fence the bridge on first worker exit, then cancel and drain peers."""
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    failure: BaseException | None = None
    for task in done:
        try:
            await task
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
            break
    if failure is None:
        failure = RuntimeError("A native desktop worker stopped unexpectedly.")
    await fence()
    for task in tasks:
        if task not in done:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return failure


async def _login(runtime_paths: RuntimePaths, parameters: dict[str, object]) -> dict[str, object]:
    from mindroom.desktop.login_method import DesktopLoginMethod
    from mindroom.desktop.session import (
        client_ed25519_fingerprint,
        desktop_session_path,
        login_desktop_client,
        resolve_desktop_login_method,
        save_desktop_session,
    )
    from mindroom.desktop.sso import receive_sso_login_token

    _allow_keys(parameters, {"homeserver", "user_id", "method", "password", "login_token", "sso_idp", "replace"})
    homeserver = _required_text(parameters, "homeserver")
    user_id = _optional_text(parameters, "user_id")
    try:
        requested = DesktopLoginMethod(str(parameters.get("method", "auto")))
    except ValueError as exc:
        raise NativeProtocolError("invalid_request", "Login method must be auto, password, or sso.") from exc
    session_path = desktop_session_path(runtime_paths)
    replace_existing = parameters.get("replace", False)
    if not isinstance(replace_existing, bool):
        raise NativeProtocolError("invalid_request", "Login replace must be a boolean.")
    try:
        session_mode = session_path.lstat().st_mode
    except FileNotFoundError:
        session_mode = None
    if session_mode is not None:
        if not replace_existing:
            raise NativeProtocolError(
                "login_failed",
                "A desktop Matrix session already exists.",
                recovery="Choose Replace session only when creating a new device.",
            )
        if not stat.S_ISREG(session_mode):
            raise NativeProtocolError(
                "login_failed",
                "The saved Matrix session path is not a regular file.",
                recovery="Move the directory, link, or special file aside before signing in again.",
            )
    method = await resolve_desktop_login_method(requested, homeserver=homeserver, runtime_paths=runtime_paths)
    password, login_token = _optional_text(parameters, "password"), _optional_text(parameters, "login_token")
    if method is DesktopLoginMethod.PASSWORD:
        if user_id is None or password is None or login_token is not None:
            raise NativeProtocolError("invalid_request", "Password login requires user_id and password only.")
    else:
        if password is not None:
            raise NativeProtocolError("invalid_request", "SSO login does not accept a password.")
        if login_token is None:
            login_token = await asyncio.to_thread(
                receive_sso_login_token,
                homeserver,
                open_browser=True,
                announce=lambda message: print(message, file=sys.stderr, flush=True),
                idp_id=_optional_text(parameters, "sso_idp"),
            )
    owner, session = await login_desktop_client(
        homeserver=homeserver,
        user_id=user_id,
        password=password,
        login_token=login_token,
        runtime_paths=runtime_paths,
    )
    try:
        save_desktop_session(session_path, session)
        return {
            "homeserver": session.homeserver,
            "user_id": session.user_id,
            "device_id": session.device_id,
            "ed25519": client_ed25519_fingerprint(owner.client),
        }
    finally:
        await owner.close()


async def _pair(
    runtime_paths: RuntimePaths,
    config: NativeDesktopConfig,
    parameters: dict[str, object],
) -> dict[str, object]:
    from mindroom.desktop.cloudflare_access import cloudflare_access_headers
    from mindroom.desktop.pairing_client import send_desktop_pairing_claim
    from mindroom.desktop.session import (
        desktop_session_path,
        load_desktop_http_headers,
        load_desktop_session,
        open_desktop_client,
    )

    _expect_keys(parameters, {"code"})
    code = _required_text(parameters, "code")
    session = load_desktop_session(desktop_session_path(runtime_paths))
    headers_path = _optional_env_path(runtime_paths, "MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE")
    http_headers = load_desktop_http_headers(headers_path)
    if session.cloudflare_access:
        http_headers = cloudflare_access_headers(session.homeserver, http_headers)
    owner = await open_desktop_client(session, runtime_paths=runtime_paths, http_headers=http_headers)
    try:
        verification = await send_desktop_pairing_claim(owner, config.controller, code=code)
        return {"verification": verification, "confirmation_command": f"!desktop confirm {code} {verification}"}
    finally:
        await owner.close()


async def serve_native_stream(
    host: NativeDesktopHost,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> None:
    """Serve bounded NDJSON until the parent closes stdin."""
    sequence = 0
    emit_lock = asyncio.Lock()
    last_status = ""

    async def emit(payload: dict[str, object]) -> None:
        async with emit_lock:
            output_stream.write(encode_native_message(payload))
            output_stream.flush()

    async def emit_status_if_changed() -> None:
        nonlocal last_status, sequence
        status = host.status()
        rendered = repr(status)
        if rendered == last_status:
            return
        last_status = rendered
        sequence += 1
        await emit({"v": 1, "type": "status", "sequence": sequence, "status": status})

    async def watch_status() -> None:
        while True:
            await asyncio.sleep(1)
            await emit_status_if_changed()

    await emit(host.hello())
    await emit_status_if_changed()
    watcher = asyncio.create_task(watch_status(), name="native_desktop_status")
    regular_tasks: set[asyncio.Task[None]] = set()
    stop_tasks: set[asyncio.Task[None]] = set()

    async def answer(request: NativeRequest) -> None:
        try:
            result = await host.handle(request)
            await emit({"v": 1, "type": "response", "request_id": request.request_id, "ok": True, "result": result})
            if request.action != "status":
                await emit_status_if_changed()
        except NativeProtocolError as exc:
            await emit(
                {
                    "v": 1,
                    "type": "response",
                    "request_id": request.request_id,
                    "ok": False,
                    "error": exc.to_payload(),
                },
            )

    async def reject_busy(request: NativeRequest) -> None:
        error = NativeProtocolError(
            "busy",
            "The native desktop helper is handling its maximum number of local requests.",
            recovery="Wait for the current operation to finish, then retry.",
            retryable=True,
        )
        await emit(
            {"v": 1, "type": "response", "request_id": request.request_id, "ok": False, "error": error.to_payload()},
        )

    try:
        while line := await _read_native_line(input_stream):
            try:
                request = parse_native_request(line.rstrip(b"\r\n"))
            except NativeProtocolError as exc:
                await emit(
                    {"v": 1, "type": "response", "request_id": None, "ok": False, "error": exc.to_payload()},
                )
                continue
            if request.action in _IMMEDIATE_NATIVE_ACTIONS:
                await answer(request)
                continue
            tasks, limit = (
                (stop_tasks, _MAX_STOP_NATIVE_REQUESTS)
                if request.action == "stop"
                else (regular_tasks, _MAX_REGULAR_NATIVE_REQUESTS)
            )
            if len(tasks) >= limit:
                await reject_busy(request)
                continue
            task = asyncio.create_task(answer(request), name=f"native_desktop_request_{request.action}")
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            await asyncio.sleep(0)
    finally:
        for task in regular_tasks:
            task.cancel()
        await asyncio.gather(*regular_tasks, return_exceptions=True)
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await host.shutdown()


async def _read_native_line(input_stream: BinaryIO) -> bytes:
    """Read one record and drain the remainder of an oversized line."""
    line = await asyncio.to_thread(input_stream.readline, MAX_NATIVE_INPUT_BYTES + 2)
    if len(line) <= MAX_NATIVE_INPUT_BYTES or line.endswith(b"\n"):
        return line
    while chunk := await asyncio.to_thread(input_stream.readline, MAX_NATIVE_INPUT_BYTES + 2):
        if chunk.endswith(b"\n"):
            break
    return line


async def run_native_stdio(runtime_paths: RuntimePaths, *, helper_version: str) -> None:
    """Run the packaged helper over inherited standard streams."""
    protocol_output = sys.stdout.buffer
    sys.stdout = sys.stderr
    await serve_native_stream(
        NativeDesktopHost(runtime_paths, helper_version=helper_version),
        input_stream=sys.stdin.buffer,
        output_stream=protocol_output,
    )


def _permission_status() -> dict[str, object]:
    unknown = {"state": "unknown", "can_request": sys.platform == "darwin", "recovery": None}
    if sys.platform != "darwin":
        return {"accessibility": unknown.copy(), "screen_recording": unknown.copy()}
    try:
        import ApplicationServices
        import Quartz

        accessibility = bool(ApplicationServices.AXIsProcessTrusted())
        screen_recording = bool(Quartz.CGPreflightScreenCaptureAccess())
    except (ImportError, AttributeError):
        return {"accessibility": unknown.copy(), "screen_recording": unknown.copy()}
    return {
        "accessibility": {
            "state": "granted" if accessibility else "missing",
            "can_request": True,
            "recovery": None if accessibility else "Grant Accessibility in System Settings > Privacy & Security.",
        },
        "screen_recording": {
            "state": "granted" if screen_recording else "missing",
            "can_request": True,
            "recovery": None if screen_recording else "Grant Screen Recording in System Settings > Privacy & Security.",
        },
    }


def _request_permission(permission: str) -> None:
    if sys.platform != "darwin":
        raise NativeProtocolError("permission_missing", "Desktop permissions are available only on macOS.")
    if permission == "accessibility":
        import ApplicationServices

        ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: True},
        )
    else:
        import Quartz

        Quartz.CGRequestScreenCaptureAccess()


def _expect_keys(parameters: dict[str, object], allowed: set[str]) -> None:
    if set(parameters) != allowed:
        raise NativeProtocolError("invalid_request", "Action parameters are missing or unsupported.")


def _allow_keys(parameters: dict[str, object], allowed: set[str]) -> None:
    if set(parameters) - allowed:
        raise NativeProtocolError("invalid_request", "Action parameters contain unsupported fields.")


def _required_text(parameters: dict[str, object], key: str) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise NativeProtocolError("invalid_request", f"Native desktop {key} must be non-empty text.")
    return value


def _optional_text(parameters: dict[str, object], key: str) -> str | None:
    return None if parameters.get(key) is None else _required_text(parameters, key)


def _required_int(parameters: dict[str, object], key: str, *, minimum: int, maximum: int | None = None) -> int:
    value = parameters.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise NativeProtocolError("invalid_request", f"Native desktop {key} is outside its allowed range.")
    return value


def _saved_session_identity(runtime_paths: RuntimePaths) -> tuple[str, dict[str, str]]:
    """Read only the saved device identity, without opening a Matrix connection."""
    # Keep the Matrix/crypto imports in session out of native protocol startup.
    from mindroom.desktop.session import (
        DesktopSessionError,
        DesktopSessionNotFoundError,
        desktop_session_path,
        load_desktop_session,
    )

    try:
        session = load_desktop_session(desktop_session_path(runtime_paths))
    except DesktopSessionNotFoundError:
        return "missing", {}
    except (DesktopSessionError, OSError):
        return "invalid", {}
    return "ready", {
        "homeserver": session.homeserver,
        "user_id": session.user_id,
        "device_id": session.device_id,
    }


def _redacted_identity(details: dict[str, object]) -> dict[str, object]:
    return {key: details[key] for key in ("homeserver", "user_id", "device_id", "ed25519") if key in details}


def _error_payload(
    code: str,
    message: str,
    *,
    recovery: str | None = None,
    retryable: bool = False,
) -> dict[str, object]:
    return {"code": code, "message": message, "recovery": recovery, "retryable": retryable}


def _optional_env_path(runtime_paths: RuntimePaths, name: str) -> Path | None:
    value = runtime_paths.env_value(name)
    return Path(value).expanduser() if value else None


__all__ = [
    "NativeBridgeRuntime",
    "NativeDesktopHost",
    "NativeHostDependencies",
    "run_native_stdio",
    "serve_native_stream",
    "supervise_native_tasks",
]
