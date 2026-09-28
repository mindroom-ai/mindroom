"""Build the local capability providers and bridge for one Desktop run, shared by the app helper and the terminal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.desktop.bridge import DesktopBridge, DesktopBridgePolicy
from mindroom.desktop.filesystem import DesktopFilesystem
from mindroom.desktop.login_environment import capture_login_environment
from mindroom.desktop.playwright_mcp import PlaywrightMCPBrowserProvider
from mindroom.desktop.provider import PyAutoGuiDesktopProvider
from mindroom.desktop.shell import DesktopShell

if TYPE_CHECKING:
    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.desktop.native_config import NativeDesktopConfig


@dataclass(frozen=True, slots=True)
class DesktopBridgeComponents:
    """One run's bridge and the local providers its owner closes after stopping it."""

    bridge: DesktopBridge
    browser: PlaywrightMCPBrowserProvider | None
    filesystem: DesktopFilesystem | None
    shell: DesktopShell | None


async def build_desktop_bridge(
    config: NativeDesktopConfig,
    *,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    control_lease_expires_at_ms: int | None = None,
) -> DesktopBridgeComponents:
    """Build exactly the saved capabilities, closing any already built provider if construction fails.

    Only selected apps construct the GUI provider, so folder, shell, and browser runs need no GUI
    runtime. A control lease expiry grants app input from the start; without one the bridge is observe-only.
    """
    browser = None
    filesystem = None
    shell = None
    try:
        if config.browser.enabled:
            browser = PlaywrightMCPBrowserProvider(
                output_dir=runtime_paths.storage_root / "desktop-browser",
                executable_path=config.browser.executable_path,
                user_data_dir=config.browser.user_data_dir,
                call_timeout_seconds=config.browser.timeout_seconds,
                extension_token=runtime_paths.env_value("PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
            )
        provider = (
            PyAutoGuiDesktopProvider(
                allowed_app_ids=frozenset(config.allowed_app_ids),
                max_screenshot_width=config.capture.max_screenshot_width,
                jpeg_quality=config.capture.jpeg_quality,
            )
            if config.allowed_app_ids
            else None
        )
        filesystem = DesktopFilesystem(config.files.roots) if config.files.roots else None
        if config.shell.enabled:
            shell = DesktopShell(environment=await capture_login_environment())
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
    except BaseException:
        if shell is not None:
            await shell.close()
        if filesystem is not None:
            filesystem.close()
        if browser is not None:
            await browser.close()
        raise
    return DesktopBridgeComponents(bridge=bridge, browser=browser, filesystem=filesystem, shell=shell)


__all__ = ["DesktopBridgeComponents", "build_desktop_bridge"]
