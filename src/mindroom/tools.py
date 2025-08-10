"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agno.tools import Toolkit
from loguru import logger

from .tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

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


# Register all available tools with metadata
@register_tool_with_metadata(
    name="calculator",
    display_name="Calculator",
    description="Mathematical calculations and expressions",
    category=ToolCategory.DEVELOPMENT,
    icon="Calculator",
)
def calculator_tools() -> type:
    from agno.tools.calculator import CalculatorTools

    return CalculatorTools


@register_tool_with_metadata(
    name="file",
    display_name="File Operations",
    description="Read, write, and manage files",
    category=ToolCategory.DEVELOPMENT,
    icon="Folder",
)
def file_tools() -> type:
    from agno.tools.file import FileTools

    return FileTools


@register_tool_with_metadata(
    name="shell",
    display_name="Shell Commands",
    description="Execute shell commands and scripts",
    category=ToolCategory.DEVELOPMENT,
    icon="Terminal",
)
def shell_tools() -> type:
    from agno.tools.shell import ShellTools

    return ShellTools


@register_tool_with_metadata(
    name="csv",
    display_name="CSV Files",
    description="Read and analyze CSV data",
    category=ToolCategory.RESEARCH,
    icon="FileText",
)
def csv_tools() -> type:
    from agno.tools.csv_toolkit import CsvTools

    return CsvTools


@register_tool_with_metadata(
    name="arxiv",
    display_name="arXiv",
    description="Search and retrieve academic papers",
    category=ToolCategory.RESEARCH,
    icon="Book",
    dependencies=["pypdf"],
)
def arxiv_tools() -> type:
    from agno.tools.arxiv import ArxivTools

    return ArxivTools


@register_tool_with_metadata(
    name="duckduckgo",
    display_name="DuckDuckGo",
    description="Web search without tracking",
    category=ToolCategory.RESEARCH,
    icon="Search",
)
def duckduckgo_tools() -> type:
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools


@register_tool_with_metadata(
    name="wikipedia",
    display_name="Wikipedia",
    description="Search encyclopedia articles",
    category=ToolCategory.RESEARCH,
    icon="Globe",
    dependencies=["wikipedia"],
)
def wikipedia_tools() -> type:
    from agno.tools.wikipedia import WikipediaTools

    return WikipediaTools


@register_tool_with_metadata(
    name="newspaper",
    display_name="News Articles",
    description="Extract and analyze news articles from URLs",
    category=ToolCategory.RESEARCH,
    icon="Newspaper",
)
def newspaper_tools() -> type:
    from agno.tools.newspaper import NewspaperTools

    return NewspaperTools


@register_tool_with_metadata(
    name="yfinance",
    display_name="Yahoo Finance",
    description="Stock market data and financial information",
    category=ToolCategory.RESEARCH,
    icon="TrendingUp",
)
def yfinance_tools() -> type:
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools


@register_tool_with_metadata(
    name="python",
    display_name="Python Execution",
    description="Execute Python code in a sandboxed environment",
    category=ToolCategory.DEVELOPMENT,
    icon="Code",
)
def python_tools() -> type:
    from agno.tools.python import PythonTools

    return PythonTools


@register_tool_with_metadata(
    name="pandas",
    display_name="Data Analysis",
    description="Pandas data manipulation and analysis",
    category=ToolCategory.RESEARCH,
    icon="Database",
)
def pandas_tools() -> type:
    from agno.tools.pandas import PandasTools

    return PandasTools


@register_tool_with_metadata(
    name="docker",
    display_name="Docker",
    description="Manage Docker containers and images",
    category=ToolCategory.DEVELOPMENT,
    icon="FaDocker",
)
def docker_tools() -> type:
    from agno.tools.docker import DockerTools

    return DockerTools


@register_tool_with_metadata(
    name="github",
    display_name="GitHub",
    description="Repository and issue management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGithub",
    requires_config=["GITHUB_ACCESS_TOKEN"],
)
def github_tools() -> type:
    from agno.tools.github import GithubTools

    return GithubTools


