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


@register_tool("reddit")
def reddit_tools() -> type[Toolkit]:
    """Reddit tools for browsing and searching Reddit."""
    from agno.tools.reddit import RedditTools

    return RedditTools


@register_tool("youtube")
def youtube_tools() -> type[Toolkit]:
    """YouTube tools for searching and getting video information."""
    from agno.tools.youtube import YouTubeTools

    return YouTubeTools


@register_tool("x")
def x_tools() -> type[Toolkit]:
    """X (Twitter) tools for posting and searching tweets."""
    from agno.tools.x import XTools

    return XTools


@register_tool("twitter")
def twitter_tools() -> type[Toolkit]:
    """Twitter tools (alias for X)."""
    from agno.tools.x import XTools

    return XTools


@register_tool("slack")
def slack_tools() -> type[Toolkit]:
    """Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools

    return SlackTools


@register_tool("discord")
def discord_tools() -> type[Toolkit]:
    """Discord tools for messaging and server management."""
    from agno.tools.discord import DiscordTools

    return DiscordTools


@register_tool("whatsapp")
def whatsapp_tools() -> type[Toolkit]:
    """WhatsApp tools for messaging."""
    from agno.tools.whatsapp import WhatsAppTools

    return WhatsAppTools


@register_tool("zoom")
def zoom_tools() -> type[Toolkit]:
    """Zoom tools for meeting management."""
    from agno.tools.zoom import ZoomTools

    return ZoomTools


@register_tool("googlecalendar")
def googlecalendar_tools() -> type[Toolkit]:
    """Google Calendar tools for event management."""
    from agno.tools.googlecalendar import GoogleCalendarTools

    return GoogleCalendarTools


@register_tool("googlesheets")
def googlesheets_tools() -> type[Toolkit]:
    """Google Sheets tools for spreadsheet operations."""
    from agno.tools.googlesheets import GoogleSheetsTools

    return GoogleSheetsTools


@register_tool("todoist")
def todoist_tools() -> type[Toolkit]:
    """Todoist tools for task management."""
    from agno.tools.todoist import TodoistTools

    return TodoistTools


@register_tool("linear")
def linear_tools() -> type[Toolkit]:
    """Linear tools for project management."""
    from agno.tools.linear import LinearTools

    return LinearTools


@register_tool("clickup")
def clickup_tools() -> type[Toolkit]:
    """ClickUp tools for project management."""
    from agno.tools.clickup_tool import ClickUpTools

    return ClickUpTools


@register_tool("confluence")
def confluence_tools() -> type[Toolkit]:
    """Confluence tools for documentation."""
    from agno.tools.confluence import ConfluenceTools

    return ConfluenceTools


@register_tool("jira")
def jira_tools() -> type[Toolkit]:
    """Jira tools for issue tracking."""
    from agno.tools.jira import JiraTools

    return JiraTools


@register_tool("trello")
def trello_tools() -> type[Toolkit]:
    """Trello tools for board management."""
    from agno.tools.trello import TrelloTools

    return TrelloTools


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
