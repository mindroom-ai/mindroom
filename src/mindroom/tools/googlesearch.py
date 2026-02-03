"""Google Search tool configuration."""

from __future__ import annotations

from agno.tools.websearch import WebSearchTools

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)


class GoogleSearchTools(WebSearchTools):
    """Convenience wrapper for WebSearchTools with Google as the backend."""

    def __init__(
        self,
        enable_search: bool = True,
        enable_news: bool = True,
        modifier: str | None = None,
        fixed_max_results: int | None = None,
        proxy: str | None = None,
        timeout: int | None = 10,
        verify_ssl: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(
            enable_search=enable_search,
            enable_news=enable_news,
            backend="google",
            modifier=modifier,
            fixed_max_results=fixed_max_results,
            proxy=proxy,
            timeout=timeout,
            verify_ssl=verify_ssl,
            **kwargs,
        )


@register_tool_with_metadata(
    name="googlesearch",
    display_name="Google Search",
    description="Search Google for web results using the WebSearch backend",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaGoogle",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="text",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_news",
            label="Enable News",
            type="text",
            required=False,
            default=True,
        ),
        ConfigField(
            name="modifier",
            label="Modifier",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="proxy",
            label="Proxy",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="text",
            required=False,
            default=10,
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify Ssl",
            type="text",
            required=False,
            default=True,
        ),
    ],
    dependencies=["ddgs"],
    docs_url="https://docs.agno.com/tools/toolkits/search/websearch",
)
def googlesearch_tools() -> type[GoogleSearchTools]:
    """Return Google Search tools for web search."""
    return GoogleSearchTools
