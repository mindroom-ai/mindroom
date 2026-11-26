"""Crawl4AI web crawling and scraping toolkit."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from mindroom.browser.fetch_guard import continue_or_abort_browser_fetch
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.server_fetch_url import validate_server_fetch_url
from mindroom.tool_system.authoring import Toolkit
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
    RELAY_ONLY_PROXY_BYPASS,
    BrowserDestinationProxy,
    browser_egress,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from playwright.async_api import BrowserContext, Page

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _require_crawl4ai() -> ModuleType:
    """Import and return ``crawl4ai``, raising a helpful ImportError when absent."""
    try:
        import crawl4ai
    except ImportError as exc:
        msg = "`crawl4ai` not installed. Please install using `pip install crawl4ai`"
        raise ImportError(msg) from exc
    return crawl4ai


class _Crawl4aiTools(Toolkit):
    """Crawl4AI toolkit with MindRoom server-fetch URL validation and browser destination proxy."""

    # Expose only safe crawling options; raw proxy_config overrides would bypass the egress route.
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
        all: bool = False,  # noqa: A002
    ) -> None:
        _require_crawl4ai()
        self._runtime_paths = runtime_paths
        self.max_length = max_length
        self.timeout = timeout
        self.use_pruning = use_pruning
        self.pruning_threshold = pruning_threshold
        self.bm25_threshold = bm25_threshold
        self.headless = headless
        self.wait_until = wait_until

        tools: list[Callable[..., Any]] = []
        if all or enable_crawl:
            tools.append(self.crawl)

        super().__init__(name="crawl4ai_tools", tools=tools)

    def _build_config(self, search_query: str | None = None) -> dict[str, Any]:
        """Build CrawlerRunConfig parameters from toolkit settings."""
        config_params: dict[str, Any] = {
            "page_timeout": self.timeout * 1000,
            "wait_until": self.wait_until,
            "cache_mode": "bypass",
            "verbose": False,
        }

        if self.use_pruning or search_query:
            try:
                from crawl4ai.content_filter_strategy import BM25ContentFilter, PruningContentFilter
                from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

                if search_query:
                    content_filter = BM25ContentFilter(user_query=search_query, bm25_threshold=self.bm25_threshold)
                    logger.debug("crawl4ai_using_bm25_filter", search_query=search_query)
                else:
                    content_filter = PruningContentFilter(
                        threshold=self.pruning_threshold,
                        threshold_type="fixed",
                        min_word_threshold=2,
                    )
                    logger.debug("crawl4ai_using_pruning_filter")

                config_params["markdown_generator"] = DefaultMarkdownGenerator(content_filter=content_filter)
            except ImportError as exc:
                logger.warning("crawl4ai_content_filter_unavailable", error=str(exc))

        return config_params

    def crawl(self, url: str | list[str], search_query: str | None = None) -> str | dict[str, str]:
        """Crawl validated public HTTP(S) URLs."""
        if isinstance(url, str):
            validated = validate_server_fetch_url(url)
            return asyncio.run(self._async_crawl(validated, search_query))
        validated_urls = [validate_server_fetch_url(single_url) for single_url in url]
        if not validated_urls:
            return "Error: No URL provided"
        return {single_url: asyncio.run(self._async_crawl(single_url, search_query)) for single_url in validated_urls}

    async def _guard_page_context(self, page: Page, *, context: BrowserContext, **_kwargs: object) -> None:
        # Guard the context rather than the page so popups and other pages it opens are routed too.
        # Playwright passes (route, request) to a handler that declares more than one parameter.
        del page
        await context.route("**/*", lambda route: continue_or_abort_browser_fetch(route))

    async def _async_crawl(self, url: str, search_query: str | None = None) -> str:
        """Crawl one validated URL with connect-time destination checks on every browser connection."""
        crawl4ai_pkg = _require_crawl4ai()
        destination_proxy: BrowserDestinationProxy | None = None
        try:
            # Page routes see neither WebSockets, service-worker fetches, nor the address Chromium resolves for
            # itself, so the relay is Chromium's only proxy and validates every connection at dial time before
            # it goes direct or through the operator's egress proxy, which Chromium would otherwise read here.
            destination_proxy = BrowserDestinationProxy(
                egress=browser_egress(
                    self._runtime_paths.process_env,
                    os.environ,
                    egress_control=self._runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]),
                ),
            )
            await destination_proxy.start()
            browser_config = crawl4ai_pkg.BrowserConfig(
                headless=self.headless,
                verbose=False,
                proxy_config={"server": destination_proxy.endpoint},
                # Playwright forces loopback through the proxy only unless an environment switch disables it.
                extra_args=[PROXIED_WEBRTC_ONLY_ARG, f"--proxy-bypass-list={RELAY_ONLY_PROXY_BYPASS}"],
            )

            async with crawl4ai_pkg.AsyncWebCrawler(config=browser_config) as crawler:
                crawler.crawler_strategy.set_hook("on_page_context_created", self._guard_page_context)
                config = crawl4ai_pkg.CrawlerRunConfig(**self._build_config(search_query))
                logger.debug("crawl4ai_crawling", url=url)
                result = await crawler.arun(url=url, config=config)

                if not result:
                    return "Error: No content found"

                # Crawl4AI 0.8 exposes filtered and raw text only through the markdown result.
                markdown = result.markdown
                content = (markdown.fit_markdown or str(markdown)) if markdown is not None else ""
                if not content:
                    if result.html:
                        logger.warning("crawl4ai_no_markdown_extracted", url=url)
                        return "Error: Could not extract markdown from page"
                    logger.warning("crawl4ai_no_content_extracted", url=url, result_type=type(result).__name__)
                    return "Error: No readable content extracted"

                logger.debug("crawl4ai_extracted", url=url, content_length=len(content))
                if self.max_length and len(content) > self.max_length:
                    content = content[: self.max_length] + "..."
                return content
        except Exception as exc:
            logger.warning("crawl4ai_crawl_failed", url=url, error=str(exc))
            return f"Error crawling {url}: {exc}"
        finally:
            if destination_proxy is not None:
                await destination_proxy.close()


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
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#crawl4ai",
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    function_names=("crawl",),
)
def crawl4ai_tools() -> type[Toolkit]:
    """Return Crawl4AI tools for web crawling and scraping."""
    _require_crawl4ai()
    return _Crawl4aiTools
