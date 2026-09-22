"""Fixed built-in browser binding for the worker Computer composition root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException

from mindroom.worker_computer.protocol import BrowserSession

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agno.tools.toolkit import Toolkit

    from mindroom.custom_tools.browser import BrowserTools
    from mindroom.custom_tools.browser_mcp import BrowserMCPTools


@dataclass(frozen=True)
class _ComputerBrowserProvider:
    """An exact built-in provider and its result encoder, selected before resolution."""

    toolkit_type: type[BrowserTools | BrowserMCPTools]
    function_names: frozenset[str]
    encode_result: Callable[[object], object]

    def validate_toolkit(self, toolkit: Toolkit) -> BrowserTools | BrowserMCPTools:
        """Reject replacement toolkits, including subclasses of the built-in type."""
        if type(toolkit) is not self.toolkit_type:
            raise HTTPException(status_code=400, detail="Worker computer requires the built-in browser tool.")
        assert isinstance(toolkit, self.toolkit_type)
        return toolkit

    def bind_headless(
        self,
        toolkit: BrowserTools | BrowserMCPTools,
        workspace: Path,
        process_env: dict[str, str],
    ) -> tuple[BrowserTools, str]:
        """Bind only the built-in headless browser, keeping its current request wrappers."""
        from mindroom.custom_tools.browser import BrowserTools  # noqa: PLC0415

        if type(toolkit) is not BrowserTools:
            raise HTTPException(status_code=400, detail="Headless workers require the built-in browser tool.")
        return toolkit, toolkit.bind_worker_headless(workspace, process_env)

    def bind(
        self,
        toolkit: BrowserTools | BrowserMCPTools,
        display: str,
        workspace: Path,
        invoke: Callable[[Toolkit, Callable[..., object], list[object], dict[str, object]], Awaitable[object]],
    ) -> tuple[str, BrowserSession]:
        """Bind the output-wrapped toolkit; the caller owns invocation policy and timeout."""
        key = toolkit.bind_worker_display(display, workspace)

        async def execute(current_function: str, *args: object, **kwargs: object) -> object:
            # Resolve each call on the retained toolkit, including its output wrappers.
            if current_function not in self.function_names:
                msg = "Unsupported worker browser function."
                raise ValueError(msg)
            function = toolkit.get_async_functions().get(current_function)
            if function is None or function.entrypoint is None:
                msg = "Unsupported worker browser function."
                raise ValueError(msg)
            return await invoke(toolkit, function.entrypoint, list(args), kwargs)

        return key, BrowserSession(execute=execute, close=toolkit.aclose)


def select_browser_provider(
    tool_name: str,
    function_name: str,
    factory: Callable[[], type] | None,
    encode_json: Callable[[object], object],
) -> _ComputerBrowserProvider:
    """Validate fixed provider identity and supported function before constructing tools."""
    # Browser SDK imports remain lazy for slim runner startup.
    from mindroom.custom_tools.browser import BrowserTools  # noqa: PLC0415
    from mindroom.custom_tools.browser_mcp import BrowserMCPTools  # noqa: PLC0415
    from mindroom.tool_system.media_transport import encode_media_result  # noqa: PLC0415
    from mindroom.tools.browser import browser_tools  # noqa: PLC0415
    from mindroom.tools.browser_mcp import browser_mcp_tools  # noqa: PLC0415
    from mindroom.worker_computer.mcp_catalog import browser_mcp_catalog  # noqa: PLC0415

    if tool_name == "browser_mcp":
        if function_name not in browser_mcp_catalog():
            raise HTTPException(status_code=400, detail="Unsupported native browser MCP function.")
        expected_factory = browser_mcp_tools
        provider = _ComputerBrowserProvider(
            BrowserMCPTools,
            frozenset(browser_mcp_catalog()),
            encode_media_result,
        )
    elif tool_name == "browser" and function_name == "browser_control":
        from agno.tools.function import ToolResult  # noqa: PLC0415

        def encode_action_browser_result(result: object) -> object:
            if isinstance(result, ToolResult):
                return encode_media_result(result)
            return encode_json(result)

        expected_factory = browser_tools
        provider = _ComputerBrowserProvider(
            BrowserTools,
            frozenset({"browser_control"}),
            encode_action_browser_result,
        )
    else:
        raise HTTPException(status_code=400, detail="Unsupported worker browser function.")
    if factory is not expected_factory:
        raise HTTPException(status_code=400, detail="Worker computer requires the built-in browser factory.")
    return provider
