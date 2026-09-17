"""Fixed provider identity and retained-function dispatch at Computer composition."""

from pathlib import Path

import pytest
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit
from fastapi import HTTPException

from mindroom.api.computer_browser_binding import select_browser_provider
from mindroom.api.sandbox_runner import _run_toolkit_entrypoint
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import BrowserTools
from mindroom.custom_tools.browser_mcp import BrowserMCPTools
from mindroom.tools.browser import browser_tools
from mindroom.tools.browser_mcp import browser_mcp_tools


@pytest.mark.parametrize("native", [False, True])
def test_provider_rejects_replaced_factory_and_subclass(tmp_path: Path, native: bool) -> None:
    """Only exact built-in factories and toolkit types can bind the Computer."""
    name = "browser_mcp" if native else "browser"
    function = "browser_snapshot" if native else "browser_control"
    factory = browser_mcp_tools if native else browser_tools
    with pytest.raises(HTTPException, match="built-in browser factory"):
        select_browser_provider(name, function, lambda: Toolkit, str)
    provider = select_browser_provider(name, function, factory, str)
    toolkit_type = BrowserMCPTools if native else BrowserTools
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    replacement_type = type("ReplacementBrowser", (toolkit_type,), {})
    with pytest.raises(HTTPException, match="built-in browser tool"):
        provider.validate_toolkit(replacement_type(runtime_paths=paths))
    assert type(provider.validate_toolkit(toolkit_type(runtime_paths=paths))) is toolkit_type


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_retained_session_rejects_functions_outside_fixed_provider(
    tmp_path: Path,
    native: bool,
) -> None:
    """A function added to a toolkit cannot widen the fixed Computer surface."""
    name = "browser_mcp" if native else "browser"
    function = "browser_snapshot" if native else "browser_control"
    factory = browser_mcp_tools if native else browser_tools
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    toolkit = BrowserMCPTools(runtime_paths=paths) if native else BrowserTools(runtime_paths=paths)
    provider = select_browser_provider(name, function, factory, str)

    async def unexpected() -> str:
        return "escaped allowlist"

    toolkit.async_functions["browser_run_code_unsafe"] = Function(
        name="browser_run_code_unsafe",
        entrypoint=unexpected,
    )
    _key, session = provider.bind(provider.validate_toolkit(toolkit), ":77", tmp_path, _run_toolkit_entrypoint)
    try:
        with pytest.raises(ValueError, match="Unsupported"):
            await session.execute("browser_run_code_unsafe")
    finally:
        await session.close()
