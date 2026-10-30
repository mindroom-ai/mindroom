"""Tests for renaming a toolkit's model-facing functions."""

from __future__ import annotations

import asyncio

from agno.tools.toolkit import Toolkit as AgnoToolkit

from mindroom.tool_system.agno_toolkits import rename_toolkit_functions, toolkit_function_names
from mindroom.tool_system.authoring import Toolkit, tool, toolkit_functions


class DemoTools(AgnoToolkit):
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


class OwnedDemoTools(Toolkit):
    """Owned toolkit with one function named by ``@tool``."""

    name = "owned_demo_tools"

    @tool(name="lookup")
    def search(self, query: str) -> str:
        """Search."""
        return f"owned:{query}"


def test_renaming_an_agno_toolkits_functions_keeps_both_variants_and_the_original_methods() -> None:
    """Both of an Agno toolkit's variants take the alias, which also names the sync one on the toolkit."""
    toolkit = DemoTools()

    rename_toolkit_functions(toolkit, {"search": "demo_search"})

    assert set(toolkit.functions) == {"demo_search"}
    assert set(toolkit.async_functions) == {"demo_search"}
    assert toolkit.functions["demo_search"].name == "demo_search"
    assert toolkit.async_functions["demo_search"].name == "demo_search"
    assert toolkit_function_names(toolkit) == ("demo_search",)
    assert asyncio.run(toolkit_functions(toolkit)["demo_search"](query="docs")) == "async:docs"
    assert toolkit.demo_search("docs") == "sync:docs"
    assert toolkit.search("docs") == "sync:docs"


def test_renaming_an_owned_toolkits_functions_keeps_the_original_methods() -> None:
    """An owned toolkit offers the function under its alias, which also names it on the toolkit."""
    toolkit = OwnedDemoTools()

    rename_toolkit_functions(toolkit, {"lookup": "demo_lookup"})

    assert toolkit_function_names(toolkit) == ("demo_lookup",)
    assert toolkit_functions(toolkit)["demo_lookup"](query="docs") == "owned:docs"
    assert toolkit.demo_lookup("docs") == "owned:docs"
    assert toolkit.search("docs") == "owned:docs"
