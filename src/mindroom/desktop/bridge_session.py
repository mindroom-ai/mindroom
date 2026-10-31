"""Open, start, and close one Desktop bridge run, shared by the app helper and the terminal."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nio import AuthenticatedToDeviceEvent

from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.filesystem import DesktopFilesystem
from mindroom.desktop.native_config import require_supported_local_access
from mindroom.desktop.playwright_mcp import PlaywrightMCPBrowserProvider
from mindroom.desktop.provider import PyAutoGuiDesktopProvider
from mindroom.desktop.session import open_desktop_client, prepare_desktop_client
from mindroom.desktop.shell import DesktopShell
from mindroom.desktop.transport import DesktopTransport
from mindroom.logging_config import get_logger
from mindroom.matrix.olm_to_device import resolve_pinned_device

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.native_config import NativeDesktopConfig
    from mindroom.desktop.session import DesktopMatrixSession, DesktopOwnedSession
    from mindroom.matrix.device_identity import PinnedMatrixDevice

logger = get_logger(__name__)


class DesktopWorkerStoppedError(RuntimeError):
    """A bridge worker ended on its own while its session was still open."""

    def __init__(self) -> None:
        super().__init__("A desktop worker stopped unexpectedly.")


async def _verify_controller_then_publish_keys(client: nio.AsyncClient, controller: PinnedMatrixDevice) -> None:
    """Check the pinned controller's current identity before publishing this device's encryption keys."""
    await resolve_pinned_device(client, controller)
    await prepare_desktop_client(client)


@dataclass(frozen=True, slots=True)
class DesktopBridgeSessionDeps:
    """Matrix effects of a bridge session, replaced by offline tests."""

    open_client: Callable[
        [DesktopMatrixSession, RuntimePaths, Mapping[str, str] | None],
        Awaitable[DesktopOwnedSession],
    ] = open_desktop_client
    prepare_client: Callable[[nio.AsyncClient, PinnedMatrixDevice], Awaitable[None]] = (
        _verify_controller_then_publish_keys
    )


@dataclass(eq=False)
class DesktopBridgeSession:
    """One started Desktop run; its caller supervises `workers`, and `close` stops and releases the run."""

    bridge: DesktopBridge
    browser: PlaywrightMCPBrowserProvider | None
    workers: tuple[asyncio.Task[None], ...]
    _resources: AsyncExitStack = field(repr=False)
    _closing: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _live: bool = field(default=True, init=False, repr=False)

    @property
    def live(self) -> bool:
        """Whether the bridge's local state is current; false once close has stopped it and drained its workers."""
        return self._live

    async def close(self) -> None:
        """Stop the bridge, then release what start acquired, in reverse order.

        The first call starts the teardown eagerly, so admission, new execution, control, and shell access end
        before it first yields, as a direct `bridge.stop()` would.
        Concurrent and repeated calls share one close and its outcome, and a cancelled caller does not cut it short.
        """
        if self._closing is None:
            self._closing = asyncio.eager_task_factory(asyncio.get_running_loop(), self._stop_and_release())
        await asyncio.shield(self._closing)

    async def _stop_and_release(self) -> None:
        # Native input runs in threads, so cancelling a worker cannot stop it: the bridge first ends shell access,
        # waits for the active action, and gives recorded replies a bounded final delivery, and only then do the
        # workers stop and the journal, device lease, and session go.
        await self.bridge.stop()
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self._live = False
        # Every release is attempted even when an earlier one raises; the last failure is this close's outcome.
        await self._resources.aclose()


