"""Tavily real-time web search and URL extraction toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


def _require_tavily() -> type[Any]:
    """Import and return ``tavily.TavilyClient``, raising a helpful ImportError when absent."""
    try:
        from tavily import TavilyClient
    except ImportError as exc:
        msg = "`tavily-python` not installed. Please install using `pip install tavily-python`"
        raise ImportError(msg) from exc
    return TavilyClient


class _TavilyTools(Toolkit):
    """Search the web and extract URL content using the Tavily API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base_url: str | None = None,
        enable_search: bool = True,
        enable_search_context: bool = False,
        enable_extract: bool = False,
        all: bool = False,  # noqa: A002
        max_tokens: int = 6000,
        include_answer: bool = True,
        search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "advanced",
        extract_depth: Literal["basic", "advanced"] = "basic",
        include_images: bool = False,
        include_favicon: bool = False,
        extract_timeout: int | None = None,
        extract_format: Literal["markdown", "text"] = "markdown",
        format: Literal["json", "markdown"] = "markdown",  # noqa: A002
        topic: Literal["general", "news", "finance"] | None = None,
        time_range: Literal["day", "week", "month", "year", "d", "w", "m", "y"] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        country: str | None = None,
        auto_parameters: bool = False,
        chunks_per_source: int | None = None,
        search_params: dict[str, Any] | None = None,
    ) -> None:
        tavily_client_cls = _require_tavily()
        self.api_key = api_key
        if not self.api_key:
            logger.error("tavily_api_key_missing")
        self.api_base_url = api_base_url

        self.client = tavily_client_cls(api_key=self.api_key, api_base_url=self.api_base_url)
        self.search_depth = search_depth
        self.extract_depth = extract_depth
        self.max_tokens = max_tokens
        self.include_answer = include_answer
        self.include_images = include_images
        self.include_favicon = include_favicon
        self.extract_timeout = extract_timeout
        self.extract_format = extract_format
        self.format = format
        self.topic = topic
        self.time_range = time_range
        self.start_date = start_date
        self.end_date = end_date
        self.days = days
        self.include_domains = include_domains
        self.exclude_domains = exclude_domains
        self.country = country
        self.auto_parameters = auto_parameters
        self.chunks_per_source = chunks_per_source
        self.search_params = search_params

        tools: list[Callable[..., Any]] = []
        if enable_search or all:
            if enable_search_context:
                tools.append(self.web_search_with_tavily)
            else:
                tools.append(self.web_search_using_tavily)
        if enable_extract or all:
            tools.append(self.extract_url_content)

        super().__init__(name="tavily_tools", tools=tools)

    def web_search_using_tavily(self, query: str, max_results: int = 5) -> str:
        """Use this function to search the web for a given query.

        This function uses the Tavily API to provide realtime online information about the query.

        Args:
            query: Query to search for.
            max_results: Maximum number of results to return. Defaults to 5.

        Returns:
            JSON or markdown string of results related to the query.

        """
        params: dict[str, Any] = {
            "query": query,
            "search_depth": self.search_depth,
            "include_answer": self.include_answer,
            "max_results": max_results,
            "topic": self.topic,
            "time_range": self.time_range,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "days": self.days,
            "include_domains": self.include_domains,
            "exclude_domains": self.exclude_domains,
            "country": self.country,
            "chunks_per_source": self.chunks_per_source,
        }
        if self.auto_parameters:
            params["auto_parameters"] = True
        if self.search_params:
            params.update(self.search_params)
        params = {key: value for key, value in params.items() if value is not None}

        response = self.client.search(**params)

        clean_response: dict[str, Any] = {"query": query}
        if "answer" in response:
            clean_response["answer"] = response["answer"]

        clean_results = []
        current_token_count = len(json.dumps(clean_response))
        for result in response.get("results", []):
            item = {
                "title": result["title"],
                "url": result["url"],
                "content": result["content"],
                "score": result["score"],
            }
            current_token_count += len(json.dumps(item))
            if current_token_count > self.max_tokens:
                break
            clean_results.append(item)
        clean_response["results"] = clean_results

        if self.format == "json":
            return json.dumps(clean_response) if clean_response else "No results found."
        markdown = f"# {query}\n\n"
        if "answer" in clean_response:
            markdown += f"### Summary\n{clean_response.get('answer')}\n\n"
        for result in clean_response["results"]:
            markdown += f"### [{result['title']}]({result['url']})\n{result['content']}\n\n"
        return markdown

    def web_search_with_tavily(self, query: str) -> str:
        """Use this function to search the web for a given query.

        This function uses the Tavily API to provide realtime online information about the query.

        Args:
            query: Query to search for.

        Returns:
            JSON string of results related to the query.

        """
        return self.client.get_search_context(query=query, search_depth=self.search_depth, max_tokens=self.max_tokens)

    def extract_url_content(self, urls: str) -> str:
        """Extract content from one or more URLs using Tavily's Extract API.

        This function retrieves the main content from web pages in markdown or text format.

        Args:
            urls: Single URL or multiple comma-separated URLs to extract content from.
                Example: "https://example.com" or "https://example.com,https://another.com"

        Returns:
            Extracted content in the specified format (markdown or text).

        """
        url_list = [url.strip() for url in urls.split(",") if url.strip()]
        if not url_list:
            return "Error: No valid URLs provided."

        try:
            extract_params: dict[str, Any] = {
                "urls": url_list,
                "extract_depth": self.extract_depth,
            }
            if self.include_images:
                extract_params["include_images"] = True
            if self.include_favicon:
                extract_params["include_favicon"] = True
            if self.extract_timeout is not None:
                extract_params["timeout"] = self.extract_timeout

            response = self.client.extract(**extract_params)
            results = response.get("results", []) if isinstance(response, dict) else []
            if not results:
                return "Error: No content could be extracted from the provided URL(s)."
            if self.extract_format == "markdown":
                return self._format_extract_markdown(results)
            if self.extract_format == "text":
                return self._format_extract_text(results)
            return json.dumps(results, indent=2)
        except Exception as exc:
            logger.exception("tavily_extract_failed")
            return f"Error extracting content: {exc}"

    @staticmethod
    def _format_extract_markdown(results: list[dict[str, Any]]) -> str:
        output = []
        for result in results:
            url = result.get("url", "Unknown URL")
            raw_content = result.get("raw_content", "")
            failed_reason = result.get("failed_reason")
            if failed_reason:
                output.append(f"## {url}\n\n **Extraction Failed**: {failed_reason}\n\n")
            elif raw_content:
                output.append(f"## {url}\n\n{raw_content}\n\n")
            else:
                output.append(f"## {url}\n\n*No content available*\n\n")
        return "".join(output) if output else "No content extracted."

    @staticmethod
    def _format_extract_text(results: list[dict[str, Any]]) -> str:
        output = []
        for result in results:
            url = result.get("url", "Unknown URL")
            raw_content = result.get("raw_content", "")
            failed_reason = result.get("failed_reason")
            output.append(f"URL: {url}")
            output.append("-" * 80)
            if failed_reason:
                output.append(f"EXTRACTION FAILED: {failed_reason}")
            elif raw_content:
                output.append(raw_content)
            else:
                output.append("No content available")
            output.append("\n")
        return "\n".join(output) if output else "No content extracted."