@register_tool_with_metadata(
    name="email",
    display_name="Email",
    description="Send emails via SMTP",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Mail",
    requires_config=["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD"],
)
def email_tools() -> type:
    from agno.tools.email import EmailTools

    return EmailTools


@register_tool_with_metadata(
    name="telegram",
    display_name="Telegram",
    description="Send and receive Telegram messages",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTelegram",
    requires_config=["TELEGRAM_BOT_TOKEN"],
)
def telegram_tools() -> type:
    from agno.tools.telegram import TelegramTools

    return TelegramTools


@register_tool_with_metadata(
    name="tavily",
    display_name="Tavily Search",
    description="Advanced AI-powered web search engine",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Search",
    requires_config=["TAVILY_API_KEY"],
)
def tavily_tools() -> type:
    from agno.tools.tavily import TavilyTools

    return TavilyTools


@register_tool_with_metadata(
    name="googlesearch",
    display_name="Google Search",
    description="Search the web using Google",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Search",
    requires_config=["GOOGLE_SEARCH_API_KEY", "GOOGLE_SEARCH_CSE_ID"],
)
def googlesearch_tools() -> type:
    from agno.tools.googlesearch import GoogleSearchTools

    return GoogleSearchTools


@register_tool_with_metadata(
    name="website",
    display_name="Website Reader",
    description="Extract and analyze content from websites",
    category=ToolCategory.RESEARCH,
    icon="Globe",
)
def website_tools() -> type:
    from agno.tools.website import WebsiteTools

    return WebsiteTools


@register_tool_with_metadata(
    name="jina",
    display_name="Jina Reader",
    description="Advanced content extraction and processing",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FileText",
    requires_config=["JINA_API_KEY"],
)
def jina_tools() -> type:
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools


@register_tool_with_metadata(
    name="gmail",
    display_name="Gmail",
    description="Read, search, and manage Gmail emails",
    category=ToolCategory.EMAIL,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.OAUTH,
    icon="FaGoogle",
    requires_config=["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
)
def gmail_tools() -> type[Toolkit]:
    """Gmail tools using Agno's native Gmail toolkit."""
    from agno.tools.gmail import GmailTools

    logger.info("Using Agno's native Gmail toolkit")
    return GmailTools


# Social media and communication tools
@register_tool_with_metadata(
    name="reddit",
    display_name="Reddit",
    description="Browse subreddits and search posts",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaReddit",
    requires_config=["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
    dependencies=["praw"],
)
def reddit_tools() -> type[Toolkit]:
    """Reddit tools for browsing and searching Reddit."""
    from agno.tools.reddit import RedditTools

    return RedditTools


@register_tool_with_metadata(
    name="youtube",
    display_name="YouTube",
    description="Search videos and get transcripts",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.API_KEY,
    icon="FaYoutube",
    dependencies=["youtube-transcript-api"],
)
def youtube_tools() -> type[Toolkit]:
    """YouTube tools for searching and getting video information."""
    from agno.tools.youtube import YouTubeTools

    return YouTubeTools


@register_tool_with_metadata(
    name="twitter",
    display_name="Twitter/X",
    description="Post tweets and search Twitter",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTwitter",
    requires_config=["TWITTER_API_KEY", "TWITTER_API_SECRET"],
    dependencies=["tweepy"],
)
def twitter_tools() -> type[Toolkit]:
    """Twitter/X tools for posting and searching tweets."""
    from agno.tools.x import XTools

    return XTools


@register_tool_with_metadata(
    name="slack",
    display_name="Slack",
    description="Send messages and manage channels",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSlack",
    requires_config=["SLACK_TOKEN"],
    dependencies=["slack-sdk"],
)
def slack_tools() -> type[Toolkit]:
    """Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools

    return SlackTools


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
