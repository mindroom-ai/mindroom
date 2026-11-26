"""Jina AI Reader web content reading and search toolkit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field, HttpUrl

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


class JinaReaderToolsConfig(BaseModel):
    """Validated configuration for JinaReaderTools."""

    api_key: str | None = Field(None, description="API key for Jina Reader")
    base_url: HttpUrl = Field(HttpUrl("https://r.jina.ai/"), description="Base URL for Jina Reader API")
    search_url: HttpUrl = Field(HttpUrl("https://s.jina.ai/"), description="Search URL for Jina Reader API")
    max_content_length: int = Field(10000, description="Maximum content length in characters")
    timeout: int | None = Field(None, description="Timeout for Jina Reader API requests")
    search_query_content: bool | None = Field(False, description="Toggle full URL content in query search result")


class JinaReaderTools(Toolkit):
    """Read web pages and run web searches through the Jina AI Reader API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://r.jina.ai/",
        search_url: str = "https://s.jina.ai/",
        max_content_length: int = 10000,
        timeout: int | None = None,
        search_query_content: bool = True,
        enable_read_url: bool = True,
        enable_search_query: bool = False,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.api_key = api_key
        self.config = JinaReaderToolsConfig(
            api_key=self.api_key,
            base_url=HttpUrl(base_url),
            search_url=HttpUrl(search_url),
            max_content_length=max_content_length,
            timeout=timeout,
            search_query_content=search_query_content,
        )

        tools: list[Callable[..., Any]] = []
        if all or enable_read_url:
            tools.append(self.read_url)
        if all or enable_search_query:
            tools.append(self.search_query)

        super().__init__(name="jina_reader_tools", tools=tools)

    def read_url(self, url: str) -> str:
        """Reads a URL and returns the truncated content using Jina Reader API."""
        full_url = f"{self.config.base_url}{url}"
        try:
            response = httpx.get(full_url, headers=self._get_headers(), timeout=self.config.timeout)
            response.raise_for_status()
            content = response.json()
        except Exception as exc:
            error_msg = f"Error reading URL: {exc}"
            logger.exception("jina_read_url_failed", url=url, error=str(exc))
            return error_msg
        else:
            return self._truncate_content(str(content))

    def search_query(self, query: str) -> str:
        """Performs a web search using Jina Reader API and returns the truncated results."""
        full_url = f"{self.config.search_url}"
        headers = self._get_headers()
        if not self.config.search_query_content:
            headers["X-Respond-With"] = "no-content"

        try:
            response = httpx.post(full_url, headers=headers, json={"q": query}, timeout=self.config.timeout)
            response.raise_for_status()
            content = response.json()
        except Exception as exc:
            error_msg = f"Error performing search: {exc}"
            logger.exception("jina_search_query_failed", query=query, error=str(exc))
            return error_msg
        else:
            return self._truncate_content(str(content))

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-With-Links-Summary": "true",
            "X-With-Images-Summary": "true",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.timeout:
            headers["X-Timeout"] = str(self.config.timeout)
        return headers

    def _truncate_content(self, content: str) -> str:
        """Truncate content to the maximum allowed length."""
        if len(content) > self.config.max_content_length:
            return content[: self.config.max_content_length] + "... (content truncated)"
        return content


@register_tool_with_metadata(
    name="jina",
    file_access=ToolFileAccess.NONE,
    display_name="Jina Reader",
    description="Web content reading and search using Jina AI Reader API",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGlobe",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("JINA_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default="https://r.jina.ai/",
        ),
        ConfigField(
            name="search_url",
            label="Search URL",
            type="url",
            required=False,
            default="https://s.jina.ai/",
        ),
        ConfigField(
            name="max_content_length",
            label="Max Content Length",
            type="number",
            required=False,
            default=10000,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="search_query_content",
            label="Search Query Content",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_url",
            label="Enable Read URL",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_query",
            label="Enable Search Query",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["httpx", "pydantic"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#jina",
    function_names=("read_url", "search_query"),
)
def jina_tools() -> type[JinaReaderTools]:
    """Return Jina Reader tools for web content reading and search."""
    return JinaReaderTools
