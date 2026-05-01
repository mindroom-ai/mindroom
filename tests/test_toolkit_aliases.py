"""Tests for shared Agno toolkit alias helpers."""

from __future__ import annotations

import asyncio

from agno.tools.toolkit import Toolkit

from mindroom.tool_system.toolkit_aliases import apply_toolkit_function_aliases


class DemoTools(Toolkit):
    """Small toolkit with matching sync and async model-facing function names."""

    def __init__(self) -> None:
        super().__init__(
            name="demo_tools",
            tools=[self.search],
            async_tools=[(self.asearch, "search")],
        )

    def search(self, query: str) -> str:
        """Search synchronously."""
        return f"sync:{query}"

    async def asearch(self, query: str) -> str:
        """Search asynchronously."""
        return f"async:{query}"


def test_apply_toolkit_function_aliases_renames_sync_and_async_functions() -> None:
    """Alias helper updates sync and async functions without replacing original methods."""
    toolkit = DemoTools()

    apply_toolkit_function_aliases(toolkit, {"search": "demo_search"})

    assert set(toolkit.functions) == {"demo_search"}
    assert set(toolkit.async_functions) == {"demo_search"}
    assert toolkit.functions["demo_search"].name == "demo_search"
    assert toolkit.async_functions["demo_search"].name == "demo_search"
    assert toolkit.functions["demo_search"].entrypoint("docs") == "sync:docs"
    assert asyncio.run(toolkit.async_functions["demo_search"].entrypoint("docs")) == "async:docs"
    assert toolkit.demo_search("docs") == "sync:docs"
    assert toolkit.search("docs") == "sync:docs"
