"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agno.tools import Toolkit
from loguru import logger

# Registry mapping tool names to their factory functions
TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}


def register_tool(name: str) -> Callable[[Callable[[], type[Toolkit]]], Callable[[], type[Toolkit]]]:
    """Decorator to register a tool factory function.

    Args:
        name: The name to register the tool under

    Returns:
        Decorator function
    """

    def decorator(func: Callable[[], type[Toolkit]]) -> Callable[[], type[Toolkit]]:
        TOOL_REGISTRY[name] = func
        return func

    return decorator


# Register all available tools
@register_tool("calculator")
def calculator_tools() -> type:
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools


@register_tool("file")
def file_tools() -> type:
    from agno.tools.file import FileTools

    return FileTools


@register_tool("shell")
def shell_tools() -> type:
    from agno.tools.shell import ShellTools

    return ShellTools


@register_tool("csv")
def csv_tools() -> type:
    from agno.tools.csv_toolkit import CsvTools

    return CsvTools


@register_tool("arxiv")
def arxiv_tools() -> type:
    from agno.tools.arxiv import ArxivTools

    return ArxivTools


@register_tool("duckduckgo")
def duckduckgo_tools() -> type:
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools


@register_tool("wikipedia")
def wikipedia_tools() -> type:
    from agno.tools.wikipedia import WikipediaTools

    return WikipediaTools


@register_tool("newspaper")
def newspaper_tools() -> type:
    from agno.tools.newspaper import NewspaperTools

    return NewspaperTools


@register_tool("yfinance")
def yfinance_tools() -> type:
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools


@register_tool("python")
def python_tools() -> type:
    from agno.tools.python import PythonTools

    return PythonTools


@register_tool("pandas")
def pandas_tools() -> type:
    from agno.tools.pandas import PandasTools

    return PandasTools


@register_tool("docker")
def docker_tools() -> type:
    from agno.tools.docker import DockerTools

    return DockerTools


@register_tool("github")
def github_tools() -> type:
    from agno.tools.github import GithubTools

    return GithubTools


@register_tool("email")
def email_tools() -> type:
    from agno.tools.email import EmailTools

    return EmailTools


@register_tool("telegram")
def telegram_tools() -> type:
    from agno.tools.telegram import TelegramTools

    return TelegramTools


@register_tool("tavily")
def tavily_tools() -> type:
    from agno.tools.tavily import TavilyTools

    return TavilyTools


@register_tool("googlesearch")
def googlesearch_tools() -> type:
    from agno.tools.googlesearch import GoogleSearchTools

    return GoogleSearchTools


@register_tool("website")
def website_tools() -> type:
    from agno.tools.website import WebsiteTools

    return WebsiteTools


@register_tool("jina")
def jina_tools() -> type:
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools


@register_tool("gmail")
def gmail_tools() -> type[Toolkit]:
    """Gmail tools using Agno's native Gmail toolkit."""
    from agno.tools.gmail import GmailTools

    logger.info("Using Agno's native Gmail toolkit")
    return GmailTools


@register_tool("integrations")
def integrations_tools() -> type[Toolkit]:
    """Tools for various external service integrations."""
    from agno.tools import Toolkit

    from .integrations_tool import (
        get_facebook_page,
        get_imdb_details,
        get_spotify_current,
        list_dropbox_files,
        search_amazon,
        search_github_repos,
        search_imdb,
        search_reddit,
        search_walmart,
        send_telegram,
    )

    class IntegrationsTools(Toolkit):
        """Toolkit for external service integrations."""

        def __init__(self) -> None:
            super().__init__(name="integrations")

        def search_amazon(self, query: str, max_results: int = 5) -> str:
            """Search Amazon for products."""
            return search_amazon(query, max_results)

        def search_imdb(self, query: str, type: str = "movie") -> str:
            """Search IMDb for movies or TV shows."""
            return search_imdb(query, type)

        def get_imdb_details(self, title: str) -> str:
            """Get detailed information about a movie or show."""
            return get_imdb_details(title)

        def get_spotify_current(self) -> str:
            """Get currently playing track on Spotify."""
            return get_spotify_current()

        def search_walmart(self, query: str, max_results: int = 5) -> str:
            """Search Walmart for products."""
            return search_walmart(query, max_results)

        def send_telegram(self, chat_id: str, message: str) -> str:
            """Send a message via Telegram bot."""
            return send_telegram(chat_id, message)

        def search_reddit(self, query: str, subreddit: str | None = None, limit: int = 5) -> str:
            """Search Reddit for posts."""
            return search_reddit(query, subreddit, limit)

        def list_dropbox_files(self, path: str = "/") -> str:
            """List files in Dropbox folder."""
            return list_dropbox_files(path)

        def search_github_repos(self, query: str, limit: int = 5) -> str:
            """Search GitHub repositories."""
            return search_github_repos(query, limit)

        def get_facebook_page(self, page_id: str) -> str:
            """Get information about a Facebook page."""
            return get_facebook_page(page_id)

    return IntegrationsTools


# Simple tools removed - functionality moved to proper API integrations
# @register_tool("simple") - Removed as mocked implementations have been cleaned up


def get_tool_by_name(tool_name: str) -> Any:
    """Get a tool instance by its registered name.

    Args:
        tool_name: The registered name of the tool

    Returns:
        An instance of the requested tool

    Raises:
        ValueError: If the tool name is not registered
    """
    if tool_name not in TOOL_REGISTRY:
        available = ", ".join(sorted(TOOL_REGISTRY.keys()))
        raise ValueError(f"Unknown tool: {tool_name}. Available tools: {available}")

    try:
        tool_factory = TOOL_REGISTRY[tool_name]
        tool_class = tool_factory()
        return tool_class()
    except ImportError as e:
        logger.warning(f"Could not import tool '{tool_name}': {e}")
        logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
        raise