async def start_desktop_bridge_session(
    config: NativeDesktopConfig,
    saved_session: DesktopMatrixSession,
    *,
    runtime_paths: RuntimePaths,
    http_headers: Mapping[str, str] | None,
    control_lease_seconds: int | None = None,
    shell_auto_approve_seconds: int | None = None,
    deps: DesktopBridgeSessionDeps | None = None,
) -> DesktopBridgeSession:
    """Open Matrix, build exactly the saved capabilities, admit commands, verify the controller, then start the workers.

    Each resource registers its release as soon as it exists, so a failure at any step releases what was acquired;
    a run that never started its workers is not stopped, so nothing is delivered before the controller is verified.
    A control lease grants app input from the start and a shell grant auto-approves commands from the start;
    without them the run is observe-only and every shell command needs a local decision.
    """
    deps = deps or DesktopBridgeSessionDeps()
    resources = AsyncExitStack()
    try:
        owner = await deps.open_client(saved_session, runtime_paths, http_headers)
        # Closing the owner releases the durable source lease before its HTTP connection.
        resources.push_async_callback(owner.close)
        client = owner.client
        lease_expires_at_ms = (
            None if control_lease_seconds is None else round((time.time() + control_lease_seconds) * 1000)
        )
        bridge, browser = await _build_bridge(
            config,
            client=client,
            runtime_paths=runtime_paths,
            resources=resources,
            control_lease_expires_at_ms=lease_expires_at_ms,
        )
        if shell_auto_approve_seconds is not None:
            bridge.grant_local_shell(shell_auto_approve_seconds)
        client.add_to_device_callback(bridge.on_to_device_event, AuthenticatedToDeviceEvent)
        resources.callback(client.to_device_callbacks.remove, client.to_device_callbacks[-1])
        await deps.prepare_client(client, config.controller)
        transport = DesktopTransport(owner.source, wait_for_capacity=bridge.wait_for_capacity)
        workers = (
            asyncio.create_task(bridge.run(), name="desktop_workers"),
            asyncio.create_task(transport.run(), name="desktop_transport"),
        )
    except BaseException:
        try:
            await resources.aclose()
        except Exception:
            # The startup failure is the outcome; a release that also failed is secondary.
            logger.exception("desktop_bridge_session_rollback_failed")
        raise
    return DesktopBridgeSession(bridge, browser, workers, resources)


async def _build_bridge(
    config: NativeDesktopConfig,
    *,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    resources: AsyncExitStack,
    control_lease_expires_at_ms: int | None,
) -> tuple[DesktopBridge, PlaywrightMCPBrowserProvider | None]:
    """Build the saved capabilities and their bridge, registering each provider's release on `resources`.

    Only selected apps construct the GUI provider, so folder, shell, and browser runs need no GUI runtime.
    Folder and shell access are refused before anything is built on a computer without POSIX.
    Stopping the bridge closes its shell and closing it closes its folders; their own releases cover a build that failed first.
    """
    require_supported_local_access(config)
    browser = None
    if config.browser.enabled:
        browser = PlaywrightMCPBrowserProvider(
            output_dir=runtime_paths.storage_root / "desktop-browser",
            executable_path=config.browser.executable_path,
            user_data_dir=config.browser.user_data_dir,
            call_timeout_seconds=config.browser.timeout_seconds,
            extension_token=runtime_paths.env_value("PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
        )
        resources.push_async_callback(browser.close)
    provider = (
        PyAutoGuiDesktopProvider(
            allowed_app_ids=frozenset(config.allowed_app_ids),
            max_screenshot_width=config.capture.max_screenshot_width,
            jpeg_quality=config.capture.jpeg_quality,
        )
        if config.allowed_app_ids
        else None
    )
    filesystem = None
    if config.files.roots:
        filesystem = DesktopFilesystem(config.files.roots)
        resources.callback(filesystem.close)
    shell = None
    if config.shell.enabled:
        # Login-shell capture needs pwd, so only runs that passed the POSIX check import it.
        from mindroom.desktop.login_environment import capture_login_environment  # noqa: PLC0415

        shell = DesktopShell(environment=await capture_login_environment())
        resources.push_async_callback(shell.close)
    bridge = DesktopBridge(
        client=client,
        provider=provider,
        policy=DesktopBridgePolicy(
            controller=config.controller,
            allowed_requester_ids=frozenset(config.allowed_requester_ids),
            allowed_agent_names=frozenset(config.allowed_agent_names),
            allowed_app_ids=frozenset(config.allowed_app_ids),
            allow_control=control_lease_expires_at_ms is not None,
            control_lease_expires_at_ms=control_lease_expires_at_ms,
            browser_enabled=config.browser.enabled,
            allowed_file_roots=config.files.roots,
            shell_enabled=config.shell.enabled,
        ),
        browser_provider=browser,
        filesystem=filesystem,
        shell=shell,
        journal_path=runtime_paths.storage_root / "desktop_bridge" / "commands.sqlite3",
        legacy_journal_path=runtime_paths.storage_root / "desktop_bridge" / "command_journal.json",
    )
    resources.callback(bridge.close)
    return bridge, browser


async def first_worker_exit(workers: Collection[asyncio.Task[None]]) -> BaseException:
    """Wait until the first worker ends and return why; one that returns normally stopped unexpectedly.

    A cancelled worker cancels the caller too, since its `exception()` raises `CancelledError`.
    """
    done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
    for worker in done:
        if (failure := worker.exception()) is not None:
            return failure
    return DesktopWorkerStoppedError()


__all__ = [
    "DesktopBridgeSession",
    "DesktopBridgeSessionDeps",
    "DesktopWorkerStoppedError",
    "first_worker_exit",
    "start_desktop_bridge_session",
]
