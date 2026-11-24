"""Native Playwright MCP functions bound only by the worker Computer dispatcher."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.tool_system.authoring import Toolkit, passthrough_tool
from mindroom.worker_computer.browser_proxy import browser_egress
from mindroom.worker_computer.mcp_catalog import browser_mcp_catalog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.worker_computer.mcp_provider import WorkerBrowserMCP


class BrowserMCPTools(Toolkit):
    """Expose immutable native schemas without starting a primary-side process."""

    def __init__(self, *, runtime_paths: RuntimePaths | None = None, allow_private_networks: bool = False) -> None:
        if type(allow_private_networks) is not bool:
            msg = "allow_private_networks must be a boolean."
            raise ValueError(msg)
        self._runtime_paths = runtime_paths
        self._allow_private_networks = allow_private_networks
        self._provider: WorkerBrowserMCP | None = None
        super().__init__(
            name="browser_mcp",
            tools=[
                passthrough_tool(name=name, description=tool["description"], parameters=tool["inputSchema"])(
                    self._entrypoint(name),
                )
                for name, tool in browser_mcp_catalog().items()
            ],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Offer each native function non-strict: its optional fields stay omitted, not required empty values."""
        return replace(function, strict=False)

    def _entrypoint(self, name: str) -> Callable[..., Awaitable[object]]:
        async def call(**kwargs: object) -> object:
            if self._provider is None:
                msg = "browser_mcp requires an enabled dedicated computer worker."
                raise RuntimeError(msg)
            return await self._provider.execute(name, kwargs)

        return call

    def bind_worker_display(self, display: str, workspace: Path) -> str:
        """Bind prepared worker-owned paths; resource creation remains lazy."""
        from mindroom.worker_computer.mcp_provider import WorkerBrowserMCP  # noqa: PLC0415

        if self._runtime_paths is None:
            msg = "browser_mcp requires prepared worker runtime paths."
            raise RuntimeError(msg)
        self._provider = WorkerBrowserMCP(
            display=display,
            workspace=workspace,
            storage_root=self._runtime_paths.storage_root,
            allow_private_networks=self._allow_private_networks,
            allow_loopback=True,
            # The Computer browser runs in a dedicated worker, where the egress proxy may enforce approved egress.
            egress=browser_egress(self._runtime_paths.process_env, os.environ, egress_control=True),
        )
        return f"browser_mcp:{self._allow_private_networks}"

    async def aclose(self) -> None:
        """Close MCP and verifier resources, preserving disk state."""
        if self._provider is not None:
            await self._provider.close()
