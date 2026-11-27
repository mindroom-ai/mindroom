"""Browserbase remote headless browser automation toolkit."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_BrowserbaseAction = Literal["navigate", "screenshot", "get_page_content", "close_session"]
_ACTION_FIELDS: dict[str, str] = {
    "navigate": "url connect_url",
    "screenshot": "path full_page connect_url",
    "get_page_content": "connect_url",
    "close_session": "",
}


class _BrowserbaseTools(Toolkit):
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
        from browserbase import Browserbase

        self.parse_html = parse_html
        self.max_content_length = max_content_length

        self.api_key = api_key
        if not self.api_key:
            msg = "BROWSERBASE_API_KEY is required. Please set the BROWSERBASE_API_KEY environment variable."
            raise ValueError(msg)

        if not project_id:
            msg = "BROWSERBASE_PROJECT_ID is required. Please set the BROWSERBASE_PROJECT_ID environment variable."
            raise ValueError(msg)
        self.project_id: str = project_id

        self.base_url = base_url
        if self.base_url:
            self.app = Browserbase(api_key=self.api_key, base_url=self.base_url)
            logger.debug("browserbase_custom_endpoint", base_url=self.base_url)
        else:
            self.app = Browserbase(api_key=self.api_key)

        self._async_playwright: Any = None
        self._async_browser: Any = None
        self._async_page: Any = None
        self._session: Any = None
        self._connect_url: str | None = None

        toggles: dict[_BrowserbaseAction, bool] = {
            "navigate": all or enable_navigate_to,
            "screenshot": all or enable_screenshot,
            "get_page_content": all or enable_get_page_content,
            "close_session": all or enable_close_session,
        }
        self._enabled_actions: tuple[_BrowserbaseAction, ...] = tuple(
            action for action, enabled in toggles.items() if enabled
        )
        super().__init__(name="browserbase_tools", tools=(self.browserbase,) if self._enabled_actions else ())

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``browserbase`` schema and parameters to enabled actions."""
        if function.name == "browserbase":
            return restrict_actions(function, self._enabled_actions, action_fields=_ACTION_FIELDS)
        return function

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
        from playwright.async_api import async_playwright

        if connect_url:
            self._connect_url = connect_url
        elif not self._connect_url:
            await asyncio.to_thread(self._ensure_session)

        if not self._async_playwright:
            self._async_playwright = await async_playwright().start()
            if self._async_playwright and self._connect_url:
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
        for entity, replacement in (
            ("&nbsp;", " "),
            ("&amp;", "&"),
            ("&lt;", "<"),
            ("&gt;", ">"),
            ("&quot;", '"'),
            ("&#39;", "'"),
        ):
            html = html.replace(entity, replacement)
        return re.sub(r"\s+", " ", html).strip()

    def _truncate_content(self, content: str) -> str:
        if self.max_content_length is None or len(content) <= self.max_content_length:
            return content
        truncated = content[: self.max_content_length]
        return (
            f"{truncated}\n\n[Content truncated. Original length: {len(content)} characters. "
            f"Showing first {self.max_content_length} characters.]"
        )

    async def browserbase(
        self,
        action: _BrowserbaseAction,
        url: str | None = None,
        path: str | None = None,
        full_page: bool = True,
        connect_url: str | None = None,
    ) -> str:
        """Control a remote Browserbase headless browser session.

        - `navigate`: Open `url` (optional `connect_url`).
        - `screenshot`: Save page screenshot to `path` (optional `full_page`, `connect_url`).
        - `get_page_content`: Return current page text or HTML (optional `connect_url`).
        - `close_session`: Close and clean up the active browser session.
        """
        if (disabled := require_enabled_action(action, self._enabled_actions)) is not None:
            return error_json(disabled)
        if action == "close_session":
            return await self._close_session()
        if action == "navigate" and not url:
            return error_json("url is required for navigate.")
        if action == "screenshot" and not path:
            return error_json("path is required for screenshot.")
        try:
            await self._initialize_browser(connect_url)
            return await self._run_page_action(action, url=url, path=path, full_page=full_page)
        except Exception:
            await self._cleanup()
            raise

    async def _run_page_action(
        self,
        action: _BrowserbaseAction,
        *,
        url: str | None,
        path: str | None,
        full_page: bool,
    ) -> str:
        if action == "navigate":
            assert url is not None
            if self._async_page:
                await self._async_page.goto(url, wait_until="networkidle")
            title = await self._async_page.title() if self._async_page else ""
            return json.dumps({"status": "complete", "title": title, "url": url})
        if action == "screenshot":
            assert path is not None
            if self._async_page:
                await self._async_page.screenshot(path=path, full_page=full_page)
            return json.dumps({"status": "success", "path": path})
        if not self._async_page:
            return ""
        raw_content = await self._async_page.content()
        content = self._extract_text_content(raw_content) if self.parse_html else raw_content
        return self._truncate_content(content)

    async def _close_session(self) -> str:
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
        bool_config_field("enable_navigate_to", "Enable Navigate To"),
        bool_config_field("enable_screenshot", "Enable Screenshot"),
        bool_config_field("enable_get_page_content", "Enable Get Page Content"),
        bool_config_field("enable_close_session", "Enable Close Session"),
        bool_config_field("all", "All", default=False),
        bool_config_field("parse_html", "Parse Html"),
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
    function_names=("browserbase",),
    action_scoped_grants=("browserbase",),
)
def browserbase_tools() -> type[Toolkit]:
    """Return Browserbase tools for browser automation and web scraping."""
    return _BrowserbaseTools
