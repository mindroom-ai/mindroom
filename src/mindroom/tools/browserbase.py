"""Browserbase remote headless browser automation toolkit."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


def _require_browserbase() -> type[Any]:
    """Import and return ``browserbase.Browserbase``, raising a helpful ImportError when absent."""
    try:
        from browserbase import Browserbase
    except ImportError as exc:
        msg = "`browserbase` not installed. Please install using `pip install browserbase`"
        raise ImportError(msg) from exc
    return Browserbase


def _require_async_playwright() -> Any:  # noqa: ANN401
    """Import and return ``playwright.async_api.async_playwright``, raising a helpful ImportError when absent."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        msg = "`playwright` not installed. Please install using `pip install playwright` and run `playwright install`"
        raise ImportError(msg) from exc
    return async_playwright


class BrowserbaseTools(Toolkit):
    """Navigate, screenshot, and extract content from remote Browserbase browser sessions."""

    def __init__(
        self,
        api_key: str | None = None,
        project_id: str | None = None,
        base_url: str | None = None,
        enable_navigate_to: bool = True,
        enable_screenshot: bool = True,
        enable_get_page_content: bool = True,
        enable_close_session: bool = True,
        all: bool = False,  # noqa: A002
        parse_html: bool = True,
        max_content_length: int | None = 100000,
    ) -> None:
        browserbase_cls = _require_browserbase()
        self.parse_html = parse_html
        self.max_content_length = max_content_length

        self.api_key = api_key
        if not self.api_key:
            msg = "BROWSERBASE_API_KEY is required. Please set the BROWSERBASE_API_KEY environment variable."
            raise ValueError(msg)

        self.project_id = project_id
        if not self.project_id:
            msg = "BROWSERBASE_PROJECT_ID is required. Please set the BROWSERBASE_PROJECT_ID environment variable."
            raise ValueError(msg)

        self.base_url = base_url
        if self.base_url:
            self.app = browserbase_cls(api_key=self.api_key, base_url=self.base_url)
            logger.debug("browserbase_custom_endpoint", base_url=self.base_url)
        else:
            self.app = browserbase_cls(api_key=self.api_key)

        self._async_playwright: Any = None
        self._async_browser: Any = None
        self._async_page: Any = None
        self._session: Any = None
        self._connect_url: str | None = None

        tools: list[Callable[..., Any]] = []
        if all or enable_navigate_to:
            tools.append(self.navigate_to)
        if all or enable_screenshot:
            tools.append(self.screenshot)
        if all or enable_get_page_content:
            tools.append(self.get_page_content)
        if all or enable_close_session:
            tools.append(self.close_session)

        super().__init__(name="browserbase_tools", tools=tools)

    def _ensure_session(self) -> None:
        if not self._session:
            try:
                self._session = self.app.sessions.create(project_id=self.project_id)
                self._connect_url = self._session.connect_url if self._session else ""
                if self._session:
                    logger.debug("browserbase_session_created", session_id=self._session.id)
            except Exception:
                logger.exception("browserbase_session_creation_failed")
                raise

    async def _initialize_browser(self, connect_url: str | None = None) -> None:
        async_playwright = _require_async_playwright()
        if connect_url:
            self._connect_url = connect_url
        elif not self._connect_url:
            await asyncio.to_thread(self._ensure_session)

        if not self._async_playwright:
            self._async_playwright = await async_playwright().start()
            if self._async_playwright:
                self._async_browser = await self._async_playwright.chromium.connect_over_cdp(self._connect_url)
            context = self._async_browser.contexts[0] if self._async_browser else None
            if context:
                self._async_page = context.pages[0] if context.pages else await context.new_page()

    async def _cleanup(self) -> None:
        if self._async_browser:
            await self._async_browser.close()
            self._async_browser = None
        if self._async_playwright:
            await self._async_playwright.stop()
            self._async_playwright = None
        self._async_page = None

    @staticmethod
    def _extract_text_content(html: str) -> str:
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
        html = re.sub(r"<[^>]+>", " ", html)
        html = html.replace("&nbsp;", " ")
        html = html.replace("&amp;", "&")
        html = html.replace("&lt;", "<")
        html = html.replace("&gt;", ">")
        html = html.replace("&quot;", '"')
        html = html.replace("&#39;", "'")
        html = re.sub(r"\s+", " ", html)
        return html.strip()

    def _truncate_content(self, content: str) -> str:
        if self.max_content_length is None or len(content) <= self.max_content_length:
            return content
        truncated = content[: self.max_content_length]
        return (
            f"{truncated}\n\n[Content truncated. Original length: {len(content)} characters. "
            f"Showing first {self.max_content_length} characters.]"
        )

    async def navigate_to(self, url: str, connect_url: str | None = None) -> str:
        """Navigates to a URL asynchronously.

        Args:
            url: The URL to navigate to.
            connect_url: The connection URL from an existing session.

        Returns:
            JSON string with navigation status.

        """
        try:
            await self._initialize_browser(connect_url)
            if self._async_page:
                await self._async_page.goto(url, wait_until="networkidle")
            title = await self._async_page.title() if self._async_page else ""
            return json.dumps({"status": "complete", "title": title, "url": url})
        except Exception:
            await self._cleanup()
            raise

    async def screenshot(self, path: str, full_page: bool = True, connect_url: str | None = None) -> str:
        """Takes a screenshot of the current page asynchronously.

        Args:
            path: Where to save the screenshot.
            full_page: Whether to capture the full page.
            connect_url: The connection URL from an existing session.

        Returns:
            JSON string confirming screenshot was saved.

        """
        try:
            await self._initialize_browser(connect_url)
            if self._async_page:
                await self._async_page.screenshot(path=path, full_page=full_page)
            return json.dumps({"status": "success", "path": path})
        except Exception:
            await self._cleanup()
            raise

    async def get_page_content(self, connect_url: str | None = None) -> str:
        """Gets the content of the current page asynchronously.

        Args:
            connect_url: The connection URL from an existing session.

        Returns:
            The page content (text-only if parse_html=True, otherwise raw HTML).

        """
        try:
            await self._initialize_browser(connect_url)
            if not self._async_page:
                return ""
            raw_content = await self._async_page.content()
            content = self._extract_text_content(raw_content) if self.parse_html else raw_content
            return self._truncate_content(content)
        except Exception:
            await self._cleanup()
            raise

    async def close_session(self) -> str:
        """Closes a browser session asynchronously.

        Returns:
            JSON string with closure status.

        """
        try:
            await self._cleanup()
            self._session = None
            self._connect_url = None
            return json.dumps(
                {
                    "status": "closed",
                    "message": "Browser resources cleaned up. Session will auto-close if not already closed.",
                },
            )
        except Exception as exc:
            return json.dumps({"status": "warning", "message": f"Cleanup completed with warning: {exc}"})


