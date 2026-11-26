"""Tests for renaming a toolkit's model-facing functions."""

from __future__ import annotations

import asyncio

from mindroom.tool_system.authoring import Toolkit, tool, toolkit_functions
from mindroom.tool_system.toolkits import rename_toolkit_functions, toolkit_function_names


class OwnedDemoTools(Toolkit):
    """Native toolkit with sync and async functions."""

    name = "owned_demo_tools"

    @tool(name="lookup")
    def search(self, query: str) -> str:
        """Search."""
        return f"owned:{query}"

    @tool(name="async_lookup")
    async def asearch(self, query: str) -> str:
        """Search asynchronously."""
        return f"async:{query}"


def test_renaming_an_owned_toolkits_functions_keeps_the_original_methods() -> None:
    """A toolkit offers functions under their aliases, which also name them on the toolkit."""
    toolkit = OwnedDemoTools()

    rename_toolkit_functions(toolkit, {"lookup": "demo_lookup", "async_lookup": "demo_async_lookup"})

    assert toolkit_function_names(toolkit) == ("demo_lookup", "demo_async_lookup")
    assert toolkit_functions(toolkit)["demo_lookup"](query="docs") == "owned:docs"
    assert asyncio.run(toolkit_functions(toolkit)["demo_async_lookup"](query="docs")) == "async:docs"
    assert toolkit.demo_lookup("docs") == "owned:docs"
    assert asyncio.run(toolkit.demo_async_lookup("docs")) == "async:docs"
    assert toolkit.search("docs") == "owned:docs"
    assert asyncio.run(toolkit.asearch("docs")) == "async:docs"
