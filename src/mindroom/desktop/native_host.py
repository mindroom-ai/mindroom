"""App-owned native desktop helper lifecycle and stdio protocol."""

# Platform and optional runtime imports stay lazy so status works before desktop
# extras are installed. The action dispatcher mirrors the finite wire action set.
# ruff: noqa: C901, EM101, PLC0415, PLR0911, PLR0912, PLR0915, TRY003

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, cast

from mindroom.desktop.command_journal import (
    DesktopCommandJournalError,
    check_controller_binding,
    controller_journal_key,
)
from mindroom.desktop.local_dashboard import local_dashboard_configuration
from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.desktop.native_config import (
    NativeBrowserConfig,
    NativeCaptureConfig,
    NativeConfigError,
    NativeControllerEntry,
    NativeDesktopConfig,
    load_native_config,
    merge_single_controller_update,
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
from mindroom.file_locks import async_exclusive_file_lock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.bridge import DesktopBridge
    from mindroom.desktop.bridge_session import DesktopBridgeSession, DesktopBridgeSessionDeps
    from mindroom.desktop.playwright_mcp import PlaywrightMCPBrowserProvider
    from mindroom.matrix.device_identity import PinnedMatrixDevice


_CAPABILITIES = ("observe", "control", "browser", "files", "shell")
_MAX_REGULAR_NATIVE_REQUESTS = 4
_MAX_STOP_NATIVE_REQUESTS = 1
_IMMEDIATE_NATIVE_ACTIONS = frozenset(
    {
        "status",
        "dashboard_configuration",
        # Grant shares revoke's ordered lane, so a revoke sent after a grant always lands after it.
        "grant_control",
        "revoke_control",
        "decide_shell",
        "grant_shell",
        "revoke_shell",
        "kill_shell_handle",
    },
)


class _NativeBridgeRuntimeProtocol(Protocol):
    """Bridge operations exposed to the local coordinator."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def status(self) -> dict[str, object]: ...
    def grant_control(
        self,
        duration_seconds: int | None = None,
        *,
        computer: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]: ...
    def revoke_control(self, *, controller_key: str | None = None) -> dict[str, object]: ...
    def decide_shell(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int,
        auto_approve_until_revoked: bool = False,
    ) -> dict[str, object]: ...
    def grant_shell(
        self,
        duration_seconds: int | None = None,
        *,
        until_revoked: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]: ...
    def revoke_shell(self, *, controller_key: str | None = None) -> dict[str, object]: ...
    def kill_shell_handle(self, handle: str) -> dict[str, object]: ...
    async def verify_controller(self, entry: NativeControllerEntry) -> None: ...
    def update_controllers(self, config: NativeDesktopConfig) -> dict[str, object]: ...
    async def pair_controller(self, controller: PinnedMatrixDevice, *, code: str) -> dict[str, str]: ...
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
        self._host_lock_fd: int | None = None
        self._helper_state = "running"
        self._last_error: dict[str, object] | None = None
        self._pairing_state = "unpaired"
        self._config: NativeDesktopConfig | None = None
        self._config_error: dict[str, object] | None = None
        self._config_error_revision = 0
        self._refresh_config()

    def _refresh_config(self) -> None:
        """Follow terminal edits while stopped without changing a running bridge's authority."""
        if self._runtime is not None or self._startup_task is not None:
            return
        try:
            config = load_native_config(native_config_path(self._runtime_paths.storage_root))
        except NativeConfigError as exc:
            self._config = None
            self._config_error_revision = exc.revision
            self._config_error = (
                NativeProtocolError(exc.code, str(exc)).to_payload() if exc.code != "configuration_missing" else None
            )
        else:
            if self._config is not None and _primary_controller(self._config) != _primary_controller(config):
                self._pairing_state = "unpaired"
            self._config = config
            self._config_error = None
            self._config_error_revision = 0

    def hello(self) -> dict[str, object]:
        """Return the first process record."""
        return {
            "v": NATIVE_PROTOCOL_VERSION,
            "type": "hello",
            "protocol_version": NATIVE_PROTOCOL_VERSION,
            "helper_version": self._helper_version,
            "capabilities": list(_CAPABILITIES),
        }

    def status(self) -> dict[str, object]:
        """Return complete redacted process state."""
        self._refresh_config()
        config = self._config
        session_state, session_identity = _saved_session_identity(self._runtime_paths)
        runtime_status = self._runtime.status() if self._runtime is not None else {}
        if isinstance(opened := runtime_status.get("session"), dict):
            # A running bridge keeps the session it opened, whatever a later login saved.
            session_state, session_identity = "ready", cast("dict[str, str]", opened)
        gui_mode = str(runtime_status.get("gui_mode", "stopped"))
        bridge_state = (
            gui_mode if gui_mode in {"stopped", "observe_only", "control", "stopping", "faulted"} else "faulted"
        )
        if self._helper_state == "stopping":
            bridge_state = "stopping"
        browser_configured = bool(config and config.browser.enabled)
        primary = _primary_controller(config)
        raw_shell = runtime_status.get(
            "shell",
            {
                "enabled": bool(config and config.shell.enabled),
                "pending": None,
                "auto_approve_remaining_seconds": 0,
                "auto_approve_until_revoked": False,
                "active_request_id": None,
                "handles": [],
            },
        )
        shell_status_payload = dict(cast("dict[str, object]", raw_shell))
        shell_status_payload["ready"] = bool(shell_status_payload.get("enabled")) and bridge_state in {
            "observe_only",
            "control",
        }
        shell_status_payload["controllers"] = _shell_controllers_status(config, runtime_status)
        return {
            "config": {
                "state": "ready" if config is not None else ("invalid" if self._config_error else "missing"),
                "revision": config.revision if config is not None else self._config_error_revision,
                "enabled": config.enabled if config is not None else False,
                "controller_user_id": primary.user_id if primary is not None else None,
                "controller_device_id": primary.device_id if primary is not None else None,
                "allowed_requester_ids": list(config.allowed_requester_ids) if config is not None else [],
                "allowed_agent_names": list(config.allowed_agent_names) if config is not None else [],
                "controllers": [
                    {
                        "controller_key": controller_journal_key(entry.controller),
                        "user_id": entry.controller.user_id,
                        "device_id": entry.controller.device_id,
                        "ed25519": entry.controller.ed25519,
                        "allowed_requester_ids": list(entry.allowed_requester_ids),
                        "allowed_agent_names": list(entry.allowed_agent_names),
                    }
                    for entry in (config.controllers if config is not None else ())
                ],
                "allowed_app_ids": list(config.allowed_app_ids) if config is not None else [],
                "file_roots": [str(root) for root in config.files.roots] if config is not None else [],
                "shell_enabled": config.shell.enabled if config is not None else False,
            },
            "pairing": {
                "state": self._pairing_state,
                "session_state": session_state,
                "homeserver": session_identity.get("homeserver"),
                "user_id": session_identity.get("user_id"),
                "device_id": session_identity.get("device_id"),
                "controller_fingerprint": primary.ed25519 if primary is not None else None,
            },
            "helper": {"state": self._helper_state, "version": self._helper_version},
            "bridge": {
                "state": bridge_state,
                "active_action": runtime_status.get("active_action"),
                "last_error": runtime_status.get("last_error", self._config_error or self._last_error),
            },
            "authority": {
                "control_available": bool(runtime_status.get("control_available", False)),
                "lease_remaining_seconds": runtime_status.get("lease_remaining_seconds", 0),
                "lease_expires_at_ms": runtime_status.get("lease_expires_at_ms"),
                "control_scope": runtime_status.get("control_scope", "saved_apps"),
                "controllers": _authority_controllers_status(config, runtime_status),
            },
            "permissions": _permission_status(),
            "shell": shell_status_payload,
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
            "capabilities": list(_CAPABILITIES),
        }

    async def handle(self, request: NativeRequest) -> dict[str, object]:
        """Execute one request and return a redacted result."""
        if request.action in _IMMEDIATE_NATIVE_ACTIONS or request.action == "stop":
            return await self._handle_guarded(request)
        async with self._lock:
            return await self._handle_guarded(request)

    async def _handle_guarded(self, request: NativeRequest) -> dict[str, object]:
        try:
            return await self._dispatch(request)
        except NativeProtocolError:
            raise
        except NativeConfigError as exc:
            raise NativeProtocolError(exc.code, str(exc)) from exc
        except ValueError as exc:
            raise NativeProtocolError("invalid_request", str(exc)) from exc
        except Exception as exc:
            error = NativeProtocolError(
                "internal_error",
                str(exc) or "Native desktop operation failed.",
                recovery="Retry the operation or restart the helper.",
                retryable=True,
            )
            self._last_error = error.to_payload()
            raise error from exc

    async def _dispatch(self, request: NativeRequest) -> dict[str, object]:
        action, parameters = request.action, request.parameters
        self._refresh_config()
        if action == "status":
            _expect_keys(parameters, set())
            return {"status": self.status()}
        if action == "dashboard_configuration":
            _expect_keys(parameters, set())
            # The port-owner check runs launchctl and lsof; the bridge tasks share this loop.
            return await asyncio.to_thread(local_dashboard_configuration, self._runtime_paths)
        if action in {"configure", "set_allowed_apps", "set_browser_config", "set_local_access", "finish_setup"}:
            edited_keys = {
                "configure": {"config"},
                "set_allowed_apps": {"allowed_app_ids"},
                "set_browser_config": {"browser"},
                "set_local_access": {"files", "shell"},
                "finish_setup": {"expected_session"},
            }[action]
            _expect_keys(parameters, {"expected_revision", *edited_keys})
            if self._runtime is not None:
                raise NativeProtocolError("busy", "Stop the desktop bridge before changing its configuration.")
            with _host_lock_guard(self._runtime_paths.storage_root, held=False):
                self._refresh_config()
                if action == "set_allowed_apps":
                    if self._config is None:
                        raise NativeProtocolError("invalid_request", "Complete desktop setup before saving app access.")
                    config = self._config.with_allowed_apps(parameters.get("allowed_app_ids"))
                elif action == "set_local_access":
                    config = self._require_config().with_local_access(parameters.get("files"), parameters.get("shell"))
                elif action == "set_browser_config":
                    current = self._require_config()
                    browser_raw = parameters.get("browser")
                    if not isinstance(browser_raw, dict):
                        raise NativeProtocolError("invalid_request", "Native desktop browser must be a JSON object.")
                    browser = cast("dict[str, object]", browser_raw)
                    _expect_keys(browser, {"enabled", "executable_path", "user_data_dir"})
                    payload = current.to_payload()
                    payload["browser"] = {**browser, "timeout_seconds": current.browser.timeout_seconds}
                    config = NativeDesktopConfig.from_payload(payload, validate_browser_paths=False)
                    if (
                        config.browser.executable_path != current.browser.executable_path
                        and config.browser.executable_path is not None
                        and not config.browser.executable_path.is_file()
                    ):
                        raise NativeProtocolError(
                            "invalid_request",
                            "Native desktop browser executable_path must be a file.",
                        )
                    if (
                        config.browser.user_data_dir != current.browser.user_data_dir
                        and config.browser.user_data_dir is not None
                        and not config.browser.user_data_dir.is_dir()
                    ):
                        raise NativeProtocolError(
                            "invalid_request",
                            "Native desktop browser user_data_dir must be a directory.",
                        )
                elif action == "finish_setup":
                    config = replace(self._require_config(), enabled=True)
                else:
                    raw_config = parameters.get("config")
                    config = NativeDesktopConfig.from_payload(raw_config)
                    if isinstance(raw_config, dict) and raw_config.get("v") == 1:
                        controllers, preserve_local_access = merge_single_controller_update(
                            self._config,
                            config.controllers[0],
                            promote_to_primary=True,
                        )
                        config = replace(config, controllers=controllers)
                    else:
                        preserve_local_access = (
                            self._config is not None
                            and bool(config.controllers)
                            and all(
                                any(saved.controller == entry.controller for saved in self._config.controllers)
                                for entry in config.controllers
                            )
                        )
                    # Folder and shell authority carries over only when saving an already-pinned controller identity.
                    previous = self._config if preserve_local_access else None
                    if previous is not None and "files" not in cast("dict[str, object]", raw_config):
                        config = replace(config, files=previous.files, shell=previous.shell)
                    else:
                        config = config.with_canonical_new_roots(previous.files.roots if previous else ())
                for entry in config.controllers:
                    try:
                        check_controller_binding(self._runtime_paths.storage_root, entry.controller)
                    except DesktopCommandJournalError as exc:
                        raise NativeProtocolError("invalid_request", str(exc)) from exc
                expected_revision = _required_int(parameters, "expected_revision", minimum=0)
                if action == "finish_setup":
                    from mindroom.desktop.session import desktop_session_path

                    expected_session_raw = parameters.get("expected_session")
                    if not isinstance(expected_session_raw, dict):
                        raise NativeProtocolError("invalid_request", "Expected desktop session must be a JSON object.")
                    expected_session = cast("dict[str, object]", expected_session_raw)
                    _expect_keys(expected_session, {"homeserver", "user_id", "device_id"})
                    for key in expected_session:
                        _required_text(expected_session, key)
                    async with async_exclusive_file_lock(
                        desktop_session_path(self._runtime_paths).with_suffix(".lock"),
                    ):
                        session_state, session_identity = _saved_session_identity(self._runtime_paths)
                        if session_state != "ready":
                            raise NativeProtocolError(
                                "session_missing",
                                "Sign in to a saved Matrix session before finishing setup.",
                            )
                        if session_identity != expected_session:
                            raise NativeProtocolError(
                                "session_conflict",
                                "The saved Matrix session changed; review setup and retry.",
                            )
                        self._config = save_native_config(
                            native_config_path(self._runtime_paths.storage_root),
                            config,
                            expected_revision=expected_revision,
                        )
                else:
                    self._config = save_native_config(
                        native_config_path(self._runtime_paths.storage_root),
                        config,
                        expected_revision=expected_revision,
                    )
            self._last_error = None
            return {"status": self.status()}
        if action == "upsert_controller":
            _expect_keys(
                parameters,
                {"expected_revision", "controller", "allowed_requester_ids", "allowed_agent_names"},
            )
            with _host_lock_guard(self._runtime_paths.storage_root, held=self._host_lock_fd is not None):
                self._refresh_config()
                expected_revision = _required_int(parameters, "expected_revision", minimum=0)
                entry = NativeControllerEntry.from_payload(
                    {
                        "controller": parameters.get("controller"),
                        "allowed_requester_ids": parameters.get("allowed_requester_ids"),
                        "allowed_agent_names": parameters.get("allowed_agent_names"),
                    },
                )
                try:
                    check_controller_binding(self._runtime_paths.storage_root, entry.controller)
                except DesktopCommandJournalError as exc:
                    raise NativeProtocolError("invalid_request", str(exc)) from exc
                updated = (
                    self._config.with_upserted_controller(entry)
                    if self._config is not None
                    else NativeDesktopConfig(
                        revision=0,
                        enabled=False,
                        controllers=(entry,),
                        allowed_app_ids=(),
                        capture=NativeCaptureConfig(),
                        browser=NativeBrowserConfig(),
                    )
                )
                if expected_revision != updated.revision:
                    raise NativeProtocolError(
                        "revision_conflict",
                        "Native desktop configuration changed; reload it and try again.",
                    )
                runtime = self._runtime
                if runtime is not None:
                    try:
                        await runtime.verify_controller(entry)
                    except NativeProtocolError:
                        raise
                    except Exception as exc:
                        raise NativeProtocolError(
                            "controller_verification_failed",
                            str(exc) or "Could not verify the controller device on Matrix.",
                            recovery="Check that the controller device exists and matches the pinned key, then retry.",
                            retryable=True,
                        ) from exc
                self._config = save_native_config(
                    native_config_path(self._runtime_paths.storage_root),
                    updated,
                    expected_revision=expected_revision,
                )
                if runtime is not None and self._runtime is runtime:
                    runtime.update_controllers(self._config)
            self._last_error = None
            return {"status": self.status()}
        if action == "remove_controller":
            _expect_keys(parameters, {"expected_revision", "controller_key"})
            with _host_lock_guard(self._runtime_paths.storage_root, held=self._host_lock_fd is not None):
                self._refresh_config()
                expected_revision = _required_int(parameters, "expected_revision", minimum=0)
                controller_key = _required_text(parameters, "controller_key")
                updated = self._require_config().with_removed_controller(controller_key)
                self._config = save_native_config(
                    native_config_path(self._runtime_paths.storage_root),
                    updated,
                    expected_revision=expected_revision,
                )
                runtime = self._runtime
                if runtime is not None and self._runtime is runtime:
                    if not self._config.controllers:
                        self._helper_state = "stopping"
                        try:
                            await runtime.stop()
                        finally:
                            if self._runtime is runtime:
                                self._runtime = None
                                self._release_host_lock()
                            self._helper_state = "running"
                    else:
                        runtime.update_controllers(self._config)
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
            config = self._require_config()
            _allow_keys(parameters, {"code", "cloudflare_access", "expected_revision", "controller_key"})
            code = _required_text(parameters, "code")
            _optional_bool(parameters, "cloudflare_access")
            controller_key = _optional_text(parameters, "controller_key")
            if "expected_revision" in parameters:
                expected_revision = _required_int(parameters, "expected_revision", minimum=0)
                if expected_revision != config.revision:
                    raise NativeProtocolError(
                        "revision_conflict",
                        "Desktop configuration changed; review setup and retry pairing.",
                    )
            target_entry = _resolve_pair_controller(config, controller_key)
            runtime = self._runtime
            self._pairing_state = "claiming"
            try:
                if self._dependencies.pair is not None:
                    result = await self._dependencies.pair(self._runtime_paths, config, parameters)
                elif runtime is not None:
                    claimed = await runtime.pair_controller(target_entry.controller, code=code)
                    result = _pair_result(
                        code=code,
                        verification=claimed["verification"],
                        device_id=claimed["device_id"],
                        device_ed25519=claimed["device_ed25519"],
                        entry=target_entry,
                    )
                else:
                    result = await _pair(
                        self._runtime_paths,
                        config,
                        parameters,
                        entry=target_entry,
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
            if not config.has_local_capability:
                raise NativeProtocolError(
                    "configuration_missing",
                    "Select and save at least one local capability before starting.",
                )
            if self._runtime is not None:
                raise NativeProtocolError("already_running", "The desktop bridge is already running.")
            self._host_lock_fd = _acquire_host_lock(self._runtime_paths.storage_root)
            started = False
            try:
                runtime = (self._dependencies.runtime_factory or NativeBridgeRuntime)(self._runtime_paths, config)
                self._helper_state = "starting"
                self._startup_task = asyncio.create_task(runtime.start())
                try:
                    await self._startup_task
                except asyncio.CancelledError as exc:
                    self._helper_state = "running"
                    raise NativeProtocolError("not_running", "Desktop bridge startup was stopped.") from exc
                except Exception as exc:
                    from mindroom.desktop.startup_errors import desktop_startup_error

                    self._helper_state = "faulted"
                    raise desktop_startup_error(exc) from exc
                finally:
                    self._startup_task = None
                self._runtime = runtime
                self._helper_state = "running"
                started = True
                return {"status": self.status()}
            finally:
                if not started:
                    self._release_host_lock()
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
            try:
                await runtime.stop()
            finally:
                # A stopped runtime is retired even when a release failed; that failure is this stop's response.
                if self._runtime is runtime:
                    self._runtime = None
                    self._release_host_lock()
                self._helper_state = "running"
            return {"status": self.status()}
        if action == "grant_control":
            # The whole computer is granted only until revoked; a duration grants only the saved apps.
            grant_params, controller_key = _optional_controller_key(parameters)
            computer = (
                grant_params == {"until_revoked": True, "scope": "computer"} and grant_params["until_revoked"] is True
            )
            if not computer:
                _expect_keys(grant_params, {"duration_seconds"})
            try:
                self._require_runtime().grant_control(
                    None if computer else _required_int(grant_params, "duration_seconds", minimum=60, maximum=3600),
                    computer=computer,
                    controller_key=controller_key,
                )
            except ValueError as exc:
                raise NativeProtocolError("control_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "revoke_control":
            revoke_params, controller_key = _optional_controller_key(parameters)
            _expect_keys(revoke_params, set())
            try:
                self._require_runtime().revoke_control(controller_key=controller_key)
            except ValueError as exc:
                raise NativeProtocolError("control_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "decide_shell":
            _expect_keys(
                parameters,
                {"command_id", "approved", "auto_approve_seconds"} | ({"auto_approve_until_revoked"} & set(parameters)),
            )
            command_id = _required_text(parameters, "command_id")
            approved = parameters.get("approved")
            if not isinstance(approved, bool):
                raise NativeProtocolError("invalid_request", "Native desktop approved must be a boolean.")
            auto_approve_seconds = _required_int(parameters, "auto_approve_seconds", minimum=0, maximum=3600)
            auto_approve_until_revoked = _optional_bool(parameters, "auto_approve_until_revoked")
            if (
                0 < auto_approve_seconds < 60
                or ((auto_approve_seconds or auto_approve_until_revoked) and not approved)
                or (auto_approve_seconds and auto_approve_until_revoked)
            ):
                raise NativeProtocolError(
                    "invalid_request",
                    "Native desktop auto-approval must be 60 through 3600 seconds or until revoked, "
                    "chosen only with approval.",
                )
            runtime = self._require_runtime()
            try:
                runtime.decide_shell(
                    command_id,
                    approved=approved,
                    auto_approve_seconds=auto_approve_seconds,
                    auto_approve_until_revoked=auto_approve_until_revoked,
                )
            except ValueError as exc:
                raise NativeProtocolError("shell_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "grant_shell":
            grant_params, controller_key = _optional_controller_key(parameters)
            if set(grant_params) == {"until_revoked"} and grant_params["until_revoked"] is True:
                duration_seconds = None
            elif set(grant_params) == {"duration_seconds"}:
                duration_seconds = _required_int(grant_params, "duration_seconds", minimum=60, maximum=3600)
            else:
                raise NativeProtocolError(
                    "invalid_request",
                    "Native desktop grant_shell needs exactly duration_seconds or until_revoked: true.",
                )
            runtime = self._require_runtime()
            try:
                runtime.grant_shell(
                    duration_seconds,
                    until_revoked=duration_seconds is None,
                    controller_key=controller_key,
                )
            except ValueError as exc:
                raise NativeProtocolError("shell_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "revoke_shell":
            revoke_params, controller_key = _optional_controller_key(parameters)
            _expect_keys(revoke_params, set())
            runtime = self._require_runtime()
            try:
                runtime.revoke_shell(controller_key=controller_key)
            except ValueError as exc:
                raise NativeProtocolError("shell_denied", str(exc)) from exc
            return {"status": self.status()}
        if action == "kill_shell_handle":
            _expect_keys(parameters, {"handle"})
            handle = _required_text(parameters, "handle")
            runtime = self._require_runtime()
            try:
                runtime.kill_shell_handle(handle)
            except ValueError as exc:
                raise NativeProtocolError("shell_denied", str(exc)) from exc
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
            try:
                if self._runtime is not None:
                    runtime, self._runtime = self._runtime, None
                    await runtime.stop()
            finally:
                self._release_host_lock()
                self._helper_state = "stopped"

    def _release_host_lock(self) -> None:
        lock_fd, self._host_lock_fd = self._host_lock_fd, None
        _release_host_lock(lock_fd)

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
    """Run one Desktop bridge session for the app, recording a worker failure for its status."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        config: NativeDesktopConfig,
        deps: DesktopBridgeSessionDeps | None = None,
    ) -> None:
        self._runtime_paths, self._config = runtime_paths, config
        self._deps = deps
        self._session: DesktopBridgeSession | None = None
        self._opened_session: dict[str, str] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._fault: str | None = None

    async def start(self) -> None:
        """Open one observe-only bridge session."""
        from mindroom.desktop.bridge_session import start_desktop_bridge_session
        from mindroom.desktop.session import desktop_http_headers, desktop_session_path, load_desktop_session

        session = load_desktop_session(desktop_session_path(self._runtime_paths))
        http_headers = desktop_http_headers(
            session.homeserver,
            _matrix_http_headers_path(self._runtime_paths),
            cloudflare_access=session.cloudflare_access,
        )
        self._opened_session = {
            "homeserver": session.homeserver,
            "user_id": session.user_id,
            "device_id": session.device_id,
        }
        self._session = await start_desktop_bridge_session(
            self._config,
            session,
            runtime_paths=self._runtime_paths,
            http_headers=http_headers,
            deps=self._deps,
        )
        self._supervisor = asyncio.create_task(self._supervise(self._session), name="native_desktop_supervisor")

    async def stop(self) -> None:
        """Fence admission, drain the active action, and close resources; a requested stop is never a fault."""
        # Cancelling the supervisor and starting the close both happen before the first yield, so admission is fenced at once.
        if self._supervisor is not None:
            self._supervisor.cancel()
        try:
            if self._session is not None:
                # The close is shared, so this also waits for a close the cancelled supervisor began.
                await self._session.close()
        finally:
            if self._supervisor is not None:
                await asyncio.gather(self._supervisor, return_exceptions=True)

    def status(self) -> dict[str, object]:
        """Return current local bridge authority; a fault shows once the failed bridge has drained."""
        session = self._live_session()
        if session is not None:
            browser = session.browser
            return {
                **session.bridge.local_status(),
                "browser_connected": browser is not None and browser.running,
                "session": self._opened_session,
            }
        if self._fault is not None:
            return {
                "gui_mode": "faulted",
                "control_available": False,
                "lease_remaining_seconds": 0,
                "lease_expires_at_ms": None,
                "active_action": None,
                "last_error": NativeProtocolError(
                    "internal_error",
                    self._fault,
                    recovery="Stop and restart the desktop bridge.",
                    retryable=True,
                ).to_payload(),
                "browser_connected": False,
            }
        return {"gui_mode": "stopped", "control_available": False, "browser_connected": False}

    def grant_control(
        self,
        duration_seconds: int | None = None,
        *,
        computer: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]:
        """Grant a bounded lease over the saved apps, or control of the whole computer until revoked or stopped."""
        return self._required_bridge().grant_local_control(
            duration_seconds,
            computer=computer,
            controller_key=controller_key,
        )

    def revoke_control(self, *, controller_key: str | None = None) -> dict[str, object]:
        """Revoke new local control actions."""
        return self._required_bridge().revoke_local_control(controller_key=controller_key)

    def decide_shell(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int,
        auto_approve_until_revoked: bool = False,
    ) -> dict[str, object]:
        """Approve or deny one exact pending command locally."""
        return self._required_bridge().decide_local_shell(
            command_id,
            approved=approved,
            auto_approve_seconds=auto_approve_seconds,
            auto_approve_until_revoked=auto_approve_until_revoked,
        )

    def grant_shell(
        self,
        duration_seconds: int | None = None,
        *,
        until_revoked: bool = False,
        controller_key: str | None = None,
    ) -> dict[str, object]:
        """Enable local auto-approval for a bounded duration or until revoked or stopped."""
        return self._required_bridge().grant_local_shell(
            duration_seconds,
            until_revoked=until_revoked,
            controller_key=controller_key,
        )

    def revoke_shell(self, *, controller_key: str | None = None) -> dict[str, object]:
        """Revoke local shell authority and kill handles for one or all controllers."""
        return self._required_bridge().revoke_local_shell(controller_key=controller_key)

    def kill_shell_handle(self, handle: str) -> dict[str, object]:
        """Kill one handle from any caller; its owner's check reports it killed."""
        return self._required_bridge().kill_local_shell_handle(handle)

    async def verify_controller(self, entry: NativeControllerEntry) -> None:
        """Verify one pinned controller device on this running Matrix client."""
        await self._required_session().verify_controller(entry.controller)

    def update_controllers(self, config: NativeDesktopConfig) -> dict[str, object]:
        """Hot-reload live bridge controllers from saved config without dropping handles."""
        session = self._required_session()
        self._config = config
        return session.update_controllers(config)

    async def pair_controller(self, controller: PinnedMatrixDevice, *, code: str) -> dict[str, str]:
        """Claim one pairing against a pinned controller using this live bridge's Matrix client."""
        from mindroom.desktop.pairing_client import send_desktop_pairing_claim_on_client
        from mindroom.desktop.session import client_ed25519_fingerprint

        client = self._required_bridge().client
        verification = await send_desktop_pairing_claim_on_client(client, controller, code=code)
        return {
            "verification": verification,
            "device_id": self._opened_session.get("device_id") or str(client.device_id or ""),
            "device_ed25519": client_ed25519_fingerprint(client),
        }

    async def connect_browser(self) -> None:
        """Connect the configured installed-profile extension."""
        await self._required_browser().execute("start", {})

    async def disconnect_browser(self) -> None:
        """Disconnect the installed-profile extension without stopping Matrix."""
        await self._required_browser().execute("stop", {})

    def _live_session(self) -> DesktopBridgeSession | None:
        return self._session if self._session is not None and self._session.live else None

    def _required_session(self) -> DesktopBridgeSession:
        session = self._live_session()
        if session is None:
            raise ValueError("The desktop bridge is not running.")
        return session

    def _required_bridge(self) -> DesktopBridge:
        return self._required_session().bridge

    def _required_browser(self) -> PlaywrightMCPBrowserProvider:
        session = self._live_session()
        if session is None or session.browser is None:
            raise ValueError("Browser integration is not configured.")
        return session.browser

    async def _supervise(self, session: DesktopBridgeSession) -> None:
        from mindroom.desktop.bridge_session import first_worker_exit

        self._fault = str(await first_worker_exit(session.workers))
        await session.close()


async def _login(runtime_paths: RuntimePaths, parameters: dict[str, object]) -> dict[str, object]:
    from mindroom.desktop.session import (
        client_ed25519_fingerprint,
        desktop_http_headers,
        desktop_session_path,
        login_desktop_client,
        resolve_desktop_login_method,
        save_desktop_session,
    )
    from mindroom.desktop.sso import receive_sso_login_token

    _allow_keys(
        parameters,
        {"homeserver", "user_id", "method", "password", "login_token", "sso_idp", "replace", "cloudflare_access"},
    )
    homeserver = _required_text(parameters, "homeserver")
    user_id = _optional_text(parameters, "user_id")
    cloudflare_access = _optional_bool(parameters, "cloudflare_access")
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
    http_headers = desktop_http_headers(
        homeserver,
        _matrix_http_headers_path(runtime_paths),
        cloudflare_access=cloudflare_access,
    )
    method = await resolve_desktop_login_method(
        requested,
        homeserver=homeserver,
        runtime_paths=runtime_paths,
        http_headers=http_headers,
    )
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
        http_headers=http_headers,
        cloudflare_access=cloudflare_access,
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
    *,
    entry: NativeControllerEntry,
) -> dict[str, object]:
    from mindroom.desktop.pairing_client import send_desktop_pairing_claim
    from mindroom.desktop.session import (
        client_ed25519_fingerprint,
        desktop_http_headers,
        desktop_session_path,
        load_desktop_session,
        open_desktop_client,
        save_desktop_session,
    )

    code = _required_text(parameters, "code")
    cloudflare_access = _optional_bool(parameters, "cloudflare_access")
    session_path = desktop_session_path(runtime_paths)
    session = load_desktop_session(session_path)
    http_headers = desktop_http_headers(
        session.homeserver,
        _matrix_http_headers_path(runtime_paths),
        cloudflare_access=cloudflare_access or session.cloudflare_access,
    )
    owner = await open_desktop_client(session, runtime_paths=runtime_paths, http_headers=http_headers)
    try:
        if "expected_revision" in parameters:
            try:
                current_config = load_native_config(native_config_path(runtime_paths.storage_root))
            except NativeConfigError as exc:
                raise NativeProtocolError(
                    "revision_conflict",
                    "Desktop configuration changed; review setup and retry pairing.",
                ) from exc
            if current_config != config:
                raise NativeProtocolError(
                    "revision_conflict",
                    "Desktop configuration changed; review setup and retry pairing.",
                )
        verification = await send_desktop_pairing_claim(owner, entry.controller, code=code)
        device_ed25519 = client_ed25519_fingerprint(owner.client)
        if cloudflare_access and not session.cloudflare_access:
            save_desktop_session(session_path, replace(session, cloudflare_access=True), expected_session=session)
        return _pair_result(
            code=code,
            verification=verification,
            device_id=session.device_id,
            device_ed25519=device_ed25519,
            entry=entry,
        )
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
        await emit({"v": NATIVE_PROTOCOL_VERSION, "type": "status", "sequence": sequence, "status": status})

    async def watch_status() -> None:
        while True:
            await asyncio.sleep(1)
            await emit_status_if_changed()

    await emit(host.hello())
    await emit_status_if_changed()
    watcher = asyncio.create_task(watch_status(), name="native_desktop_status")
    regular_tasks: set[asyncio.Task[None]] = set()
    stop_tasks: set[asyncio.Task[None]] = set()

    async def respond_error(request_id: str | None, error: NativeProtocolError) -> None:
        await emit(
            {
                "v": NATIVE_PROTOCOL_VERSION,
                "type": "response",
                "request_id": request_id,
                "ok": False,
                "error": error.to_payload(),
            },
        )

    async def answer(request: NativeRequest) -> None:
        try:
            result = await host.handle(request)
            await emit(
                {
                    "v": NATIVE_PROTOCOL_VERSION,
                    "type": "response",
                    "request_id": request.request_id,
                    "ok": True,
                    "result": result,
                },
            )
            if request.action != "status":
                await emit_status_if_changed()
        except NativeProtocolError as exc:
            await respond_error(request.request_id, exc)

    try:
        while line := await _read_native_line(input_stream):
            try:
                request = parse_native_request(line.rstrip(b"\r\n"))
            except NativeProtocolError as exc:
                await respond_error(None, exc)
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
                await respond_error(
                    request.request_id,
                    NativeProtocolError(
                        "busy",
                        "The native desktop helper is handling its maximum number of local requests.",
                        recovery="Wait for the current operation to finish, then retry.",
                        retryable=True,
                    ),
                )
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


def _primary_controller(config: NativeDesktopConfig | None) -> PinnedMatrixDevice | None:
    return config.controllers[0].controller if config is not None and config.controllers else None


def _resolve_pair_controller(config: NativeDesktopConfig, controller_key: str | None) -> NativeControllerEntry:
    if controller_key is None:
        if not config.controllers:
            raise NativeProtocolError(
                "configuration_missing",
                "Native desktop configuration has no paired controllers.",
            )
        return config.controllers[0]
    for entry in config.controllers:
        if controller_journal_key(entry.controller) == controller_key:
            return entry
    raise NativeProtocolError("invalid_request", "Unknown desktop controller.")


def _pair_result(
    *,
    code: str,
    verification: str,
    device_id: str,
    device_ed25519: str,
    entry: NativeControllerEntry,
) -> dict[str, object]:
    result: dict[str, object] = {
        "verification": verification,
        "confirmation_command": f"!desktop confirm {code} {verification}",
        "device_id": device_id,
        "device_ed25519": device_ed25519,
    }
    if len(entry.allowed_agent_names) == 1:
        result["agent_confirmation_command"] = f"!desktop confirm {entry.allowed_agent_names[0]} {code} {verification}"
    return result


def _authority_controllers_status(
    config: NativeDesktopConfig | None,
    runtime_status: dict[str, object],
) -> dict[str, dict[str, object]]:
    live = runtime_status.get("controllers")
    if isinstance(live, dict):
        return {
            str(key): {
                "control_available": bool(entry.get("control_available", False)),
                "control_scope": entry.get("control_scope", "saved_apps"),
                "lease_remaining_seconds": entry.get("lease_remaining_seconds", 0),
                "lease_expires_at_ms": entry.get("lease_expires_at_ms"),
            }
            for key, entry in live.items()
            if isinstance(entry, dict)
        }
    return {
        controller_journal_key(entry.controller): {
            "control_available": False,
            "control_scope": "saved_apps",
            "lease_remaining_seconds": 0,
            "lease_expires_at_ms": None,
        }
        for entry in (config.controllers if config is not None else ())
    }


def _shell_controllers_status(
    config: NativeDesktopConfig | None,
    runtime_status: dict[str, object],
) -> dict[str, dict[str, object]]:
    live = runtime_status.get("controllers")
    if isinstance(live, dict):
        result: dict[str, dict[str, object]] = {}
        for key, entry in live.items():
            shell_entry = entry.get("shell") if isinstance(entry, dict) else None
            if isinstance(shell_entry, dict):
                result[str(key)] = {
                    "auto_approve_remaining_seconds": shell_entry.get("auto_approve_remaining_seconds", 0),
                    "auto_approve_until_revoked": bool(shell_entry.get("auto_approve_until_revoked", False)),
                }
        if result or not (config and config.controllers):
            return result
    return {
        controller_journal_key(entry.controller): {
            "auto_approve_remaining_seconds": 0,
            "auto_approve_until_revoked": False,
        }
        for entry in (config.controllers if config is not None else ())
    }


def _optional_controller_key(parameters: dict[str, object]) -> tuple[dict[str, object], str | None]:
    if "controller_key" not in parameters:
        return parameters, None
    controller_key = _required_text(parameters, "controller_key")
    return {key: value for key, value in parameters.items() if key != "controller_key"}, controller_key


def _permission_status() -> dict[str, object]:
    unknown = {"state": "unknown", "can_request": sys.platform == "darwin", "recovery": None}
    if sys.platform != "darwin":
        return {
            "accessibility": unknown.copy(),
            "screen_recording": unknown.copy(),
            "gui_permissions_ready": False,
        }
    try:
        import ApplicationServices
        import Quartz

        accessibility = bool(ApplicationServices.AXIsProcessTrusted())  # ty: ignore[missing-attribute]
        screen_recording = bool(Quartz.CGPreflightScreenCaptureAccess())  # ty: ignore[missing-attribute]
    except (ImportError, AttributeError):
        return {
            "accessibility": unknown.copy(),
            "screen_recording": unknown.copy(),
            "gui_permissions_ready": False,
        }
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
        "gui_permissions_ready": accessibility and screen_recording,
    }


def _request_permission(permission: str) -> None:
    if sys.platform != "darwin":
        raise NativeProtocolError("permission_missing", "Desktop permissions are available only on macOS.")
    if permission == "accessibility":
        import ApplicationServices

        ApplicationServices.AXIsProcessTrustedWithOptions(  # ty: ignore[missing-attribute]
            {ApplicationServices.kAXTrustedCheckOptionPrompt: True},  # ty: ignore[missing-attribute]
        )
    else:
        import Quartz

        Quartz.CGRequestScreenCaptureAccess()  # ty: ignore[missing-attribute]


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


def _optional_bool(parameters: dict[str, object], key: str) -> bool:
    value = parameters.get(key, False)
    if not isinstance(value, bool):
        raise NativeProtocolError("invalid_request", f"Native desktop {key} must be a boolean.")
    return value


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


def _matrix_http_headers_path(runtime_paths: RuntimePaths) -> Path | None:
    value = runtime_paths.env_value("MINDROOM_DESKTOP_MATRIX_HTTP_HEADERS_FILE")
    return Path(value) if value else None


if sys.platform == "win32":
    import msvcrt

    def _try_lock_host_fd(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except PermissionError:
            return False
        return True

    def _unlock_host_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback

        def _try_lock_host_fd(fd: int) -> bool:  # noqa: ARG001
            return True

        def _unlock_host_fd(fd: int) -> None:  # noqa: ARG001
            return None

    else:

        def _try_lock_host_fd(fd: int) -> bool:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

        def _unlock_host_fd(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _host_lock_path(storage_root: Path) -> Path:
    return storage_root / "desktop_bridge" / "host.lock"


def _acquire_host_lock(storage_root: Path) -> int:
    lock_path = _host_lock_path(storage_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        lock_path.parent.chmod(0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NativeProtocolError("invalid_request", "Native desktop host lock path must be a regular file.")
        if not _try_lock_host_fd(fd):
            raise NativeProtocolError(
                "already_running",
                "Another MindRoom app or CLI bridge is already running Desktop access for this storage path.",
                recovery="Stop Computer Access in the other MindRoom app or terminal bridge and try again.",
            )
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        locked = True
        return fd
    finally:
        if not locked:
            os.close(fd)


def _release_host_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        _unlock_host_fd(fd)
    finally:
        os.close(fd)


@contextmanager
def desktop_host_lock(storage_root: Path) -> Iterator[None]:
    """Acquire and hold ``host.lock`` exclusively for one running Desktop host or CLI bridge."""
    fd = _acquire_host_lock(storage_root)
    try:
        yield
    finally:
        _release_host_lock(fd)


@contextmanager
def _host_lock_guard(storage_root: Path, *, held: bool) -> Iterator[None]:
    """Ensure no other process holds host.lock for the duration of a configuration mutation."""
    if held:
        yield
        return
    with desktop_host_lock(storage_root):
        yield


__all__ = [
    "NativeBridgeRuntime",
    "NativeDesktopHost",
    "NativeHostDependencies",
    "desktop_host_lock",
    "run_native_stdio",
    "serve_native_stream",
]
