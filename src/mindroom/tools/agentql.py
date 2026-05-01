"""AgentQL tool configuration."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from types import ModuleType

    from agno.tools.agentql import AgentQLTools
    from playwright.async_api import Page as AsyncPage
    from playwright.sync_api import Page as SyncPage


def _ensure_agentql_playwright_stealth_compat() -> None:
    """Expose the legacy stealth helpers AgentQL imports when crawl4ai installs playwright-stealth 2.x."""
    playwright_stealth = importlib.import_module("playwright_stealth")
    if {"StealthConfig", "stealth_async", "stealth_sync"} <= vars(playwright_stealth).keys():
        return

    _install_agentql_playwright_stealth_compat(playwright_stealth)


def _install_agentql_playwright_stealth_compat(playwright_stealth: ModuleType) -> None:
    """Install the legacy playwright-stealth exports still imported by AgentQL."""
    from playwright_stealth.core import BrowserType, StealthConfig
    from playwright_stealth.properties import Properties

    def _combine_scripts(config: StealthConfig | None) -> tuple[Properties, str]:
        properties = Properties(browser_type=config.browser_type if config else BrowserType.CHROME)
        scripts = "\n".join((config or StealthConfig()).enabled_scripts(properties))
        return properties, scripts

    async def _stealth_async(page: AsyncPage, config: StealthConfig | None = None) -> None:
        properties, script = _combine_scripts(config)
        await page.set_extra_http_headers(properties.as_dict()["header"])
        await page.add_init_script(script)

    def _stealth_sync(page: SyncPage, config: StealthConfig | None = None) -> None:
        properties, script = _combine_scripts(config)
        page.set_extra_http_headers(properties.as_dict()["header"])
        page.add_init_script(script)

    namespace = vars(playwright_stealth)
    namespace["StealthConfig"] = StealthConfig
    namespace["stealth_async"] = _stealth_async
    namespace["stealth_sync"] = _stealth_sync


@register_tool_with_metadata(
    name="agentql",
    display_name="AgentQL",
    description="AI-powered web scraping and data extraction from websites",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSpider",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_scrape_website",
            label="Enable Scrape Website",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_custom_scrape_website",
            label="Enable Custom Scrape Website",
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
            name="agentql_query",
            label="Agentql Query",
            type="text",
            required=False,
            default="",
        ),
    ],
    dependencies=["agentql", "playwright"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/agentql",
    function_names=("custom_scrape_website", "scrape_website"),
)
def agentql_tools() -> type[AgentQLTools]:
    """Return AgentQL tools for AI-powered web scraping."""
    _ensure_agentql_playwright_stealth_compat()

    from agno.tools.agentql import AgentQLTools

    return AgentQLTools