@register_tool_with_metadata(
    name="tavily",
    file_access=ToolFileAccess.NONE,
    display_name="Tavily",
    description="Real-time web search API for retrieving current information",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("TAVILY_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_base_url",
            env=("TAVILY_API_BASE_URL",),
            label="API Base URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_context",
            label="Enable Search Context",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_extract",
            label="Enable Extract",
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
        ConfigField(
            name="max_tokens",
            label="Max Tokens",
            type="number",
            required=False,
            default=6000,
        ),
        ConfigField(
            name="include_answer",
            label="Include Answer",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="search_depth",
            label="Search Depth",
            type="text",
            required=False,
            default="advanced",
        ),
        ConfigField(
            name="extract_depth",
            label="Extract Depth",
            type="text",
            required=False,
            default="basic",
        ),
        ConfigField(
            name="include_images",
            label="Include Images",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="include_favicon",
            label="Include Favicon",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="extract_timeout",
            label="Extract Timeout",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="extract_format",
            label="Extract Format",
            type="text",
            required=False,
            default="markdown",
        ),
        ConfigField(
            name="format",
            label="Format",
            type="text",
            required=False,
            default="markdown",
        ),
        ConfigField(
            name="topic",
            label="Topic",
            type="text",
            required=False,
            default=None,
            description="Search category: general, news, or finance",
        ),
        ConfigField(
            name="time_range",
            label="Time Range",
            type="text",
            required=False,
            default=None,
            description="Time window for results: day, week, month, year (or d/w/m/y)",
        ),
        ConfigField(
            name="start_date",
            label="Start Date",
            type="text",
            required=False,
            default=None,
            description="Only include results published after this date (YYYY-MM-DD)",
        ),
        ConfigField(
            name="end_date",
            label="End Date",
            type="text",
            required=False,
            default=None,
            description="Only include results published before this date (YYYY-MM-DD)",
        ),
        ConfigField(
            name="days",
            label="Days",
            type="number",
            required=False,
            default=None,
            description="Number of days back to include results (news topic only)",
        ),
        ConfigField(
            name="include_domains",
            label="Include Domains",
            type="string[]",
            required=False,
            default=None,
            description="Restrict results to these domains",
        ),
        ConfigField(
            name="exclude_domains",
            label="Exclude Domains",
            type="string[]",
            required=False,
            default=None,
            description="Exclude these domains from results",
        ),
        ConfigField(
            name="country",
            label="Country",
            type="text",
            required=False,
            default=None,
            description="Boost results from this country (e.g. united states)",
        ),
        ConfigField(
            name="auto_parameters",
            label="Auto Parameters",
            type="boolean",
            required=False,
            default=False,
            description="Let Tavily auto-tune search parameters; explicitly set parameters take precedence",
        ),
        ConfigField(
            name="chunks_per_source",
            label="Chunks Per Source",
            type="number",
            required=False,
            default=None,
            description="Number of content chunks per source (1-3), advanced search only",
        ),
    ],
    dependencies=["tavily-python"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#tavily",
    function_names=("extract_url_content", "web_search_using_tavily", "web_search_with_tavily"),
)
def tavily_tools() -> type[Toolkit]:
    """Return Tavily tools for real-time web search."""
    _require_tavily()
    return _TavilyTools
