"""Crawl4AI tool configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.browser_fetch_guard import continue_or_abort_browser_fetch
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.server_fetch_url import validate_server_fetch_url
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.worker_computer.browser_proxy import (
    PROXIED_WEBRTC_ONLY_ARG,
    BrowserDestinationProxy,
    browser_upstream_proxy,
)

if TYPE_CHECKING:
    from agno.tools.crawl4ai import Crawl4aiTools
    from playwright.async_api import BrowserContext, Page

    from mindroom.constants import RuntimePaths


@register_tool_with_metadata(
    name="crawl4ai",
    file_access=ToolFileAccess.NONE,
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
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    function_names=("crawl",),
)
def crawl4ai_tools() -> type[Crawl4aiTools]:  # noqa: C901
    """Return Crawl4AI tools for web crawling and scraping."""
    import agno.tools.crawl4ai as agno_crawl4ai
    from agno.tools.crawl4ai import Crawl4aiTools
    from agno.utils.log import log_debug, log_warning

    class MindRoomCrawl4aiTools(Crawl4aiTools):
        """Crawl4AI toolkit with MindRoom server-fetch URL validation."""

        # Mirror the authored upstream options; the upstream proxy_config mapping would bypass the egress route.
        def __init__(
            self,
            runtime_paths: RuntimePaths,
            max_length: int | None = 5000,
            timeout: int = 60,
            use_pruning: bool = False,
            pruning_threshold: float = 0.48,
            bm25_threshold: float = 1.0,
            headless: bool = True,
            wait_until: str = "domcontentloaded",
            enable_crawl: bool = True,
            all: bool = False,  # noqa: A002 - upstream option name
        ) -> None:
            super().__init__(
                max_length=max_length,
                timeout=timeout,
                use_pruning=use_pruning,
                pruning_threshold=pruning_threshold,
                bm25_threshold=bm25_threshold,
                headless=headless,
                wait_until=wait_until,
                enable_crawl=enable_crawl,
                all=all,
            )
            self._runtime_paths = runtime_paths

        def crawl(self, url: str | list[str], search_query: str | None = None) -> str | dict[str, str]:
            """Crawl validated public HTTP(S) URLs."""
            if isinstance(url, str):
                return super().crawl(validate_server_fetch_url(url), search_query)
            validated_urls = [validate_server_fetch_url(single_url) for single_url in url]
            return super().crawl(validated_urls, search_query)

        async def _guard_page_context(self, page: Page, *, context: BrowserContext, **_kwargs: object) -> None:
            # Guard the context rather than the page so popups and other pages it opens are routed too.
            # Playwright passes (route, request) to a handler that declares more than one parameter.
            del page
            await context.route("**/*", lambda route: continue_or_abort_browser_fetch(route))

        async def _async_crawl(self, url: str, search_query: str | None = None) -> str:
            """Crawl one validated URL with connect-time destination checks on every browser connection."""
            destination_proxy: BrowserDestinationProxy | None = None
            try:
                # Chromium inherits this environment, so an operator egress proxy stays its only route.
                upstream = browser_upstream_proxy(
                    self._runtime_paths.process_env,
                    os.environ,
                    egress_control=self._runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]),
                )
                if upstream is not None:
                    proxy_server = upstream.server
                else:
                    # Page routes see neither WebSockets, service-worker fetches, nor the address Chromium
                    # resolves for itself, so every TCP connection dials an address validated at connect time.
                    destination_proxy = BrowserDestinationProxy()
                    await destination_proxy.start()
                    proxy_server = destination_proxy.endpoint
                browser_config = agno_crawl4ai.BrowserConfig(
                    headless=self.headless,
                    verbose=False,
                    proxy_config={"server": proxy_server},
                    # Playwright forces loopback through the proxy only unless an environment switch disables it.
                    extra_args=[PROXIED_WEBRTC_ONLY_ARG, "--proxy-bypass-list=<-loopback>"],
                )

                async with agno_crawl4ai.AsyncWebCrawler(config=browser_config) as crawler:
                    crawler.crawler_strategy.set_hook("on_page_context_created", self._guard_page_context)
                    config = agno_crawl4ai.CrawlerRunConfig(**self._build_config(search_query))
                    log_debug(f"Crawling {url} with config: {config}")
                    result = await crawler.arun(url=url, config=config)

                    if not result:
                        return "Error: No content found"

                    # Crawl4AI 0.8 exposes filtered and raw text only through the markdown result.
                    markdown = result.markdown
                    content = (markdown.fit_markdown or str(markdown)) if markdown is not None else ""
                    if not content:
                        if result.html:
                            log_warning("Only HTML available, no markdown extracted")
                            return "Error: Could not extract markdown from page"
                        log_warning(f"No content extracted. Result type: {type(result)}")
                        return "Error: No readable content extracted"

                    log_debug(f"Extracted content length: {len(content)}")
                    if self.max_length and len(content) > self.max_length:
                        content = content[: self.max_length] + "..."
                    return content
            except Exception as exc:
                log_warning(f"Exception during crawl: {exc}")
                return f"Error crawling {url}: {exc}"
            finally:
                if destination_proxy is not None:
                    await destination_proxy.close()

    return MindRoomCrawl4aiTools
