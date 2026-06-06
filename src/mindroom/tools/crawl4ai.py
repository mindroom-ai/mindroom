"""Crawl4AI tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.browser_fetch_guard import continue_or_abort_browser_fetch
from mindroom.server_fetch_url import validate_server_fetch_url
from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.crawl4ai import Crawl4aiTools
    from playwright.async_api import Page


@register_tool_with_metadata(
    name="crawl4ai",
    display_name="Crawl4AI",
    description="Web crawling and scraping using the Crawl4ai library",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaSpider",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="max_length",
            label="Max Length",
            type="number",
            required=False,
            default=5000,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=60,
        ),
        ConfigField(
            name="use_pruning",
            label="Use Pruning",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="pruning_threshold",
            label="Pruning Threshold",
            type="number",
            required=False,
            default=0.48,
        ),
        ConfigField(
            name="bm25_threshold",
            label="Bm25 Threshold",
            type="number",
            required=False,
            default=1.0,
        ),
        ConfigField(
            name="headless",
            label="Headless",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="wait_until",
            label="Wait Until",
            type="text",
            required=False,
            default="domcontentloaded",
        ),
        ConfigField(
            name="proxy_config",
            label="Proxy Config",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_crawl",
            label="Enable Crawl",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["crawl4ai"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/crawl4ai",
    function_names=("crawl",),
)
def crawl4ai_tools() -> type[Crawl4aiTools]:  # noqa: C901
    """Return Crawl4AI tools for web crawling and scraping."""
    import agno.tools.crawl4ai as agno_crawl4ai
    from agno.tools.crawl4ai import Crawl4aiTools
    from agno.utils.log import log_debug, log_warning

    class MindRoomCrawl4aiTools(Crawl4aiTools):
        """Crawl4AI toolkit with MindRoom server-fetch URL validation."""

        def crawl(self, url: str | list[str], search_query: str | None = None) -> str | dict[str, str]:
            """Crawl validated public HTTP(S) URLs."""
            if isinstance(url, str):
                return super().crawl(validate_server_fetch_url(url), search_query)
            validated_urls = [validate_server_fetch_url(single_url) for single_url in url]
            return super().crawl(validated_urls, search_query)

        async def _guard_page_context(self, page: Page, **_kwargs: object) -> None:
            await page.route("**/*", continue_or_abort_browser_fetch)

        async def _async_crawl(self, url: str, search_query: str | None = None) -> str:
            """Crawl one validated URL with server-fetch guards on Playwright requests."""
            try:
                browser_config = agno_crawl4ai.BrowserConfig(
                    headless=self.headless,
                    verbose=False,
                    **self.proxy_config,
                )

                async with agno_crawl4ai.AsyncWebCrawler(config=browser_config) as crawler:
                    crawler.crawler_strategy.set_hook("on_page_context_created", self._guard_page_context)
                    config = agno_crawl4ai.CrawlerRunConfig(**self._build_config(search_query))
                    log_debug(f"Crawling {url} with config: {config}")
                    result = await crawler.arun(url=url, config=config)

                    if not result:
                        return "Error: No content found"

                    content = ""
                    if result.fit_markdown:
                        content = result.fit_markdown
                        log_debug("Using fit_markdown")
                    elif result.markdown:
                        if isinstance(result.markdown, str):
                            content = result.markdown
                            log_debug("Using str(markdown)")
                        else:
                            content = result.markdown.raw_markdown
                            log_debug("Using markdown.raw_markdown")
                    elif result.text:
                        content = result.text
                        log_debug("Using text attribute")
                    elif result.html:
                        log_warning("Only HTML available, no markdown extracted")
                        return "Error: Could not extract markdown from page"

                    if not content:
                        log_warning(f"No content extracted. Result type: {type(result)}")
                        return "Error: No readable content extracted"

                    log_debug(f"Extracted content length: {len(content)}")
                    if self.max_length and len(content) > self.max_length:
                        content = content[: self.max_length] + "..."
                    return content
            except Exception as exc:
                log_warning(f"Exception during crawl: {exc}")
                return f"Error crawling {url}: {exc}"

    return MindRoomCrawl4aiTools
