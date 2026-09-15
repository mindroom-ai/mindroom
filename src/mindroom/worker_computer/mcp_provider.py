"""Headed, persistent native MCP browser owned by one Computer binding."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from mcp import StdioServerParameters
from mcp.client.stdio import get_default_environment

from mindroom.mcp.results import tool_result_from_call_result
from mindroom.playwright_mcp_session import PlaywrightMCPSession
from mindroom.worker_computer.browser_guard import BrowserURLVerifier
from mindroom.worker_computer.browser_proxy import BrowserDestinationProxy
from mindroom.worker_computer.mcp_catalog import browser_mcp_catalog, verify_browser_mcp_catalog

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import ToolResult

_SERVER = "/opt/mindroom-browser-mcp/node_modules/@playwright/mcp/cli.js"
_BROWSER = "/opt/mindroom-browser-mcp/chromium"


class WorkerBrowserMCP:
    """Retain a guarded MCP browser; every call must arrive through the Computer gate."""

    def __init__(
        self,
        *,
        display: str,
        workspace: Path,
        storage_root: Path,
        allow_private_networks: bool = False,
    ) -> None:
        self._display = display
        self._workspace = workspace.resolve()
        self._profile = storage_root.resolve() / "browser-profiles" / "native-mcp"
        self._output = self._workspace / "browser"
        self._verifier = BrowserURLVerifier(allow_private_networks=allow_private_networks)
        self._proxy = BrowserDestinationProxy(allow_private_networks=allow_private_networks)
        self._session: PlaywrightMCPSession | None = None
        self._ready = False

    def _server_parameters(self) -> StdioServerParameters:
        """Build fixed offline launch options; neither model nor workspace supplies code."""
        env = get_default_environment()
        env.update(
            {
                "DISPLAY": self._display,
                "MINDROOM_BROWSER_VERIFY_ENDPOINT": self._verifier.endpoint,
                "MINDROOM_BROWSER_VERIFY_TOKEN": self._verifier.token,
            },
        )
        return StdioServerParameters(
            command="node",
            args=[
                _SERVER,
                "--caps",
                "vision,pdf",
                "--sandbox",
                "--block-service-workers",
                "--proxy-server",
                self._proxy.endpoint,
                "--proxy-bypass",
                "<-loopback>",
                "--executable-path",
                _BROWSER,
                "--user-data-dir",
                str(self._profile),
                "--output-dir",
                str(self._output),
                "--output-mode",
                "stdout",
                "--init-page",
                "/opt/mindroom-browser-mcp/browser_guard.cjs",
            ],
            env=env,
            cwd=str(self._workspace),
        )

    async def execute(self, function_name: str, arguments: dict[str, object]) -> ToolResult:
        """Validate catalog and install initial context guard before any native call."""
        if function_name not in browser_mcp_catalog():
            msg = "Unsupported native browser MCP function."
            raise ValueError(msg)
        arguments = self._file_arguments(function_name, arguments)
        try:
            if not self._ready:
                self._workspace.mkdir(parents=True, exist_ok=True)
                self._profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._output.mkdir(parents=True, exist_ok=True)
                await self._verifier.start()
                await self._proxy.start()
                self._session = PlaywrightMCPSession(self._server_parameters())
                tools = await self._session.list_tools()
                verify_browser_mcp_catalog([tool.model_dump(by_alias=True) for tool in tools])
                bootstrap = await self._session.call_tool("browser_tabs", {"action": "list"})
                tool_result_from_call_result("browser_mcp", bootstrap)
                self._ready = True
            assert self._session is not None
            result = await self._session.call_tool(function_name, arguments)
        except BaseException:
            await self.close()
            raise
        return tool_result_from_call_result("browser_mcp", result)

    def _file_arguments(self, function_name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Confine upload/drop files before startup; retain native cancellation/data-only calls."""
        if function_name not in {"browser_file_upload", "browser_drop"} or "paths" not in arguments:
            return arguments
        paths = arguments["paths"]
        if not isinstance(paths, list):
            msg = "Native browser paths must be an array of file paths."
            raise ValueError(msg)  # noqa: TRY004 - invalid tool payloads share the validation error contract
        canonical_paths: list[str] = []
        for value in paths:
            if not isinstance(value, str) or not value:
                msg = "Native browser paths must contain non-empty file paths."
                raise ValueError(msg)
            msg = "Native browser files must be existing regular files within the worker workspace."
            try:
                path = (self._workspace / value).resolve(strict=True)
                allowed = path.is_relative_to(self._workspace) and path.is_file()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(msg) from exc
            if not allowed:
                raise ValueError(msg)
            canonical_paths.append(str(path))
        return {**arguments, "paths": canonical_paths}

    async def close(self) -> None:
        """Reap MCP/browser and callback resources while keeping profile and output files."""
        try:
            if self._session is not None:
                try:
                    if self._ready and self._session.running:
                        # Normal stop flushes the persistent profile. A stuck or
                        # disconnected browser still reaches bounded forced cleanup.
                        with contextlib.suppress(Exception):
                            async with asyncio.timeout(2):
                                await self._session.call_tool("browser_close", {})
                finally:
                    await self._session.close()
                    self._session = None
        finally:
            self._ready = False
            try:
                await self._proxy.close()
            finally:
                await self._verifier.close()