@register_tool_with_metadata(
    name="browserbase",
    file_access=ToolFileAccess.UNCONFINED,
    requires_primary_runtime=True,
    display_name="Browserbase",
    description="Browser automation and web scraping using headless browsers",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaChrome",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("BROWSERBASE_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="project_id",
            env=("BROWSERBASE_PROJECT_ID",),
            label="Project ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="base_url",
            env=("BROWSERBASE_BASE_URL",),
            label="Base URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_navigate_to",
            label="Enable Navigate To",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_screenshot",
            label="Enable Screenshot",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_page_content",
            label="Enable Get Page Content",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_close_session",
            label="Enable Close Session",
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
        ConfigField(
            name="parse_html",
            label="Parse Html",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="max_content_length",
            label="Max Content Length",
            type="number",
            required=False,
            default=100000,
        ),
    ],
    dependencies=["browserbase", "playwright"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#browserbase",
    function_names=(
        "aclose_session",
        "aget_page_content",
        "anavigate_to",
        "ascreenshot",
        "close_session",
        "get_page_content",
        "navigate_to",
        "screenshot",
    ),
)
def browserbase_tools() -> type[BrowserbaseTools]:
    """Return Browserbase tools for browser automation and web scraping."""
    _require_browserbase()
    return BrowserbaseTools
